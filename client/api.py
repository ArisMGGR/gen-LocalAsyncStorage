"""Thin HTTP client for talking to the storage server.

Wraps :mod:`httpx` and automatically attaches the API key header to every
request. All methods raise on non-2xx responses so the app can surface errors
clearly; :func:`describe_error` turns one into a short message for the user.

:meth:`upload_file` / :meth:`download_to_file` stream to and from disk in
chunks, report progress, and don't impose a read/write timeout, so multi-GB
transfers neither exhaust memory nor time out mid-flight.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable

import httpx

from common.hashing import FileEntry, sha256_file

# Chunk size for streamed transfers. 1 MiB keeps memory flat on huge files.
_CHUNK = 1024 * 1024

# Progress callback: (bytes_transferred_so_far, total_bytes). total may be 0 when
# unknown.
ProgressCB = Callable[[int, int], None]

# Cancel check: called between chunks of a streamed transfer; returning True
# aborts it. Lets the caller stop an in-flight file instead of waiting it out.
CancelCB = Callable[[], bool]


class Cancelled(Exception):
    """Raised when a streamed transfer is stopped by its ``cancel`` callback."""

    def __init__(self, message: str = "cancelled"):
        super().__init__(message)


def describe_error(exc: BaseException) -> str:
    """A short, user-facing reason a request to the server failed.

    Tells a rejected API key apart from a server that can't be reached, so a
    wrong key isn't reported as a connection problem.
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return "The server rejected the API key — check it in Settings"
    # A malformed URL (e.g. no http://) is a settings mistake, not a network
    # one; UnsupportedProtocol is a TransportError, so it's checked first.
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return "The server URL isn't valid — check it in Settings (e.g. http://server-laptop:8000)"
    if isinstance(exc, httpx.TransportError):
        return "Could not reach the server"
    return str(exc)


class StorageAPI:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0):
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )
        # For big streamed transfers, keep the connect/pool timeout but let data
        # flow without a read/write deadline (a slow-but-steady multi-GB upload
        # must not be killed). The server has its own idle timeout to drop truly
        # stalled connections.
        self._transfer_timeout = httpx.Timeout(timeout, read=None, write=None)

    @staticmethod
    def _check(r: httpx.Response) -> None:
        """Raise a clean error carrying the server's ``detail`` message, if any."""
        if r.is_error:
            detail = None
            try:
                detail = r.json().get("detail")
            except Exception:
                pass
            raise RuntimeError(detail or f"{r.status_code} {r.reason_phrase}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "StorageAPI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def health(self) -> dict:
        r = self._client.get("/health")
        r.raise_for_status()
        return r.json()

    def fetch_manifest(self) -> tuple[list[FileEntry], list[str]]:
        """Return (files, folders) in one request.

        ``folders`` are the explicitly-created folders the server tracks so that
        empty folders still show up; older servers omit the key and yield [].
        """
        r = self._client.get("/manifest")
        r.raise_for_status()
        data = r.json()
        files = [FileEntry.from_dict(d) for d in data.get("files", [])]
        folders = [str(p) for p in data.get("folders", [])]
        return files, folders

    def upload_file(
        self,
        path: str,
        local_path: Path,
        mtime: float | None = None,
        progress: ProgressCB | None = None,
        cancel: CancelCB | None = None,
    ) -> FileEntry:
        """Stream a local file to the server, reporting progress as it sends.

        The file is hashed and sent in chunks straight from disk, so memory stays
        flat regardless of size. ``progress(sent, total)`` is called as bytes go
        out, and ``cancel()`` is polled between chunks: when it returns True the
        upload is abandoned mid-flight with :class:`Cancelled`.
        """
        total = local_path.stat().st_size
        if mtime is None:
            mtime = local_path.stat().st_mtime
        sha = sha256_file(local_path)

        def body():
            sent = 0
            with open(local_path, "rb") as fh:
                while True:
                    if cancel is not None and cancel():
                        raise Cancelled()
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if progress is not None:
                        progress(sent, total)
                    yield chunk

        r = self._client.put(
            "/upload",
            params={"path": path},
            content=body(),
            headers={
                "X-Content-SHA256": sha,
                "X-Mtime": str(mtime),
                # Advertise the length so the server can reject an oversized file
                # before reading the body (httpx would otherwise chunk-encode a
                # generator body with no Content-Length).
                "Content-Length": str(total),
            },
            timeout=self._transfer_timeout,
        )
        # Surface the server's reason, e.g. "a file named 'x' already exists".
        self._check(r)
        return FileEntry.from_dict(r.json()["entry"])

    def download_to_file(
        self,
        path: str,
        dest_path: Path,
        progress: ProgressCB | None = None,
        cancel: CancelCB | None = None,
        place: Callable[[Path], Path] | None = None,
    ) -> Path:
        """Stream a remote file to ``dest_path`` atomically, verifying its hash.

        Writes to a uniquely named ``.part`` temp file beside it (never one that
        already exists, so a file of the user's is never overwritten), checks
        the sha256, then renames into place. ``progress(received, total)`` is called as bytes arrive, and
        ``cancel()`` is polled between chunks: when it returns True the download
        stops with :class:`Cancelled` and the partial file is removed, so a
        stopped transfer never leaves a half-written copy behind.

        ``place(dest_path)``, if given, picks the final path at the moment the
        file is put in place (in the same folder), so a name taken while the
        bytes streamed can still be avoided. Returns the path written.
        """
        with self._client.stream(
            "GET",
            "/download",
            params={"path": path},
            timeout=self._transfer_timeout,
        ) as r:
            r.raise_for_status()
            sha = r.headers.get("X-Content-SHA256", "")
            mtime = float(r.headers.get("X-Mtime", "0"))
            total = int(r.headers.get("Content-Length", "0") or 0)

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=dest_path.parent, prefix=f".{dest_path.name}.", suffix=".part"
            )
            hasher = hashlib.sha256()
            received = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in r.iter_bytes(_CHUNK):
                        if cancel is not None and cancel():
                            raise Cancelled()
                        fh.write(chunk)
                        hasher.update(chunk)
                        received += len(chunk)
                        if progress is not None:
                            progress(received, total)
                if sha and hasher.hexdigest() != sha:
                    raise ValueError("content hash mismatch")
                if place is not None:
                    dest_path = place(dest_path)
                os.replace(tmp, dest_path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        if mtime:
            os.utime(dest_path, (mtime, mtime))
        return dest_path

    def delete(self, path: str) -> None:
        r = self._client.post("/delete", params={"path": path})
        self._check(r)

    def move(self, src: str, dst: str) -> FileEntry | None:
        """Move/rename a file on the server from ``src`` to ``dst``."""
        r = self._client.post("/move", params={"src": src, "dst": dst})
        self._check(r)
        entry = r.json().get("entry")
        return FileEntry.from_dict(entry) if entry else None

    def create_folder(self, path: str) -> list[str]:
        """Create an (empty) folder on the server. Returns the folder list."""
        r = self._client.post("/folder", params={"path": path})
        self._check(r)
        return [str(p) for p in r.json().get("folders", [])]

    def delete_folder(self, path: str) -> list[str]:
        """Delete an empty folder on the server. Returns the folder list."""
        r = self._client.post("/folder/delete", params={"path": path})
        self._check(r)
        return [str(p) for p in r.json().get("folders", [])]
