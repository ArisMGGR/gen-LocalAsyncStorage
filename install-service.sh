#!/usr/bin/env bash
#
# Step-by-step setup for the Async Storage server as a systemd service
# ("async-storage-bot").
#
# It walks through everything the Server Laptop needs, one question at a time,
# offering a default for each (press Enter to accept it):
#
#   1. Python     — checks for Python 3.11+, creates venv/, installs requirements
#   2. Import     — optionally brings over the settings and stored files of a
#                   previous install (e.g. an older checkout of this repo)
#   3. Settings   — API key, listen address, port, data folder, storage limits
#   4. Network    — Tailscale check and (if ufw is active) the firewall rule
#   5. Review     — shows the plan; your config, data and service are not
#                   touched until you confirm
#   6. Apply      — writes server-config.toml, moves/copies the data, installs
#                   and starts the service, then health-checks it
#
# Run it from your checkout, as your normal login user (it calls sudo itself,
# only for the steps that need root). Re-running it is safe: your current
# settings are offered as the defaults.
#
#   ./install-service.sh
#
set -euo pipefail

SERVICE_NAME="async-storage-bot.service"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}"
TOTAL_STEPS=6

# --- Locate the repo (this script lives at its root) ------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"
CONFIG="$SCRIPT_DIR/server-config.toml"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -d "$SCRIPT_DIR/server" ] \
  || die "can't find the 'server/' package next to this script. Run it from your checkout ($SCRIPT_DIR)."

# --- This installer is Linux + systemd only ---------------------------------
command -v systemctl >/dev/null 2>&1 \
  || die "systemctl not found — this installer needs Linux with systemd. On macOS use launchd, on Windows NSSM; see docs/setup-service.md."

[ -t 0 ] || die "this setup asks questions — run it from a terminal."

# --- Who should the service run as? -----------------------------------------
# When invoked with sudo, run the service as the human who called sudo, not root.
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  TARGET_USER="$SUDO_USER"
else
  TARGET_USER="$(id -un)"
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

# Run a command as the target user (matters only when this script itself is
# running as root via sudo — otherwise it's just the command).
as_user() {
  if [ "$(id -u)" -eq 0 ] && [ "$TARGET_USER" != "root" ]; then
    sudo -u "$TARGET_USER" "$@"
  else
    "$@"
  fi
}

# Run a command with root privileges (via sudo when we aren't already root).
as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

# --- Prompt helpers ----------------------------------------------------------
say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m[%s/%s] %s\033[0m\n' "$1" "$TOTAL_STEPS" "$2"; }

# ask VAR "Question" [default] — reads one line; empty input takes the default.
ask() {
  local __var="$1" __q="$2" __def="${3-}" __ans
  if [ -n "$__def" ]; then
    read -r -p "    $__q [$__def]: " __ans
  else
    read -r -p "    $__q: " __ans
  fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

# confirm "Question" [Y|N] — succeeds on yes.
confirm() {
  local q="$1" def="${2:-Y}" hint ans
  if [ "$def" = "Y" ]; then hint="Y/n"; else hint="y/N"; fi
  while true; do
    read -r -p "    $q [$hint] " ans
    case "${ans:-$def}" in
      [Yy]*) return 0 ;;
      [Nn]*) return 1 ;;
    esac
    say "    Please answer y or n."
  done
}

human() {  # bytes -> "1.5GiB"
  numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"
}
bytes_to_gib() { awk -v b="$1" 'BEGIN { printf "%g", b / 1073741824 }'; }
gib_to_bytes() { awk -v g="$1" 'BEGIN { printf "%.0f", g * 1073741824 }'; }

echo "Async Storage server setup"
echo "    folder:       $SCRIPT_DIR"
echo "    run as user:  $TARGET_USER"
echo "    Press Enter to accept the default shown in [brackets]."

# =============================================================================
step 1 "Python and dependencies"
# =============================================================================
py_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; }
py_ver() { "$1" -c 'import platform; print(platform.python_version())'; }
deps_ok() { as_user "$PYTHON" -c "import fastapi, uvicorn" >/dev/null 2>&1; }

