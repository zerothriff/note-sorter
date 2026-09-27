#!/usr/bin/env python3
"""
Note sorter: send things to Signal "Note to Self", a local LLM (Ollama) files
them into tabs, and a small web page shows the tabs.

Standard library only - no pip installs needed.
Settings can be changed with environment variables (see below).
"""
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- settings
SIGNAL_API = os.environ.get("SIGNAL_API", "http://127.0.0.1:8080")
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("MODEL", "qwen3.5")
DB_PATH = os.path.expanduser(os.environ.get("DB_PATH", "~/note-sorter/notes.db"))
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", "8765"))
POLL_SECONDS = 5
SEND_REPLY = os.environ.get("SEND_REPLY", "1") == "1"   # "→ Books" confirmation
STARTER_TABS = ["Music", "Books", "Movies & TV", "Links", "To-do", "Ideas", "Buy"]
REPLY_PREFIX = "→ "

URL_RE = re.compile(r"https?://[^\s<>\"]+")


# ---------------------------------------------------------------- database
def q(sql, args=(), one=False):
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    q("""CREATE TABLE IF NOT EXISTS tabs (
           name TEXT PRIMARY KEY COLLATE NOCASE, created REAL)""")
    q("""CREATE TABLE IF NOT EXISTS items (
           id INTEGER PRIMARY KEY, sig_ts INTEGER UNIQUE, created REAL,
           text TEXT, url TEXT, title TEXT, descr TEXT,
           tab TEXT, forced_tab TEXT, done INTEGER DEFAULT 0)""")
    for t in STARTER_TABS + ["Inbox"]:
        q("INSERT OR IGNORE INTO tabs VALUES (?, ?)", (t, time.time()))


def tab_names():
    return [r["name"] for r in q("SELECT name FROM tabs ORDER BY name")]


