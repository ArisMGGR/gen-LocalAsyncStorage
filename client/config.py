"""Client configuration, read from a TOML file.

The path to the config file is taken from the ``STORAGE_CLIENT_CONFIG``
environment variable, or defaults to ``./config.toml``. See
``config.example.toml`` for the expected shape.
"""

from __future__ import annotations

import contextlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ClientConfig:
    server_url: str
    api_key: str
    # Where downloads are saved. Empty until the user picks one in the app; the
    # desktop app won't download anything without it.
    download_dir: str = ""


def default_config_path() -> Path:
    """Where the client config is read from / written to."""
    return Path(os.environ.get("STORAGE_CLIENT_CONFIG", "./config.toml")).expanduser()


# Backwards-compatible alias.
_default_config_path = default_config_path


def normalize_server_url(text: str) -> str | None:
    """``text`` as an http(s) server URL, or None if it isn't one.

    "server-laptop:8000" becomes "http://server-laptop:8000" (the same rule as
    install-client.ps1), so a missing scheme isn't later reported as an
    unreachable server. A trailing slash is dropped.
    """
    url = str(text).strip()
    if not url:
        return None
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        url = "http://" + url
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return url.rstrip("/")


def save_config(
    server_url: str,
    api_key: str,
    download_dir: str = "",
    path: str | Path | None = None,
) -> Path:
    """Write the server URL, API key and download folder to the TOML config file.
    Returns its path.

    Used by the desktop app's setup/settings screens so the user never has to
    edit the file by hand.
    """
    cfg_path = (Path(path) if path else default_config_path()).expanduser()
    url = str(server_url).strip().rstrip("/")
    key = str(api_key).strip()
    folder = str(download_dir).strip()

    def esc(s: str) -> str:  # minimal TOML basic-string escaping
        return s.replace("\\", "\\\\").replace('"', '\\"')

    content = (
        "# Client configuration (managed by the desktop app).\n"
        "[server]\n"
        f'url = "{esc(url)}"\n'
        f'api_key = "{esc(key)}"\n'
        "\n[client]\n"
        f'download_dir = "{esc(folder)}"\n'
    )
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(content, encoding="utf-8")
    # It holds the API key. No-op on Windows, where the user's folder is private.
    with contextlib.suppress(OSError):
        os.chmod(cfg_path, 0o600)
    return cfg_path


def read_config_draft(path: str | Path | None = None) -> ClientConfig | None:
    """Whatever the config file holds, as written and unchecked, or None if it
    can't be parsed at all.

    Used when :func:`load_config` refuses the file (e.g. a URL it can't
    normalise), so the app can prefill its settings screen from it.
    """
    cfg_path = Path(path) if path else default_config_path()
    try:
        with open(cfg_path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    server = raw.get("server") or {}
    client = raw.get("client") or {}
    if not isinstance(server, dict) or not isinstance(client, dict):
        return None
    return ClientConfig(
        server_url=str(server.get("url", "")).strip(),
        api_key=str(server.get("api_key", "")).strip(),
        download_dir=str(client.get("download_dir", "")).strip(),
    )


def load_config(path: str | Path | None = None) -> ClientConfig:
    cfg_path = Path(path) if path else _default_config_path()
    if not cfg_path.is_file():
        raise RuntimeError(
            f"Config file not found: {cfg_path}. Copy config.example.toml and edit it, "
            f"or set STORAGE_CLIENT_CONFIG to its location."
        )

    try:
        with open(cfg_path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Could not read config file {cfg_path}: {exc}") from exc

    server = raw.get("server") or {}
    client = raw.get("client") or {}
    if not isinstance(server, dict) or not isinstance(client, dict):
        raise RuntimeError(f"[server] and [client] in {cfg_path} must be tables")
    raw_url = str(server.get("url", "")).strip()
    api_key = str(server.get("api_key", "")).strip()
    if not raw_url or not api_key:
        raise RuntimeError("config must set [server].url and [server].api_key")
    if not api_key.isascii():
        raise RuntimeError("[server].api_key has characters an HTTP header can't carry")
    server_url = normalize_server_url(raw_url)
    if server_url is None:
        raise RuntimeError(
            f"[server].url {raw_url!r} is not a server address, "
            "e.g. http://server-laptop:8000"
        )

    download_dir = str(client.get("download_dir", "")).strip()
    return ClientConfig(
        server_url=server_url, api_key=api_key, download_dir=download_dir
    )
