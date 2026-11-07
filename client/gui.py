"""Desktop app: browse the server and download files on demand.

This is the client. There is no sync of any kind — nothing is downloaded until
you ask for it, and once a file is downloaded it's yours: the app never looks at
it again. The window is a **folder browser**: it shows the contents of the
folder you're in, with sub-folders you can open and a ``↓ Download`` action on
every file.

Downloads go into the download folder picked in Settings. A file lands straight
in it; a folder lands in it as a sub-folder with its own structure. The only time
the app looks at the local disk is at that moment, to catch a name that's
already taken: a folder that already exists is refused, and for files you choose
Overwrite, Keep both (``name (1).ext``) or Skip.

Organise your files with **New folder** and **Move…**; uploads (drag-and-drop or
"Add files…") land in the folder you're currently viewing, and never replace
anything — a file or folder whose name is already taken there is refused. The
list is polled live, so changes on the server are reflected automatically.

Deleting is reversible: files (and whole folders, contents included) are moved
into a top-level ``Trash`` folder. Open it to **Restore** items to where they
came from, **Delete forever**, or **Empty Trash** — the only steps that actually
destroy anything.

Long jobs run in the background, never freezing the window: downloads and
uploads share one lane, deletes and moves another, and anything else waits in a
queue. The **Tasks** strip shows what's running with a Stop button, and the
**Queue** strip lists what's waiting so it can be dropped before it starts.
Stopping the delete of a single folder puts back whatever it already moved to
Trash; a delete of several items keeps what it had already moved.

    python -m client.gui

Reads the same ``config.toml`` as the CLI (see config.example.toml) for the
server URL and API key. Drag-and-drop uploads use the optional ``tkinterdnd2``
package; without it the "Add files…" button still uploads.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, simpledialog, ttk

from client.api import Cancelled, ProgressCB, StorageAPI, describe_error
from client.config import (
    ClientConfig,
    default_config_path,
    load_config,
    normalize_server_url,
    read_config_draft,
    save_config,
)
from client.ops import Lane
from common.hashing import FileEntry, normalize_rel_path

try:  # optional: real OS drag-and-drop of files onto the window
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except Exception:  # not installed → the "Add files…" button still works
    DND_FILES = None
    TkinterDnD = None
    _HAS_DND = False

# App palette: black background, white text, red accents/buttons — nothing else.
BG = "#000000"        # window / panels
FG = "#ffffff"        # text
RED = "#ee2a3e"       # buttons, selection, progress, accents
RED_DARK = "#b71c2b"  # pressed / active / hover
RED_DIM = "#5c1119"   # disabled buttons

# Fonts. Bold on buttons and selected rows keeps them easy to read.
UI_FONT = ("Segoe UI", 10)
UI_FONT_BOLD = ("Segoe UI", 10, "bold")

# How often (ms) to re-check the server for changes.
LIVE_REFRESH_MS = 4000

# The action cell. Plain-text arrows (not emoji) so they render in the theme
# colour, never multi-colour.
FILE_ACTION = "↓ Download"
FOLDER_ACTION = "Open"

# Deleted files are moved here rather than destroyed, so a delete can be undone.
TRASH = "Trash"

# The task strip has one row per kind of work, so a transfer and a delete each
# show their own progress instead of fighting over one bar.
TASK_TRANSFER = 0
TASK_DELETE = 1
TASK_MOVE = 2
TASK_COUNT = 3
TASK_NAMES = ("Transfer", "Delete", "Move")


# ---- pure helpers (no GUI) ---------------------------------------------------


def trash_icon(root: tk.Misc, color: str = FG) -> tk.PhotoImage:
    """A small monochrome trash can, drawn in code so it renders the same
    everywhere (emoji fonts vary, and the palette avoids coloured glyphs)."""
    img = tk.PhotoImage(master=root, width=18, height=18)
    for x1, y1, x2, y2 in (
        (7, 1, 11, 2),    # handle
        (6, 2, 7, 4), (11, 2, 12, 4),
        (2, 4, 16, 6),    # lid
        (4, 7, 5, 17),    # body: left side
        (13, 7, 14, 17),  # right side
        (4, 16, 14, 17),  # bottom
        (7, 9, 8, 14),    # slats
        (10, 9, 11, 14),
    ):
        img.put(color, to=(x1, y1, x2, y2))
    return img


def open_in_explorer(path: Path) -> None:
    """Open ``path`` in the OS file manager (Windows/macOS/Linux)."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def human_size(n: int) -> str:
    """Render a byte count as a short human-readable string."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def disk_percent(disk: dict[str, int] | None) -> float:
    """How full the server disk is, 0–100 (0 when unknown)."""
    if not disk or disk["total"] <= 0:
        return 0.0
    return min(100.0, 100.0 * disk["used"] / disk["total"])


def disk_text(disk: dict[str, int] | None) -> str:
    """'X used of Y (Z free)' for the server disk, or a dash when unknown."""
    if not disk:
        return "—"
    return (
        f"{human_size(disk['used'])} used of {human_size(disk['total'])}"
        f" ({human_size(disk['free'])} free)"
    )


def human_speed(bytes_per_sec: float) -> str:
    """Render a transfer rate as B/s, KB/s, MB/s or GB/s."""
    rate = float(bytes_per_sec)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if rate < 1024 or unit == "GB/s":
            return f"{rate:.0f} {unit}" if unit == "B/s" else f"{rate:.1f} {unit}"
        rate /= 1024
    return f"{rate:.1f} GB/s"


def parent_folder(path: str) -> str:
    """The parent folder of a file/folder path ("" for a top-level item)."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


def leaf_name(path: str) -> str:
    """The last path segment (the file or folder name)."""
    return path.rsplit("/", 1)[-1] if "/" in path else path


def folder_contents(
    cwd: str, file_paths, folder_paths
) -> tuple[list[str], list[str]]:
    """Return (subfolders, files) directly inside folder ``cwd``.

    ``cwd`` is "" for the root. Sub-folders are derived both from explicit
    folders and from the prefixes of file paths, so a folder shows up whether or
    not it currently holds files. Returns full paths, each sorted.
    """
    prefix = "" if not cwd else cwd.rstrip("/") + "/"
    plen = len(prefix)
    subfolders: set[str] = set()
    files: list[str] = []

    for p in file_paths:
        if prefix and not p.startswith(prefix):
            continue
        rest = p[plen:]
        if not rest:
            continue
        if "/" in rest:
            subfolders.add(prefix + rest.split("/", 1)[0])
        else:
            files.append(p)

    for f in folder_paths:
        if prefix and not f.startswith(prefix):
            continue
        rest = f[plen:]
        if not rest:  # f == cwd itself
            continue
        subfolders.add(prefix + rest.split("/", 1)[0])

    return sorted(subfolders), sorted(files)


def all_folders(file_paths, folder_paths) -> list[str]:
    """Every folder that exists, explicit or implied by a file's path."""
    folders: set[str] = set(folder_paths)
    for p in file_paths:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[: i]))
    return sorted(folders)


def depth(path: str) -> int:
    """How many segments a path has (used to delete deepest folders first)."""
    return path.count("/") + 1


def paths_under(folder: str, file_paths) -> list[str]:
    """Every file at or below ``folder`` (recursive), sorted."""
    prefix = folder + "/"
    return sorted(p for p in file_paths if p == folder or p.startswith(prefix))


def subfolder_entries(selected: list[str], folder_paths) -> list[str]:
    """Explicit folder entries at or below any selected folder, deepest first.

    These are the empty shells left behind once a folder's files have moved
    away; they're removed after the files, deepest first, so no parent is
    dropped while a child still hangs off it.
    """
    out: list[str] = []
    for sel in selected:
        prefix = sel + "/"
        for f in folder_paths:
            if (f == sel or f.startswith(prefix)) and f not in out:
                out.append(f)
    return sorted(out, key=depth, reverse=True)


def in_trash(path: str) -> bool:
    """True for the Trash folder itself and anything inside it."""
    return path == TRASH or path.startswith(TRASH + "/")


def trash_path(path: str) -> str:
    """Where ``path`` lands when deleted."""
    return f"{TRASH}/{path}"


def untrash(path: str) -> str:
    """Where a trashed path came from (strip the leading ``Trash/``)."""
    return path[len(TRASH) + 1:] if path.startswith(TRASH + "/") else path


def is_reserved_trash_name(path: str) -> bool:
    """True if ``path`` starts with a top-level name that is "Trash" ignoring
    case. The server compares names ignoring case, so a top-level "trash"
    would take Trash's name (see :func:`trash_blocker`)."""
    return path.split("/", 1)[0].casefold() == TRASH.casefold()


def trash_blocker(file_paths, folder_paths) -> str | None:
    """A top-level item that stops anything reaching Trash, or None.

    A top-level file named like Trash, or a folder spelt differently from it
    ("trash") while no "Trash" exists, holds Trash's name on the server, which
    then refuses every move into Trash.
    """
    for p in file_paths:
        if "/" not in p and p.casefold() == TRASH.casefold():
            return p
    roots = {f.split("/", 1)[0] for f in all_folders(file_paths, folder_paths)}
    if TRASH in roots:
        return None
    return next((r for r in sorted(roots) if r.casefold() == TRASH.casefold()), None)


def folder_spellings(file_paths, folder_paths) -> dict[str, str]:
    """Every folder on the server, keyed case-folded, as it is spelt there."""
    return {f.casefold(): f for f in all_folders(file_paths, folder_paths)}


def match_case(path: str, spelling: dict[str, str], whole: bool = False) -> str:
    """``path`` with each folder on the way spelt as the server spells it
    ("docs/x" beside folder "Docs" becomes "Docs/x"): the server refuses a
    second spelling of a folder. With ``whole`` the last segment is matched
    too, so a folder that exists already is reused."""
    parts = path.split("/")
    last = len(parts) if whole else len(parts) - 1
    out = ""
    for i, part in enumerate(parts):
        nxt = f"{out}/{part}" if out else part
        out = spelling.get(nxt.casefold(), nxt) if i < last else nxt
    return out


def learn_spelling(spelling: dict[str, str], path: str, whole: bool = False) -> None:
    """Record the folders on the way to ``path`` (and ``path`` itself with
    ``whole``) in ``spelling``, so later plans in a batch spell them alike."""
    parts = path.split("/")
    for i in range(1, len(parts) + (1 if whole else 0)):
        prefix = "/".join(parts[:i])
        spelling.setdefault(prefix.casefold(), prefix)


def free_path(path: str, taken, is_folder: bool = False) -> str:
    """``path`` if nothing on the server uses it, else the first free
    ``name (n).ext`` beside it (a folder gets ``name (n)``).

    ``taken`` holds case-folded names (see :func:`server_names`): the server
    treats "A.txt" and "a.txt" as one name.
    """
    if path.casefold() not in taken:
        return path
    parent, leaf = parent_folder(path), leaf_name(path)
    if is_folder:
        stem, ext = leaf, ""
    else:
        stem, ext = PurePosixPath(leaf).stem, PurePosixPath(leaf).suffix
    n = 1
    while True:
        name = f"{stem} ({n}){ext}"
        candidate = f"{parent}/{name}" if parent else name
        if candidate.casefold() not in taken:
            return candidate
        n += 1


def clear_ancestors(path: str, files: set[str], taken: set[str]) -> str:
    """``path`` with any folder on the way that is really a file numbered like
    a folder ("Trash/report/b.txt" beside file "Trash/report" becomes
    "Trash/report (1)/b.txt"): the server refuses anything beneath a file.

    ``files`` and ``taken`` hold case-folded names (see :func:`server_names`).
    """
    parts = path.split("/")
    for i in range(1, len(parts)):
        node = "/".join(parts[:i])
        if node.casefold() in files:
            parts[: i] = free_path(node, taken, is_folder=True).split("/")
    return "/".join(parts)


def server_names(file_paths, folder_paths) -> set[str]:
    """Every path in use on the server, live files and every folder, case-folded.

    The server refuses a name that differs from a taken one only by case (on a
    case-insensitive disk it is the same file), so plans compare that way too.
    """
    return {p.casefold() for p in set(file_paths) | set(all_folders(file_paths, folder_paths))}


