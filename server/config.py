"""Server configuration, read from a TOML config file.

The server is configured through a config file so it is easy to run as a
background service (systemd, launchd, a Docker container, ...): the API key and
every other setting live in one file rather than being passed on the command
line or baked into the service definition.

The file is located, in order, from:

    1. the ``--config PATH`` argument to ``python -m server`` (which sets
       ``STORAGE_SERVER_CONFIG`` before the app is imported), or
    2. the ``STORAGE_SERVER_CONFIG`` environment variable, or
    3. ``./server-config.toml`` next to where the server is started.

See ``server-config.example.toml`` for the expected shape. Any individual
setting may still be overridden by its environment variable (below), which is
handy for one-off runs and for keeping the API key out of the file in a
container; when both are present the environment variable wins.

Settings and their environment-variable overrides:
    [server].api_key   / STORAGE_API_KEY
        Required. The shared secret clients present in the X-API-Key header.
    [server].data_dir  / STORAGE_DATA_DIR
        Files and manifest DB location. Default: ./data
    [server].host      / STORAGE_HOST
        Bind address. Default: 127.0.0.1 (loopback). Set it to a Tailscale/LAN
        IP to expose the server there; avoid 0.0.0.0 unless every interface is
        trusted.
    [server].port      / STORAGE_PORT
        Bind port. Default: 8000.
    [server].log_dir   / STORAGE_LOG_DIR
        Directory for per-day log files (YYYY-MM-DD.log). Default: ./logs
    [limits].max_file_bytes           / STORAGE_MAX_FILE_BYTES
        Largest upload accepted, in bytes. Default: 10 GiB. Guards against
        unbounded uploads.
    [limits].max_concurrent_uploads   / STORAGE_MAX_CONCURRENT_UPLOADS
        Uploads processed at once. Default: 4. Bounds how much disk/IO
        concurrent uploads can consume.
    [limits].max_total_bytes          / STORAGE_MAX_TOTAL_BYTES
        Total live storage allowed across all files, in bytes. Default: 0
        (unlimited). Set it to your disk budget to stop uploads filling the disk.
    [limits].upload_timeout           / STORAGE_UPLOAD_TIMEOUT
        Seconds to wait for the next chunk of an upload body before aborting it.
        Default: 60. Drops stalled/slow-loris uploads holding a slot.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Largest upload the server will accept by default. Uploads are streamed to disk
# and rejected the moment they exceed this, so the cap bounds both memory and
# disk growth rather than letting a single request exhaust the machine.
_DEFAULT_MAX_FILE_BYTES = 10 * 1024**3  # 10 GiB

# How many uploads may be written concurrently by default.
_DEFAULT_MAX_CONCURRENT_UPLOADS = 4

# Total live storage allowed by default. 0 means unlimited: a personal server's
# owner sizes this to their disk, so we don't guess a cap that would silently
# break their uploads.
_DEFAULT_MAX_TOTAL_BYTES = 0

# Seconds to wait for the next body chunk before aborting a stalled upload.
_DEFAULT_UPLOAD_TIMEOUT = 60.0

# Reject an API key shorter than this. A short key is brute-forceable; the
# generator in the docs produces ~43 chars, so this only catches weak keys set
# by hand. Guards against footguns, not a substitute for a random key.
_MIN_API_KEY_LEN = 16

# The placeholder in server-config.example.toml. It's long enough to pass the
# length check, but it is public, so a copied example must not start a server.
_EXAMPLE_API_KEY = "PASTE_A_LONG_RANDOM_KEY_HERE"

# Where per-day log files go when log_dir isn't set.
DEFAULT_LOG_DIR = "./logs"

# Where the config file is looked for when no explicit path is given.
_DEFAULT_CONFIG_FILENAME = "server-config.toml"


@dataclass(frozen=True)
class ServerConfig:
    api_key: str
    data_dir: Path
    host: str
    port: int
    log_dir: Path
    max_file_bytes: int
    max_concurrent_uploads: int
    max_total_bytes: int
    upload_timeout: float

    @property
    def storage_dir(self) -> Path:
        """Directory holding the actual file blobs."""
        return self.data_dir / "storage"

    @property
    def db_path(self) -> Path:
        """SQLite file holding the manifest."""
        return self.data_dir / "manifest.db"


def default_config_path() -> Path:
    """The config file the server will read when no path is passed explicitly."""
    return Path(
        os.environ.get("STORAGE_SERVER_CONFIG", f"./{_DEFAULT_CONFIG_FILENAME}")
    ).expanduser()


def _load_file(path: Path) -> dict[str, Any]:
    """Parse the TOML config file, or return an empty mapping if it's absent.

    A missing file is not an error on its own: the server can still be driven
    entirely by environment variables (useful in containers). Loading only
    fails loudly when the file exists but is malformed, so a typo is never
    silently ignored.
    """
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Could not read config file {path}: {exc}") from exc


def _resolve(env_name: str, table: dict[str, Any], key: str, default: Any) -> Any:
    """Pick a setting's value: environment override, then file, then default.

    An environment variable wins when it is set to a non-empty value, so a
    deployment can keep secrets out of the file. Otherwise the file's value is
    used, falling back to the built-in default. A blank value counts as unset
    in both places — ``host = ""`` must mean the 127.0.0.1 default, not "every
    interface", which is what an empty bind address means to the server.
    """
    env_val = os.environ.get(env_name)
    if env_val is not None and env_val.strip() != "":
        return env_val
    val = table.get(key)
    if val is not None and not (isinstance(val, str) and val.strip() == ""):
        return val
    return default


def load_config(path: str | Path | None = None) -> ServerConfig:
    cfg_path = Path(path).expanduser() if path else default_config_path()
    raw = _load_file(cfg_path)
    server = raw.get("server", {}) or {}
    limits = raw.get("limits", {}) or {}
    if not isinstance(server, dict) or not isinstance(limits, dict):
        raise RuntimeError(f"[server] and [limits] in {cfg_path} must be tables")

    api_key = str(_resolve("STORAGE_API_KEY", server, "api_key", "")).strip()
    if not api_key:
        raise RuntimeError(
            "No API key configured. Set [server].api_key in the config file "
            f"({cfg_path}) — copy {_DEFAULT_CONFIG_FILENAME.replace('.toml', '.example.toml')} "
            "to get started — or set the STORAGE_API_KEY environment variable. "
            "Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if api_key == _EXAMPLE_API_KEY:
        raise RuntimeError(
            f"[server].api_key in {cfg_path} is still the example's placeholder. "
            "Generate a key with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if len(api_key) < _MIN_API_KEY_LEN:
        raise RuntimeError(
            f"API key is too short ({len(api_key)} chars); use at least "
            f"{_MIN_API_KEY_LEN}. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    data_dir = (
        Path(str(_resolve("STORAGE_DATA_DIR", server, "data_dir", "./data")))
        .expanduser()
        .resolve()
    )
    host = str(_resolve("STORAGE_HOST", server, "host", "127.0.0.1"))

    # Per-day log files go to a "logs" folder next to where the server is
    # started (the checkout, under the systemd unit) unless overridden.
    log_dir = (
        Path(str(_resolve("STORAGE_LOG_DIR", server, "log_dir", DEFAULT_LOG_DIR)))
        .expanduser()
        .resolve()
    )

    try:
        port = int(_resolve("STORAGE_PORT", server, "port", 8000))
    except (TypeError, ValueError):
        raise RuntimeError("port must be an integer")
    if not 1 <= port <= 65535:
        raise RuntimeError("port must be from 1 to 65535")

    try:
        max_file_bytes = int(
            _resolve(
                "STORAGE_MAX_FILE_BYTES", limits, "max_file_bytes", _DEFAULT_MAX_FILE_BYTES
            )
        )
    except (TypeError, ValueError):
        raise RuntimeError("max_file_bytes must be an integer")
    if max_file_bytes <= 0:
        raise RuntimeError("max_file_bytes must be a positive integer")

    try:
        max_concurrent_uploads = int(
            _resolve(
                "STORAGE_MAX_CONCURRENT_UPLOADS",
                limits,
                "max_concurrent_uploads",
                _DEFAULT_MAX_CONCURRENT_UPLOADS,
            )
        )
    except (TypeError, ValueError):
        raise RuntimeError("max_concurrent_uploads must be an integer")
    if max_concurrent_uploads <= 0:
        raise RuntimeError("max_concurrent_uploads must be a positive integer")

    try:
        max_total_bytes = int(
            _resolve(
                "STORAGE_MAX_TOTAL_BYTES", limits, "max_total_bytes", _DEFAULT_MAX_TOTAL_BYTES
            )
        )
    except (TypeError, ValueError):
        raise RuntimeError("max_total_bytes must be an integer")
    if max_total_bytes < 0:
        raise RuntimeError("max_total_bytes must be zero (unlimited) or positive")

    try:
        upload_timeout = float(
            _resolve(
                "STORAGE_UPLOAD_TIMEOUT", limits, "upload_timeout", _DEFAULT_UPLOAD_TIMEOUT
            )
        )
    except (TypeError, ValueError):
        raise RuntimeError("upload_timeout must be a number")
    if not math.isfinite(upload_timeout) or upload_timeout <= 0:
        raise RuntimeError("upload_timeout must be a positive number of seconds")

    return ServerConfig(
        api_key=api_key,
        data_dir=data_dir,
        host=host,
        port=port,
        log_dir=log_dir,
        max_file_bytes=max_file_bytes,
        max_concurrent_uploads=max_concurrent_uploads,
        max_total_bytes=max_total_bytes,
        upload_timeout=upload_timeout,
    )
