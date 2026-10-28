# Async Storage DOS — a simplified, self-hosted file store

Turn one machine (your **Server Laptop**) into personal file storage, and reach
it from your other machines through a small desktop app. Nothing syncs in the
background: you browse what's on the server and download exactly the files you
want, when you want them. It works across different networks using
[Tailscale](docs/setup-tailscale.md), with no port-forwarding and no third-party
cloud holding your files.

```
   Client machine (any network)                    Server Laptop
 ┌──────────────────────────┐                  ┌───────────────────────────┐
 │  desktop app (tkinter)   │                  │  FastAPI server :8000     │
 │  browse server files     │   Tailscale      │  data/storage/  (files)   │
 │  download on demand  ───►│◄══ encrypted ═══►│  data/manifest.db         │
 │  your download folder    │   mesh, any net  │  X-API-Key auth           │
 └──────────────────────────┘                  └───────────────────────────┘
```

## How it works

- The **server** stores every file plus a manifest (path, sha256, size, mtime,
  and delete *tombstones*). One shared API key guards every request.
- The **client** is a desktop app that lists the server's files live. You
  download the ones you want into a download folder you pick once, and upload
  new files by dragging them onto the window.
- The **server is the source of truth**. A downloaded file is simply yours: the
  app never tracks, updates or deletes it afterwards.

## Requirements

- Python 3.11+ (uses the standard-library `tomllib`)
- `pip install -r requirements.txt`

## Run the server (on the Server Laptop)

The server reads everything — the API key and all other settings — from a
**config file**, so nothing secret sits on the command line.

```bash
pip install -r requirements.txt

# Create your config from the example:
cp server-config.example.toml server-config.toml

# Generate an API key and paste it into [server].api_key in that file:
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Start the server (data/ is created automatically). It reads ./server-config.toml
# by default; pass --config to point elsewhere:
python -m server
python -m server --config /etc/async-storage-bot/config.toml   # explicit path
```

The config file has two tables (see `server-config.example.toml` for the fully
commented version):

```toml
[server]
api_key = "<the-key-you-generated>"   # required, min 16 chars
data_dir = "./data"                    # files + manifest DB (created automatically)
host = "127.0.0.1"                      # loopback by default; see Binding below
port = 8000
# log_dir = "./data/logs"              # optional; defaults to <data_dir>/logs

[limits]
max_file_bytes = 10737418240           # 10 GiB — largest single upload accepted
max_concurrent_uploads = 4             # uploads written to disk at once
max_total_bytes = 0                    # 0 = unlimited; a total storage budget
upload_timeout = 60                    # seconds to wait for the next upload chunk
```

Uploads only ever **create** files: an upload whose name is already taken —
by a file, a folder, or another upload still in flight — is refused with
`409`, never silently replaced. Names are compared **ignoring case**
(`Photo.JPG` takes `photo.jpg`), because Windows and macOS disks, and
exFAT/NTFS drives anywhere, hold them as one file. A move may change only the
case of a file's own name. For the same reason a new name (upload, move or
folder) is refused with `400` if any part of it ends in a dot or a space,
contains `< > : " | ? *`, or is a Windows device name such as `CON` or
`nul.txt`.

`max_file_bytes` is the largest upload accepted; requests are streamed to disk
and rejected the moment they exceed it, so the server never buffers an unbounded
file in memory. `max_total_bytes` set to your disk size stops uploads filling
the disk, and `upload_timeout` drops stalled/slow-loris connections.

**Logs.** The server writes all of its output — startup, requests, shutdown — to
a **per-day log file** in `log_dir` (default `<data_dir>/logs`), one
`YYYY-MM-DD.txt` per day, and still logs to the console so `journalctl` keeps
working under the service. A new file is started automatically at midnight.