def plan_trash(
    files: list[str], folders: list[str], file_paths, folder_paths
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Where each deleted item lands in Trash, never on top of something there.

    Returns (file moves, folder targets). A folder goes in whole: if Trash
    already holds something with its name it becomes ``name (1)`` there, its
    contents kept together. A file whose name is taken becomes ``name (1).ext``.
    Files inside a selected folder travel with the folder. Folders on the way
    keep the spelling Trash already has for them (see :func:`match_case`).
    """
    taken = server_names(file_paths, folder_paths)
    files_there = {p.casefold() for p in file_paths}
    spelling = folder_spellings(file_paths, folder_paths)
    # Trash itself is never respelt onto a user's "trash" (trash_blocker).
    spelling[TRASH.casefold()] = TRASH
    moves: list[tuple[str, str]] = []
    targets: list[tuple[str, str]] = []
    for folder in folders:
        dst = clear_ancestors(match_case(trash_path(folder), spelling), files_there, taken)
        dst = free_path(dst, taken, is_folder=True)
        taken.add(dst.casefold())
        learn_spelling(spelling, dst, whole=True)
        targets.append((folder, dst))
        for path in paths_under(folder, file_paths):
            moved = dst + path[len(folder):]
            taken.add(moved.casefold())
            moves.append((path, moved))
    inside = tuple(f + "/" for f in folders)
    for path in files:
        if inside and path.startswith(inside):
            continue
        dst = clear_ancestors(match_case(trash_path(path), spelling), files_there, taken)
        dst = free_path(dst, taken)
        taken.add(dst.casefold())
        learn_spelling(spelling, dst)
        moves.append((path, dst))
    return moves, targets


def trash_folder_moves(
    targets: list[tuple[str, str]], folder_paths
) -> list[tuple[str, str]]:
    """Every explicit folder at or below each trashed folder, paired with its
    place in Trash (see :func:`plan_trash` for the targets).

    Files don't carry these across, and an empty one has no files at all, so
    each is recreated in Trash and its original removed; Restore brings them
    back the same way.
    """
    out: list[tuple[str, str]] = []
    for src, dst in targets:
        for f in sorted(folder_paths):
            if f == src or f.startswith(src + "/"):
                out.append((f, dst + f[len(src):]))
    return out


def trash_origins(entries) -> dict[str, str]:
    """Where trashed things came from, from each trashed file's ``moved_from``.

    Trash numbers a name that's already taken there ("a (1).txt"), so the Trash
    path alone can't say where an item came from; the server records every
    move's source instead. The map holds each trashed file and every trashed
    folder above it whose path lines up with the file's source.
    """
    out: dict[str, str] = {}
    for e in entries:
        src = e.moved_from
        if not (in_trash(e.path) and src and not in_trash(src)):
            continue
        node = e.path
        while node != TRASH:
            rest = e.path[len(node):]  # "" for the file, "/x.txt" for its folder
            if len(src) <= len(rest) or not src.endswith(rest):
                break
            out.setdefault(node, src[: len(src) - len(rest)])
            node = parent_folder(node)
    return out


def trash_origin(path: str, origins: dict[str, str] | None = None) -> str:
    """Where trashed ``path`` goes back to: from ``origins`` (see
    :func:`trash_origins`) when it or a folder above it is known, else the
    Trash path without its leading ``Trash/``."""
    node = path
    while origins and node and node != TRASH:
        if node in origins:
            return origins[node] + path[len(node):]
        node = parent_folder(node)
    return untrash(path)


def plan_restore(
    paths: list[str], file_paths, folder_paths, origins: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """Moves that put trashed files back where they came from. One whose spot
    has been taken since comes back as ``name (1).ext`` beside it, and a
    folder on the way that exists in another case is used as it is spelt."""
    taken = server_names(file_paths, folder_paths)
    files_there = {p.casefold() for p in file_paths}
    spelling = folder_spellings(file_paths, folder_paths)
    moves: list[tuple[str, str]] = []
    for path in paths:
        dst = clear_ancestors(match_case(trash_origin(path, origins), spelling), files_there, taken)
        dst = free_path(dst, taken)
        taken.add(dst.casefold())
        learn_spelling(spelling, dst)
        moves.append((path, dst))
    return moves


def plan_restore_folders(
    folder_entries: list[str],
    origins: dict[str, str] | None = None,
    file_paths=(),
    folder_paths=(),
) -> list[tuple[str, str]]:
    """(trashed folder entry, where it goes back) for each explicit folder
    inside Trash. A folder that exists there already, in any case, is simply
    reused. ``file_paths`` should include where the batch's files are going,
    so a folder is spelt as they are."""
    spelling = folder_spellings(file_paths, folder_paths)
    files = {p.casefold() for p in file_paths}
    out: list[tuple[str, str]] = []
    for f in folder_entries:
        if f.startswith(TRASH + "/"):
            dst = match_case(trash_origin(f, origins), spelling, whole=True)
            # Numbered as its files are (see plan_restore) if the name is now a file.
            dst = clear_ancestors(dst + "/-", files, files)[: -len("/-")]
            learn_spelling(spelling, dst, whole=True)
            out.append((f, dst))
    return out


def plan_folder_move(
    src_folder: str, dest: str, file_paths
) -> tuple[list[tuple[str, str]], str]:
    """Moves that nest ``src_folder`` under ``dest``, keeping its own name.

    Folder ``a/b`` dropped on ``c`` becomes ``c/b/…``. Returns the (src, dst)
    pairs for every file below it, plus the folder's new path.
    """
    parent = parent_folder(src_folder)
    strip = 0 if not parent else len(parent) + 1
    prefix = src_folder + "/"
    moves: list[tuple[str, str]] = []
    for p in sorted(file_paths):
        if p == src_folder or p.startswith(prefix):
            rel = p[strip:]
            dst = rel if not dest else f"{dest}/{rel}"
            if dst != p:
                moves.append((p, dst))
    leaf = leaf_name(src_folder)
    return moves, (leaf if not dest else f"{dest}/{leaf}")


def plan_selection_move(
    sel_files: list[str],
    sel_folders: list[str],
    dest: str,
    file_paths,
    folder_paths,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """Work out a mixed selection's move into ``dest``.

    Files keep their name; folders nest under ``dest`` keeping their leaf.
    Returns (file moves, folder moves, skipped). *Folder moves* pair every
    explicit folder at or below a moved folder with its new path: files don't
    carry those across, so each is recreated at the destination and its old
    entry removed. *Skipped* pairs an item with why it can't go (a folder into
    itself or a descendant, or anything onto the top-level name Trash, in any
    case). A folder already sitting in ``dest`` is silently left alone.
    ``dest`` is spelt as the server spells it (see :func:`match_case`), so a
    typed "docs" beside folder "Docs" moves into "Docs".
    """
    moves: list[tuple[str, str]] = []
    folder_moves: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    if dest:
        dest = match_case(dest, folder_spellings(file_paths, folder_paths), whole=True)

    for path in sel_files:
        dst = leaf_name(path) if not dest else f"{dest}/{leaf_name(path)}"
        if dst == path:
            continue
        if is_reserved_trash_name(dst):
            skipped.append((path, f"“{TRASH}” is reserved for deleted items"))
        else:
            moves.append((path, dst))

    for folder in sel_folders:
        if dest == folder or dest.startswith(folder + "/"):
            skipped.append((folder, "a folder can't be moved into itself"))
            continue
        if parent_folder(folder) == dest:
            continue  # already there
        if not dest and is_reserved_trash_name(leaf_name(folder)):
            skipped.append((folder, f"“{TRASH}” is reserved for deleted items"))
            continue
        file_moves, new_folder = plan_folder_move(folder, dest, file_paths)
        moves.extend(file_moves)
        for f in sorted(folder_paths):
            if f == folder or f.startswith(folder + "/"):
                folder_moves.append((f, new_folder + f[len(folder):]))
    return moves, folder_moves, skipped


def collect_uploads(paths: list[str]) -> list[tuple[str, Path]]:
    """Expand dropped/selected paths into (server_path, local_file) pairs.

    A dropped file becomes its bare filename on the server; a dropped folder is
    walked recursively with its files kept under the folder's name. Server paths
    are normalised, and anything that isn't an existing file or folder is skipped.
    The caller prefixes these with the folder currently being viewed.
    """
    pairs: list[tuple[str, Path]] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    rel = (Path(p.name) / f.relative_to(p)).as_posix()
                    pairs.append((normalize_rel_path(rel), f))
        elif p.is_file():
            pairs.append((normalize_rel_path(p.name), p))
    return pairs


def split_upload_clashes(
    raw_paths: list[str], cwd: str, file_paths, folder_paths
) -> tuple[list[str], list[str]]:
    """Split dropped/selected items into (to upload, names already taken).

    Uploads never replace anything: a dropped file or folder whose name is
    already used in ``cwd`` on the server — by a file or a folder — is refused,
    as is a second dropped item with the same name as an earlier one.
    """
    # Case-folded, like the server's own check: Windows/macOS disks can't hold
    # "A.txt" beside "a.txt".
    taken = server_names(file_paths, folder_paths) | {TRASH.casefold()}  # reserved
    seen: set[str] = set()
    ok: list[str] = []
    clashes: list[str] = []
    for raw in raw_paths:
        name = Path(raw).name
        try:
            server = normalize_rel_path(f"{cwd}/{name}" if cwd else name)
        except ValueError:
            continue  # collect_uploads skips it too
        key = server.casefold()
        if key in taken or key in seen:
            clashes.append(name + ("/" if Path(raw).is_dir() else ""))
            continue
        seen.add(key)
        ok.append(raw)
    return ok, clashes


def oversized_uploads(
    pairs: list[tuple[str, Path]], max_bytes: int | None
) -> list[tuple[str, int]]:
    """Return (server_path, size) for every file larger than ``max_bytes``.

    ``None`` means the limit is unknown (an older server), so nothing is refused.
    """
    if max_bytes is None:
        return []
    too_big: list[tuple[str, int]] = []
    for server_path, local_path in pairs:
        size = local_path.stat().st_size
        if size > max_bytes:
            too_big.append((server_path, size))
    return too_big


def upload_one(
    api: StorageAPI,
    server_path: str,
    local_path: Path,
    progress: ProgressCB | None = None,
    cancel=None,
) -> None:
    """Upload one local file to the server under ``server_path`` (streamed).

    The server refuses a time before 1970 or far in the future, so a bogus one
    falls back to now, as on the phone.
    """
    mtime = local_path.stat().st_mtime
    now = time.time()
    if not 0 < mtime <= now:
        mtime = now
    api.upload_file(server_path, local_path, mtime, progress, cancel)


def upload_files(
    api: StorageAPI,
    pairs: list[tuple[str, Path]],
    progress: ProgressCB | None = None,
    cancel=None,
    on_file=None,
) -> tuple[list[str], list[tuple[str, str]], bool]:
    """Upload every pair, isolating failures.

    Returns (uploaded, [(path, err)], stopped). ``cancel()`` is checked before
    each file and inside the stream, so stopping abandons the file in flight and
    skips the rest; the server keeps whatever already landed. ``on_file(index)``
    fires as each pair starts, so a progress row can name it. A file that has
    gone missing since it was picked fails on its own, like any other error.
    """

    def size_of(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0  # upload_one reports it

    total = sum(size_of(lp) for _, lp in pairs)
    done = 0
    uploaded: list[str] = []
    failed: list[tuple[str, str]] = []
    for i, (server_path, local_path) in enumerate(pairs):
        if cancel is not None and cancel():
            return uploaded, failed, True
        if on_file is not None:
            on_file(i)
        size = size_of(local_path)
        base = done

        def cb(sent: int, _t: int, base: int = base) -> None:
            if progress is not None:
                progress(base + sent, total or 1)

        try:
            upload_one(api, server_path, local_path, cb, cancel)
            uploaded.append(server_path)
        except Cancelled:
            return uploaded, failed, True
        except Exception as exc:  # one bad file shouldn't abort the rest
            failed.append((server_path, str(exc)))
        done += size
        if progress is not None:
            progress(done, total or 1)
    return uploaded, failed, False


def typed_folder(text: str) -> str:
    """A typed destination in the server's form ("" for the root), checked as
    a picked one would be. Raises ValueError for an unsafe path. The leading
    segment keeps a blank, "/" or "." legal; "..", for one, is still refused."""
    probe = normalize_rel_path(f"_/{text.strip()}")
    return "" if probe == "_" else probe[2:]


def error_list(errors: list[tuple[str, str]], limit: int = 12) -> str:
    """The first few errors as a bulleted list, with a count for the rest."""
    detail = "\n".join(f"• {p}: {e}" for p, e in errors[:limit])
    if len(errors) > limit:
        detail += f"\n…and {len(errors) - limit} more"
    return detail


def local_dest(root: Path, rel: str) -> Path:
    """``root/rel``, refusing anything that would land outside ``root``.

    Server paths are re-validated here: a compromised server must not be able
    to make the app write outside the download folder.
    """
    rel = normalize_rel_path(rel)
    base = root.resolve()
    dest = (base / rel).resolve()
    if base not in dest.parents:
        raise ValueError(f"path escapes the download folder: {rel!r}")
    return dest


def keep_both_name(dest: Path, taken=()) -> Path:
    """The first free ``name (n).ext`` beside ``dest``, Windows-style."""
    n = 1
    while True:
        candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        if not candidate.exists() and candidate not in taken:
            return candidate
        n += 1


@dataclass
class PlannedDownload:
    path: str        # server path
    dest: Path       # where it's written
    overwrite: bool  # the user chose to replace a file already there


@dataclass
class DownloadPlan:
    items: list[PlannedDownload]
    dirs: list[Path]                     # created up front, so empty sub-folders come too
    folder_clashes: list[str]            # selected folders whose name is taken locally
    file_clashes: list[PlannedDownload]  # files whose name is taken locally


def plan_downloads(
    root: Path, files: list[str], folders: list[str], file_paths, folder_paths
) -> DownloadPlan:
    """Work out where each download goes, and which names are already taken.

    A file lands straight in ``root``; a folder lands as ``root/<its name>``
    with everything below it. A folder whose name is taken is refused outright;
    a file whose name is taken is left in ``file_clashes`` for the user to
    decide. Two items of the same batch that would share a name aren't a clash
    — the later one is simply saved as ``name (1).ext``.
    """
    plan = DownloadPlan([], [], [], [])
    taken: set[Path] = set()
    for folder in folders:
        name = leaf_name(folder)
        target = local_dest(root, name)
        if target.exists() or target in taken:
            plan.folder_clashes.append(folder)
            continue
        taken.add(target)
        plan.dirs.append(target)
        prefix = folder + "/"
        strip = len(prefix)
        for sub in sorted(folder_paths):
            if sub.startswith(prefix):
                plan.dirs.append(local_dest(root, f"{name}/{sub[strip:]}"))
        for path in paths_under(folder, file_paths):
            dest = local_dest(root, f"{name}/{path[strip:]}")
            plan.items.append(PlannedDownload(path, dest, False))

    inside = tuple(f + "/" for f in folders)
    for path in files:
        if inside and path.startswith(inside):
            continue  # already coming along with its folder
        dest = local_dest(root, leaf_name(path))
        if dest in taken:
            dest = keep_both_name(dest, taken)
        elif dest.exists():
            plan.file_clashes.append(PlannedDownload(path, dest, False))
            continue
        taken.add(dest)
        plan.items.append(PlannedDownload(path, dest, False))
    return plan


def resolve_clashes(plan: DownloadPlan, choice: str) -> None:
    """Apply the user's answer — "overwrite", "keep" or "skip" — to every clash."""
    clashes, plan.file_clashes = plan.file_clashes, []
    if choice == "skip":
        return
    taken = {item.dest for item in plan.items} | set(plan.dirs)
    for item in clashes:
        # Two clashing files with one name can't both overwrite it: the second
        # keeps both instead.
        if choice == "overwrite" and item.dest not in taken:
            item.overwrite = True
        else:
            item.dest = keep_both_name(item.dest, taken)
        taken.add(item.dest)
        plan.items.append(item)


