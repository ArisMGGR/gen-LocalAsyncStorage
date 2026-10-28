"""SQLite-backed manifest store.

The manifest records the identity of every file the server has ever seen. A
deleted file is kept as a *tombstone* (``deleted = 1``) rather than removed, so
clients can distinguish "this file was deleted" from "I have never heard of this
file" and avoid re-uploading something the user intentionally removed.

Access is guarded by a lock because the FastAPI dev server may touch the DB from
multiple threads; SQLite itself serialises writes but the connection object is
not safe to share across threads without care.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from common.hashing import FileEntry


def fold(path: str) -> str:
    """A path's case-insensitive identity.

    Windows and macOS disks (and exFAT/NTFS drives anywhere) treat "a.txt" and
    "A.txt" as one file, so two names that fold the same must never both be
    given out: the second would silently overwrite the first's blob.
    """
    return path.casefold()


class ManifestDB:
    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        # check_same_thread=False lets us share one connection across the
        # server's worker threads; the lock below serialises our own access.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("fold", 1, fold, deterministic=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS manifest (
                    path    TEXT PRIMARY KEY,
                    sha256  TEXT NOT NULL,
                    size    INTEGER NOT NULL,
                    mtime   REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    moved_from TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # Manifests made before moved_from existed gain the column empty.
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(manifest)")}
            if "moved_from" not in columns:
                self._conn.execute(
                    "ALTER TABLE manifest ADD COLUMN moved_from TEXT NOT NULL DEFAULT ''"
                )
            # Explicitly-created folders. Folders otherwise exist only implicitly
            # (as the prefix of a file's path); this table lets an *empty* folder
            # persist and show up on every client until files are put into it.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY)"
            )
            self._conn.commit()

    def _row_to_entry(self, row: sqlite3.Row) -> FileEntry:
        return FileEntry(
            path=row["path"],
            sha256=row["sha256"],
            size=row["size"],
            mtime=row["mtime"],
            deleted=bool(row["deleted"]),
            moved_from=row["moved_from"],
        )

    def list_all(self) -> list[FileEntry]:
        """Return every manifest entry, including tombstones."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM manifest ORDER BY path"
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def count_live(self) -> int:
        """Number of live (non-tombstone) files."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM manifest WHERE deleted = 0"
            ).fetchone()
        return int(row["n"])

    def total_live_bytes(self) -> int:
        """Sum of sizes of all live (non-tombstone) files. Drives the quota."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS total FROM manifest WHERE deleted = 0"
            ).fetchone()
        return int(row["total"])

    def get(self, path: str) -> FileEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM manifest WHERE path = ?", (path,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def upsert(self, path: str, sha256: str, size: int, mtime: float) -> FileEntry:
        """Insert or update a live (non-deleted) file, bumping its version."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO manifest (path, sha256, size, mtime, version, deleted, moved_from)
                VALUES (?, ?, ?, ?, 1, 0, '')
                ON CONFLICT(path) DO UPDATE SET
                    sha256     = excluded.sha256,
                    size       = excluded.size,
                    mtime      = excluded.mtime,
                    version    = manifest.version + 1,
                    deleted    = 0,
                    moved_from = ''
                """,
                (path, sha256, size, mtime),
            )
            self._conn.commit()
        return self.get(path)  # type: ignore[return-value]

    def mark_deleted(self, path: str) -> FileEntry | None:
        """Turn an existing entry into a tombstone. No-op if unknown."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM manifest WHERE path = ?", (path,)
            ).fetchone()
            if existing is None:
                return None
            self._conn.execute(
                """
                UPDATE manifest
                SET deleted = 1, size = 0, mtime = ?, version = version + 1
                WHERE path = ?
                """,
                (time.time(), path),
            )
            self._conn.commit()
        return self.get(path)

    def move_file(self, src: str, dst: str) -> FileEntry | None:
        """Move a live file from ``src`` to ``dst`` in one transaction.

        The destination becomes a live entry carrying the source's content
        identity (sha/size/mtime) and ``moved_from = src``, and the source is
        left as a tombstone so other clients notice it moved away. Returns the
        new ``dst`` entry, or ``None`` if ``src`` isn't a live file.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM manifest WHERE path = ?", (src,)
            ).fetchone()
            if row is None or row["deleted"]:
                return None
            self._conn.execute(
                """
                INSERT INTO manifest (path, sha256, size, mtime, version, deleted, moved_from)
                VALUES (?, ?, ?, ?, 1, 0, ?)
                ON CONFLICT(path) DO UPDATE SET
                    sha256     = excluded.sha256,
                    size       = excluded.size,
                    mtime      = excluded.mtime,
                    version    = manifest.version + 1,
                    deleted    = 0,
                    moved_from = excluded.moved_from
                """,
                (dst, row["sha256"], row["size"], row["mtime"], src),
            )
            self._conn.execute(
                """
                UPDATE manifest
                SET deleted = 1, size = 0, mtime = ?, version = version + 1
                WHERE path = ?
                """,
                (now, src),
            )
            self._conn.commit()
        return self.get(dst)

    # -- folders -------------------------------------------------------------

    def is_folder(self, path: str) -> bool:
        """True if ``path`` names a folder: an explicit folder, or the prefix of
        a live file or a sub-folder."""
        prefix = path + "/"
        n = len(prefix)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM folders WHERE path = ? OR substr(path, 1, ?) = ?
                UNION ALL
                SELECT 1 FROM manifest WHERE deleted = 0 AND substr(path, 1, ?) = ?
                LIMIT 1
                """,
                (path, n, prefix, n, prefix),
            ).fetchone()
        return row is not None

    def live_file_like(self, path: str, allow: str | None = None) -> str | None:
        """A live file whose path equals ``path`` ignoring case, or None.

        ``allow`` is skipped (a move may change the case of its own source).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT path FROM manifest WHERE deleted = 0 AND fold(path) = ? "
                "AND path IS NOT ? LIMIT 1",
                (fold(path), allow),
            ).fetchone()
        return row["path"] if row else None

    def is_folder_like(self, path: str) -> bool:
        """``is_folder``, ignoring case."""
        prefix = fold(path) + "/"
        n = len(prefix)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM folders WHERE fold(path) = ? OR substr(fold(path), 1, ?) = ?
                UNION ALL
                SELECT 1 FROM manifest WHERE deleted = 0 AND substr(fold(path), 1, ?) = ?
                LIMIT 1
                """,
                (fold(path), n, prefix, n, prefix),
            ).fetchone()
        return row is not None

    def list_folders(self) -> list[str]:
        """Return every explicitly-created folder path, sorted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM folders ORDER BY path"
            ).fetchall()
        return [r["path"] for r in rows]

    def create_folder(self, path: str) -> None:
        """Record ``path`` and all its ancestors as folders (idempotent)."""
        segments = path.split("/")
        ancestors = ["/".join(segments[: i + 1]) for i in range(len(segments))]
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO folders (path) VALUES (?)",
                [(p,) for p in ancestors],
            )
            self._conn.commit()

    def delete_folder(self, path: str) -> None:
        """Remove a single folder entry. Emptiness is enforced by the caller."""
        with self._lock:
            self._conn.execute("DELETE FROM folders WHERE path = ?", (path,))
            self._conn.commit()