VENV_PY="$SCRIPT_DIR/venv/bin/python"
if [ -x "$VENV_PY" ] && py_ok "$VENV_PY"; then
  PYTHON="$VENV_PY"
  say "    Using the existing virtualenv (Python $(py_ver "$PYTHON"))."
else
  BASE_PY="$(command -v python3 || true)"
  [ -n "$BASE_PY" ] \
    || die "no python3 found. Install Python 3.11+ (e.g. 'sudo apt install python3 python3-venv') and re-run."
  py_ok "$BASE_PY" \
    || die "$BASE_PY is Python $(py_ver "$BASE_PY"); the server needs 3.11 or newer."
  say "    Found $BASE_PY (Python $(py_ver "$BASE_PY"))."

  if confirm "Install the dependencies into a private virtualenv (venv/)? (recommended)" Y; then
    say "    Creating venv/ ..."
    if ! as_user "$BASE_PY" -m venv "$SCRIPT_DIR/venv" >/dev/null 2>&1; then
      # Debian/Ubuntu ship the venv module as a separate package.
      as_user rm -rf "$SCRIPT_DIR/venv"
      if command -v apt-get >/dev/null 2>&1 \
         && confirm "Python's venv module is missing. Install it with apt (python3-venv)?" Y; then
        as_root apt-get install -y python3-venv
        as_user "$BASE_PY" -m venv "$SCRIPT_DIR/venv" || die "still couldn't create the virtualenv."
      else
        die "couldn't create the virtualenv. Install python3-venv and re-run."
      fi
    fi
    PYTHON="$VENV_PY"
  else
    PYTHON="$BASE_PY"
  fi
fi

# Inside the venv, always sync to requirements.txt (a no-op when up to date);
# for a system Python only install when something is missing.
if [ "$PYTHON" = "$VENV_PY" ] || ! deps_ok; then
  say "    Installing requirements (this can take a minute) ..."
  as_user "$PYTHON" -m pip install -q --upgrade pip || true
  as_user "$PYTHON" -m pip install -q -r "$SCRIPT_DIR/requirements.txt" || true
fi
deps_ok || die "dependencies are still missing. Install them manually, then re-run:
       $PYTHON -m pip install -r requirements.txt"
say "    ✓ Dependencies ready."

# Print a config file's settings as shell assignments. Relative paths are
# resolved against $2, the folder the server that used this config ran from.
read_config() {
  "$PYTHON" - "$1" "$2" <<'PY'
import pathlib, shlex, sys, tomllib
path, base = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
raw = tomllib.loads(path.read_text(encoding="utf-8"))
s, l = raw.get("server") or {}, raw.get("limits") or {}
def absolute(v):
    p = pathlib.Path(str(v)).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())
data_dir = s.get("data_dir") or "./data"
log_dir = s.get("log_dir") or ""
vals = {
    "C_API_KEY": s.get("api_key", ""),
    "C_HOST": s.get("host", "127.0.0.1"),
    "C_PORT": s.get("port", 8000),
    "C_DATA_DIR_RAW": data_dir,
    "C_DATA_DIR": absolute(data_dir),
    "C_LOG_DIR_RAW": log_dir,
    "C_LOG_DIR": absolute(log_dir) if log_dir else "",
    "C_MAX_FILE": l.get("max_file_bytes", 10 * 1024**3),
    "C_MAX_CONC": l.get("max_concurrent_uploads", 4),
    "C_MAX_TOTAL": l.get("max_total_bytes", 0),
    "C_TIMEOUT": l.get("upload_timeout", 60),
}
for k, v in vals.items():
    print(f"{k}={shlex.quote(str(v))}")
PY
}

load_config_vars() {  # $1 = config file, $2 = folder its relative paths are based on
  local out
  out="$(read_config "$1" "$2")" || die "couldn't read $1 — is it valid TOML?"
  eval "$out"
}