# ---------------------------------------------------------------- helpers
def http_json(method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def meta(page, prop):
    for pat in (
        rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{prop}["\']',
    ):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def link_info(url):
    """Best-effort page title + description (Spotify, YouTube, most sites)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) note-sorter"})
        with urllib.request.urlopen(req, timeout=8) as r:
            page = r.read(600_000).decode("utf-8", "replace")
    except Exception:
        return "", ""
    title = meta(page, "og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = html.unescape(m.group(1)).strip() if m else ""
    return title[:200], meta(page, "og:description")[:300]


def clean_tab(name):
    name = re.sub(r"[^\w &'-]", "", str(name)).strip()[:24]
    return name.title() if name.islower() else name


def resolve_tab(name, tabs):
    """Match an existing tab case-insensitively; returns (name, is_new)."""
    name = name or "Inbox"
    for t in tabs:
        if t.lower() == name.lower():
            return t, False
    return name, True


# ---------------------------------------------------------------- the LLM
def classify(text, title, descr, tabs):
    prompt = f"""You sort saved notes and links into tabs.
Existing tabs: {", ".join(tabs)}

Rules:
- Use an existing tab whenever it reasonably fits.
- Only if the item clearly fits none of them, invent ONE new tab: 1-2 words,
  general, Title Case (e.g. "Recipes", not "Pasta Recipes To Try").
- If you truly cannot tell what the item is, use "Inbox".

Item text: {text}
Link title: {title or "(none)"}
Link description: {descr or "(none)"}

Reply with JSON only: {{"tab": "<tab name>"}}"""
    res = http_json("POST", f"{OLLAMA}/api/generate", {
        "model": MODEL, "prompt": prompt, "stream": False,
        "think": False,          # skip hidden reasoning - much faster
        "format": "json",
        "keep_alive": 0,         # unload from VRAM right after
        "options": {"temperature": 0},
    }, timeout=180)
    try:
        return clean_tab(json.loads(res.get("response") or "{}").get("tab", ""))
    except (ValueError, AttributeError):
        return "Inbox"


# ---------------------------------------------------------------- Signal
def fetch_new(acct):
    msgs = http_json("GET", f"{SIGNAL_API}/v1/receive/{urllib.parse.quote(acct)}",
                     timeout=90)
    if not isinstance(msgs, list):
        return
    for m in msgs:
        env = m.get("envelope") or {}
        sent = (env.get("syncMessage") or {}).get("sentMessage") or {}
        text = (sent.get("message") or "").strip()
        dest = sent.get("destinationNumber") or sent.get("destination")
        to_self = dest == acct or (
            sent.get("destinationUuid") and sent.get("destinationUuid") == env.get("sourceUuid"))
        if not text or not to_self or text.startswith(REPLY_PREFIX):
            continue  # not a Note to Self message, or our own reply

        forced = None
        tag = re.match(r"#([\w-]+)\s*", text)       # "#books ..." forces a tab
        if tag:
            forced = tag.group(1).replace("-", " ").replace("_", " ")
            text = text[tag.end():].strip() or text

        url_m = URL_RE.search(text)
        url = url_m.group(0).rstrip(").,") if url_m else None
        title, descr = link_info(url) if url else ("", "")
        q("""INSERT OR IGNORE INTO items
               (sig_ts, created, text, url, title, descr, forced_tab)
             VALUES (?, ?, ?, ?, ?, ?, ?)""",
          (sent.get("timestamp") or env.get("timestamp"), time.time(),
           text, url, title, descr, forced))
        print("received:", text[:70], flush=True)


def sort_pending(acct):
    for it in q("SELECT * FROM items WHERE tab IS NULL ORDER BY id"):
        tabs = tab_names()
        if it["forced_tab"]:
            tab, new = resolve_tab(clean_tab(it["forced_tab"]), tabs)
        else:
            tab, new = resolve_tab(classify(it["text"], it["title"], it["descr"], tabs), tabs)
        if new:
            q("INSERT OR IGNORE INTO tabs VALUES (?, ?)", (tab, time.time()))
        q("UPDATE items SET tab = ? WHERE id = ?", (tab, it["id"]))
        print(f"  sorted -> {tab}{' (new tab)' if new else ''}", flush=True)
        if SEND_REPLY:
            try:
                http_json("POST", f"{SIGNAL_API}/v2/send", {
                    "message": f"{REPLY_PREFIX}{tab}" + (" (new tab)" if new else ""),
                    "number": acct, "recipients": [acct]})
            except Exception as e:
                print("  reply failed:", e, flush=True)


def poll_forever():
    acct = None
    while True:
        try:
            if not acct:
                acct = http_json("GET", f"{SIGNAL_API}/v1/accounts")[0]
                print("linked Signal account found", flush=True)
            fetch_new(acct)
            sort_pending(acct)   # if Ollama is down, items wait and retry
        except Exception as e:
            print("waiting (", e, ")", flush=True)
            time.sleep(15)
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- web page
CSS = """
:root{--bg:#f6f5f2;--card:#fff;--fg:#1d1d1b;--muted:#6b6a66;--accent:#3a6ea5;--line:#e3e1dc}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1f1f1f;--fg:#ecebe8;--muted:#9a9894;--accent:#7fb0e6;--line:#2e2e2e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px 0}
h1{font-size:18px;margin:0 0 10px}
nav{display:flex;gap:8px;overflow-x:auto;padding-bottom:12px}
nav a{white-space:nowrap;text-decoration:none;color:var(--fg);background:var(--card);border:1px solid var(--line);
 border-radius:999px;padding:6px 12px;font-size:14px}
nav a.on{background:var(--accent);border-color:var(--accent);color:#fff}
nav .n{opacity:.7;margin-left:4px}
main{max-width:720px;margin:0 auto;padding:16px}
.item{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.text{word-break:break-word}.text a{color:var(--accent)}
.title{color:var(--muted);font-size:14px;margin-top:4px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px;font-size:13px;color:var(--muted)}
.row .when{margin-right:auto}
button,select,input{font:inherit;font-size:13px;padding:5px 9px;border-radius:8px;border:1px solid var(--line);
 background:var(--bg);color:var(--fg)}
form{margin:0;display:inline}
.empty{color:var(--muted);text-align:center;padding:40px 0}
.tools{margin-top:24px;padding-top:16px;border-top:1px solid var(--line);font-size:14px;color:var(--muted)}
"""


def esc(s):
    return html.escape(str(s or ""), quote=True)


def linkify(s):
    return URL_RE.sub(lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener">{m.group(0)}</a>',
                      esc(s))


def render(tab):
    counts = q("""SELECT t.name, COUNT(i.id) AS n FROM tabs t
                  LEFT JOIN items i ON i.tab = t.name AND i.done = 0
                  GROUP BY t.name ORDER BY t.name = 'Inbox' DESC, t.name""")
    if not tab:
        tab = next((r["name"] for r in counts if r["n"]), "Inbox")
    shown = [r for r in counts if r["n"] or r["name"] in ("Inbox", tab)]
    nav = "".join(
        f'<a class="{"on" if r["name"] == tab else ""}" href="/?tab={urllib.parse.quote(r["name"])}">'
        f'{esc(r["name"])}<span class="n">{r["n"] or ""}</span></a>' for r in shown)
    nav += f'<a class="{"on" if tab == "__done" else ""}" href="/?tab=__done">Done</a>'

    done_view = tab == "__done"
    items = (q("SELECT * FROM items WHERE done = 1 ORDER BY id DESC LIMIT 200") if done_view else
             q("SELECT * FROM items WHERE done = 0 AND tab = ? ORDER BY id DESC", (tab,)))
    all_tabs = tab_names()
    back = esc(tab)
    cards = []
    for it in items:
        when = time.strftime("%b %d, %H:%M", time.localtime(it["created"]))
        title = f'<div class="title">{esc(it["title"])}</div>' if it["title"] else ""
        if done_view:
            actions = (f'<span>{esc(it["tab"])}</span>'
                       f'<form method="post" action="/undone"><input type="hidden" name="id" value="{it["id"]}">'
                       f'<input type="hidden" name="back" value="{back}"><button>Restore</button></form>'
                       f'<form method="post" action="/delete"><input type="hidden" name="id" value="{it["id"]}">'
                       f'<input type="hidden" name="back" value="{back}"><button>Delete</button></form>')
        else:
            opts = "".join(f'<option{" selected" if t == tab else ""}>{esc(t)}</option>' for t in all_tabs)
            actions = (f'<form method="post" action="/move"><input type="hidden" name="id" value="{it["id"]}">'
                       f'<input type="hidden" name="back" value="{back}">'
                       f'<select name="tab" onchange="this.form.submit()">{opts}</select></form>'
                       f'<form method="post" action="/done"><input type="hidden" name="id" value="{it["id"]}">'
                       f'<input type="hidden" name="back" value="{back}"><button>✓ Done</button></form>')
        cards.append(f'<div class="item"><div class="text">{linkify(it["text"])}</div>{title}'
                     f'<div class="row"><span class="when">{when}</span>{actions}</div></div>')
    body = "".join(cards) or '<div class="empty">Nothing here.</div>'

    tools = ""
    if not done_view and tab != "Inbox":
        dl = "".join(f'<option value="{esc(t)}">' for t in all_tabs if t != tab)
        tools = (f'<div class="tools"><form method="post" action="/rename">'
                 f'<input type="hidden" name="old" value="{back}">Rename or merge “{esc(tab)}” into: '
                 f'<input name="new" list="tl" required size="12"><datalist id="tl">{dl}</datalist> '
                 f'<button>Save</button></form></div>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Note Sorter</title>
<style>{CSS}</style></head><body><header><h1>Note Sorter</h1><nav>{nav}</nav></header>
<main>{body}{tools}</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/":
            self.send_error(404)
            return
        tab = urllib.parse.parse_qs(u.query).get("tab", [""])[0]
        page = render(tab).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        f = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        path = urllib.parse.urlparse(self.path).path
        back = f.get("back", "")
        if path == "/done":
            q("UPDATE items SET done = 1 WHERE id = ?", (f.get("id"),))
        elif path == "/undone":
            q("UPDATE items SET done = 0 WHERE id = ?", (f.get("id"),))
        elif path == "/delete":
            q("DELETE FROM items WHERE id = ?", (f.get("id"),))
        elif path == "/move":
            tab, new = resolve_tab(clean_tab(f.get("tab", "")), tab_names())
            if new:
                q("INSERT OR IGNORE INTO tabs VALUES (?, ?)", (tab, time.time()))
            q("UPDATE items SET tab = ? WHERE id = ?", (tab, f.get("id")))
        elif path == "/rename":
            old = f.get("old", "")
            new, is_new = resolve_tab(clean_tab(f.get("new", "")), tab_names())
            if old and new and old.lower() != new.lower():
                if is_new:
                    q("INSERT OR IGNORE INTO tabs VALUES (?, ?)", (new, time.time()))
                q("UPDATE items SET tab = ? WHERE tab = ?", (new, old))
                if old.lower() != "inbox":
                    q("DELETE FROM tabs WHERE name = ?", (old,))
                back = new
        self.send_response(303)
        self.send_header("Location", "/?tab=" + urllib.parse.quote(back))
        self.end_headers()


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    init_db()
    threading.Thread(target=poll_forever, daemon=True).start()
    print(f"Note Sorter running - open http://{WEB_HOST}:{WEB_PORT}", flush=True)
    ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler).serve_forever()
