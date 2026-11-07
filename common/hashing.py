"""Hashing helpers and manifest data models shared by the server and client.

A *manifest* is the list of every file the server knows about. Each entry is a
:class:`FileEntry`. The server sends it and the client reads it to list the
server's files; both use this one model.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

# How many bytes to read at a time when hashing. 1 MiB keeps memory flat even
# for large files.
_CHUNK = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    """Return the hex sha256 digest of a file, read in streaming chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class FileEntry:
    """One row of a manifest: the identity of a single file.

    Attributes:
        path: Forward-slash relative path within the server's storage (the key).
        sha256: Hex digest of the file's contents.
        size: Size in bytes.
        mtime: Last-modified time as a float unix timestamp.
        deleted: True if this is a tombstone (the file was deleted).
        moved_from: The path this file was last moved from ("" if it was
            uploaded there), so Restore can put a trashed file back under its
            own name even when Trash had to number it.
    """

    path: str
    sha256: str
    size: int
    mtime: float
    deleted: bool = False
    moved_from: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FileEntry":
        return cls(
            path=d["path"],
            sha256=d["sha256"],
            size=int(d["size"]),
            mtime=float(d["mtime"]),
            deleted=bool(d.get("deleted", False)),
            moved_from=str(d.get("moved_from") or ""),
        )


def normalize_rel_path(path: str) -> str:
    """Normalise a relative path to the manifest's canonical form.

    Uses forward slashes so paths generated on Windows and POSIX clients match,
    and rejects anything that would escape the storage root (``..`` or absolute
    paths) to prevent path-traversal writes on the server.
    """
    p = str(path).replace("\\", "/").strip("/")
    # A null byte can't appear in a real filename and makes filesystem calls
    # raise deep in the stack; reject it up front so callers get a clean error.
    if "\x00" in p:
        raise ValueError(f"unsafe path: {path!r}")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise ValueError(f"unsafe path: {path!r}")
    if not parts:
        raise ValueError("empty path")
    return "/".join(parts)