# Defaults for a brand-new install.
C_API_KEY=""; C_HOST=""; C_PORT=8000
C_DATA_DIR_RAW="./data"; C_DATA_DIR="$SCRIPT_DIR/data"
C_LOG_DIR_RAW=""; C_LOG_DIR=""
C_MAX_FILE=$((10 * 1024 * 1024 * 1024)); C_MAX_CONC=4; C_MAX_TOTAL=0; C_TIMEOUT=60

# =============================================================================
step 2 "Import from a previous install"
# =============================================================================
# Candidates are stored as "config-file|folder-it-ran-from".
CANDIDATES=()
add_candidate() {
  local cfg wd c
  cfg="$(readlink -f "$1" 2>/dev/null || true)"
  wd="$(readlink -f "$2" 2>/dev/null || true)"
  [ -n "$cfg" ] && [ -f "$cfg" ] || return 0
  [ "$wd" = "$SCRIPT_DIR" ] && return 0
  for c in "${CANDIDATES[@]:-}"; do
    [ "${c%%|*}" = "$cfg" ] && return 0
  done
  CANDIDATES+=("$cfg|$wd")
}

# 1) whatever the currently installed service runs from,
# Every value of a "Key=" line in the unit, with the quoting and escaping this
# script writes (below) undone: Environment= lines are quoted, % is doubled.
unit_setting() {
  sed -n "s/^$1=//p" "$UNIT_DEST" \
    | sed -e 's/^"\(.*\)"$/\1/' -e 's/%%/%/g' -e 's/\\"/"/g' -e 's/\\\\/\\/g'
}
if [ -f "$UNIT_DEST" ]; then
  unit_wd="$(unit_setting WorkingDirectory | head -1)"
  unit_cfg="$(unit_setting Environment | sed -n 's/^STORAGE_SERVER_CONFIG=//p' | head -1)"
  if [ -n "$unit_wd" ]; then
    add_candidate "${unit_cfg:-$unit_wd/server-config.toml}" "$unit_wd"
  fi
fi
# 2) other checkouts next to this one or in the home folder.
for d in "$(dirname "$SCRIPT_DIR")"/*/ "${TARGET_HOME:-/nonexistent}"/*/; do
  d="${d%/}"
  [ -d "$d/server" ] && [ -f "$d/server-config.toml" ] && add_candidate "$d/server-config.toml" "$d"
done

OLD_CFG=""; OLD_WD=""
while true; do
  if [ "${#CANDIDATES[@]}" -gt 0 ]; then
    say "    Found existing installs you can bring your settings and files over from:"
    i=1
    for c in "${CANDIDATES[@]}"; do
      say "      $i) ${c#*|}"
      i=$((i + 1))
    done
    say "      0) Don't import anything"
    say "      (or type the path of another old install folder)"
    default=1
    [ -f "$CONFIG" ] && default=0   # this folder is already set up
    ask choice "Import from" "$default"
  else
    say "    No previous install found automatically."
    ask choice "Path of an old install folder to import from (Enter to skip)" ""
    choice="${choice:-0}"
  fi

  if [ "$choice" = "0" ]; then
    break
  elif [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -le "${#CANDIDATES[@]}" ]; then
    c="${CANDIDATES[$((choice - 1))]}"
    OLD_CFG="${c%%|*}"; OLD_WD="${c#*|}"
    break
  else
    p="${choice/#\~/$TARGET_HOME}"
    if [ -d "$p" ] && [ -f "$p/server-config.toml" ]; then
      OLD_WD="$(readlink -f "$p")"; OLD_CFG="$OLD_WD/server-config.toml"
    elif [ -f "$p" ]; then
      OLD_CFG="$(readlink -f "$p")"; OLD_WD="$(dirname "$OLD_CFG")"
    else
      say "    No server-config.toml found at '$choice'. Try again."
      continue
    fi
    if [ "$OLD_WD" = "$SCRIPT_DIR" ]; then
      say "    That's this folder — nothing to import from. Try again."
      OLD_CFG=""; OLD_WD=""
      continue
    fi
    break
  fi
done

