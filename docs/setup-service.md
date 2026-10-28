# Run the server as a service (systemd)

On the Server Laptop you'll usually want the storage server running in the
background — started at boot, restarted if it crashes — just like any other
background service. On Linux that's a **systemd service**, and
this repo ships a ready-to-edit unit: [`async-storage-bot.service`](../async-storage-bot.service).

It runs as **your own login user**, and reads every setting (the API key, bind
address, port, limits) from `server-config.toml`, so the service definition
holds no secrets.

## Quick install (recommended)

From your checkout, run the bundled installer as your normal login user:

```bash
./install-service.sh
```

It's a step-by-step wizard that does everything below for you, asking one
question at a time with a default you can accept with Enter:

1. **Python** — checks for Python 3.11+, creates `venv/` (installing
   `python3-venv` with apt if it's missing), and installs the requirements.
2. **Import from a previous install** — it looks for the folder the service
   currently runs from and other checkouts next to this one (or you type a
   path). It brings over that install's settings and API key, so clients keep
   working, and offers to **move**, **copy**, or **leave in place** its stored
   files (`data/`: the `storage/` folder and `manifest.db`).
3. **Settings** — API key (keep, paste, or generate), listen address (it offers
   your Tailscale IP, `127.0.0.1`, or `0.0.0.0`), port, data folder, total
   storage limit and largest upload.
4. **Network** — checks Tailscale is installed and connected, and offers to
   allow the port through `ufw` if that firewall is active.
5. **Review** — a summary of everything it's about to do; your config, data and
   service are untouched until you confirm.
6. **Apply** — stops the running service, moves/copies the data, writes
   `server-config.toml` (backing up the old one as `.bak`), installs and starts
   the unit, health-checks it, and prints the server URL and API key for your
   clients.

It calls `sudo` itself only for the steps that need root. Re-running it is safe
and is how you change settings later: the current ones are offered as defaults,
and choosing to keep them leaves `server-config.toml` byte-for-byte unchanged.

The rest of this page is the **manual** equivalent, if you'd rather set it up by
hand or the installer doesn't fit your setup.

## 1. Get the code and dependencies in place

Put the project wherever you keep your other bots (your home directory is fine)
and install its dependencies:

```bash
cd ~/Async_Storage_DOS          # your checkout of this repo
pip install -r requirements.txt
```

If you prefer an isolated virtualenv:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 2. Create the config file

Copy the example to `server-config.toml` **in the same folder** and edit it:

```bash
cp server-config.example.toml server-config.toml
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # generate a key
${EDITOR:-nano} server-config.toml                              # paste it into [server].api_key
```

Set the `host` you want (`127.0.0.1` for loopback only, or your Tailscale
`100.x.y.z` IP / `0.0.0.0` to reach it from other machines). The server picks up
`server-config.toml` from its working directory automatically — no extra config
needed.

## 3. Edit the unit and install it

Open `async-storage-bot.service` and change the three `EDIT` lines:

- `User=` — your login username (the account your other bots run as).
- `WorkingDirectory=` — the folder containing `server/` (e.g. `/home/you/Async_Storage_DOS`).
- `ExecStart=` — `/usr/bin/python3 -m server`, or your venv's python if you made one.

Then install and start it:

```bash
sudo cp async-storage-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now async-storage-bot.service
```

## 4. Verify

```bash
systemctl status async-storage-bot.service     # should be "active (running)"
journalctl -u async-storage-bot.service -f     # follow the live logs
curl -s http://HOST:PORT/health                # -> {"status":"ok"}
```

Use the `host` and `port` from `server-config.toml`: a server bound to your
Tailscale IP doesn't answer on `127.0.0.1`. (For `0.0.0.0`, use `127.0.0.1`.)

The server also writes a **per-day log file** to `log_dir` (default
`<data_dir>/logs`), one `YYYY-MM-DD.txt` per day, so you have a persistent record
alongside `journalctl`:

```bash
tail -f "$(grep -oP 'data_dir\s*=\s*"\K[^"]+' server-config.toml)/logs/$(date +%F).txt"
```

## Managing the service

```bash
sudo systemctl restart async-storage-bot.service   # after editing the config
sudo systemctl stop async-storage-bot.service
sudo systemctl disable async-storage-bot.service   # stop starting at boot
```

The server re-reads the config file only at startup, so **restart the service
after changing `server-config.toml`**.

## Uninstalling

Run the bundled uninstaller from your checkout:

```bash
./uninstall-service.sh
```

It stops and disables the service, removes the unit from
`/etc/systemd/system/`, and reloads systemd. It then asks — **defaulting to
no** — whether to also delete `server-config.toml` (which holds the API key) and
the data directory (your stored files). The code and any virtualenv are always
left untouched, so `./install-service.sh` can put it back at any time.

To do it by hand instead:

```bash
sudo systemctl disable --now async-storage-bot.service
sudo rm /etc/systemd/system/async-storage-bot.service
sudo systemctl daemon-reload
```

## Notes

- **It runs as you.** Because `User=` is your login account and the code/data
  live in your home directory, the data directory (`[server].data_dir`, default
  `./data` next to the code) is owned by you and needs no extra permissions
  setup.
- **Not on Linux?** The same config file works anywhere. On macOS use a
  `launchd` plist that runs `python3 -m server` from this folder; on Windows,
  NSSM or a Scheduled Task. No code changes — just the config file.
