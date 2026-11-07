#!/usr/bin/env bash
#
# Uninstaller for the Async Storage DOS server systemd service
# ("async-storage-bot"). It stops and disables the service and removes the unit
# file, then offers to delete the config file and the stored data — both kept by
# default, so nothing of yours is thrown away unless you say so.
#
# The code and any virtualenv are always left in place. Run it from your
# checkout, as your normal login user (it calls sudo itself only for the steps
# that need root):
#
#   ./uninstall-service.sh
#
set -euo pipefail

SERVICE_NAME="async-storage-bot.service"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}"

# --- Locate the repo (this script lives at its root) ------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "error: systemctl not found — nothing to uninstall on a non-systemd system." >&2
  exit 1
fi

# --- Same user handling as the installer ------------------------------------
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  TARGET_USER="$SUDO_USER"
else
  TARGET_USER="$(id -un)"
fi
as_user() {
  if [ "$(id -u)" -eq 0 ] && [ "$TARGET_USER" != "root" ]; then
    sudo -u "$TARGET_USER" "$@"
  else
    "$@"
  fi
}
as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

# --- Work out the config + data paths from the installed unit ----------------
# (before we remove it, while the paths are still recorded there).
CONFIG=""
WORKDIR="$SCRIPT_DIR"
DATA_OVERRIDE=""
# Every value of a "Key=" line in the unit, with the quoting and escaping
# install-service.sh writes undone (it quotes Environment= lines and doubles %).
unit_setting() {
  sed -n "s/^$1=//p" "$UNIT_DEST" \
    | sed -e 's/^"\(.*\)"$/\1/' -e 's/%%/%/g' -e 's/\\"/"/g' -e 's/\\\\/\\/g'
}
if [ -f "$UNIT_DEST" ]; then
  CONFIG="$(unit_setting Environment | sed -n 's/^STORAGE_SERVER_CONFIG=//p' | head -1 || true)"
  wd="$(unit_setting WorkingDirectory | head -1 || true)"
  [ -n "$wd" ] && WORKDIR="$wd"
  DATA_OVERRIDE="$(unit_setting Environment | sed -n 's/^STORAGE_DATA_DIR=//p' | head -1 || true)"
fi
# The service reads ./server-config.toml from its working directory, which is
# not necessarily this checkout.
[ -z "$CONFIG" ] && CONFIG="$WORKDIR/server-config.toml"

DATA_DIR=""
if command -v python3 >/dev/null 2>&1; then
  DATA_DIR="$(as_user python3 - "$CONFIG" "$WORKDIR" "$DATA_OVERRIDE" <<'PY' || true
import os, pathlib, sys, tomllib
config, workdir = sys.argv[1], sys.argv[2]
override = sys.argv[3] if len(sys.argv) > 3 else ""
data = override or "./data"
if not override:
    try:
        with open(config, "rb") as fh:
            raw = tomllib.load(fh)
        v = raw.get("server", {}).get("data_dir")
        if v:
            data = str(v)
    except Exception:
        pass
p = pathlib.Path(os.path.expanduser(data))
if not p.is_absolute():
    p = pathlib.Path(workdir) / p
print(p)
PY
)"
fi

echo "==> Uninstalling ${SERVICE_NAME}"

# --- Stop, disable, and remove the service ----------------------------------
# "Installed" = the unit file is present, or systemd otherwise knows the unit.
if [ -f "$UNIT_DEST" ] || as_root systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
  if as_root systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "    stopping the service"
    as_root systemctl stop "$SERVICE_NAME"
  fi
  if as_root systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "    disabling the service"
    as_root systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  if [ -f "$UNIT_DEST" ]; then
    echo "    removing $UNIT_DEST"
    as_root rm -f "$UNIT_DEST"
  fi
  as_root systemctl daemon-reload
  as_root systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
  echo "✓ Service stopped, disabled, and removed."
else
  echo "    ${SERVICE_NAME} isn't installed — nothing to stop or remove."
fi

# --- Optionally delete the config file (holds the API key) ------------------
if [ -f "$CONFIG" ]; then
  read -r -p "==> Also delete the config file (holds the API key)? $CONFIG [y/N] " ans
  if [[ "${ans:-N}" =~ ^[Yy] ]]; then
    # The installer's backup of it holds the key too.
    as_user rm -f "$CONFIG" "$CONFIG.bak"
    echo "    removed $CONFIG"
  else
    echo "    keeping $CONFIG"
  fi
fi

# --- Optionally delete the stored data (your files!) ------------------------
# Guard hard against deleting something that isn't clearly a data directory.
if [ -n "$DATA_DIR" ] && [ -d "$DATA_DIR" ] \
   && [ "$DATA_DIR" != "/" ] \
   && [ "$DATA_DIR" != "${HOME:-/root}" ] \
   && [ "$DATA_DIR" != "$WORKDIR" ] \
   && [ "$DATA_DIR" != "$SCRIPT_DIR" ]; then
  echo "==> Your stored files live in: $DATA_DIR"
  read -r -p "    Delete this data directory and everything in it? [y/N] " ans
  if [[ "${ans:-N}" =~ ^[Yy] ]]; then
    as_user rm -rf "$DATA_DIR"
    echo "    removed $DATA_DIR"
  else
    echo "    keeping $DATA_DIR"
  fi
fi

echo
echo "Done. The code and virtualenv (if any) were left in place."
echo "Reinstall any time with: ./install-service.sh"
