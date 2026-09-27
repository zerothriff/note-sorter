# Note Sorter

Send yourself notes, links, songs, books, screenshots, and anything else through Signal's **Note to Self**. A local LLM running on your own GPU files each item into a tab, and a web page, which you can add to your phone's home screen, shows your tabs as a grid of cards with pictures.

Everything runs on your own machine. There's no cloud service and no account besides Signal.

```
Phone ──Signal "Note to Self"──▶ signal-cli (container)
                                        │
                                  sorter.py ──▶ Ollama + Qwen 3.5 (container, GPU)
                                        │
                           notes.db + thumbs/ ──▶ web viewer (port 8765, via Tailscale)
```

## How it works

### Sending things

- Share anything to **Note to Self** in Signal: a link, a note, a screenshot, a photo, or a link plus a comment.
- Start a message with `#tabname` to put it in a specific tab, for example `#books https://...`.
- The sorter replies in Note to Self with `→ Books`, or `→ Recipes (new tab)`, so you know it landed.

### Sorting

- The local model reads your text, the link's title and description, and any image, then picks a tab.
- It reuses existing tabs whenever it reasonably can. If nothing fits, it creates one new short tab, such as "Recipes". If it truly can't tell, the item goes to **Inbox**.
- Images you send go to the model too, so a screenshot of a book post can land in Books with no text at all.
- The model loads into VRAM only while sorting, about a second per item, and unloads right after, so it doesn't hold GPU memory while you're gaming.

### Pictures and titles

Each card gets an image from the best source available, in this order:

