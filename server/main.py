"""FastAPI application: the storage server.

Run it with::

    python -m server            # reads ./server-config.toml (see config.py)

Endpoints (all require the ``X-API-Key`` header except ``/health``):

    GET  /health                     liveness probe (no auth)
    GET  /manifest                   files (incl. tombstones) + explicit folders
    GET  /download?path=...          stream a file's bytes
    PUT  /upload?path=...            upload a new file (X-Content-SHA256, X-Mtime
                                     headers); 409 if the name is already taken
    POST /delete?path=...            mark a file deleted (tombstone)
    POST /move?src=...&dst=...       move/rename a file
    POST /folder?path=...            create an (empty) folder
    POST /folder/delete?path=...     delete an empty folder
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import logging
import math
import time
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response

from common.hashing import normalize_rel_path
from server.auth import key_matches, require_api_key
from server.config import load_config
from server.db import ManifestDB, fold
from server.logging_setup import configure_logging
from server.storage import BlobStore

config = load_config()
config.data_dir.mkdir(parents=True, exist_ok=True)

# Configure logging here too (idempotent) so file logging is in place even when
# the app is launched directly, e.g. `uvicorn server.main:app`.
configure_logging(config.log_dir)
log = logging.getLogger("server")

db = ManifestDB(config.db_path)
store = BlobStore(config.storage_dir)
log.info("Storage server ready: data=%s port=%d", config.data_dir, config.port)

# How far in the future a client-supplied mtime may be before we reject it. A
# little skew is normal; wildly future timestamps are only useful to win
# conflict resolution unfairly, so cap them.
_MTIME_FUTURE_SKEW = 86400  # 1 day

# Bound how many uploads write to disk at once so bounded-size requests can't be
# stacked to saturate IO. Extra uploads wait here rather than run in parallel.
_upload_semaphore = asyncio.Semaphore(config.max_concurrent_uploads)

# Paths with an upload in flight, case-folded (see db.fold). Uploads never
# replace an existing file, and this stops two uploads racing for the same free
# name from both landing (the second would silently overwrite the first), or a
# move or new folder taking a name an upload is about to commit to. Only
# touched on the event loop, which is why the endpoints that check it are
# ``async def``.
_uploads_in_flight: set[str] = set()

# Bytes held against the storage quota by uploads still in flight, so
# concurrent uploads can't each see the same free space and together overrun
# max_total_bytes. Only touched on the event loop.
_reserved_bytes = 0

# An upload with no Content-Length reserves quota in steps this big, so the
# budget isn't re-checked against the database on every chunk.
_QUOTA_STEP = 8 * 1024 * 1024

# "Content Too Large". A plain number: Starlette before 0.48 has no
# HTTP_413_CONTENT_TOO_LARGE, and newer versions deprecate the old name.
_HTTP_413 = 413

# What Windows can't hold as a name, or reads as another name: device names
# (which also block "nul.txt"), and characters it forbids. A name ending in a
# dot or space is checked separately — Windows drops those, so "a.txt." is
# stored as "a.txt" and would overwrite it.
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"{dev}{n}" for dev in ("COM", "LPT") for n in list("123456789") + ["¹", "²", "³"]]
)
_FORBIDDEN_CHARS = frozenset('<>:"|?*') | {chr(c) for c in range(1, 32)}

# Disable the interactive API docs and OpenAPI schema: this is a private,
# single-user service and there's no reason to advertise its surface.
app = FastAPI(
    title="Async Storage DOS",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _safe_path(path: str) -> str:
    try:
        return normalize_rel_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _new_name(path: str) -> str:
    """``_safe_path`` for a name about to be created (upload, move target, folder).

    Every segment must be a name a Windows disk (or an exFAT/NTFS drive
    anywhere) stores as given; otherwise two names could land on one file and
    the second would overwrite the first. Existing paths are only looked up
    through ``_safe_path``, so anything already stored can still be moved
    away or deleted.
    """
    rel = _safe_path(path)
    for seg in rel.split("/"):
        if seg[-1] in ". ":
            problem = "ends in a dot or a space"
        elif set(seg) & _FORBIDDEN_CHARS:
            problem = 'contains one of < > : " | ? * or a control character'
        elif seg.partition(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
            problem = "is a name Windows reserves for a device"
        else:
            continue
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{seg}' can't be used as a name: it {problem}",
        )
    return rel


@app.get("/health")
def health(x_api_key: str = Header(default="")) -> dict:
    # Liveness stays open so a plain probe can confirm the server is up, but the
    # file count is only disclosed to a caller presenting the API key — an
    # unauthenticated caller shouldn't learn how much is stored.
    body = {"status": "ok"}
    if key_matches(x_api_key):
        body["files"] = db.count_live()
    return body


@app.get("/manifest", dependencies=[Depends(require_api_key)])
def get_manifest() -> dict:
    # "folders" carries explicitly-created (possibly empty) folders. Older
    # clients read only "files" and ignore it, so this stays backward compatible.
    return {
        "files": [e.to_dict() for e in db.list_all()],
        "folders": db.list_folders(),
    }


@app.get("/download", dependencies=[Depends(require_api_key)])
def download(path: str = Query(...)) -> Response:
    rel = _safe_path(path)
    entry = db.get(rel)
    if entry is None or entry.deleted or not store.exists(rel):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    # Stream straight from disk instead of loading the whole file into memory,
    # so serving a large file never spikes server RAM.
    return FileResponse(
        path=store.path_for(rel),
        media_type="application/octet-stream",
        headers={"X-Content-SHA256": entry.sha256, "X-Mtime": str(entry.mtime)},
    )


@app.put("/upload", dependencies=[Depends(require_api_key)])
async def upload(
    request: Request,
    path: str = Query(...),
    x_content_sha256: str = Header(...),
    x_mtime: float = Header(...),
) -> dict:
    rel = _new_name(path)
    max_bytes = config.max_file_bytes

    # Uploads only ever create new files: a name that's already taken — by a
    # file, a folder, or an upload still in flight — is refused, never replaced.
    _refuse_taken_name(rel)
    _uploads_in_flight.add(fold(rel))
    try:
        return await _receive_upload(request, rel, max_bytes, x_content_sha256, x_mtime)
    finally:
        _uploads_in_flight.discard(fold(rel))


def _refuse_taken_name(rel: str, folder_ok: bool = False, allow: str | None = None) -> None:
    """Raise 409 if a file put at ``rel`` would replace or collide with anything.

    Names are compared ignoring case: on a case-insensitive disk "A.txt" is the
    same file as "a.txt", so taking it would overwrite that file's bytes.

    With ``folder_ok`` an existing folder at ``rel`` (spelt exactly so) is
    allowed, for creating a folder (idempotent) rather than a file. ``allow``
    is a file that may match, for a move that only changes its source's case.
    """
    if fold(rel) in _uploads_in_flight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{rel}' is already being uploaded",
        )
    existing = db.live_file_like(rel, allow=allow)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a file named '{existing}' already exists",
        )
    if db.is_folder_like(rel) and not (folder_ok and db.is_folder(rel)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a folder named '{rel}' already exists",
        )
    # Nothing can be placed beneath a file, or beneath a file being uploaded,
    # and a folder on the way may not be a case variant of an existing one
    # ("docs/x" beside "Docs" would be a second folder for the same disk dir).
    parts = rel.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        name = "/".join(parts[:i])
        if db.live_file_like(name) is not None or fold(name) in _uploads_in_flight:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"'{name}' is a file, not a folder",
            )
        if db.is_folder_like(name) and not db.is_folder(name):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a folder named like '{name}' already exists with different case",
            )


def _reserve_quota(held: int, need: int, ahead: int = 0) -> int:
    """Grow an upload's quota reservation from ``held`` to at least ``need`` bytes.

    Also takes up to ``ahead`` more bytes while they're free, so a stream of
    unknown length doesn't come back here on every chunk. Returns the new
    reservation, or raises 413 if the committed files plus every upload's
    reservation would go over ``max_total_bytes``.
    """
    global _reserved_bytes
    available = config.max_total_bytes - db.total_live_bytes() - _reserved_bytes
    extra = need - held
    if extra > available:
        raise HTTPException(
            status_code=_HTTP_413,
            detail="storage quota exceeded",
        )
    grant = min(extra + ahead, available)
    _reserved_bytes += grant
    return held + grant


async def _receive_upload(
    request: Request, rel: str, max_bytes: int, x_content_sha256: str, x_mtime: float
) -> dict:
    # Reject a nonsensical mtime: NaN or infinite, negative, or implausibly far
    # in the future. mtime drives conflict resolution, so an arbitrary future
    # value is a way to unfairly force a version to win.
    if (
        not math.isfinite(x_mtime)
        or x_mtime < 0
        or x_mtime > time.time() + _MTIME_FUTURE_SKEW
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Mtime is out of the acceptable range",
        )

    # Parse Content-Length once (may be absent or malformed).
    declared_len: int | None = None
    raw_len = request.headers.get("content-length")
    if raw_len is not None:
        try:
            declared_len = int(raw_len)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid Content-Length header",
            )

    # Reject an oversized single file up front when the client is honest about
    # the length. The streaming check below still guards a missing or understated
    # Content-Length.
    if declared_len is not None and declared_len > max_bytes:
        raise HTTPException(
            status_code=_HTTP_413,
            detail=f"file exceeds max of {max_bytes} bytes",
        )

    # Stream the body to a temp file, hashing and counting bytes as we go. The
    # file is only committed to its final location if it stays under every limit
    # and its hash matches; any breach raises inside the block and discards the
    # temp. The semaphore bounds how many uploads write to disk at once.
    hasher = hashlib.sha256()
    size = 0
    # Quota held by this upload. Space is reserved before it's written, so
    # concurrent uploads can't count the same free space, and released only
    # once the file is in the manifest (and counted there) or abandoned.
    quota = config.max_total_bytes > 0
    reserved = 0
    try:
        async with _upload_semaphore:
            if quota and declared_len is not None:
                reserved = _reserve_quota(reserved, declared_len)

            stream = request.stream()
            with _disk_errors_as_http("cannot store that path"), store.staged_write(
                rel, x_mtime
            ) as fh:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            stream.__anext__(), timeout=config.upload_timeout
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise HTTPException(
                            status_code=status.HTTP_408_REQUEST_TIMEOUT,
                            detail="upload stalled; timed out waiting for data",
                        )
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=_HTTP_413,
                            detail=f"file exceeds max of {max_bytes} bytes",
                        )
                    if quota and size > reserved:
                        reserved = _reserve_quota(reserved, size, ahead=_QUOTA_STEP)
                    hasher.update(chunk)
                    fh.write(chunk)

                actual = hasher.hexdigest()
                if actual != x_content_sha256:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"sha256 mismatch: sent {x_content_sha256}, computed {actual}",
                    )

        entry = db.upsert(rel, sha256=actual, size=size, mtime=x_mtime)
    finally:
        _release_quota(reserved)
    return {"entry": entry.to_dict()}


def _release_quota(held: int) -> None:
    global _reserved_bytes
    _reserved_bytes -= held


@contextlib.contextmanager
def _disk_errors_as_http(what: str) -> Iterator[None]:
    """Report an OS error from the disk (a name too long for it, a full disk)
    as a clean HTTP error rather than a bare 500; used by uploads and moves.
    Only the OS's reason is sent, never the server's own paths."""
    try:
        yield
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise HTTPException(status_code=507, detail="the server's disk is full")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{what}: {exc.strerror or type(exc).__name__}",
        )


@app.post("/delete", dependencies=[Depends(require_api_key)])
def delete(path: str = Query(...)) -> dict:
    rel = _safe_path(path)
    # Only a live entry's own blob is removed: on a case-insensitive disk a
    # path the manifest doesn't hold can still name another file's blob.
    existing = db.get(rel)
    if existing is not None and not existing.deleted:
        store.delete(rel)
    entry = db.mark_deleted(rel)
    if entry is None:
        # Nothing to delete; report it as an idempotent success tombstone.
        return {"entry": None, "note": "path not in manifest"}
    return {"entry": entry.to_dict()}


@app.post("/move", dependencies=[Depends(require_api_key)])
async def move(src: str = Query(...), dst: str = Query(...)) -> dict:
    # async so it runs on the event loop, in step with uploads: the destination
    # check and the move can't interleave with an upload claiming that name.
    s = _safe_path(src)
    d = _new_name(dst)
    if s == d:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source and destination are the same",
        )
    entry = db.get(s)
    if entry is None or entry.deleted or not store.exists(s):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    # Never replace a file, a folder, or a file still being uploaded. The source
    # itself may match, so a move can change only the case of a name.
    _refuse_taken_name(d, allow=s)
    # e.g. a name too long for the disk: reported cleanly rather than as a 500,
    # and without the server's own paths.
    with _disk_errors_as_http("cannot move to that path"):
        store.move(s, d)
    new_entry = db.move_file(s, d)
    return {"entry": new_entry.to_dict() if new_entry else None}


@app.post("/folder", dependencies=[Depends(require_api_key)])
async def create_folder(path: str = Query(...)) -> dict:
    # async for the same reason as move: it checks names uploads are claiming.
    rel = _new_name(path)
    # A folder can't share a name with a file, or sit beneath one.
    _refuse_taken_name(rel, folder_ok=True)
    db.create_folder(rel)
    return {"folders": db.list_folders()}


@app.post("/folder/delete", dependencies=[Depends(require_api_key)])
def delete_folder(path: str = Query(...)) -> dict:
    rel = _safe_path(path)
    prefix = rel + "/"
    has_files = any(not e.deleted and e.path.startswith(prefix) for e in db.list_all())
    has_subfolders = any(f.startswith(prefix) for f in db.list_folders())
    if has_files or has_subfolders:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="folder is not empty",
        )
    db.delete_folder(rel)
    return {"folders": db.list_folders()}
