"""Logging setup: mirror every log line to a per-day text file.

A new file named ``YYYY-MM-DD.txt`` is created for each day inside the log
directory, so each day's output is kept in its own file. Output still goes to
the console (stderr) as well, so ``journalctl`` keeps showing logs when the
server runs as a systemd service.

Call :func:`configure_logging` once, before the server starts, and let uvicorn's
and the app's loggers propagate to the root logger (the server passes
``log_config=None`` to uvicorn so it doesn't install its own handlers).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

# One shared format for the console and the file, so a line reads the same in
# both places: timestamp, level, logger name, then the message.
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Guard so configuring twice (e.g. once from __main__ and once when the app
# module is imported) doesn't stack duplicate handlers.
_configured = False


class DailyFileHandler(logging.Handler):
    """Write each record to ``<directory>/<YYYY-MM-DD>.txt`` for the current day.

    The target file tracks the date: when the day rolls over, the next record
    transparently switches to that day's file. Files are opened in append mode,
    so restarting the server keeps writing to the same day's log rather than
    truncating it.
    """

    def __init__(self, directory: Path, encoding: str = "utf-8") -> None:
        super().__init__()
        self.directory = Path(directory)
        self.encoding = encoding
        self.directory.mkdir(parents=True, exist_ok=True)
        self._current_day: str | None = None
        self._stream = None

    def _open_for_today(self) -> None:
        today = date.today().isoformat()
        if self._stream is not None and today == self._current_day:
            return
        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except OSError:
                pass
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.directory / f"{today}.txt", "a", encoding=self.encoding)
        self._current_day = today

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._open_for_today()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001 - logging must never crash the caller
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.flush()
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def configure_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Route root logging to the console and to a per-day file under ``log_dir``.

    Returns the directory logs are written to. Idempotent — calling it more than
    once won't add duplicate handlers. If the log directory can't be opened, the
    server still runs and logs to the console only.
    """
    global _configured
    log_dir = Path(log_dir)

    root = logging.getLogger()
    root.setLevel(level)
    if _configured:
        return log_dir

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        daily = DailyFileHandler(log_dir)
        daily.setFormatter(formatter)
        root.addHandler(daily)
    except OSError as exc:
        root.warning("Could not open log directory %s (%s); logging to console only.", log_dir, exc)

    _configured = True
    return log_dir