Every setting can also be overridden by its environment variable
(`STORAGE_API_KEY`, `STORAGE_DATA_DIR`, `STORAGE_HOST`, `STORAGE_PORT`,
`STORAGE_LOG_DIR`, `STORAGE_MAX_FILE_BYTES`, `STORAGE_MAX_CONCURRENT_UPLOADS`,
`STORAGE_MAX_TOTAL_BYTES`, `STORAGE_UPLOAD_TIMEOUT`), which takes precedence over
the file — handy for a one-off run or for keeping the key out of the file in a
container. Point the server at a specific config file with `--config PATH` or the
`STORAGE_SERVER_CONFIG` environment variable.

> **Binding:** the server listens on `127.0.0.1` by default. To reach it from
> other machines over Tailscale, set `[server].host` to the server's Tailscale
> IP (its `100.x.y.z` address). Only use `0.0.0.0` if every network interface on
> the machine is trusted.

### Run it as a service

To keep the server running in the background — started at boot and restarted if
it crashes, like any other bot — install it as a systemd service. The easiest
way is the bundled installer, run as your normal login user from your checkout:

```bash
./install-service.sh
```

It's a step-by-step wizard — each question has a default, so pressing Enter
through it gives a working setup:

1. **Python** — checks for 3.11+, creates `venv/`, installs the requirements.
2. **Import** — finds a previous install (the folder the service currently runs
   from, or other checkouts next to this one) and brings over its settings and
   API key, and **moves or copies its stored files** — handy when upgrading to a
   new checkout. Skip it for a fresh setup.
3. **Settings** — API key (keep, paste, or generate), listen address (it offers
   your Tailscale IP), port, data folder, and storage limits.
4. **Network** — checks Tailscale and offers to open the port in `ufw`.
5. **Review** — shows the plan; nothing is changed until you confirm.
6. **Apply** — writes `server-config.toml`, installs the service to run as
   **your own user**, starts it, health-checks it, and prints the URL and key
   your clients need. (It calls `sudo` itself for the steps that need root.)

Re-run it any time to change settings — your current ones are the defaults.

Prefer to do it by hand? This repo also ships a ready-to-edit unit,
[`async-storage-bot.service`](async-storage-bot.service):

```bash
# edit the three EDIT lines (User=, WorkingDirectory=, ExecStart=), then:
sudo cp async-storage-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now async-storage-bot.service
```

To remove it later, run **`./uninstall-service.sh`** — it stops and disables the
service and deletes the unit, then asks (defaulting to *no*) whether to also
delete the config file and your stored data.

Full walkthrough, including how to manage the service, in
**[docs/setup-service.md](docs/setup-service.md)**.

## Run the client (on each other machine)

### Windows: automatic setup

Double-click **`install-client.bat`** in your checkout (or run
`powershell -ExecutionPolicy Bypass -File .\install-client.ps1`). It's a
step-by-step wizard like the server's — each question has a default, so
pressing Enter through it gives a working setup:

1. **Python** — finds Python 3.11+ (offers to install it with `winget` if it's
   missing), creates `venv/`, and installs the client's requirements.
2. **Settings** — server URL, API key and download folder (a folder picker
   opens), then a **connection test** that tells "can't reach the server" apart
   from "wrong API key". If it fails you can re-enter them or keep them anyway.
3. **Shortcut** — an **Async Storage** shortcut on your desktop that starts the
   app without a console window.
4. **Review** — shows the plan; your config and shortcut aren't touched until
   you confirm.
5. **Apply** — writes `config.toml`, creates the shortcut, and offers to start
   the app.

Re-run it any time to change settings — your current ones are the defaults.
To remove it, run **`uninstall-client.bat`**: it removes the desktop shortcut and
`venv/`, then asks (defaulting to *no*) whether to also delete `config.toml`.
Your download folder and everything in it are never touched.

### Manually (any OS)

```bash
pip install -r requirements.txt
python -m client.gui        # the desktop app
```

**First launch** opens a **setup screen** — enter the server URL (e.g.
`http://server-laptop:8000`), the same API key as the server, and the **download
folder** your downloads are saved into; it's saved for you (no editing files by
hand). Change any of it from the **⚙ Settings** button in the app. (You can still pre-create `config.toml` from
`config.example.toml` if you prefer, or point `STORAGE_CLIENT_CONFIG` at one.)