DATA_ACTION="none"      # none | move | copy | keep
NEW_DATA=""
if [ -n "$OLD_CFG" ]; then
  load_config_vars "$OLD_CFG" "$OLD_WD"
  say "    Importing settings from $OLD_CFG"

  OLD_DATA="$C_DATA_DIR"
  if [ ! -e "$OLD_DATA/manifest.db" ] && [ ! -d "$OLD_DATA/storage" ]; then
    say "    No stored files at $OLD_DATA — nothing to bring over."
    C_DATA_DIR_RAW="./data"; C_DATA_DIR="$SCRIPT_DIR/data"
  else
    case "$OLD_DATA" in
      "$OLD_WD"/*)
        rel="${OLD_DATA#"$OLD_WD"/}"
        NEW_DATA="$SCRIPT_DIR/$rel"
        size="$(du -sb "$OLD_DATA" 2>/dev/null | cut -f1)"; size="${size:-0}"
        free="$(df -PB1 "$SCRIPT_DIR" | awk 'NR == 2 { print $4 }')"
        say "    Your stored files are in the old folder: $OLD_DATA ($(human "$size"))"

        if [ -e "$NEW_DATA" ] && [ -n "$(ls -A "$NEW_DATA" 2>/dev/null)" ]; then
          say "    ! $NEW_DATA already has data in it, so the old files can't be moved or"
          say "      copied there without mixing the two. The new install will use them"
          say "      from the old folder instead (sort it out by hand later if you like)."
          DATA_ACTION="keep"
        else
          say "      1) Move them here    — instant on the same disk; the old folder keeps no data"
          say "      2) Copy them here    — the old copy stays as a backup; needs $(human "$size") free ($(human "$free") available)"
          say "      3) Leave them there  — the new install reads them from the old folder"
          default=1
          [ "$free" -gt $((size + size / 10)) ] && default=2
          while true; do
            ask choice "Choice" "$default"
            case "$choice" in
              1) DATA_ACTION="move"; break ;;
              2) DATA_ACTION="copy"; break ;;
              3) DATA_ACTION="keep"; break ;;
            esac
            say "    Please pick 1, 2 or 3."
          done
        fi

        if [ "$DATA_ACTION" = "keep" ]; then
          C_DATA_DIR_RAW="$OLD_DATA"; NEW_DATA=""
        else
          C_DATA_DIR_RAW="./$rel"; C_DATA_DIR="$NEW_DATA"
        fi
        ;;
      *)
        # Outside the old folder (e.g. /var/lib/...): nothing to move, just keep
        # pointing at it.
        say "    Your stored files live outside the old folder, at $OLD_DATA."
        say "    The new install will keep using them there."
        C_DATA_DIR_RAW="$OLD_DATA"
        ;;
    esac
  fi

  # A log folder set explicitly inside the old folder moves to the same spot here.
  if [ -n "$C_LOG_DIR" ]; then
    case "$C_LOG_DIR" in
      "$OLD_WD"/*) C_LOG_DIR_RAW="./${C_LOG_DIR#"$OLD_WD"/}" ;;
      *)           C_LOG_DIR_RAW="$C_LOG_DIR" ;;
    esac
  fi
elif [ -f "$CONFIG" ]; then
  load_config_vars "$CONFIG" "$SCRIPT_DIR"
  say "    Not importing. This folder already has a server-config.toml; its settings"
  say "    are used as the defaults below."
else
  say "    Not importing — setting up fresh."
fi

# =============================================================================
step 3 "Settings"
# =============================================================================
TS_IP=""
if command -v tailscale >/dev/null 2>&1; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
fi

WRITE_CONFIG=1
KEY_SOURCE="existing"
ASK_SETTINGS=1
if [ -n "$C_API_KEY" ]; then
  say "    Current settings:"
  say "      listen on:      $C_HOST:$C_PORT"
  say "      data folder:    $C_DATA_DIR_RAW"
  say "      storage limit:  $([ "$C_MAX_TOTAL" -eq 0 ] && echo unlimited || human "$C_MAX_TOTAL")"
  say "      max file size:  $(human "$C_MAX_FILE")"
  if confirm "Keep these settings (and the same API key, so clients keep working)?" Y; then
    ASK_SETTINGS=0
    # Nothing changed and nothing moved: leave the existing file byte-for-byte.
    [ -z "$OLD_CFG" ] && WRITE_CONFIG=0
  fi
fi

if [ "$ASK_SETTINGS" -eq 1 ]; then
  # --- API key
  if [ -n "$C_API_KEY" ] && confirm "Keep the current API key? (clients already use it)" Y; then
    KEY_SOURCE="existing"
  else
    say "    Every client must present the same API key as the server."
    while true; do
      read -r -s -p "    Paste an existing key, or press Enter to generate a new one: " key; echo
      if [ -z "$key" ]; then
        C_API_KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
        KEY_SOURCE="generated"
        break
      elif [ "${#key}" -lt 16 ]; then
        say "    That key is only ${#key} characters; the server needs at least 16. Try again."
      else
        C_API_KEY="$key"; KEY_SOURCE="pasted"
        break
      fi
    done
  fi

  # --- Listen address
  HOSTS=(); LABELS=()
  if [ -n "$TS_IP" ]; then
    HOSTS+=("$TS_IP"); LABELS+=("this machine's Tailscale IP — reachable from your devices anywhere (recommended)")
  fi
  HOSTS+=("127.0.0.1"); LABELS+=("this machine only")
  HOSTS+=("0.0.0.0");   LABELS+=("every network interface — only if all of them are trusted")
  say "    Which address should the server listen on?"
  for i in "${!HOSTS[@]}"; do
    say "      $((i + 1))) ${HOSTS[$i]}  — ${LABELS[$i]}"
  done
  say "      (or type an address directly)"
  def_host="${C_HOST:-${TS_IP:-127.0.0.1}}"
  ask choice "Listen address" "$def_host"
  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#HOSTS[@]}" ]; then
    C_HOST="${HOSTS[$((choice - 1))]}"
  else
    C_HOST="$choice"
  fi

  # --- Port
  while true; do
    ask C_PORT "Port" "$C_PORT"
    [[ "$C_PORT" =~ ^[0-9]+$ ]] && [ "$C_PORT" -ge 1 ] && [ "$C_PORT" -le 65535 ] && break
    say "    The port must be a number from 1 to 65535."
  done

  # --- Data folder (already decided when importing stored files)
  if [ "$DATA_ACTION" = "none" ] && [ -z "$OLD_CFG" ]; then
    ask C_DATA_DIR_RAW "Folder for the stored files and database" "$C_DATA_DIR_RAW"
  fi

  # --- Limits
  while true; do
    ask gib "Total storage limit in GiB (0 = unlimited)" "$(bytes_to_gib "$C_MAX_TOTAL")"
    [[ "$gib" =~ ^[0-9]+([.][0-9]+)?$ ]] && break
    say "    Enter a number, e.g. 100 or 0."
  done
  C_MAX_TOTAL="$(gib_to_bytes "$gib")"
  while true; do
    ask gib "Largest single upload in GiB" "$(bytes_to_gib "$C_MAX_FILE")"
    [[ "$gib" =~ ^[0-9]+([.][0-9]+)?$ ]] && [ "$(gib_to_bytes "$gib")" -gt 0 ] && break
    say "    Enter a number above 0, e.g. 10."
  done
  C_MAX_FILE="$(gib_to_bytes "$gib")"
fi
[ -z "$C_HOST" ] && C_HOST="127.0.0.1"

# =============================================================================
step 4 "Network"
# =============================================================================
ON_TAILSCALE=0
[ -n "$TS_IP" ] && [ "$C_HOST" = "$TS_IP" ] && ON_TAILSCALE=1

if ! command -v tailscale >/dev/null 2>&1; then
  say "    Tailscale isn't installed. It's how your devices reach this server from any"
  say "    network — see docs/setup-tailscale.md (you can re-run this setup afterwards)."
elif [ -z "$TS_IP" ]; then
  say "    Tailscale is installed but not connected ('sudo tailscale up' to sign in)."
else
  say "    Tailscale is connected: $TS_IP"
fi
case "$C_HOST" in
  127.*|localhost) say "    ! Listening on $C_HOST: only this machine can reach the server." ;;
esac

OPEN_FW=0
if command -v ufw >/dev/null 2>&1; then
  case "$C_HOST" in
    127.*|localhost) ;;
    *)
      if confirm "If the ufw firewall is active, allow port $C_PORT through it?" Y; then
        OPEN_FW=1
      fi
      ;;
  esac
fi

# =============================================================================
step 5 "Review"
# =============================================================================
say "    Install folder:  $SCRIPT_DIR"
say "    Run as user:     $TARGET_USER"
say "    Python:          $PYTHON"
if [ -n "$OLD_CFG" ]; then
  say "    Import from:     $OLD_WD"
fi
case "$DATA_ACTION" in
  move) say "    Stored files:    MOVE $OLD_DATA -> $NEW_DATA" ;;
  copy) say "    Stored files:    COPY $OLD_DATA -> $NEW_DATA" ;;
  keep) say "    Stored files:    keep using $OLD_DATA" ;;
  none) say "    Stored files:    $C_DATA_DIR_RAW" ;;
esac
if [ "$WRITE_CONFIG" -eq 1 ]; then
  say "    Config file:     write $CONFIG$([ -f "$CONFIG" ] && echo " (old one saved as .bak)")"
else
  say "    Config file:     keep $CONFIG unchanged"
fi
case "$KEY_SOURCE" in
  existing)  say "    API key:         unchanged" ;;
  pasted)    say "    API key:         the one you pasted" ;;
  generated) say "    API key:         newly generated (shown at the end)" ;;
esac
say "    Listen on:       http://$C_HOST:$C_PORT"
say "    Limits:          storage $([ "$C_MAX_TOTAL" -eq 0 ] && echo unlimited || human "$C_MAX_TOTAL"), per file $(human "$C_MAX_FILE")"
[ "$OPEN_FW" -eq 1 ] && say "    Firewall:        allow port $C_PORT in ufw (if active)"
say "    Service:         $SERVICE_NAME — started now and at every boot"
echo
confirm "Go ahead?" Y || { say "Stopped. Nothing was changed apart from the Python dependencies."; exit 0; }

# =============================================================================
step 6 "Applying"
# =============================================================================
# Stop the running service first so its files aren't in use while they move.
if systemctl is-active --quiet "$SERVICE_NAME"; then
  say "==> Stopping the running $SERVICE_NAME"
  as_root systemctl stop "$SERVICE_NAME"
fi

case "$DATA_ACTION" in
  move)
    say "==> Moving stored files to $NEW_DATA"
    as_user mkdir -p "$(dirname "$NEW_DATA")"
    [ -d "$NEW_DATA" ] && as_user rmdir "$NEW_DATA"   # empty (checked above)
    as_user mv "$OLD_DATA" "$NEW_DATA"
    ;;
  copy)
    say "==> Copying stored files to $NEW_DATA (this can take a while)"
    as_user mkdir -p "$NEW_DATA"
    as_user cp -a "$OLD_DATA/." "$NEW_DATA/"
    ;;
esac

if [ "$WRITE_CONFIG" -eq 1 ]; then
  if [ -f "$CONFIG" ]; then
    as_user cp -p "$CONFIG" "$CONFIG.bak"
  fi
  say "==> Writing $CONFIG"
  # Values go through the environment, not argv, so the key never shows in `ps`.
  CFG_API_KEY="$C_API_KEY" CFG_HOST="$C_HOST" CFG_PORT="$C_PORT" \
  CFG_DATA_DIR="$C_DATA_DIR_RAW" CFG_LOG_DIR="$C_LOG_DIR_RAW" \
  CFG_MAX_FILE="$C_MAX_FILE" CFG_MAX_CONC="$C_MAX_CONC" \
  CFG_MAX_TOTAL="$C_MAX_TOTAL" CFG_TIMEOUT="$C_TIMEOUT" \
  "$PYTHON" - "$SCRIPT_DIR/server-config.example.toml" "$CONFIG" <<'PY'
import os, pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
out = pathlib.Path(sys.argv[2])
e = os.environ

def q(s):  # TOML basic string
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

settings = {
    "api_key": q(e["CFG_API_KEY"]),
    "data_dir": q(e["CFG_DATA_DIR"]),
    "host": q(e["CFG_HOST"]),
    "port": e["CFG_PORT"],
    "max_file_bytes": e["CFG_MAX_FILE"],
    "max_concurrent_uploads": e["CFG_MAX_CONC"],
    "max_total_bytes": e["CFG_MAX_TOTAL"],
    "upload_timeout": e["CFG_TIMEOUT"],
}
for key, value in settings.items():
    text, n = re.subn(rf"^{key} = .*$", lambda m: f"{key} = {value}", text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"server-config.example.toml has no '{key} = ' line")
if e.get("CFG_LOG_DIR"):
    text = re.sub(r"^# log_dir = .*$", lambda m: f"log_dir = {q(e['CFG_LOG_DIR'])}",
                  text, count=1, flags=re.M)

header = ("# Written by install-service.sh. Re-run it to change these settings, or edit\n"
          "# by hand and restart the service (sudo systemctl restart async-storage-bot).\n\n")
out.write_text(header + text, encoding="utf-8")
out.chmod(0o600)
PY
  if [ "$(id -u)" -eq 0 ] && [ "$TARGET_USER" != "root" ]; then
    chown "$TARGET_USER:" "$CONFIG"
  fi
fi

# --- Sanity-check the config before we wire up the service ------------------
# The path goes in argv, never into the code, so any character in it is safe.
CHECK_CONFIG="import sys, server.config as c; c.load_config(sys.argv[1])"
if ! as_user "$PYTHON" -c "$CHECK_CONFIG" "$CONFIG" >/dev/null 2>&1; then
  say "error: the config file did not validate:" >&2
  as_user "$PYTHON" -c "$CHECK_CONFIG" "$CONFIG" || true
  die "fix $CONFIG and re-run."
fi

# --- Is something else already on the port? ---------------------------------
if command -v ss >/dev/null 2>&1 && [ -n "$(ss -ltnH "sport = :$C_PORT" 2>/dev/null)" ]; then
  say "    ! Something else is already listening on port $C_PORT:"
  ss -ltnpH "sport = :$C_PORT" 2>/dev/null | sed 's/^/      /' || true
  confirm "Continue anyway? (the server will fail to start until it's freed)" N || die "stopped — free the port or pick another."
fi

# --- Firewall ---------------------------------------------------------------
if [ "$OPEN_FW" -eq 1 ]; then
  if as_root ufw status 2>/dev/null | grep -q "Status: active"; then
    if [ "$ON_TAILSCALE" -eq 1 ]; then
      say "==> Allowing port $C_PORT on the Tailscale interface in ufw"
      as_root ufw allow in on tailscale0 to any port "$C_PORT" proto tcp >/dev/null
    else
      say "==> Allowing port $C_PORT in ufw"
      as_root ufw allow "$C_PORT/tcp" >/dev/null
    fi
  else
    say "    ufw isn't active — no firewall change needed."
  fi
fi

# --- Generate and install the systemd unit ----------------------------------
say "==> Writing the systemd unit to $UNIT_DEST"
AFTER="network-online.target"
# Bound to the Tailscale IP: start after tailscaled so the address exists.
[ "$ON_TAILSCALE" -eq 1 ] && AFTER="$AFTER tailscaled.service"
TMP_UNIT="$(mktemp)"
# Paths are escaped for systemd, so a checkout path with spaces, quotes, % or $
# in it still gives a working unit.
UNIT_AFTER="$AFTER" UNIT_USER="$TARGET_USER" UNIT_DIR="$SCRIPT_DIR" \
UNIT_CONFIG="$CONFIG" UNIT_PYTHON="$PYTHON" \
  "$PYTHON" - "$TMP_UNIT" <<'PY'
import os, pathlib, sys
e = os.environ

def spec(s):  # "%" starts a systemd specifier
    return s.replace("%", "%%")

def word(s, expand=True):  # one double-quoted word of an ExecStart=/Environment= line
    s = spec(s.replace("\\", "\\\\").replace('"', '\\"'))
    if expand:  # ExecStart= expands $VAR
        s = s.replace("$", "$$")
    return '"' + s + '"'

unit = f"""# Generated by install-service.sh
[Unit]
Description=Async Storage server (async-storage-bot)
After={e["UNIT_AFTER"]}
Wants={e["UNIT_AFTER"]}

[Service]
Type=simple
User={e["UNIT_USER"]}
WorkingDirectory={spec(e["UNIT_DIR"])}
Environment={word("STORAGE_SERVER_CONFIG=" + e["UNIT_CONFIG"], expand=False)}
ExecStart={word(e["UNIT_PYTHON"])} -m server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
pathlib.Path(sys.argv[1]).write_text(unit, encoding="utf-8")
PY

as_root cp "$TMP_UNIT" "$UNIT_DEST"
as_root chown root:root "$UNIT_DEST"
as_root chmod 644 "$UNIT_DEST"
rm -f "$TMP_UNIT"

say "==> Enabling and starting the service"
as_root systemctl daemon-reload
as_root systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
as_root systemctl restart "$SERVICE_NAME"

# --- Health check -----------------------------------------------------------
PROBE_HOST="$C_HOST"
[ "$PROBE_HOST" = "0.0.0.0" ] && PROBE_HOST="127.0.0.1"
HEALTH_URL="http://$PROBE_HOST:$C_PORT/health"
healthy=0
for _ in $(seq 1 15); do
  if "$PYTHON" -c "import urllib.request, sys; urllib.request.urlopen(sys.argv[1], timeout=2)" "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1; break
  fi
  sleep 1
done

echo
if [ "$healthy" -eq 1 ]; then
  say "✓ The server is running and answering on $HEALTH_URL"
  say "✓ $SERVICE_NAME starts automatically at boot."
elif systemctl is-active --quiet "$SERVICE_NAME"; then
  say "! The service is running but didn't answer $HEALTH_URL yet. Recent logs:"
  as_root journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
else
  say "✗ $SERVICE_NAME failed to start. Recent logs:" >&2
  as_root journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
  exit 1
fi

# --- What the clients need --------------------------------------------------
echo
say "Connect your clients (desktop app ⚙ Settings, or the Android app) with:"
if [ "$ON_TAILSCALE" -eq 1 ]; then
  say "    Server URL:  http://$(hostname -s):$C_PORT   (MagicDNS name)"
  say "            or   http://$C_HOST:$C_PORT"
else
  say "    Server URL:  http://$C_HOST:$C_PORT"
fi
if [ "$KEY_SOURCE" = "generated" ]; then
  say "    API key:     $C_API_KEY"
  say "    (It's saved in $CONFIG.)"
elif [ "$KEY_SOURCE" = "existing" ]; then
  say "    API key:     unchanged — clients that worked before still work."
  if confirm "Show the API key?" N; then say "    API key:     $C_API_KEY"; fi
else
  say "    API key:     the one you pasted."
fi

if [ -n "$OLD_CFG" ]; then
  echo
  case "$DATA_ACTION" in
    keep) say "Note: your stored files still live in $OLD_DATA — don't delete that folder." ;;
    *)    say "The service no longer uses $OLD_WD. Once you've checked everything works,"
          say "you can delete that folder." ;;
  esac
fi

echo
say "Manage it with:"
say "  systemctl status ${SERVICE_NAME}"
say "  journalctl -u ${SERVICE_NAME} -f"
say "  sudo systemctl restart ${SERVICE_NAME}   # after editing ${CONFIG}"
say "  ./install-service.sh                     # re-run this setup to change settings"
