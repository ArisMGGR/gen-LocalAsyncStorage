"""Entry point so the server runs with ``python -m server``.

Configuration is read from a TOML file (see ``server-config.example.toml``).
Point the server at it with ``--config PATH`` or the ``STORAGE_SERVER_CONFIG``
environment variable; with neither, it looks for ``./server-config.toml``.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from server.config import DEFAULT_LOG_DIR, default_config_path, load_config
from server.logging_setup import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m server", description=__doc__)
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help="Path to the TOML config file (default: ./server-config.toml or "
        "$STORAGE_SERVER_CONFIG).",
    )
    args = parser.parse_args()

    # Pin the config path in the environment so the app module, which uvicorn
    # imports in this same process by import string below, resolves the exact
    # same file we do here.
    if args.config:
        os.environ["STORAGE_SERVER_CONFIG"] = args.config

    log = logging.getLogger("server")
    try:
        config = load_config()
    except RuntimeError as exc:
        # A bad config means no log_dir to read, so record why the server
        # didn't start in the default logs folder, then exit.
        configure_logging(Path(os.environ.get("STORAGE_LOG_DIR") or DEFAULT_LOG_DIR))
        log.error("Server not started: %s", exc)
        raise SystemExit(1)

    # Set up logging before anything else logs, so the console and the per-day
    # log file capture the whole run, including the startup lines below.
    configure_logging(config.log_dir)
    log.info("Loading config from %s", default_config_path())
    log.info("Writing per-day logs to %s", config.log_dir)

    # Pass the app by import string so uvicorn owns the lifecycle. The app
    # module reads the same config file on import.
    # server_header=False stops uvicorn from advertising itself and its version
    # in the Server response header.
    # log_config=None keeps uvicorn from installing its own handlers, so its
    # logs propagate to the root logger we just configured (console + file).
    uvicorn.run(
        "server.main:app",
        host=config.host,
        port=config.port,
        log_level="info",
        server_header=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