Optional connectivity check from a terminal (needs a saved config):

```bash
python -m client status     # print server health and exit
```

The window is a **folder browser**, refreshed live. It shows the folder you're
in: sub-folders you can **double-click to open**, and files. Use **⬆ Up** or the
**..** row to go back out; the current location is shown above the list. Every
file has a **↓ Download** action.

**Downloading** — click a file's **↓ Download**, right-click a folder →
**Download folder**, or select several files and folders and press
**Download**. Everything goes into your download folder:

- a **file** lands straight in it (`/Photos/2024/a.jpg` → `a.jpg`);
- a **folder** lands in it with its own structure, empty sub-folders included
  (`/Photos/2024` → `2024/…`).

The app looks at the download folder only at that moment, to catch a name
that's already taken — nothing is tracked afterwards:

- a **folder** that already exists there is **not downloaded** (a popup says so);
- for **files** that already exist there you're asked once, for all of them:
  **Overwrite**, **Keep both** (saved as `name (1).ext`), or **Skip**;
- a name that gets taken while a download waits in the queue is never
  overwritten — that file keeps both instead.

Each download is checked against the server's sha256.

The list is checked live, so files and folders added, changed, moved, or removed
on the server update on their own, on every machine.

**Search:** type in the **🔍 Search** box (top right) to filter to every file
whose path matches, across all folders — each result shows its full path, and the
per-file actions, **Move**, and **Delete** all work on results. Double-click a
result to jump to its folder; clear the box (or press Esc in it) to return to the
folder view.

**Organise your files:**

- **Select multiple** files with **Ctrl/⌘-click** or **Shift-click**, then Move or
  Delete them all at once.
- **Drag to move** — drag a file *or a whole folder* onto a folder row (or the
  **..** row to go up a level) to move it there; a faint label of what you're
  carrying follows the cursor and the folder highlights as you hover. If several
  rows are selected, dragging any one of them moves the whole selection. A folder
  nests under its target keeping its own name.
- **New folder** creates a folder inside the one you're viewing (empty folders
  persist on the server and show up on your other machines).
- **Move…** — or select one or more files and pick (or type) a destination folder
  from the dialog; either way they're moved on the server. A folder can't be
  moved into itself.
- The red **Delete** button moves the *selected* files **to Trash** on the
  server — see below. Select folders too (or right-click one → **Delete folder**)
  and their whole contents go with them. Files you've downloaded are never
  touched.
- **Open download folder** opens your download folder in your file manager.

**Upload new files** by dragging files or folders onto the window (or click
**Add files…**) — they land **in the folder you're currently viewing**. A
confirmation prompt lists what will be sent before anything is uploaded; folders
are included recursively. Uploads never replace anything: a file or folder whose
name is already used in that folder is **not uploaded** (a popup lists them) and
the rest go ahead. Nothing can be uploaded into Trash. The window uses the standard library's `tkinter`;
drag-and-drop additionally needs `tkinterdnd2` (in `requirements.txt`) — without
it, the **Add files…** button still uploads.

Uploads and downloads are **streamed to and from disk**, so multi-gigabyte files
neither fill memory nor time out.

**Deleting is reversible — the Trash.** Deleted files (and folders, contents
included) are moved to **Trash** rather than destroyed, so a mistake costs
nothing. Trash has its own **🗑 Trash** button at the top right of the window
(showing how many items it holds) instead of appearing as a folder: click it to
open Trash and **Back to files** to return, or drag rows onto it to delete them.
Search outside Trash skips trashed files; inside Trash it searches only them.
Nothing in Trash is ever overwritten: deleting a second `a.txt` puts it there as
`a (1).txt`, and a second `Album` folder goes in whole as `Album (1)`. The
top-level name `Trash` is reserved, ignoring case: New folder, Move and uploads
refuse a top-level `trash`. If one is left from before, Delete says so; move it
into another folder first. In Trash the buttons change to match:

