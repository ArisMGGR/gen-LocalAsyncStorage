"""Blob storage on the server's local disk.

Files are stored under ``<data_dir>/storage/`` mirroring their relative path, so
the storage folder is human-browsable and looks exactly like the server's tree.

All writes are *atomic*: bytes go to a temp file in the same directory and are
then ``os.replace``-d into place. A crash mid-write therefore never leaves a
half-written file that would fail its hash check.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import IO, Iterator

from common.hashing import normalize_rel_path


class BlobStore:
    def __init__(self, storage_dir: str | Path):
        self._root = Path(storage_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _abs(self, rel_path: str) -> Path:
        """Resolve a manifest path to an absolute path, refusing escapes."""
        rel = normalize_rel_path(rel_path)
        target = (self._root / rel).resolve()
        # Defence in depth: even after normalisation, confirm the resolved path
        # stays within the storage root.
        if self._root not in target.parents and target != self._root:
            raise ValueError(f"path escapes storage root: {rel_path!r}")
        return target

    @contextlib.contextmanager
    def staged_write(self, rel_path: str, mtime: float) -> Iterator[IO[bytes]]:
        """Yield a writable temp file that is atomically committed on clean exit.

        The caller writes the bytes (streaming them in chunks, verifying size and
        hash as it goes). Only if the ``with`` block exits *without* raising is
        the temp file ``os.replace``-d into place — so a size-cap breach or a
        hash mismatch raised inside the block aborts the upload and leaves no
        partial file behind. This lets uploads be streamed to disk instead of
        buffered whole in memory.
        """
        target = self._abs(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                yield fh
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            # Clean up the temp file on any failure so we don't leak .part files
            # and never commit a partial/invalid upload.
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        # Preserve the client's modification time so mtimes stay comparable.
        os.utime(target, (mtime, mtime))

    def write(self, rel_path: str, data: bytes, mtime: float) -> None:
        """Write an in-memory bytes object atomically (used for small blobs)."""
        with self.staged_write(rel_path, mtime) as fh:
            fh.write(data)

    def read(self, rel_path: str) -> bytes:
        return self._abs(rel_path).read_bytes()

    def path_for(self, rel_path: str) -> Path:
        """Absolute on-disk path for a manifest entry, for streamed reads."""
        return self._abs(rel_path)

    def exists(self, rel_path: str) -> bool:
        return self._abs(rel_path).is_file()

    def delete(self, rel_path: str) -> None:
        """Remove a file if present; prune now-empty parent directories."""
        target = self._abs(rel_path)
        if target.is_file():
            target.unlink()
        self._prune_empty_parents(target.parent)

    def move(self, src_rel: str, dst_rel: str) -> None:
        """Move a blob from ``src_rel`` to ``dst_rel`` atomically.

        The destination's parent directories are created, the file is
        ``os.replace``-d into place (atomic within the storage root), and any
        directories the source left empty are pruned.
        """
        src = self._abs(src_rel)
        dst = self._abs(dst_rel)
        if not src.is_file():
            raise FileNotFoundError(f"no such file: {src_rel!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        self._prune_empty_parents(src.parent)

    def _prune_empty_parents(self, start: Path) -> None:
        """Remove now-empty directories up to (but never including) the root."""
        parent = start
        while parent != self._root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