1. **An image you sent** (a screenshot or photo)
2. **Signal's own link preview**, which the Signal app on your phone builds when you share a link
3. **A book cover**, when the link contains an ISBN (bookstores, Amazon `/dp/` links). Title and cover come from [Open Library](https://openlibrary.org).
4. **The site's preview image** (`og:image`)

For titles, the sorter reads the page itself. If a site hides its content from normal requests, as Instagram does, it retries as a link-preview bot, the same way Signal and iMessage fetch previews. Images are downloaded once and stored locally in `thumbs/`, so the viewer never loads anything from the original sites.

### The viewer

- Tabs across the top, with a count of active items in each. Empty tabs are hidden.
- Cards in a grid that fits your screen, with one column on a phone.
- Links show as just the site name, like **bookshop.org**, and open the full link when tapped.
- On each card:
  - **Tab dropdown** moves it to another tab.
  - **Archive** hides it in the **Archived** tab, where you can restore it.
  - **Delete** removes it and its image permanently, after asking you to confirm.
- **Rename or merge** at the bottom of each tab renames it, or moves all its items into another tab.

## Requirements

- Linux with **Podman**. Written and tested on Bazzite with an NVIDIA RTX 4080. Docker should also work with the same commands.
- An NVIDIA GPU with the container toolkit set up (CDI). About 6–8 GB of free VRAM is plenty.
- **Python 3.9+**. The script uses only the standard library, so there's nothing to `pip install`.
- A Signal account on your phone.
- **Optional:** [Tailscale](https://tailscale.com), to open the viewer from your phone.

## Setup

### 1. Ollama (the model runner)

```
podman run -d --name ollama --security-opt=label=disable --device nvidia.com/gpu=all \
  -p 127.0.0.1:11434:11434 -v ollama:/root/.ollama docker.io/ollama/ollama
```

`--security-opt=label=disable` is needed on SELinux systems like Fedora and Bazzite. Without it, the container can't access the GPU and `nvidia-smi` inside it fails with "Insufficient Permissions".

Check the GPU is visible, then pull the model:

```
podman exec ollama nvidia-smi
podman exec ollama ollama pull qwen3.5
```

The first request after a boot takes 30–60 seconds while the model loads. After that, sorting takes about a second.

### 2. Signal (signal-cli REST API)

```
podman run -d --name signal-api -p 127.0.0.1:8080:8080 \
  -v signal-cli-data:/home/.local/share/signal-cli \
  -e MODE=native docker.io/bbernhard/signal-cli-rest-api
```

Link it to your Signal account the same way you'd link Signal Desktop:

1. Open `http://127.0.0.1:8080/v1/qrcodelink?device_name=note-sorter` in a browser on the machine.
2. On your phone, open Signal, go to **Settings → Linked devices**, and scan the code.
3. Confirm with `curl -s http://127.0.0.1:8080/v1/accounts`, which should print your number.

### 3. Start both containers at boot

```
podman update --restart=always ollama
podman update --restart=always signal-api
systemctl --user enable podman-restart.service
```

### 4. Get the code

```
git clone https://github.com/zerothriff/note-sorter.git ~/note-sorter
```

To try it once by hand, run `python3 ~/note-sorter/sorter.py`, then open `http://127.0.0.1:8765` and send something to Note to Self. Stop it with Ctrl+C before moving on.

### 5. Run at boot

Install the included service file so the sorter starts automatically and restarts if it crashes:

```
mkdir -p ~/.config/systemd/user
cp ~/note-sorter/note-sorter.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now note-sorter
loginctl enable-linger
```

`enable-linger` makes it and the Podman containers start at boot, even before you log in.

### 6. View it from your phone (Tailscale)

The viewer only listens on `127.0.0.1` by default and has **no login**. Don't expose it to the public internet. Tailscale gives your devices a private, encrypted network instead:

1. Run `sudo systemctl enable --now tailscaled`, then `sudo tailscale up`, and sign in.
2. Install the Tailscale app on your phone and sign in to the same account.
3. Get the PC's address with `tailscale ip -4`.
4. In `~/.config/systemd/user/note-sorter.service`, set `WEB_HOST=` to that address, then run `systemctl --user daemon-reload && systemctl --user restart note-sorter`.
5. On your phone, open `http://<that address>:8765` and add it to your home screen. In Samsung Internet that's **≡ → Add page to → Home screen**. In Chrome it's **⋮ → Add to home screen**. In Safari it's **Share → Add to Home Screen**.

Browsers may warn that the page isn't HTTPS. Tailscale already encrypts the connection, so it's safe to continue.

## Everyday use

| To do this…                         | Run                                        |
| ----------------------------------- | ------------------------------------------ |
| Apply changes after editing the code | `systemctl --user restart note-sorter`     |
| Watch items arrive and get sorted   | `journalctl --user -u note-sorter -f`      |
| Stop / start the sorter             | `systemctl --user stop note-sorter` / `start` |

## Customizing

### Starter tabs

Set `STARTER_TABS` near the top of `sorter.py`. On every restart, the sorter:

- adds any tab in the list that doesn't exist yet
- fixes capitalization to match the list, so "To-do" becomes "To-Do"
- removes tabs that aren't in the list **and** have no items

Tabs that still have items, including archived ones, are never removed. Use **Rename or merge** in the viewer to retire them.

### Settings

All settings are optional environment variables, which you set in the service file:

| Variable     | Default                   | What it does                                 |
| ------------ | ------------------------- | -------------------------------------------- |
| `MODEL`      | `qwen3.5`                 | Any Ollama model (a vision model lets it read images) |
| `OLLAMA`     | `http://127.0.0.1:11434`  | Ollama address                               |
| `SIGNAL_API` | `http://127.0.0.1:8080`   | signal-cli REST API address                  |
| `DB_PATH`    | `~/note-sorter/notes.db`  | Where items are stored. `thumbs/` sits beside it. |
| `WEB_HOST`   | `127.0.0.1`               | Viewer bind address (see step 6)             |
| `WEB_PORT`   | `8765`                    | Viewer port                                  |
| `SEND_REPLY` | `1`                       | Set to `0` to turn off the `→ Tab` replies   |

## Your data and privacy

- **Stored locally:** everything you send is in `notes.db`, a SQLite file, and the images are in `thumbs/`. Both are in `.gitignore`. Never commit them.
- **Signal:** the container links as an extra device, so it receives all your Signal messages. The sorter ignores everything except Note to Self, and nothing leaves your machine.
- **Outbound requests:** to fetch titles and images, your PC visits the links you save, just as if you'd opened them. For book links, it sends the ISBN to Open Library. Nothing else is sent anywhere.

### Database schema

Two tables: `tabs(name PRIMARY KEY COLLATE NOCASE, created)` and `items(id, sig_ts UNIQUE, created, text, url, title, descr, tab, forced_tab, done, thumb)`. `items.tab` holds the tab's name. New items are inserted with `tab = NULL` and picked up by the sort loop. `done = 1` means archived. Older databases get the `thumb` column added automatically on startup.

## License

MIT. See `LICENSE`.