- **Restore** puts the selection back where it came from (right-click a folder →
  **Restore folder** for the whole thing, empty sub-folders included). If that
  spot has been taken since, it comes back beside it as `name (1).ext`.
- **Delete forever** and **Empty Trash** are the only actions that actually
  destroy anything, and each asks first.

**Nothing blocks the window.** Long jobs run in two background lanes — transfers
in one, deletes and moves in the other — so a delete never waits behind a big
download, and the list stays usable while they run:

- The **Tasks** strip shows a row per running job (Transfer, Install check,
  Delete, Move) with its own progress, the file in flight, the transfer rate, and
  a **Stop** button.
- The **Queue** strip lists jobs waiting their turn; **✕** drops one before it
  starts.
- **Stop** finishes the item in hand and skips the rest. Stopping a *folder
  delete* puts back whatever already reached Trash, so a half-finished delete
  never leaves a folder split between Trash and where it was. Stopping a download
  abandons the file in flight without leaving a partial copy behind.
- Asking for a file that's **already queued or downloading does nothing** — the
  status line says so rather than fetching it a second time. Drop the queued job
  with **✕** and it can be requested again.

### Android

There's also a native **Android client** (Java) with the **same rules** as the
desktop app — the same folder browser, downloads into a folder you pick (with the
same name-clash handling), Trash and restore, folder deletes and moves, per-task
progress with Stop, and a queue of waiting jobs — plus transfers that continue
in a foreground service when you leave the app. Files move by **drag-and-drop**
onto a folder or through the ⋮ menu, and uploads use the document picker. See **[android/README.md](android/README.md)**
to build it in Android Studio and point it at your server.

### Package the desktop app as an .exe

To ship the desktop app as a double-clickable `.exe`, build it with
[PyInstaller](https://pyinstaller.org):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "Local DB" \
    --icon client/app_icon.ico \
    --add-data "client/app_icon.png;client" \
    --add-data "client/app_icon.ico;client" \
    client/gui.py
#   the executable lands in dist\Local DB.exe  (run it next to config.toml)
```

Run it from the project root, so the `client` and `common` packages are found.
The window/taskbar icon is set at runtime from the bundled `client/app_icon.png`
and `client/app_icon.ico`; `--icon` gives the `.exe` file itself the icon. (On
macOS/Linux use `:` instead of `;` in `--add-data`.)

## Across different networks

See **[docs/setup-tailscale.md](docs/setup-tailscale.md)**. In short: install
Tailscale on the server and every client, sign them into the same account, and
set the client's `[server].url` to the server's MagicDNS name
(`http://server-laptop:8000`). No router changes, encrypted end to end.

## Project layout

```
server/   FastAPI app: main, storage (atomic blob writes), db (manifest), auth, config, logging
client/   desktop app (gui), background op lanes (ops), streaming api client, config, status CLI
android/  native Android client (Java) — same on-demand browse/download/upload
common/   sha256 helpers + shared manifest model
docs/     Tailscale setup guide, systemd service guide
tests/    unittest suite: python -m unittest discover tests  (server tests need fastapi)
async-storage-bot.service   systemd unit to run the server as a service
server-config.example.toml  server config template (copy to server-config.toml)
```

## Design notes & limits

- On-demand only — the client downloads files you pick; it doesn't sync a folder,
  push changes, or keep track of what it downloaded.
- Uploads only create: to change a file on the server, delete it and upload the
  new version.
- Whole-file transfer (no block-level delta) — great for documents and photos;
  large files transfer fully, but streamed to/from disk so size isn't a limit.
- The server is the source of truth; the client holds only what you download.
- Deletes are reversible by default: everything lands in `Trash` first, and only
  **Delete forever** / **Empty Trash** remove data for good.
- Tailscale handles NAT traversal and encryption, so the app stays plain HTTP
  over a trusted private network.

## License

MIT — see [LICENSE](LICENSE).
