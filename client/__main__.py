"""Client command-line entry point.

The client is now a desktop app — launch it with::

    python -m client.gui

This CLI keeps a single utility command for checking connectivity::

    python -m client status    # print server health and exit

Configuration is read from the TOML file pointed to by STORAGE_CLIENT_CONFIG
(default ./config.toml). See config.example.toml.
"""

from __future__ import annotations

import logging
import sys

from client.api import StorageAPI, describe_error
from client.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("client")


def cmd_status(config) -> int:
    api = StorageAPI(config.server_url, config.api_key)
    try:
        try:
            info = api.health()
        except Exception as exc:
            # The same short reason the app shows, not a traceback.
            log.error("server %s: %s", config.server_url, describe_error(exc))
            return 1
        # /health answers anyone, but only reports the file count to a caller
        # with the right key — so a missing count means the key was rejected.
        if "files" not in info:
            log.error(
                "server %s is up, but it rejected the API key in config.toml",
                config.server_url,
            )
            return 1
        log.info("server %s OK — %s files", config.server_url, info["files"])
        return 0
    finally:
        api.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "status"

    if command != "status":
        log.error(
            "unknown command %r. Use 'status', or launch the app with "
            "'python -m client.gui'.",
            command,
        )
        return 2
    try:
        config = load_config()
    except Exception as exc:
        # A missing or broken config.toml gets one line, not a traceback.
        log.error("%s", exc)
        return 1
    return cmd_status(config)


if __name__ == "__main__":
    raise SystemExit(main())