def write_target(item: PlannedDownload) -> Path:
    """Where to write ``item`` now. The name was checked when the user clicked;
    one taken since then (while the job waited in the queue, or while its
    bytes streamed) is never overwritten — the file keeps both instead.
    Called as the finished download is put in place."""
    dest = item.dest
    if dest.is_dir() or (dest.exists() and not item.overwrite):
        return keep_both_name(dest)
    return dest


# ---- the window --------------------------------------------------------------


class FileBrowser:
    def __init__(
        self,
        root: tk.Tk,
        config: ClientConfig | None,
        config_error: str | None = None,
        config_draft: ClientConfig | None = None,
    ):
        self.root = root
        self._config = config
        # Why config.toml couldn't be used, if it exists but didn't load; shown
        # on the setup screen so a broken file isn't mistaken for a missing one.
        # The draft is whatever could still be read from it, to prefill that
        # screen.
        self._config_error = config_error
        self._config_draft = config_draft
        self.api = StorageAPI(config.server_url, config.api_key) if config else None
        self._busy = False
        self._poll_job: str | None = None

        # Background work runs in two lanes, mirroring the phone: transfers
        # (download/upload) in one, in-app operations (delete/move/restore) in
        # the other, so a delete never has to wait behind a big download. Each
        # lane runs one job at a time; the rest wait in the Queue strip.
        self._transfers = Lane("transfer", on_change=self._lane_changed)
        self._ops = Lane("ops", on_change=self._lane_changed)
        self._task_active = [False] * TASK_COUNT
        self._task_pct = [0] * TASK_COUNT
        self._task_detail: list[str | None] = [None] * TASK_COUNT
        self._tasks_expanded = False
        self._queue_expanded = False
        # Set once a job finishes so its result ("Stopped — put 12 item(s) back")
        # survives the 4s poll; cleared by the next navigation, refresh or job.
        self._quiet_status = False
        self._closing = False  # the window is waiting for jobs to wind up
        # True while the running delete is one that undoes itself when stopped,
        # so the Stop prompt can say so.
        self._delete_rolls_back = False

        # Paths already queued or downloading, so clicking download a second
        # time doesn't fetch the same file twice. A job hands its paths back
        # when it finishes, or when it's dropped from the queue.
        self._downloading: set[str] = set()

        # Manifest state and the folder currently being viewed.
        self._cwd = ""
        self._files: dict[str, FileEntry] = {}
        self._folders: list[str] = []
        self._search = ""  # non-empty → show a flat list of matching files
        self._press: dict | None = None   # pending click/drag start info
        self._drag: dict | None = None     # active drag-to-move state
        self._drag_ghost: tk.Toplevel | None = None  # floating label under the cursor

        root.title("Async Storage DOS — Server Files")
        # Roomier than before: the window now carries a Tasks and a Queue strip
        # under the list, and the list shrinks first when space runs short.
        root.geometry("980x700")
        root.minsize(760, 520)
        self._apply_theme()

        self.server_label_var = tk.StringVar(
            value=f"Files on {config.server_url}" if config else "Not configured"
        )
        header = ttk.Frame(root)
        header.pack(fill="x", padx=12, pady=(12, 2))
        ttk.Label(
            header,
            textvariable=self.server_label_var,
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")
        # Trash lives here, not in the folder list: click to open it (and again
        # to go back), or drop dragged rows on it to delete them.
        self._trash_icon = trash_icon(root)
        self.trash_btn = ttk.Button(
            header, text=TRASH, image=self._trash_icon, compound="left",
            command=self._toggle_trash,
        )
        self.trash_btn.pack(side="right")
        self._cwd_before_trash = ""
        self.help_var = tk.StringVar(value=self._help_text())
        ttk.Label(
            root,
            textvariable=self.help_var,
            foreground=FG,
            wraplength=900,
            justify="left",
        ).pack(anchor="w", padx=12)

        # Location bar: an Up button, the current folder path, and a search box.
        locbar = ttk.Frame(root)
        locbar.pack(fill="x", padx=12, pady=(8, 0))
        self.up_btn = ttk.Button(locbar, text="↑ Up", width=6, command=self._go_up)
        self.up_btn.pack(side="left")
        self.loc_var = tk.StringVar(value="/")
        ttk.Label(locbar, textvariable=self.loc_var, foreground=FG).pack(
            side="left", padx=(8, 0)
        )
        # Search box (right): filters the whole tree to matching files as you type.
        self.search_var = tk.StringVar(value="")
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        search_entry = ttk.Entry(locbar, textvariable=self.search_var, width=26)
        search_entry.pack(side="right")
        search_entry.bind("<Escape>", lambda _e: self.search_var.set(""))
        ttk.Label(locbar, text="Search:", foreground=FG).pack(
            side="right", padx=(0, 4)
        )

        # Main area: the file/folder table on the left, upload drop zone on its right.
        content = ttk.Frame(root)
        self.content = content
        content.pack(fill="both", expand=True, padx=12, pady=8)

        table_frame = ttk.Frame(content)
        table_frame.pack(side="left", fill="both", expand=True)

        columns = ("path", "size", "action")
        self._action_col = f"#{columns.index('action') + 1}"
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="extended",
            # A modest requested height: the list takes the leftover space, and
            # gives it back to the strips when several tasks are running.
            height=8,
        )
        self.tree.heading("path", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("action", text="Action")
        self.tree.column("path", anchor="w", width=320)
        self.tree.column("size", anchor="e", width=90, stretch=False)
        self.tree.column("action", anchor="center", width=110, stretch=False)

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # Highlight a folder row while a file is dragged over it.
        self.tree.tag_configure("drophover", background=RED, foreground=FG)
        # Selected rows get bold text so they read clearly on the red highlight.
        self.tree.tag_configure("sel", font=UI_FONT_BOLD)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        # Press/motion/release drive both the per-row action click and the
        # drag-a-file-onto-a-folder move.
        self.tree.bind("<ButtonPress-1>", self._on_press)
        self.tree.bind("<B1-Motion>", self._on_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_release)
        self.tree.bind("<Double-1>", self._on_double)
        # Right-click a row for a context menu (e.g. "Download folder").
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Button-2>", self._on_right_click)  # some macOS mice

        # Drop zone for uploading new files — a tall panel to the right of the list.
        drop_text = (
            "↓\n\nDrag files or\nfolders here\nto upload\n\n(into the\ncurrent folder)"
            if _HAS_DND
            else "Use “Add files…”\nto upload\n\n(install\ntkinterdnd2 for\ndrag-and-drop)"
        )
        self._drop_text = drop_text
        self.drop_zone = tk.Label(
            content,
            text=drop_text,
            bg=BG,
            fg=FG,
            width=22,
            justify="center",
            relief="flat",
            bd=0,
            highlightbackground=RED,
            highlightcolor=RED,
            highlightthickness=1,
        )
        self.drop_zone.pack(side="right", fill="y", padx=(8, 0))
        if _HAS_DND:
            self.drop_zone.drop_target_register(DND_FILES)
            self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)

        # Controls.
        controls = ttk.Frame(root)
        self.refresh_btn = ttk.Button(controls, text="Refresh", command=self._poll_now)
        self.refresh_btn.pack(side="left")
        # Inside Trash these three swap to the Trash actions (see
        # _update_context_buttons), exactly as the phone's bottom bar does.
        self.new_folder_btn = ttk.Button(
            controls, text="New folder", command=self._new_folder_or_empty_trash
        )
        self.new_folder_btn.pack(side="left", padx=(8, 0))
        self.add_btn = ttk.Button(
            controls, text="Add files…", command=self._add_or_restore
        )
        self.add_btn.pack(side="left", padx=(8, 0))
        self.download_btn = ttk.Button(
            controls, text="Download", command=self._download_selected
        )
        self.download_btn.pack(side="left", padx=(8, 0))
        self.move_btn = ttk.Button(controls, text="Move…", command=self._move_selected)
        self.move_btn.pack(side="left", padx=(8, 0))
        # Destructive: moves the selection to Trash (or, inside Trash, destroys
        # it for good). Same red styling as the rest.
        self.delete_btn = ttk.Button(
            controls, text="Delete", command=self._delete_or_forever
        )
        self.delete_btn.pack(side="left", padx=(8, 0))
        # Gear: open the settings screen to change the server URL / API key.
        self.settings_btn = ttk.Button(
            controls, text="⚙", width=3, command=self._open_settings
        )
        self.settings_btn.pack(side="right", padx=(0, 8))
        self.open_btn = ttk.Button(
            controls, text="Open download folder", command=self._open_download_folder
        )
        self.open_btn.pack(side="right")

        # Tasks / Queue strips: what's running, and what's waiting to run. Both
        # collapse to a single line so an idle window stays quiet.
        strips = ttk.Frame(root)
        self.tasks_btn = ttk.Button(strips, text="Tasks ▲", command=self._toggle_tasks)
        self.tasks_btn.pack(side="left")
        self.queue_btn = ttk.Button(strips, text="Queue ▲", command=self._toggle_queue)
        self.queue_btn.pack(side="left", padx=(8, 0))

        # Each strip gets its own holder, so showing or hiding a panel can't
        # reorder them. They're packed from the bottom up (below), which keeps
        # the status line on screen however many task rows are showing.
        self.tasks_holder = ttk.Frame(root)
        self.queue_holder = ttk.Frame(root)

        self.tasks_panel = ttk.Frame(self.tasks_holder)
        self.tasks_empty = ttk.Label(self.tasks_panel, text="No active tasks")
        self.tasks_empty.pack(anchor="w")
        self._task_row: list[ttk.Frame] = []
        self._task_label: list[ttk.Label] = []
        self._task_bar: list[ttk.Progressbar] = []
        for t in range(TASK_COUNT):
            row = ttk.Frame(self.tasks_panel)
            label = ttk.Label(row, text=TASK_NAMES[t], width=42, anchor="w")
            label.pack(side="left")
            bar = ttk.Progressbar(row, maximum=100, mode="determinate", length=240)
            bar.pack(side="left", padx=(8, 0))
            ttk.Button(
                row, text="Stop", width=6,
                command=lambda t=t: self._confirm_stop_task(t),
            ).pack(side="left", padx=(8, 0))
            self._task_row.append(row)
            self._task_label.append(label)
            self._task_bar.append(bar)

        self.queue_panel = ttk.Frame(self.queue_holder)
        self.queue_empty = ttk.Label(self.queue_panel, text="Queue is empty")
        self.queue_empty.pack(anchor="w")
        self.queue_list = ttk.Frame(self.queue_panel)
        self.queue_list.pack(fill="x")

        # Server disk: how full the drive holding the server's data folder is.
        diskbar = ttk.Frame(root)
        ttk.Label(diskbar, text="Server disk:", foreground=FG).pack(side="left")
        self.disk_bar = ttk.Progressbar(
            diskbar, maximum=100, mode="determinate", length=240
        )
        self.disk_bar.pack(side="left", padx=(8, 0))
        self.disk_var = tk.StringVar(value="—")
        ttk.Label(diskbar, textvariable=self.disk_var, foreground=FG).pack(
            side="left", padx=(8, 0)
        )

        self.status = tk.StringVar(value="Loading…")
        self.status_label = ttk.Label(root, textvariable=self.status, foreground=FG)

        # The bottom stack, packed lowest-first so every row keeps its space and
        # the file list above absorbs what's left. Without this an expanded Tasks
        # panel would push the status line off the window.
        self.status_label.pack(side="bottom", anchor="w", padx=12, pady=(0, 10))
        self.queue_holder.pack(side="bottom", fill="x", padx=12)
        self.tasks_holder.pack(side="bottom", fill="x", padx=12)
        strips.pack(side="bottom", fill="x", padx=12, pady=(0, 2))
        controls.pack(side="bottom", fill="x", padx=12, pady=(0, 8))
        # Between the location bar and the file list.
        diskbar.pack(fill="x", padx=12, pady=(8, 0), before=self.content)

        self._refresh_tasks()
        self._refresh_queue()

        if self._config is not None:
            self._load_initial()
            if not self._config.download_dir:
                # Set up by an older version, which saved into a fixed folder:
                # the download folder must be picked before anything downloads.
                self.root.after(200, lambda: self._open_settings(first_run=True))
        else:
            # First run / no config: disable the actions and prompt for the server.
            self._set_busy(False)
            self.status.set(
                f"{config_error} — enter the settings again to replace it."
                if config_error
                else "Not configured — enter the server URL and API key to begin."
            )
            self.root.after(200, lambda: self._open_settings(first_run=True))

    # -- theme (black background, white text, red accents) --

    def _apply_theme(self) -> None:
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        # "clam" is the built-in ttk theme that actually honours custom colours
        # (the native Windows themes ignore most background settings).
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".",
            background=BG,
            foreground=FG,
            fieldbackground=BG,
            troughcolor=BG,
            bordercolor=RED,
            lightcolor=BG,
            darkcolor=BG,
            focuscolor=RED,
            insertcolor=FG,
            selectbackground=RED,
            selectforeground=FG,
        )
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure(
            "TButton", background=RED, foreground=FG, bordercolor=RED,
            relief="flat", padding=6, focuscolor=BG, font=UI_FONT_BOLD,
        )
        style.map(
            "TButton",
            background=[("pressed", RED_DARK), ("active", RED_DARK), ("disabled", RED_DIM)],
            foreground=[("disabled", "#d9a3a8")],
        )
        style.configure("TEntry", fieldbackground=BG, foreground=FG, insertcolor=FG)
        style.configure(
            "TCombobox", fieldbackground=BG, background=RED, foreground=FG,
            arrowcolor=FG, bordercolor=RED,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", BG)],
            foreground=[("readonly", FG)],
            selectbackground=[("readonly", RED)],
        )
        self.root.option_add("*TCombobox*Listbox.background", BG)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", RED)
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG)

        style.configure(
            "Treeview", background=BG, fieldbackground=BG, foreground=FG,
            bordercolor=RED, rowheight=26, font=UI_FONT,
        )
        style.map("Treeview", background=[("selected", RED)], foreground=[("selected", FG)])
        style.configure(
            "Treeview.Heading", background=BG, foreground=FG, relief="flat",
            bordercolor=RED, font=UI_FONT_BOLD,
        )
        style.map("Treeview.Heading", background=[("active", RED_DARK)])

        style.configure(
            "Vertical.TScrollbar", background=RED, troughcolor=BG,
            bordercolor=BG, arrowcolor=FG,
        )
        style.map("Vertical.TScrollbar", background=[("active", RED_DARK)])
        style.configure(
            "Horizontal.TProgressbar", background=RED, troughcolor=BG, bordercolor=RED,
        )

    # -- busy / progress --

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        configured = self.api is not None
        # Actions need a configured server and no in-flight operation.
        state = "disabled" if (busy or not configured) else "normal"
        for btn in (
            self.refresh_btn,
            self.new_folder_btn,
            self.add_btn,
            self.download_btn,
            self.move_btn,
            self.delete_btn,
            self.open_btn,
        ):
            btn.configure(state=state)
        self._update_settings_state()
        self.up_btn.configure(
            state="normal"
            if (self._cwd and not busy and not self._search and configured)
            else "disabled"
        )
        if message is not None:
            self.status.set(message)

    def _update_settings_state(self) -> None:
        """Settings stays reachable except while the file list loads or a job
        runs or waits: changing the server mid-job would switch it to another
        server (or a closed connection) part-way through."""
        locked = self._busy or self._lanes_busy()
        self.settings_btn.configure(state="disabled" if locked else "normal")

    # -- the Tasks strip: one live row per kind of work --

    def _ui(self, fn, *args) -> None:
        """Run ``fn`` on the UI thread (jobs run on lane worker threads)."""
        self.root.after(0, fn, *args)

    def _update_task(self, task: int, pct: int, detail: str | None = None) -> None:
        """Mark a task row active at ``pct`` with optional detail text."""
        self._quiet_status = False  # this job's own messages take over
        self._task_active[task] = True
        self._task_pct[task] = max(0, min(100, int(pct)))
        self._task_detail[task] = detail
        self._refresh_tasks()

    def _lanes_busy(self) -> bool:
        return self._transfers.busy() or self._ops.busy()

    def _finish_task(self, task: int) -> None:
        """Hide a task's row; its work is over."""
        # Let the job's closing message stand until the user moves on.
        self._quiet_status = True
        self._task_active[task] = False
        self._task_pct[task] = 0
        self._task_detail[task] = None
        self._refresh_tasks()

    def _refresh_tasks(self) -> None:
        """Redraw the task rows, the strip's active count, and its visibility."""
        active = 0
        for t in range(TASK_COUNT):
            self._task_row[t].pack_forget()  # re-packed below to keep the order
        for t in range(TASK_COUNT):
            if not self._task_active[t]:
                continue
            active += 1
            self._task_row[t].pack(fill="x", pady=1)
            self._task_bar[t].configure(value=self._task_pct[t])
            detail = self._task_detail[t]
            text = f"{TASK_NAMES[t]}  {self._task_pct[t]}%"
            if detail:
                text += f"  {detail}"
            self._task_label[t].configure(text=text)
        self.tasks_btn.configure(text=f"Tasks ({active}) ▲" if active else "Tasks ▲")
        if active:
            self.tasks_empty.pack_forget()
        else:
            self.tasks_empty.pack(anchor="w")
        self._show_panel(self.tasks_panel, self._tasks_expanded)

    def _refresh_queue(self) -> None:
        """Rebuild the Queue strip from both lanes' waiting jobs."""
        for child in self.queue_list.winfo_children():
            child.destroy()
        waiting = [(op, self._transfers) for op in self._transfers.waiting()]
        waiting += [(op, self._ops) for op in self._ops.waiting()]
        for op, lane in waiting:
            row = ttk.Frame(self.queue_list)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=op.label).pack(side="left")
            ttk.Button(
                row, text="✕", width=3,
                command=lambda lane=lane, i=op.id: self._drop_queued(lane, i),
            ).pack(side="right")
        n = len(waiting)
        self.queue_btn.configure(text=f"Queue ({n}) ▲" if n else "Queue ▲")
        self._update_settings_state()  # a job started, finished or was dropped
        if n:
            self.queue_empty.pack_forget()
        else:
            self.queue_empty.pack(anchor="w")
        self._show_panel(self.queue_panel, self._queue_expanded)

    def _show_panel(self, panel: ttk.Frame, visible: bool) -> None:
        """Show or hide a strip's panel inside its holder."""
        if visible:
            panel.pack(fill="x")
        else:
            panel.pack_forget()

    def _toggle_tasks(self) -> None:
        self._tasks_expanded = not self._tasks_expanded
        self._refresh_tasks()

    def _toggle_queue(self) -> None:
        self._queue_expanded = not self._queue_expanded
        self._refresh_queue()

    def _lane_changed(self) -> None:
        """A lane started/finished/queued a job (called from its worker thread)."""
        self._ui(self._refresh_queue)

    def _drop_queued(self, lane: Lane, op_id: int) -> None:
        dropped = lane.cancel_queued(op_id)
        if dropped is not None:
            # Its files were never fetched, so let them be asked for again.
            self._release_downloads(dropped.meta.get("paths", []))
            self.status.set("Removed a waiting job from the queue.")
        self._refresh_queue()

    def _release_downloads(self, paths) -> None:
        """Forget the paths a finished or dropped download job had claimed."""
        for path in paths:
            self._downloading.discard(path)

    def _confirm_stop_task(self, task: int) -> None:
        """Confirm, then stop the running job of this kind."""
        if not self._task_active[task]:
            return
        if task == TASK_DELETE and self._delete_rolls_back:
            message = (
                "Stop the running Delete?\n\nAnything already moved to Trash is put "
                "back, so the folder is left as it was."
            )
        else:
            message = (
                f"Stop the running {TASK_NAMES[task]}?\n\nWork already done is "
                "kept; the rest is skipped."
            )
        if messagebox.askyesno("Stop task?", message, default="no"):
            self._stop_task(task)

    def _stop_task(self, task: int) -> None:
        lane = self._transfers if task == TASK_TRANSFER else self._ops
        if lane.cancel_running() is not None:
            self.status.set("Stopping…")

    # -- running work in the background lanes --

    def _enqueue_transfer(self, label: str, body, meta: dict | None = None):
        """Queue a download/upload; it runs as soon as the transfer lane is free."""
        op = self._transfers.submit(label, body, meta)
        self._refresh_queue()
        return op

    def _enqueue_op(self, label: str, body) -> None:
        """Queue a delete/move/restore on the in-app operations lane."""
        self._ops.submit(label, body)
        self._refresh_queue()

    def _post_task(
        self, task: int, state: dict, done: int, total: int, detail: str | None = None
    ) -> None:
        """Throttled per-item progress for a task row, called from a worker."""
        pct = int(done * 100 / total) if total else 100
        if pct != state.get("pct"):
            state["pct"] = pct
            self._ui(self._update_task, task, pct, detail)

    def _task_progress(self, task: int, detail_fn) -> ProgressCB:
        """Byte-level progress for a transfer, with a smoothed speed readout.

        ``detail_fn()`` supplies the text after the percentage — a callable so a
        batch can name the file it's currently on. The rate is an EMA over the
        per-percent intervals, so the row reads e.g. "Transfer  42%  photo.raw —
        12.3 MB/s" instead of flickering.
        """
        state = {"pct": -1, "t": time.monotonic(), "bytes": 0, "ema": 0.0}

        def cb(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            if pct != state["pct"]:
                now = time.monotonic()
                dt = now - state["t"]
                db = done - state["bytes"]
                inst = db / dt if dt > 0 else 0.0
                state["ema"] = inst if state["ema"] == 0 else 0.3 * inst + 0.7 * state["ema"]
                state["pct"] = pct
                state["t"] = now
                state["bytes"] = done
                detail = detail_fn()
                if state["ema"]:
                    detail = f"{detail} — {human_speed(state['ema'])}"
                self._ui(self._update_task, task, pct, detail)

        return cb

    # -- live manifest polling --

    def _load_initial(self) -> None:
        self._set_busy(True, "Loading file list…")

        def work() -> None:
            try:
                files, folders = self.api.fetch_manifest()
                live = [e for e in files if not e.deleted]
                self.root.after(0, self._first_loaded, live, folders)
            except Exception as exc:
                self.root.after(0, self._load_failed, describe_error(exc))

        threading.Thread(target=work, daemon=True).start()

    def _first_loaded(self, files: list[FileEntry], folders: list[str]) -> None:
        self._set_busy(False)
        self._apply_manifest(files, folders)
        self._schedule_poll()

    def _load_failed(self, error: str) -> None:
        self._set_busy(False, f"{error} — retrying…")
        self._schedule_poll()  # keep trying; the server may just be starting

    def _schedule_poll(self) -> None:
        self._poll_job = self.root.after(LIVE_REFRESH_MS, self._on_poll_tick)

    def _on_poll_tick(self) -> None:
        if not self._busy:
            self._poll_now(silent=True)
        self._schedule_poll()

    def _poll_now(self, silent: bool = False) -> None:
        if self._busy or self.api is None:
            return
        if not silent:
            self._quiet_status = False  # the Refresh button asks for a fresh line

        def work() -> None:
            try:
                files, folders = self.api.fetch_manifest()
                live = [e for e in files if not e.deleted]
                self.root.after(0, self._apply_manifest, live, folders)
            except Exception as exc:
                if not silent:
                    self.root.after(0, self.status.set, describe_error(exc))

        threading.Thread(target=work, daemon=True).start()

    def _apply_manifest(self, files: list[FileEntry], folders: list[str]) -> None:
        self._files = {e.path: e for e in files}
        self._folders = list(folders)
        self._normalize_cwd()
        self._render()
        self._render_disk()
        if not self._busy and not self._quiet_status and not self._lanes_busy():
            n = len(self._files)
            here = f" in /{self._cwd}" if self._cwd else ""
            self.status.set(
                f"{n} file{'s' if n != 1 else ''} on the server — live-updating{here}."
            )

    def _render_disk(self) -> None:
        disk = getattr(self.api, "disk", None)
        self.disk_bar["value"] = disk_percent(disk)
        self.disk_var.set(disk_text(disk))

    # -- folder navigation & rendering --

    def _folder_exists(self, folder: str) -> bool:
        if folder in self._folders:
            return True
        prefix = folder + "/"
        if any(f.startswith(prefix) for f in self._folders):
            return True
        return any(p.startswith(prefix) for p in self._files)

    def _normalize_cwd(self) -> None:
        """If the current folder vanished, climb to the nearest existing one.
        Trash itself stays open: it has no entry until something is in it."""
        while self._cwd and self._cwd != TRASH and not self._folder_exists(self._cwd):
            self._cwd = parent_folder(self._cwd)

    def _render(self) -> None:
        selected = set(self.tree.selection())
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())

        if self._search:
            self._render_search(selected)
            self._restore_focus(focus)
            return

        if self._in_trash():
            self.loc_var.set(self._cwd.replace("/", " › "))
        else:
            self.loc_var.set("/" + self._cwd if self._cwd else "/")
        # At the top of Trash there's nowhere to go "up" to: Back leaves Trash.
        can_go_up = bool(self._cwd) and self._cwd != TRASH
        self.up_btn.configure(
            state="normal" if (can_go_up and not self._busy) else "disabled"
        )

        if can_go_up:
            self.tree.insert("", "end", iid="up", values=("↑  ..", "", ""))

        subfolders, files = folder_contents(
            self._cwd, self._files.keys(), self._folders
        )
        if not self._cwd:
            subfolders = [f for f in subfolders if f != TRASH]  # it has its own button
        for folder in subfolders:
            # Trailing "/" marks a folder (monochrome, no coloured emoji).
            self.tree.insert(
                "", "end", iid="dir:" + folder,
                values=(leaf_name(folder) + "/", "—", FOLDER_ACTION),
            )

        for path in files:
            entry = self._files[path]
            self.tree.insert(
                "", "end", iid="file:" + path,
                values=("      " + leaf_name(path), human_size(entry.size), FILE_ACTION),
            )

        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        self._restore_focus(focus)
        self._update_context_buttons()

    def _restore_focus(self, iid: str) -> None:
        """Keep the focused row across a rebuild: Shift-click extends from it,
        and the arrow keys move from it."""
        if iid and self.tree.exists(iid):
            self.tree.focus(iid)

    def _render_search(self, selected: set) -> None:
        """Flat list of every file whose path contains the query (any folder).

        Outside Trash, trashed files don't show; inside it, only they do.
        """
        query = self._search.lower()
        trash = self._in_trash()
        matches = sorted(
            p for p in self._files
            if query in p.lower() and in_trash(p) == trash
        )
        n = len(matches)
        self.loc_var.set(f"🔍 “{self._search}” — {n} match{'es' if n != 1 else ''}")
        self.up_btn.configure(state="disabled")
        for path in matches:
            entry = self._files[path]
            # Show the full path so the file's folder is clear in results.
            self.tree.insert(
                "", "end", iid="file:" + path,
                values=(path, human_size(entry.size), FILE_ACTION),
            )
        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        self._update_context_buttons()

    def _on_search_changed(self) -> None:
        self._search = self.search_var.get().strip()
        self._render()

    def _reveal(self, path: str) -> None:
        """Clear the search and jump to the file's folder, selecting it."""
        self.search_var.set("")  # clears self._search and re-renders the folder view
        self._cwd = parent_folder(path)
        self._render()
        fiid = "file:" + path
        if self.tree.exists(fiid):
            self.tree.selection_set(fiid)
            self.tree.see(fiid)
        where = f"/{self._cwd}" if self._cwd else "/ (root)"
        self.status.set(f"Location: {where}")

    def _navigate(self, folder: str) -> None:
        self._quiet_status = False
        self._cwd = folder
        self._render()
        if self._in_trash():
            self.status.set(
                "Trash — Restore puts items back; Delete forever removes them for good."
            )
            return
        where = f"/{self._cwd}" if self._cwd else "/ (root)"
        self.status.set(f"Location: {where}")

    def _go_up(self) -> None:
        if self._cwd and self._cwd != TRASH and not self._busy:
            self._navigate(parent_folder(self._cwd))

    # -- Trash context: the same buttons, different jobs --

    def _in_trash(self) -> bool:
        """True while browsing inside the Trash folder."""
        return bool(self._cwd) and in_trash(self._cwd)

    def _toggle_trash(self) -> None:
        """Open Trash, or go back to the folder you were in before."""
        if self._busy:
            return
        self.search_var.set("")
        if self._in_trash():
            back = self._cwd_before_trash
            self._navigate(back if self._folder_exists(back) else "")
        else:
            self._cwd_before_trash = self._cwd
            self._navigate(TRASH)

    def _update_context_buttons(self) -> None:
        """Swap the action buttons to their Trash meanings (and back)."""
        trash = self._in_trash()
        n = len(paths_under(TRASH, self._files))
        self.trash_btn.configure(
            text="Back to files" if trash else (f"{TRASH} ({n})" if n else TRASH)
        )
        self.drop_zone.configure(
            text=f"{TRASH} only holds\ndeleted items\n\n(go back to your\nfiles to upload)"
            if trash else self._drop_text
        )
        self.add_btn.configure(text="Restore" if trash else "Add files…")
        self.delete_btn.configure(text="Delete forever" if trash else "Delete")
        self.new_folder_btn.configure(text="Empty Trash" if trash else "New folder")
        if not self._busy and self.api is not None:
            # Moving things around inside Trash isn't meaningful — restore first.
            self.move_btn.configure(state="disabled" if trash else "normal")

    def _add_or_restore(self) -> None:
        if self._in_trash():
            self._restore_selected()
        else:
            self._add_files_dialog()

    def _delete_or_forever(self) -> None:
        if self._in_trash():
            self._delete_forever_selected()
        else:
            self._delete_selected()

    def _new_folder_or_empty_trash(self) -> None:
        if self._in_trash():
            self._empty_trash()
        else:
            self._new_folder()

    # -- selection helpers --

    def _selected_folder_paths(self) -> list[str]:
        """Paths of every selected folder row."""
        return [iid[4:] for iid in self.tree.selection() if iid.startswith("dir:")]

    def _selection_files(self) -> list[str]:
        """Selected files plus every file under a selected folder, deduped."""
        seen: dict[str, None] = {}
        for path in self._selected_file_paths():
            seen[path] = None
        for folder in self._selected_folder_paths():
            for path in paths_under(folder, self._files):
                seen[path] = None
        return list(seen)

    def _ask_choice(self, title: str, question: str, options: list[str]) -> int | None:
        """Modal list picker. Returns the chosen index, or None if cancelled."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        ttk.Label(dlg, text=question, wraplength=420).pack(
            anchor="w", padx=12, pady=(12, 8)
        )
        result: dict[str, int | None] = {"value": None}

        def choose(index: int) -> None:
            result["value"] = index
            dlg.destroy()

        for i, option in enumerate(options):
            ttk.Button(dlg, text=option, command=lambda i=i: choose(i)).pack(
                fill="x", padx=12, pady=(0, 6)
            )
        ttk.Button(dlg, text="Cancel", command=dlg.destroy).pack(
            fill="x", padx=12, pady=(6, 12)
        )
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.grab_set()
        self.root.wait_window(dlg)
        return result["value"]

    # -- clicks --

    def _on_double(self, event) -> None:
        if self._busy:
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        # In search results, double-clicking a file jumps to its folder.
        if self._search and iid.startswith("file:"):
            self._reveal(iid[5:])
            return
        if iid == "up":
            self._go_up()
        elif iid.startswith("dir:"):
            self._navigate(iid[4:])

    # -- drag a file (or selection) onto a folder to move it --

    def _on_press(self, event) -> None:
        # Record where the press landed and which files a drag would carry. The
        # selection is read here, before Tk's default handler may collapse a
        # multi-selection to the single pressed row.
        if self._busy:
            self._press = None
            return
        iid = self.tree.identify_row(event.y)
        # What a drag from here would carry: the pressed row on its own, or the
        # whole selection (files and folders) when the pressed row is part of it.
        files: list[str] = []
        folders: list[str] = []
        multi = len(self.tree.selection()) > 1
        if iid and iid.startswith("file:"):
            pressed = iid[5:]
            selected = self._selected_file_paths()
            if multi and pressed in selected:
                files, folders = selected, self._selected_folder_paths()
            else:
                files = [pressed]
        elif iid and iid.startswith("dir:"):
            pressed = iid[4:]
            selected = self._selected_folder_paths()
            if multi and pressed in selected:
                files, folders = self._selected_file_paths(), selected
            else:
                folders = [pressed]
        self._press = {
            "x": event.x,
            "y": event.y,
            "iid": iid,
            "region": self.tree.identify("region", event.x, event.y),
            "col": self.tree.identify_column(event.x),
            "files": files,
            "folders": folders,
        }
        self._drag = None
        # No "break": let the default handler run so click/Ctrl/Shift selection works.

    def _on_motion(self, event):
        if self._busy or not self._press:
            return
        iid = self._press["iid"]
        # Files and folders are draggable, but not while showing search results
        # (no folders are visible to drop onto there).
        if not iid or self._search:
            return
        if not (iid.startswith("file:") or iid.startswith("dir:")):
            return
        if self._drag is None:
            if abs(event.x - self._press["x"]) < 6 and abs(event.y - self._press["y"]) < 6:
                return "break"  # still within the click threshold; just suppress drag-select
            files = self._press["files"]
            folders = self._press["folders"]
            if not files and not folders:
                return "break"
            self._drag = {"files": files, "folders": folders, "hover": None}
            self.tree.configure(cursor="hand2")
            carried = [f"file:{p}" for p in files] + [f"dir:{p}" for p in folders]
            if len(carried) > 1:  # keep the whole set highlighted during the drag
                self.tree.selection_set(carried)
            self._show_drag_ghost(files, folders, event.x_root, event.y_root)
        self._update_drop_hover(event.y)
        self._update_trash_hover(event.x_root, event.y_root)
        self._move_drag_ghost(event.x_root, event.y_root)
        return "break"

    def _over_trash_btn(self, x_root: int, y_root: int) -> bool:
        """Is the pointer over the Trash button (a drop target for deleting)?"""
        return self.root.winfo_containing(x_root, y_root) is self.trash_btn

    def _update_trash_hover(self, x_root: int, y_root: int) -> None:
        over = not self._in_trash() and self._over_trash_btn(x_root, y_root)
        self.trash_btn.state(["pressed"] if over else ["!pressed"])

    # A small semi-transparent window that trails the cursor while dragging,
    # showing what's being carried (like moving items on a phone / in Drive).
    def _show_drag_ghost(
        self, files: list[str], folders: list[str], x_root: int, y_root: int
    ) -> None:
        self._destroy_drag_ghost()
        carried = files + folders
        if len(carried) == 1:
            label = leaf_name(carried[0]) + ("/" if folders else "")
        else:
            label = f"{len(carried)} items"
        ghost = tk.Toplevel(self.root)
        ghost.overrideredirect(True)  # borderless
        try:
            ghost.attributes("-topmost", True)
            ghost.attributes("-alpha", 0.72)  # faint while held
        except tk.TclError:
            pass  # transparency unsupported on some window managers; show it solid
        tk.Label(
            ghost,
            text=f"  ⠿  {label}  ",
            bg=RED,
            fg=FG,
            bd=0,
            relief="flat",
            padx=6,
            pady=3,
        ).pack()
        self._drag_ghost = ghost
        self._move_drag_ghost(x_root, y_root)

    def _move_drag_ghost(self, x_root: int, y_root: int) -> None:
        if self._drag_ghost is not None:
            self._drag_ghost.geometry(f"+{x_root + 14}+{y_root + 12}")

    def _destroy_drag_ghost(self) -> None:
        if self._drag_ghost is not None:
            try:
                self._drag_ghost.destroy()
            except tk.TclError:
                pass
            self._drag_ghost = None

    def _update_drop_hover(self, y: int) -> None:
        iid = self.tree.identify_row(y)
        target = iid if (iid == "up" or (iid and iid.startswith("dir:"))) else None
        if target == self._drag["hover"]:
            return
        old = self._drag["hover"]
        if old and self.tree.exists(old):
            self.tree.item(old, tags=())
        if target:
            self.tree.item(target, tags=("drophover",))
        self._drag["hover"] = target

    def _on_release(self, event):
        press, drag = self._press, self._drag
        self._press = None
        self._drag = None
        self._destroy_drag_ghost()

        if drag is not None:
            self.tree.configure(cursor="")
            self.trash_btn.state(["!pressed"])
            if drag["hover"] and self.tree.exists(drag["hover"]):
                self.tree.item(drag["hover"], tags=())
            if not self._in_trash() and self._over_trash_btn(event.x_root, event.y_root):
                self._trash_items(drag["files"], drag["folders"])
                return "break"
            target = self.tree.identify_row(event.y)
            if target == "up":
                dest = parent_folder(self._cwd)
            elif target and target.startswith("dir:"):
                dest = target[4:]
            else:
                self.status.set("Drop onto a folder to move, or onto Trash to delete.")
                return "break"
            if drag["folders"] and not drag["files"] and len(drag["folders"]) == 1:
                self._move_folder(drag["folders"][0], dest)  # its own nesting rules
            else:
                self._move_selection(drag["files"], drag["folders"], dest)
            return "break"

        # A plain click (no drag): run the per-row action if the action cell was hit.
        if self._busy or not press:
            return
        if press["region"] != "cell" or press["col"] != self._action_col:
            return
        iid = press["iid"]
        if not iid or self.tree.identify_row(event.y) != iid:
            return
        if iid == "up":
            self._navigate(parent_folder(self._cwd))
        elif iid.startswith("dir:"):
            self._navigate(iid[4:])
        elif iid.startswith("file:"):
            self._download_item(iid[5:])

    def _selected_file_paths(self) -> list[str]:
        """Paths of every selected file row (folders and '..' are ignored)."""
        return [iid[5:] for iid in self.tree.selection() if iid.startswith("file:")]

    def _on_tree_select(self, _event=None) -> None:
        """Bold the selected rows (leaving a live drop-hover highlight alone)."""
        selected = set(self.tree.selection())
        for iid in self.tree.get_children():
            tags = self.tree.item(iid, "tags")
            if "drophover" in tags:
                continue
            want = ("sel",) if iid in selected else ()
            if tuple(tags) != want:
                self.tree.item(iid, tags=want)

    # -- per-row action (download) --

    def _download_item(self, path: str) -> None:
        self._download([path], [])

    def _download_selected(self) -> None:
        """The toolbar Download button: every selected file and folder."""
        files = self._selected_file_paths()
        folders = self._selected_folder_paths()
        if not files and not folders:
            messagebox.showinfo(
                "Nothing selected", "Select one or more files or folders to download."
            )
            return
        self._download(files, folders)

    # -- right-click menu + download a whole folder --

    def _on_right_click(self, event) -> None:
        if self._busy or self.api is None:
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid == "up":
            return
        self.tree.selection_set(iid)  # focus the right-clicked row
        menu = tk.Menu(
            self.root, tearoff=0, bg=BG, fg=FG,
            activebackground=RED, activeforeground=FG, bd=0,
        )
        trash = self._in_trash()
        if iid.startswith("dir:"):
            folder = iid[4:]
            if trash:
                # Inside Trash a folder's actions are Open / Restore / destroy.
                menu.add_command(
                    label="Open", command=lambda: self._navigate(folder)
                )
                menu.add_command(
                    label="Restore folder",
                    command=lambda: self._restore_folder(folder),
                )
                menu.add_command(
                    label="Delete folder forever",
                    command=lambda: self._delete_forever_folder(folder),
                )
            else:
                menu.add_command(
                    label="Download folder",
                    command=lambda: self._download_folder(folder),
                )
                menu.add_command(
                    label="Move folder…", command=lambda: self._move_folder_dialog(folder)
                )
                menu.add_command(
                    label="Delete folder", command=lambda: self._delete_folder(folder)
                )
        elif iid.startswith("file:"):
            path = iid[5:]
            if trash:
                menu.add_command(
                    label="Restore", command=lambda: self._restore([path])
                )
                menu.add_command(
                    label="Delete forever",
                    command=lambda: self._delete_forever_files([path]),
                )
            else:
                menu.add_command(
                    label="Download", command=lambda: self._download_item(path)
                )
                menu.add_command(
                    label="Move…", command=lambda: self._move_selection_dialog([path])
                )
                menu.add_command(
                    label=f"Delete (to {TRASH})",
                    command=lambda: self._delete_files([path]),
                )
        else:
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _move_folder_dialog(self, folder: str) -> None:
        """Ask where a folder should go, then nest it there."""
        dest = self._choose_folder_dialog(f"‘{leaf_name(folder)}’", parent_folder(folder))
        if dest is not None:
            self._move_folder(folder, dest)

    def _move_selection_dialog(self, paths: list[str]) -> None:
        """Ask where some files should go, then move them."""
        what = f"‘{leaf_name(paths[0])}’" if len(paths) == 1 else f"{len(paths)} files"
        dest = self._choose_folder_dialog(what, parent_folder(paths[0]))
        if dest is not None:
            self._move_selection(paths, [], dest)

    def _delete_forever_files(self, paths: list[str]) -> None:
        """Confirm, then destroy specific trashed files."""
        if not messagebox.askyesno(
            "Delete forever?",
            f"Permanently delete {len(paths)} item(s) from the server?\n\n"
            "This affects every machine and cannot be undone.",
            icon="warning",
            default="no",
        ):
            return
        self._delete_forever(paths, [])

    def _delete_forever_folder(self, folder: str) -> None:
        """Confirm, then destroy a trashed folder and everything under it."""
        under = paths_under(folder, self._files)
        if not messagebox.askyesno(
            "Delete forever?",
            f"Permanently delete /{folder} and its {len(under)} item(s) from the "
            "server?\n\nThis affects every machine and cannot be undone.",
            icon="warning",
            default="no",
        ):
            return
        self._delete_forever(under, subfolder_entries([folder], self._folders))

    def _download_folder(self, folder: str) -> None:
        self._download([], [folder])

    def _download_root(self) -> Path | None:
        """The download folder; asks for one first if none is set yet."""
        if self._config is not None and not self._config.download_dir:
            messagebox.showinfo(
                "Choose a download folder", "Pick the folder downloads are saved into."
            )
            self._open_settings(first_run=True)
        if self._config is None or not self._config.download_dir:
            return None
        return Path(self._config.download_dir)

    def _download(self, files: list[str], folders: list[str]) -> None:
        """Check the chosen items against the download folder, then queue them.

        This is the only time the app looks at the local disk. A folder whose
        name is already there is refused. For files whose name is already there
        the user picks Overwrite, Keep both or Skip — once, for all of them.
        Everything else goes ahead.
        """
        if self._busy or self.api is None or not (files or folders):
            return
        root = self._download_root()
        if root is None:
            return
        if not root.is_dir():
            messagebox.showerror(
                "Download folder missing",
                f"{root}\n\ndoesn't exist any more. Choose another one in ⚙ Settings.",
            )
            return
        try:
            plan = plan_downloads(root, files, folders, self._files, self._folders)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot download", str(exc))
            return

        # Asking for a file that's already queued or downloading does nothing.
        queued = self._downloading
        already = sum(1 for i in plan.items + plan.file_clashes if i.path in queued)
        plan.items = [i for i in plan.items if i.path not in queued]
        plan.file_clashes = [i for i in plan.file_clashes if i.path not in queued]

        if plan.folder_clashes:
            names = "\n".join(
                f"• Folder “{leaf_name(f)}” already exists" for f in plan.folder_clashes
            )
            messagebox.showwarning(
                "Folder already exists",
                f"{names}\n\nin {root}, so it was not downloaded.",
            )
        if plan.file_clashes:
            choice = self._ask_clash_choice(plan.file_clashes, root)
            if choice is None:
                self.status.set("Download cancelled.")
                return
            resolve_clashes(plan, choice)

        if not plan.items and not plan.dirs:
            if already:
                self.status.set(
                    f"{leaf_name(files[0])} is already in the queue." if already == 1
                    else f"Those {already} file(s) are already in the queue."
                )
            elif not plan.folder_clashes:
                self.status.set("Nothing downloaded.")
            return

        new_folders = [f for f in folders if f not in plan.folder_clashes]
        if len(new_folders) == 1 and not files:
            label = f"Download /{new_folders[0]} ({len(plan.items)})"
        elif len(plan.items) == 1 and not new_folders:
            label = f"Download {leaf_name(plan.items[0].path)}"
        else:
            label = f"Download {len(plan.items)} files"
        self._queue_download(plan, root, label, already)

    def _ask_clash_choice(self, clashes: list[PlannedDownload], root: Path) -> str | None:
        """One question for every file whose name is taken: "overwrite", "keep",
        "skip", or None if cancelled."""
        n = len(clashes)
        names = "\n".join(f"• {c.dest.name}" for c in clashes[:12])
        if n > 12:
            names += f"\n…and {n - 12} more"
        head = (
            "A file with this name already exists" if n == 1
            else f"{n} files with these names already exist"
        )
        choice = self._ask_choice(
            "File already exists",
            f"{head} in {root}:\n\n{names}\n\nWhat should happen"
            f"{' to all of them' if n > 1 else ''}?",
            ["Overwrite", "Keep both (save as “name (1)”)", "Skip"],
        )
        return None if choice is None else ("overwrite", "keep", "skip")[choice]

    def _queue_download(
        self, plan: DownloadPlan, root: Path, label: str, already: int
    ) -> None:
        """Queue a planned download on the transfer lane.

        The window stays usable, the Tasks strip shows overall progress and the
        file in flight, and Stop abandons the current file (its ``.part`` is
        removed) and skips the rest.
        """
        items = list(plan.items)
        dirs = list(plan.dirs)
        paths = [i.path for i in items]
        n = len(items)
        sizes = {p: (self._files[p].size if p in self._files else 0) for p in paths}
        total_bytes = sum(sizes.values())

        def body(cancelled: threading.Event) -> None:
            self._ui(self._update_task, TASK_TRANSFER, 0, None)
            current = {"name": ""}
            progress = self._task_progress(TASK_TRANSFER, lambda: current["name"])
            errors: list[tuple[str, str]] = []
            failed = 0
            # Folders first, so a folder's empty sub-folders come along too.
            for folder in dirs:
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    errors.append((str(folder), str(exc)))
            done_bytes = 0
            stopped = False
            for item in items:
                if cancelled.is_set():
                    stopped = True
                    break
                current["name"] = leaf_name(item.path)
                base = done_bytes

                def cb(sent: int, _t: int, base: int = base) -> None:
                    progress(base + sent, total_bytes or 1)

                try:
                    # The name is settled as the file is put in place, not
                    # before it streams, so one taken meanwhile is kept.
                    self.api.download_to_file(
                        item.path, item.dest, cb, cancelled.is_set,
                        place=lambda _dest, item=item: write_target(item),
                    )
                except Cancelled:
                    stopped = True
                    break
                except Exception as exc:
                    errors.append((item.path, str(exc)))
                    failed += 1
                done_bytes += sizes[item.path]
                progress(done_bytes, total_bytes or 1)
            self._ui(self._download_batch_done, stopped, n, failed, errors, paths, root)

        self._downloading.update(paths)
        self._enqueue_transfer(label, body, meta={"paths": paths})
        note = f" — {already} already in the queue" if already else ""
        self.status.set(f"Queued {n} file(s) for download{note}.")

    def _download_batch_done(
        self,
        stopped: bool,
        n: int,
        failed: int,
        errors: list[tuple[str, str]],
        claimed: list[str],
        root: Path,
    ) -> None:
        self._finish_task(TASK_TRANSFER)
        self._release_downloads(claimed)
        ok = n - failed
        if stopped:
            self.status.set("Download stopped — files already saved are kept.")
        elif errors:
            self.status.set(f"Downloaded {ok} of {n}.")
            messagebox.showwarning(
                "Some files were not downloaded",
                f"Downloaded {ok} of {n}.\n\n{error_list(errors)}",
            )
        else:
            self.status.set(f"Downloaded {n} file(s) to {root}.")

    # -- open the download folder --

    def _help_text(self) -> str:
        where = (
            self._config.download_dir
            if self._config is not None and self._config.download_dir
            else "the download folder (choose one in ⚙ Settings)"
        )
        return (
            "Open folders to browse; drag files or folders onto a folder to move "
            "them (Ctrl/⌘ or Shift selects several); right-click for more. "
            f"Deleted items go to {TRASH} (top right), so they can be restored. "
            f"Downloads land in {where}."
        )

    def _open_download_folder(self) -> None:
        root = self._download_root()
        if root is None:
            return
        try:
            open_in_explorer(root)
        except Exception as exc:
            messagebox.showerror("Could not open folder", f"{root}:\n\n{exc}")

    # -- settings (server URL + API key) --

    def _open_settings(self, first_run: bool = False) -> None:
        if self._busy:
            return
        if self._lanes_busy():
            messagebox.showinfo(
                "Settings are locked",
                "Settings can be changed once the running and queued jobs have "
                "finished (or been stopped).",
            )
            return
        cur = self._config
        # A config.toml that didn't load still offers what it could be read
        # for, so the user corrects it rather than starting from nothing.
        shown = cur or self._config_draft
        result = self._settings_dialog(
            shown.server_url if shown else "",
            shown.api_key if shown else "",
            shown.download_dir if shown else "",
            first_run,
            notice=self._config_error if cur is None else None,
        )
        if result is None:
            if first_run and self._config is None:
                self.status.set(
                    f"{self._config_error} — click ⚙ to enter the settings again."
                    if self._config_error
                    else "Not configured — click ⚙ to enter the server URL and API key."
                )
            elif first_run and not self._config.download_dir:
                self.status.set(
                    "No download folder yet — click ⚙ to choose where downloads go."
                )
            return
        url, key, download_dir = result
        try:
            path = save_config(url, key, download_dir)
        except Exception as exc:
            messagebox.showerror("Could not save settings", str(exc))
            return

        # Apply the new settings to the running app.
        server_changed = cur is None or (url, key) != (cur.server_url, cur.api_key)
        self._config = ClientConfig(
            server_url=url, api_key=key, download_dir=download_dir
        )
        self._config_error = None  # the file just written is a good one
        self._config_draft = None
        self.help_var.set(self._help_text())
        self.status.set(f"Saved settings to {path}.")
        if not server_changed:
            return  # only the download folder changed; the view stays put
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
        self.api = StorageAPI(url, key)
        self.server_label_var.set(f"Files on {url}")
        self._cwd = ""
        self._files = {}  # the old server's list, until the new one answers
        self._folders = []
        self.search_var.set("")
        self._render()
        self._render_disk()
        self._set_busy(False)  # re-enable the actions now that we're configured
        if self._poll_job is None:
            self._load_initial()  # first-ever configuration → start loading + polling
        else:
            self._poll_now(silent=False)

    def _settings_dialog(
        self,
        initial_url: str,
        initial_key: str,
        initial_dir: str,
        first_run: bool,
        notice: str | None = None,
    ) -> tuple[str, str, str] | None:
        """Modal screen for the server URL, API key and download folder.
        ``notice`` is shown in the error line from the start (e.g. why the
        config file didn't load). Returns (url, key, download folder) or None."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Server settings")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg)
        frm.pack(fill="both", expand=True, padx=16, pady=14)

        if first_run:
            ttk.Label(
                frm,
                text=(
                    "The config file couldn't be used — check these settings."
                    if notice
                    else "Welcome — point the app at your storage server."
                    if not initial_url
                    else "Choose the folder your downloads are saved into."
                ),
                font=UI_FONT_BOLD,
            ).pack(anchor="w", pady=(0, 8))

        ttk.Label(frm, text="Server URL", font=UI_FONT_BOLD).pack(anchor="w")
        url_var = tk.StringVar(value=initial_url)
        url_entry = ttk.Entry(frm, textvariable=url_var, width=44)
        url_entry.pack(fill="x", pady=(2, 0))
        ttk.Label(
            frm,
            text="e.g. http://192.168.1.50:8000 or http://server-laptop:8000",
            foreground=FG,
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(frm, text="API key", font=UI_FONT_BOLD).pack(anchor="w")
        key_var = tk.StringVar(value=initial_key)
        key_entry = ttk.Entry(frm, textvariable=key_var, width=44)
        key_entry.pack(fill="x", pady=(2, 0))
        ttk.Label(
            frm,
            text="The same key as the server (STORAGE_API_KEY).",
            foreground=FG,
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(frm, text="Download folder", font=UI_FONT_BOLD).pack(anchor="w")
        dir_row = ttk.Frame(frm)
        dir_row.pack(fill="x", pady=(2, 0))
        dir_var = tk.StringVar(value=initial_dir)
        ttk.Entry(dir_row, textvariable=dir_var, width=34).pack(
            side="left", fill="x", expand=True
        )

        def browse() -> None:
            chosen = filedialog.askdirectory(
                parent=dlg,
                title="Choose the download folder",
                initialdir=dir_var.get() or str(Path.home()),
                mustexist=True,
            )
            if chosen:
                dir_var.set(str(Path(chosen)))

        ttk.Button(dir_row, text="Browse…", command=browse).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            frm,
            text="Downloads are saved here. The app never tracks them afterwards.",
            foreground=FG,
        ).pack(anchor="w", pady=(0, 8))

        err_var = tk.StringVar(value=notice or "")
        ttk.Label(frm, textvariable=err_var, foreground=RED, wraplength=420).pack(
            anchor="w"
        )

        result: dict[str, tuple[str, str, str] | None] = {"value": None}

        def save() -> None:
            raw_url = url_var.get().strip()
            key = key_var.get().strip()
            folder = dir_var.get().strip()
            if not raw_url or not key:
                err_var.set("Enter both a server URL and an API key.")
                return
            if not key.isascii():
                err_var.set("That API key has characters no server key can contain.")
                return
            # "server-laptop:8000" gains its http://, like the Windows installer.
            url = normalize_server_url(raw_url)
            if url is None:
                err_var.set("That isn't a server address — e.g. http://server-laptop:8000")
                return
            if not folder:
                err_var.set("Choose a download folder.")
                return
            if not Path(folder).expanduser().is_dir():
                err_var.set("That download folder doesn't exist.")
                return
            result["value"] = (url, key, str(Path(folder).expanduser()))
            dlg.destroy()

        def cancel() -> None:
            result["value"] = None
            dlg.destroy()

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Save", command=save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))
        dlg.bind("<Return>", lambda _e: save())
        dlg.bind("<Escape>", lambda _e: cancel())
        (url_entry if not initial_url else key_entry).focus_set()
        if first_run and initial_url and not initial_dir and not notice:
            dlg.after(100, browse)  # the folder is the only thing missing
        dlg.grab_set()
        self.root.wait_window(dlg)
        return result["value"]

    # -- create folder --

    def _new_folder(self) -> None:
        if self._busy:
            return
        where = f"/{self._cwd}" if self._cwd else "/ (root)"
        name = simpledialog.askstring(
            "New folder", f"Name of the new folder inside {where}:", parent=self.root
        )
        if not name:
            return
        try:
            rel = normalize_rel_path(f"{self._cwd}/{name}" if self._cwd else name)
        except Exception as exc:
            messagebox.showerror("Invalid name", str(exc))
            return
        if is_reserved_trash_name(rel):
            messagebox.showerror(
                "Invalid name",
                f"“{TRASH}” is reserved for deleted items (names ignore case).",
            )
            return

        self.status.set(f"Creating folder {rel}…")

        def work() -> None:
            err = None
            try:
                self.api.create_folder(rel)
            except Exception as exc:
                err = str(exc)
            self.root.after(0, self._new_folder_done, err, rel)

        threading.Thread(target=work, daemon=True).start()

    def _new_folder_done(self, error: str | None, rel: str) -> None:
        if error:
            messagebox.showerror("Could not create folder", error)
        else:
            self._cwd = rel  # step into the folder just created
            self.status.set(f"Created folder /{rel}.")
        self._poll_now(silent=True)

    # -- move a file into a folder --

    def _choose_folder_dialog(self, what: str, current: str) -> str | None:
        """Modal picker for a destination folder. Returns "" (root), a path, or None."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Move")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        ttk.Label(dlg, text=f"Move {what} to folder:").pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        options = ["(root)"] + [
            f for f in all_folders(self._files.keys(), self._folders) if not in_trash(f)
        ]
        var = tk.StringVar(value=current if current else "(root)")
        combo = ttk.Combobox(dlg, textvariable=var, values=options, width=44)
        combo.pack(fill="x", padx=12)
        ttk.Label(
            dlg,
            text="Pick an existing folder, or type a new folder path.",
            foreground=FG,
        ).pack(anchor="w", padx=12, pady=(2, 8))

        result: dict[str, str | None] = {"value": None}

        def ok() -> None:
            v = var.get().strip()
            try:
                result["value"] = "" if v == "(root)" else typed_folder(v)
            except ValueError as exc:
                messagebox.showerror("Invalid folder", str(exc), parent=dlg)
                return
            dlg.destroy()

        def cancel() -> None:
            result["value"] = None
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btns, text="Move", command=ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))
        dlg.bind("<Return>", lambda _e: ok())
        dlg.bind("<Escape>", lambda _e: cancel())
        combo.focus_set()
        dlg.grab_set()
        self.root.wait_window(dlg)
        return result["value"]

    def _move_selected(self) -> None:
        """Move the selection — files, folders, or a mix — into another folder."""
        if self.api is None:
            return
        files = self._selected_file_paths()
        folders = self._selected_folder_paths()
        if not files and not folders:
            messagebox.showinfo(
                "Select something", "Select one or more files or folders to move."
            )
            return
        count = len(files) + len(folders)
        first = (files or folders)[0]
        what = f"‘{leaf_name(first)}’" if count == 1 else f"{count} items"
        dest = self._choose_folder_dialog(what, parent_folder(first))
        if dest is None:
            return  # cancelled
        self._move_selection(files, folders, dest)

    def _move_folder(self, src_folder: str, dest: str) -> None:
        """Nest a whole folder under ``dest``, keeping its own name."""
        if dest == src_folder or dest.startswith(src_folder + "/"):
            self.status.set("A folder can't be moved into itself.")
            return
        if parent_folder(src_folder) == dest:
            self.status.set(f"‘{leaf_name(src_folder)}’ is already there.")
            return
        self._move_selection([], [src_folder], dest)

    def _move_selection(
        self, sel_files: list[str], sel_folders: list[str], dest: str
    ) -> None:
        """Queue the moves a selection implies; folders bring their contents."""
        if self.api is None:
            return
        if in_trash(dest):
            messagebox.showinfo(
                "Use Delete", f"To put things in {TRASH}, use Delete (or drop them on "
                f"the {TRASH} button)."
            )
            return
        if is_reserved_trash_name(dest):
            messagebox.showerror(
                "Invalid folder",
                f"“{dest.split('/', 1)[0]}” is reserved for deleted items (names "
                "ignore case). Pick another folder.",
            )
            return
        moves, folder_moves, skipped = plan_selection_move(
            sel_files, sel_folders, dest, self._files, self._folders
        )
        if not moves and not folder_moves:
            if skipped:
                messagebox.showinfo("Nothing moved", error_list(skipped))
            else:
                self.status.set("Nothing to move — it's already there.")
            return
        n = len(moves)
        count = max(n, len(folder_moves))
        total = count + len(skipped)  # skipped items show up as errors

        def body(cancelled: threading.Event) -> None:
            self._ui(self._update_task, TASK_MOVE, 0, None)
            errors: list[tuple[str, str]] = list(skipped)
            # Explicit folders aren't carried across by their files (and an
            # empty one has none): recreate each at the destination first. One
            # whose copy fails keeps its original, so no folder is lost.
            kept: set[str] = set()
            for src, dst in folder_moves:
                try:
                    self.api.create_folder(dst)
                except Exception as exc:
                    errors.append((f"{src} → {dst}", str(exc)))
                    kept.add(src)
            state = {"pct": -1}
            done = 0
            left: list[str] = []  # files still at their source
            for src, dst in moves:
                if cancelled.is_set():
                    left.append(src)
                    continue  # finish the current item, skip the rest
                try:
                    self.api.move(src, dst)
                except Exception as exc:
                    errors.append((f"{src} → {dst}", str(exc)))
                    left.append(src)
                done += 1
                self._post_task(TASK_MOVE, state, done, n)
            # Then drop the old folder entries, deepest first, except any kept
            # above or still holding one of those or a file that didn't move.
            for src, _dst in sorted(folder_moves, key=lambda m: m[0], reverse=True):
                if src in kept or any(p.startswith(src + "/") for p in [*left, *kept]):
                    continue
                try:
                    self.api.delete_folder(src)
                except Exception as exc:
                    errors.append((src, str(exc)))
            self._ui(self._move_done, cancelled.is_set(), "Moved", total, errors)

        self._enqueue_op(f"Move {count} item(s)", body)

    def _move_done(
        self, stopped: bool, verb: str, n: int, errors: list[tuple[str, str]]
    ) -> None:
        self._finish_task(TASK_MOVE)
        self._finish_job(stopped, verb, n, errors)

    def _bulk_done(self, verb: str, n: int, errors: list[tuple[str, str]]) -> None:
        ok = max(0, n - len(errors))
        if errors:
            self.status.set(f"{verb} {ok} of {n}.")
            messagebox.showwarning(
                f"Some items were not {verb.lower()}",
                f"{verb} {ok} of {n}.\n\n{error_list(errors)}",
            )
        else:
            self.status.set(f"{verb} {n} item{'s' if n != 1 else ''}.")
        self._poll_now(silent=True)

    # -- delete: everything goes to Trash first, so it can be undone --

    def _delete_selected(self) -> None:
        """Move the selection — files alone, or files plus whole folders — to Trash."""
        if self.api is None:
            return
        files = self._selected_file_paths()
        folders = self._selected_folder_paths()
        if not files and not folders:
            messagebox.showinfo(
                "Nothing selected", "Select one or more files, or a folder."
            )
            return
        self._trash_items(files, folders)

    def _trash_items(self, files: list[str], folders: list[str]) -> None:
        """Confirm, then move files and whole folders to Trash."""
        if self.api is None or not (files or folders):
            return
        if not folders:  # plain files
            self._delete_files(files)
            return
        if len(folders) == 1 and not files:
            self._delete_folder(folders[0])  # keeps its stop-and-undo behaviour
            return
        count = len({p for f in folders for p in paths_under(f, self._files)} | set(files))
        if not self._confirm_trash(f"{count} file(s)"):
            return
        self._delete_selection(files, folders)

    def _confirm_trash(self, what: str) -> bool:
        return messagebox.askyesno(
            "Move to Trash?",
            f"Move {what} to /{TRASH}?\n\nThey're removed from the server for every "
            "machine; you can restore them from Trash later. Files you've "
            "downloaded are not touched.",
            icon="warning",
            default="no",
        )

    def _delete_files(self, paths: list[str]) -> None:
        """Confirm, then move files to Trash."""
        if not paths:
            return
        n = len(paths)
        if not self._confirm_trash(f"'{paths[0]}'" if n == 1 else f"{n} files"):
            return
        self._delete_selection(paths, [])

    def _trash_one(self, src: str, dst: str) -> list[tuple[str, str]]:
        """Move one file to its place in Trash. Returns any error."""
        try:
            self.api.move(src, dst)
        except Exception as exc:
            return [(src, str(exc))]
        return []

    def _delete_selection(self, files: list[str], folders: list[str]) -> None:
        """Queue moving files and whole folders to Trash.

        Where everything lands is worked out now, from the current file list, so
        nothing in Trash is ever overwritten (see :func:`plan_trash`).
        """
        if self._trash_blocked():
            return
        moves, targets = plan_trash(files, folders, self._files, self._folders)
        # Explicit folders (empty ones included) have no files to carry them
        # across: each is recreated in Trash, so Restore can bring it back.
        folder_moves = trash_folder_moves(targets, self._folders)
        n = len(moves)
        count = max(n, len(folder_moves))

        def body(cancelled: threading.Event) -> None:
            self._ui(self._update_task, TASK_DELETE, 0, None)
            errors: list[tuple[str, str]] = []
            state = {"pct": -1}
            done = 0
            for src, dst in moves:
                if cancelled.is_set():
                    break  # finish the current item, skip the rest
                errors += self._trash_one(src, dst)
                done += 1
                self._post_task(TASK_DELETE, state, done, n)
            if not cancelled.is_set():
                self._carry_folders(folder_moves, errors)
            self._ui(
                self._delete_done, cancelled.is_set(), "Moved to Trash", count, errors,
            )

        self._enqueue_op(f"Trash {count} item(s)", body)

    def _trash_blocked(self) -> bool:
        """Say so, and return True, when a top-level item holds Trash's name
        (see :func:`trash_blocker`): the server would refuse every move into
        Trash."""
        name = trash_blocker(self._files, self._folders)
        if name is None:
            return False
        messagebox.showerror(
            f"Can't move to {TRASH}",
            f"“{name}” at the top level has the same name as {TRASH} (names "
            f"ignore case), so nothing can be moved to {TRASH}.\n\nMove “{name}” "
            "into another folder first, then delete again.",
        )
        return True

    def _carry_folders(
        self, folder_moves: list[tuple[str, str]], errors: list[tuple[str, str]]
    ) -> None:
        """Recreate explicit folders at their new paths, then drop the originals
        deepest first. An original whose copy couldn't be made is kept, so no
        folder is ever lost; one still holding files is refused by the server."""
        kept: set[str] = set()
        for src, dst in sorted(folder_moves):
            try:
                self.api.create_folder(dst)
            except Exception as exc:
                errors.append((dst, str(exc)))
                kept.add(src)
        for src, _dst in sorted(folder_moves, reverse=True):
            if src in kept:
                continue
            try:
                self.api.delete_folder(src)
            except Exception:
                pass

    def _delete_done(
        self, stopped: bool, verb: str, n: int, errors: list[tuple[str, str]]
    ) -> None:
        self._finish_task(TASK_DELETE)
        self._delete_rolls_back = False
        self._finish_job(stopped, verb, n, errors)

    def _finish_job(
        self, stopped: bool, verb: str, n: int, errors: list[tuple[str, str]]
    ) -> None:
        """Close out a job: the usual summary, or a note that it was stopped."""
        if stopped:
            self.status.set("Stopped — work already done is kept.")
            self._poll_now(silent=True)
        else:
            self._bulk_done(verb, n, errors)

    # -- delete a folder (contents included) --

    def _delete_folder(self, folder: str) -> None:
        """Delete a folder: the empty case straight off, otherwise its contents."""
        if self.api is None:
            return
        under = paths_under(folder, self._files)
        if not under:
            if messagebox.askyesno(
                "Delete folder?",
                f"Move the empty folder '/{folder}' to /{TRASH}?",
                icon="warning",
                default="no",
            ):
                self._delete_selection([], [folder])
            return
        if not self._confirm_trash(f"/{folder} and its {len(under)} file(s)"):
            return
        self._delete_folder_contents(folder)

    def _delete_folder_contents(self, folder: str) -> None:
        """Delete every file under a folder, then drop the emptied shells.

        Stopping this part-way puts back whatever already reached Trash, so the
        folder is never left split between Trash and where it was.
        """
        if self._trash_blocked():
            return
        moves, targets = plan_trash([], [folder], self._files, self._folders)
        folder_moves = trash_folder_moves(targets, self._folders)
        prefix = folder + "/"
        n = len(moves)

        def body(cancelled: threading.Event) -> None:
            self._ui(self._start_delete_task)
            # Moves already made, in order, so a stop can undo them.
            trashed: list[tuple[str, str]] = []
            errors: list[tuple[str, str]] = []
            state = {"pct": -1}
            done = 0
            for src, dst in moves:
                if cancelled.is_set():
                    break  # finish the current item, skip the rest
                failed = self._trash_one(src, dst)
                errors += failed
                if not failed:
                    trashed.append((src, dst))
                done += 1
                self._post_task(TASK_DELETE, state, done, n)

            if cancelled.is_set():
                # Stopped part-way → undo it. The shells are still there (they
                # only go once a delete finishes), so everything lands back
                # exactly where it was.
                self._restore_after_stop(trashed)
                return

            # Files moved to Trash, so the originals are empty → recreate the
            # explicit folders in Trash and drop them here.
            self._carry_folders(folder_moves, errors)
            self._ui(
                self._folder_contents_done, folder, prefix, "Moved to Trash", n, errors
            )

        self._enqueue_op(f"Delete /{folder}", body)

    def _start_delete_task(self) -> None:
        """Begin a folder-delete row; stopping it undoes the work."""
        self._delete_rolls_back = True
        self._update_task(TASK_DELETE, 0, None)

    def _folder_contents_done(
        self,
        folder: str,
        prefix: str,
        verb: str,
        n: int,
        errors: list[tuple[str, str]],
    ) -> None:
        self._finish_task(TASK_DELETE)
        self._delete_rolls_back = False
        # The folder is empty now — climb out of it if we're standing inside.
        if self._cwd == folder or self._cwd.startswith(prefix):
            self._cwd = parent_folder(folder)
        self._bulk_done(verb, n, errors)

    def _restore_after_stop(self, trashed: list[tuple[str, str]]) -> None:
        """Undo the part of a stopped folder delete that already happened.

        Runs on the ops worker thread, and deliberately has no cancel of its own:
        once a delete is stopped, putting its files back always runs to the end.
        """
        self._ui(self._end_delete_task)
        total = len(trashed)
        if not total:  # nothing had moved yet
            self._ui(self._nothing_to_restore)
            return
        self._ui(self._update_task, TASK_MOVE, 0, "Restoring…")
        errors: list[tuple[str, str]] = []
        state = {"pct": -1}
        done = 0
        for src, dst in reversed(trashed):  # last deleted, first back
            try:
                self.api.move(dst, src)
            except Exception as exc:
                errors.append((src, str(exc)))
            done += 1
            self._post_task(TASK_MOVE, state, done, total, "Restoring…")
        self._ui(self._restore_after_stop_done, total, errors)

    def _end_delete_task(self) -> None:
        self._delete_rolls_back = False
        self._finish_task(TASK_DELETE)

    def _nothing_to_restore(self) -> None:
        self.status.set("Stopped — nothing had been deleted yet.")
        self._poll_now(silent=True)

    def _restore_after_stop_done(
        self, total: int, errors: list[tuple[str, str]]
    ) -> None:
        self._finish_task(TASK_MOVE)
        if errors:
            self.status.set(
                f"Stopped — put {total - len(errors)} of {total} item(s) back."
            )
            messagebox.showwarning("Could not restore everything", error_list(errors))
        else:
            self.status.set(f"Stopped — put {total} item(s) back.")
        self._poll_now(silent=True)

    # -- Trash: restore, delete forever, empty --

    def _restore_selected(self) -> None:
        paths = self._selection_files()
        folder_entries = subfolder_entries(self._selected_folder_paths(), self._folders)
        if not paths and not folder_entries:
            messagebox.showinfo("Nothing selected", "Select what to restore.")
            return
        self._restore(paths, folder_entries)

    def _restore_folder(self, folder: str) -> None:
        """Restore a trashed folder: its files and its explicit (sub)folders."""
        self._restore(
            paths_under(folder, self._files), subfolder_entries([folder], self._folders)
        )

    def _restore(self, paths: list[str], folder_entries: list[str] = ()) -> None:
        """Move trashed items back to where they came from (see plan_restore).

        Explicit folders in Trash (empty ones included) come back too: each is
        recreated where it came from and its Trash entry dropped once empty.
        """
        origins = trash_origins(self._files.values())
        moves = plan_restore(paths, self._files, self._folders, origins)
        # Folders are spelt as the server and the files coming back spell them.
        folder_moves = plan_restore_folders(
            list(folder_entries), origins,
            [*self._files, *(dst for _src, dst in moves)], self._folders,
        )
        n = len(moves)
        count = max(n, len(folder_moves))

        def body(cancelled: threading.Event) -> None:
            self._ui(self._update_task, TASK_MOVE, 0, None)
            errors: list[tuple[str, str]] = []
            state = {"pct": -1}
            done = 0
            for path, dst in moves:
                if cancelled.is_set():
                    break
                try:
                    self.api.move(path, dst)
                except Exception as exc:
                    errors.append((f"{path} → {dst}", str(exc)))
                done += 1
                self._post_task(TASK_MOVE, state, done, n)
            if not cancelled.is_set():
                self._carry_folders(folder_moves, errors)
            self._ui(self._move_done, cancelled.is_set(), "Restored", count, errors)

        self._enqueue_op(f"Restore {count} item(s)", body)

    def _delete_forever_selected(self) -> None:
        paths = self._selection_files()
        folder_entries = subfolder_entries(self._selected_folder_paths(), self._folders)
        if not paths and not folder_entries:
            messagebox.showinfo("Nothing selected", "Select what to delete forever.")
            return
        if not messagebox.askyesno(
            "Delete forever?",
            f"Permanently delete {len(paths)} item(s) from the server?\n\n"
            "This affects every machine and cannot be undone.",
            icon="warning",
            default="no",
        ):
            return
        self._delete_forever(paths, folder_entries)

    def _delete_forever(self, paths: list[str], folder_entries: list[str]) -> None:
        """The irreversible step: drop items from the server for good."""
        n = len(paths)
        count = max(n, len(folder_entries))

        def body(cancelled: threading.Event) -> None:
            self._ui(self._update_task, TASK_DELETE, 0, None)
            errors: list[tuple[str, str]] = []
            state = {"pct": -1}
            done = 0
            for path in paths:
                if cancelled.is_set():
                    break
                try:
                    self.api.delete(path)
                except Exception as exc:
                    errors.append((path, str(exc)))
                done += 1
                self._post_task(TASK_DELETE, state, done, n)
            if not cancelled.is_set():
                for entry in folder_entries:
                    try:
                        self.api.delete_folder(entry)
                    except Exception:
                        pass
            self._ui(self._delete_done, cancelled.is_set(), "Deleted", count, errors)

        self._enqueue_op(f"Delete {count} forever", body)

    def _empty_trash(self) -> None:
        paths = paths_under(TRASH, self._files)
        folder_entries = subfolder_entries([TRASH], self._folders)
        if not paths and not folder_entries:
            messagebox.showinfo("Trash is empty", f"/{TRASH} has nothing in it.")
            return
        if not messagebox.askyesno(
            "Empty Trash?",
            f"Permanently delete everything in /{TRASH} from the server?\n\n"
            "This affects every machine and cannot be undone.",
            icon="warning",
            default="no",
        ):
            return
        if self._in_trash():
            self._cwd = ""  # step out; the folder is going away
        self._delete_forever(paths, folder_entries)

    # -- upload new files (into the current folder) --

    def _on_drop(self, event) -> None:
        if self._busy:
            return
        self._confirm_and_upload(list(self.root.tk.splitlist(event.data)))

    def _add_files_dialog(self) -> None:
        if self._busy:
            return
        chosen = filedialog.askopenfilenames(title="Choose files to upload")
        if chosen:
            self._confirm_and_upload(list(chosen))

    def _confirm_and_upload(self, raw_paths: list[str]) -> None:
        if self.api is None:
            return
        if self._in_trash():
            messagebox.showinfo(
                "Can't upload here",
                f"/{TRASH} only holds deleted items. Open another folder to upload.",
            )
            return
        # Uploads never replace anything: a name that's already used in this
        # folder is refused, and the rest go ahead.
        raw_paths, clashes = split_upload_clashes(
            raw_paths, self._cwd, self._files, self._folders
        )
        if clashes:
            where = f"/{self._cwd}" if self._cwd else "the root folder"
            lines = [
                f"• Folder “{c[:-1]}” already exists" if c.endswith("/")
                else f"• File “{c}” already exists"
                for c in clashes[:12]
            ]
            if len(clashes) > 12:
                lines.append(f"…and {len(clashes) - 12} more")
            messagebox.showwarning(
                "Already exists",
                "\n".join(lines) + f"\n\nin {where}, so "
                + ("it was" if len(clashes) == 1 else "they were")
                + " not uploaded.",
            )
            if not raw_paths:
                self.status.set("Nothing uploaded.")
                return
        try:
            pairs = collect_uploads(raw_paths)
        except Exception as exc:
            messagebox.showerror("Cannot read selection", str(exc))
            return
        if not pairs:
            messagebox.showinfo("Nothing to upload", "No files found in that drop.")
            return

        # Land uploads inside the folder currently being viewed.
        if self._cwd:
            pairs = [
                (normalize_rel_path(f"{self._cwd}/{sp}"), lp) for sp, lp in pairs
            ]

        # Refuse the whole batch up front if any file is over the server's cap,
        # rather than streaming it only to be rejected part-way through.
        try:
            too_big = oversized_uploads(pairs, self.api.max_file_bytes)
        except OSError as exc:
            messagebox.showerror("Cannot read selection", str(exc))
            return
        if too_big:
            lines = [f"• {p} ({human_size(n)})" for p, n in too_big[:12]]
            if len(too_big) > 12:
                lines.append(f"…and {len(too_big) - 12} more")
            messagebox.showerror(
                "File too large",
                "\n".join(lines)
                + f"\n\nThe server's limit is {human_size(self.api.max_file_bytes)}"
                " per file. Nothing was uploaded.",
            )
            self.status.set("Upload blocked: file too large.")
            return

        names = [server_path for server_path, _ in pairs]
        preview = "\n".join(f"• {n}" for n in names[:15])
        if len(names) > 15:
            preview += f"\n…and {len(names) - 15} more"
        where = f"/{self._cwd}" if self._cwd else "the root folder"
        if not messagebox.askyesno(
            "Upload to server?",
            f"Upload {len(names)} file(s) into {where}?\n\n{preview}",
        ):
            self.status.set("Upload cancelled.")
            return

        total = len(pairs)

        def body(cancelled: threading.Event) -> None:
            self._ui(self._update_task, TASK_TRANSFER, 0, None)
            current = {"name": ""}
            progress = self._task_progress(TASK_TRANSFER, lambda: current["name"])

            def tracked(sent: int, sent_total: int) -> None:
                progress(sent, sent_total)

            # Name the file in flight in the task row as the batch walks along.
            def each(pair_index: int) -> None:
                current["name"] = leaf_name(pairs[pair_index][0])

            uploaded, failed, stopped = upload_files(
                self.api, pairs, tracked, cancelled.is_set, each
            )
            self._ui(self._upload_done, stopped, uploaded, failed, total)

        self._enqueue_transfer(f"Upload {total} file(s)", body)
        self.status.set(f"Queued {total} file(s) for upload into {where}.")

    def _upload_done(
        self,
        stopped: bool,
        uploaded: list[str],
        failed: list[tuple[str, str]],
        total: int,
    ) -> None:
        self._finish_task(TASK_TRANSFER)
        if stopped:
            self.status.set(
                f"Upload stopped — {len(uploaded)} of {total} file(s) sent."
            )
        else:
            self.status.set(f"Uploaded {len(uploaded)} file(s).")
        if failed:
            messagebox.showwarning(
                "Some files were not uploaded",
                f"Uploaded {len(uploaded)} of {total}.\n\n{error_list(failed)}",
            )
        self._poll_now(silent=True)  # show the newly uploaded files

    def request_close(self) -> None:
        """The window's close button: with jobs running, ask first, then stop
        them and close once they've wound up, so no ``.part`` file or half-moved
        folder stays behind. A second click stops waiting."""
        if self._closing:
            self._finish_close()
            return
        if self._lanes_busy() and not messagebox.askyesno(
            "Stop running jobs?",
            "Jobs are still running.\n\nStop them and close? A folder delete that's "
            "under way is put back first.",
            default="no",
        ):
            return
        self._closing = True
        self._transfers.close()
        self._ops.close()
        self.status.set("Stopping jobs — close the window again to quit now.")
        self._close_when_idle()

    def _close_when_idle(self) -> None:
        # Polled, not joined: a job's last steps need this thread's event loop.
        if self._lanes_busy():
            self.root.after(100, self._close_when_idle)
        else:
            self._finish_close()

    def _finish_close(self) -> None:
        self.close()
        self.root.destroy()

    def close(self) -> None:
        # Stop queued work and ask anything in flight to wind up, so the window
        # closes instead of waiting on a long transfer.
        self._transfers.close()
        self._ops.close()
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
        if self.api is not None:
            self.api.close()


# The app's own identity on the Windows taskbar. Without it the window is
# grouped under pythonw.exe and shows Python's icon instead of the app's.
APP_USER_MODEL_ID = "AsyncStorageDOS.LocalDB"


def _set_app_id() -> None:
    """Give the process its own taskbar identity (Windows only; before any window)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass  # cosmetic; the app works without it


def _icon_dir() -> Path:
    """Where the icon files live: beside this module, or — in a PyInstaller
    build, where this file is the entry script at the bundle root — in the
    bundle's ``client`` folder (README: ``--add-data "client/app_icon.png;client"``)."""
    bundle = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle:
        return Path(bundle) / "client"
    return Path(__file__).parent


def _set_window_icon(root: tk.Tk) -> None:
    """Set the window/taskbar icon from the bundled icon files, if present."""
    try:
        ico_path = _icon_dir() / "app_icon.ico"
        if sys.platform.startswith("win") and ico_path.is_file():
            # The .ico carries every size, so the taskbar gets a crisp icon.
            root.iconbitmap(default=str(ico_path))
        icon_path = _icon_dir() / "app_icon.png"
        if icon_path.is_file():
            image = tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, image)
            root._app_icon = image  # keep a reference so Tk doesn't GC it
    except Exception:
        pass  # a missing/unsupported icon should never stop the app


def main() -> int:
    # A missing or incomplete config is no longer an error: the app opens a
    # settings screen on first run so the user never edits the file by hand.
    # A file that exists but can't be used says why on that screen, instead of
    # looking like a first run.
    config, error, draft = None, None, None
    try:
        config = load_config()
    except Exception as exc:
        path = default_config_path()
        if path.is_file():
            error = f"Couldn't use {path}: {exc}"
            draft = read_config_draft(path)

    _set_app_id()

    # TkinterDnD.Tk() is required for the drop target; fall back to plain Tk.
    root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
    _set_window_icon(root)
    app = FileBrowser(root, config, config_error=error, config_draft=draft)
    root.protocol("WM_DELETE_WINDOW", app.request_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
