# Note Sorter

Send yourself notes, links, songs, and other things through Signal's **Note to Self**. A local LLM running on your own GPU files each one into a tab, and a small web page shows the tabs so you can come back to them later.

Everything runs on your own machine. Nothing goes to a cloud service except Signal's end-to-end encrypted delivery.

```
Phone ──Signal "Note to Self"──▶ signal-cli (container)
                                        │
                                  sorter.py ──▶ Ollama + Qwen (container, GPU)
                                        │
                                   notes.db ──▶ web viewer at http://127.0.0.1:8765
```

## How it works

- Share anything to **Note to Self** in Signal. Adding a word or two, like `book rec` or `recipe`, helps a lot with sites such as Instagram that hide their content from non-logged-in visitors.
- The sorter fetches the link's title when it can, then asks the local model which tab the item belongs in.
- If nothing fits, the model can create a new short tab, such as "Recipes".
- Start a message with `#tabname` to force a tab, for example `#books https://...`.
- It replies in Note to Self with `→ Books`, or `→ Recipes (new tab)`, so you know it landed.
- The model loads into VRAM only while sorting and unloads right after, so it doesn't hold GPU memory while you're gaming.
- In the viewer, each item is a card. You can move it to another tab, mark it **✓ Done**, and rename or merge tabs.

## Requirements

- Linux with **Podman**. Written and tested on Bazzite with an NVIDIA RTX 4080. Docker should also work with the same commands.
- An NVIDIA GPU with the container toolkit set up (CDI). Around 6–8 GB of free VRAM is plenty.
- **Python 3.9+**. The script uses only the standard library, so there's nothing to `pip install`.
- A Signal account on your phone.

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

The first request after boot takes 30–60 seconds while the model loads. After that, sorting takes about 0.1 seconds.

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

> **Privacy note:** this links as an extra device, so the container receives all your Signal messages. The sorter ignores everything except Note to Self, and nothing leaves your machine, but be aware of it.

### 3. Start both containers at boot

```
podman update --restart=always ollama
podman update --restart=always signal-api
systemctl --user enable podman-restart.service
```

### 4. Run the sorter

```
mkdir -p ~/note-sorter
cp sorter.py ~/note-sorter/
python3 ~/note-sorter/sorter.py
```

Open `http://127.0.0.1:8765` and send something to Note to Self.

## Settings

All settings are optional environment variables:

| Variable     | Default                   | What it does                                 |
| ------------ | ------------------------- | -------------------------------------------- |
| `MODEL`      | `qwen3.5`                 | Any Ollama model                             |
| `OLLAMA`     | `http://127.0.0.1:11434`  | Ollama address                               |
| `SIGNAL_API` | `http://127.0.0.1:8080`   | signal-cli REST API address                  |
| `DB_PATH`    | `~/note-sorter/notes.db`  | Where your items are stored                  |
| `WEB_HOST`   | `127.0.0.1`               | Viewer bind address (see below)              |
| `WEB_PORT`   | `8765`                    | Viewer port                                  |
| `SEND_REPLY` | `1`                       | Set to `0` to turn off the `→ Tab` replies   |

The starter tabs are set in `STARTER_TABS` near the top of `sorter.py`.

## Viewing from your phone

The viewer only listens on `127.0.0.1` by default and has **no login**. Don't expose it to the internet. To reach it from your phone, a private network like Tailscale is the safe option. Run the sorter with `WEB_HOST` set to the machine's Tailscale IP.

## Your data

Everything you send is stored in `notes.db`, a SQLite file. It's listed in `.gitignore`. Never commit it.

## License

MIT. See `LICENSE`.
