"""Desktop client tests: the HTTP wrapper, the status command and the planning
rules behind moves, Trash and restore.

Run from the project root with ``python -m unittest discover tests``.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx

import client.__main__ as cli
from client.api import StorageAPI, describe_error
from client.config import ClientConfig, normalize_server_url
from common.hashing import FileEntry, normalize_rel_path
from server.db import ManifestDB  # stdlib only: a real manifest for the fake server

try:  # the planning rules live in the Tk app module
    from client import gui
except ImportError:  # no tkinter in this Python
    gui = None


def api_with(handler) -> StorageAPI:
    """A StorageAPI whose requests go to ``handler`` instead of the network."""
    api = StorageAPI("http://test", "key")
    api._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    return api


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def serve(self, body: bytes, sha: str | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, headers={
                "X-Content-SHA256": sha or hashlib.sha256(body).hexdigest(),
                "X-Mtime": "1700000000",
            })
        return api_with(handler)

    def test_download_leaves_a_users_part_file_alone(self):
        # F-003: a file of the user's named "<name>.part" must survive.
        mine = self.dir / "a.txt.part"
        mine.write_bytes(b"mine")
        self.serve(b"server bytes").download_to_file("a.txt", self.dir / "a.txt")
        self.assertEqual((self.dir / "a.txt").read_bytes(), b"server bytes")
        self.assertEqual(mine.read_bytes(), b"mine")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.txt", "a.txt.part"])
        self.assertEqual(os.path.getmtime(self.dir / "a.txt"), 1700000000)

    def test_hash_mismatch_removes_only_its_own_temp(self):
        mine = self.dir / "a.txt.part"
        mine.write_bytes(b"mine")
        with self.assertRaises(ValueError):
            self.serve(b"bytes", sha="0" * 64).download_to_file("a.txt", self.dir / "a.txt")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["a.txt.part"])
        self.assertEqual(mine.read_bytes(), b"mine")

    @unittest.skipIf(gui is None, "tkinter is not available")
    def test_a_name_taken_while_streaming_keeps_both(self):
        # F-057: a.txt saved by the user mid-download used to be replaced.
        body = b"server bytes"
        dest = self.dir / "a.txt"

        def stream():
            yield body[:4]
            dest.write_bytes(b"mine")  # taken after the name was checked
            yield body[4:]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=stream(), headers={
                "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
                "X-Mtime": "1700000000",
            })

        item = gui.PlannedDownload("a.txt", dest, False)
        written = api_with(handler).download_to_file(
            "a.txt", dest, place=lambda _d: gui.write_target(item)
        )
        self.assertEqual(written, self.dir / "a (1).txt")
        self.assertEqual(dest.read_bytes(), b"mine")
        self.assertEqual(written.read_bytes(), body)


class ErrorTextTest(unittest.TestCase):
    def test_rejected_key_is_not_reported_as_unreachable(self):
        # F-005: the app's status line uses describe_error.
        api = api_with(lambda r: httpx.Response(401, json={"detail": "Invalid"}))
        with self.assertRaises(httpx.HTTPStatusError) as caught:
            api.fetch_manifest()
        self.assertIn("API key", describe_error(caught.exception))

    def test_connection_failure_says_unreachable(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        with self.assertRaises(httpx.ConnectError) as caught:
            api_with(refuse).fetch_manifest()
        self.assertIn("Could not reach", describe_error(caught.exception))

    def test_delete_reports_the_servers_reason(self):
        # F-062: Delete forever showed httpx's text and the URL instead.
        api = api_with(lambda r: httpx.Response(500, json={"detail": "the server's disk is full"}))
        with self.assertRaises(RuntimeError) as caught:
            api.delete("a")
        self.assertEqual(str(caught.exception), "the server's disk is full")

    def test_url_without_scheme_is_not_called_unreachable(self):
        # F-029: "server-laptop:8000" is a settings mistake, not a network one.
        api = StorageAPI("server-laptop:8000", "key")
        self.addCleanup(api.close)
        with self.assertRaises(httpx.UnsupportedProtocol) as caught:
            api.fetch_manifest()
        message = describe_error(caught.exception)
        self.assertIn("URL", message)
        self.assertNotIn("Could not reach", message)


class ServerUrlTest(unittest.TestCase):
    def test_scheme_is_added_and_bad_urls_refused(self):
        # F-029: Settings and config.toml accept "host:port", as the installer does.
        self.assertEqual(normalize_server_url("server-laptop:8000"), "http://server-laptop:8000")
        self.assertEqual(normalize_server_url(" http://10.0.0.5:8000/ "), "http://10.0.0.5:8000")
        self.assertEqual(normalize_server_url("https://host"), "https://host")
        for bad in ("", "   ", "ftp://host", "http://", "host:notaport"):
            self.assertIsNone(normalize_server_url(bad), bad)


class StatusCommandTest(unittest.TestCase):
    def status_with(self, health: dict) -> int:
        fake = mock.Mock()
        fake.return_value.health.return_value = health
        config = mock.Mock(server_url="http://test", api_key="key")
        with mock.patch.object(cli, "StorageAPI", fake), self.assertLogs("client"):
            return cli.cmd_status(config)

    def test_wrong_key_fails(self):
        # F-013: /health without a file count means the key was rejected.
        self.assertEqual(self.status_with({"status": "ok"}), 1)

    def test_right_key_passes(self):
        self.assertEqual(self.status_with({"status": "ok", "files": 3}), 0)

    def test_unreachable_server_is_described(self):
        # F-051: a connection failure used to escape as a traceback.
        fake = mock.Mock()
        request = httpx.Request("GET", "http://test/health")
        fake.return_value.health.side_effect = httpx.ConnectError("refused", request=request)
        config = mock.Mock(server_url="http://test", api_key="key")
        with mock.patch.object(cli, "StorageAPI", fake), self.assertLogs("client") as logs:
            self.assertEqual(cli.cmd_status(config), 1)
        self.assertIn("Could not reach the server", "\n".join(logs.output))
        fake.return_value.close.assert_called_once()

    def test_missing_config_is_one_line_not_a_traceback(self):
        # F-061: a missing config.toml escaped as a traceback, even for a
        # command that doesn't exist.
        missing = os.path.join(tempfile.gettempdir(), "no-such-dir", "config.toml")
        with mock.patch.dict(os.environ, {"STORAGE_CLIENT_CONFIG": missing}):
            with self.assertLogs("client") as logs:
                self.assertEqual(cli.main(["status"]), 1)
            self.assertIn("Config file not found", "\n".join(logs.output))
            with self.assertLogs("client") as logs:
                self.assertEqual(cli.main(["sync"]), 2)
            self.assertIn("unknown command", "\n".join(logs.output))


class PathTest(unittest.TestCase):
    def test_normalize_rejects_escapes(self):
        self.assertEqual(normalize_rel_path("a\\b/./c"), "a/b/c")
        for bad in ("../x", "a/../../x", "", "/"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_rel_path(bad)


@unittest.skipIf(gui is None, "tkinter is not available")
class PlanTest(unittest.TestCase):
    def test_folder_move_carries_explicit_sub_folders(self):
        # F-009: the source folder shell and empty sub-folders come along.
        moves, folder_moves, skipped = gui.plan_selection_move(
            [], ["a"], "c", ["a/x.txt"], ["a", "a/empty", "c"]
        )
        self.assertEqual(moves, [("a/x.txt", "c/a/x.txt")])
        self.assertEqual(folder_moves, [("a", "c/a"), ("a/empty", "c/a/empty")])
        self.assertEqual(skipped, [])

    def test_empty_folder_move(self):
        moves, folder_moves, _ = gui.plan_selection_move([], ["e"], "c", [], ["e", "c"])
        self.assertEqual((moves, folder_moves), ([], [("e", "c/e")]))

    def test_folder_cannot_move_into_itself(self):
        moves, folder_moves, skipped = gui.plan_selection_move(
            [], ["a"], "a/b", ["a/x"], ["a", "a/b"]
        )
        self.assertEqual((moves, folder_moves), ([], []))
        self.assertEqual([s[0] for s in skipped], ["a"])

    def test_move_into_a_folder_typed_in_another_case(self):
        # F-060: "docs" beside folder "Docs" made the server refuse every file.
        files, folders = ["Docs/a.txt", "Docs/sub/b.txt", "c.txt", "e/f.txt"], ["Docs", "e"]
        moves, folder_moves, skipped = gui.plan_selection_move(
            ["c.txt"], ["e"], "docs/SUB", files, folders
        )
        self.assertEqual(moves, [("c.txt", "Docs/sub/c.txt"), ("e/f.txt", "Docs/sub/e/f.txt")])
        self.assertEqual(folder_moves, [("e", "Docs/sub/e")])
        self.assertEqual(skipped, [])

    def test_restore_into_a_taken_name_keeps_both(self):
        moves = gui.plan_restore(["Trash/a.txt"], ["a.txt", "Trash/a.txt"], [])
        self.assertEqual(moves, [("Trash/a.txt", "a (1).txt")])

    def test_restore_uses_the_recorded_origin(self):
        # F-040: a second a.txt sits in Trash as "a (1).txt" but came from a.txt;
        # a folder trashed as "b (1)" came from b, empty sub-folders included.
        entries = [
            FileEntry("Trash/a.txt", "0", 1, 0.0, moved_from="a.txt"),
            FileEntry("Trash/a (1).txt", "0", 1, 0.0, moved_from="a.txt"),
            FileEntry("Trash/b (1)/x.txt", "0", 1, 0.0, moved_from="b/x.txt"),
            FileEntry("Trash/old.txt", "0", 1, 0.0),  # trashed before moved_from existed
        ]
        origins = gui.trash_origins(entries)
        paths = [e.path for e in entries]
        self.assertEqual(
            gui.plan_restore(["Trash/a (1).txt", "Trash/old.txt"], paths, [], origins),
            [("Trash/a (1).txt", "a.txt"), ("Trash/old.txt", "old.txt")],
        )
        self.assertEqual(
            gui.plan_restore_folders(["Trash/b (1)", "Trash/b (1)/empty"], origins),
            [("Trash/b (1)", "b"), ("Trash/b (1)/empty", "b/empty")],
        )


@unittest.skipIf(gui is None, "tkinter is not available")
class UploadTest(unittest.TestCase):
    def test_a_vanished_file_fails_on_its_own(self):
        # F-038: a file deleted after it was picked used to abort the batch.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "two.txt").write_bytes(b"2")
        api = mock.Mock()
        uploaded, failed, stopped = gui.upload_files(
            api, [("one.txt", d / "one.txt"), ("two.txt", d / "two.txt")]
        )
        self.assertEqual(uploaded, ["two.txt"])
        self.assertEqual([p for p, _ in failed], ["one.txt"])
        self.assertFalse(stopped)

    def test_free_path_numbers_files_and_folders(self):
        self.assertEqual(gui.free_path("d/a.txt", {"d/a.txt", "d/a (1).txt"}), "d/a (2).txt")
        self.assertEqual(gui.free_path("d", {"d"}, is_folder=True), "d (1)")

    def test_names_differing_only_in_case_are_taken(self):
        # F-028: the server refuses "Trash/report.pdf" beside "Trash/Report.pdf".
        moves, _ = gui.plan_trash(["report.pdf"], [], ["report.pdf", "Trash/Report.pdf"], [])
        self.assertEqual(moves, [("report.pdf", "Trash/report (1).pdf")])
        ok, clashes = gui.split_upload_clashes(["/nowhere/Photo.JPG"], "", ["photo.jpg"], [])
        self.assertEqual((ok, clashes), ([], ["Photo.JPG"]))

    def test_plans_reuse_the_servers_folder_spelling(self):
        # F-048: the server refuses a second spelling of a folder.
        moves, _ = gui.plan_trash(["docs/y.txt"], [], ["docs/y.txt", "Trash/Docs/a.txt"], [])
        self.assertEqual(moves, [("docs/y.txt", "Trash/Docs/y.txt")])
        restored = gui.plan_restore(["Trash/Notes/n.txt"], ["Trash/Notes/n.txt"], ["notes"])
        self.assertEqual(restored, [("Trash/Notes/n.txt", "notes/n.txt")])
        # One batch spells a new folder one way, even if its items disagree.
        both = gui.plan_restore(["Trash/a/x", "Trash/A/y"], ["Trash/a/x", "Trash/A/y"], [])
        self.assertEqual([d for _s, d in both], ["a/x", "a/y"])
        # Trash is never respelt onto a user's "trash".
        moves, _ = gui.plan_trash(["f.txt"], [], ["f.txt"], ["trash"])
        self.assertEqual(moves, [("f.txt", "Trash/f.txt")])

    def test_a_top_level_trash_variant_blocks_and_is_reserved(self):
        # F-048: "trash" at the top level holds Trash's name on the server.
        self.assertEqual(gui.trash_blocker(["a.txt"], ["trash"]), "trash")
        self.assertEqual(gui.trash_blocker(["TRASH"], ["Trash"]), "TRASH")
        self.assertIsNone(gui.trash_blocker(["Trash/a.txt", "x/trash/b"], ["trash2"]))
        for name in ("trash", "TRASH/x", "Trash"):
            self.assertTrue(gui.is_reserved_trash_name(name), name)
        self.assertFalse(gui.is_reserved_trash_name("docs/trash"))
        moves, folder_moves, skipped = gui.plan_selection_move(
            ["x/Trash"], ["y/trash"], "", ["x/Trash", "y/trash/f"], []
        )
        self.assertEqual((moves, folder_moves), ([], []))
        self.assertEqual([s for s, _why in skipped], ["x/Trash", "y/trash"])

    def test_trash_carries_explicit_folders(self):
        # F-030: empty sub-folders go to Trash with their folder, and come back.
        self.assertEqual(
            gui.trash_folder_moves([("a", "Trash/a (1)")], ["a", "a/empty", "ab", "b"]),
            [("a", "Trash/a (1)"), ("a/empty", "Trash/a (1)/empty")],
        )
        self.assertEqual(
            gui.plan_restore_folders(["Trash", "Trash/a", "Trash/a/empty"]),
            [("Trash/a", "a"), ("Trash/a/empty", "a/empty")],
        )


@unittest.skipIf(gui is None, "tkinter is not available")
class ConfigErrorTest(unittest.TestCase):
    """F-053: a config.toml that exists but can't be used says why."""

    def start_app(self, text: str | None) -> mock.Mock:
        """Run gui.main() against a config file holding ``text`` (None: no
        file), with the window stubbed out; returns the FileBrowser stub."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.toml"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        browser = mock.Mock()
        with mock.patch.dict(os.environ, {"STORAGE_CLIENT_CONFIG": str(path)}), \
                mock.patch.object(gui, "FileBrowser", browser), \
                mock.patch.object(gui, "_HAS_DND", False), \
                mock.patch.object(gui.tk, "Tk"), \
                mock.patch.object(gui, "_set_window_icon"), \
                mock.patch.object(gui, "_set_app_id"):
            gui.main()
        return browser

    def test_unparsable_file_is_named_not_treated_as_a_first_run(self):
        # A Windows path in a TOML basic string: "\U" is a bad escape.
        browser = self.start_app(
            '[server]\nurl = "http://h:8000"\napi_key = "k"\n'
            '[client]\ndownload_dir = "C:\\Users\\me\\Downloads"\n'
        )
        _root, config = browser.call_args.args
        self.assertIsNone(config)
        self.assertIn("config.toml", browser.call_args.kwargs["config_error"])
        self.assertIsNone(browser.call_args.kwargs["config_draft"])

    def test_a_refused_file_prefills_the_settings(self):
        browser = self.start_app(
            '[server]\nurl = "ftp://h"\napi_key = "the-key"\n'
            '[client]\ndownload_dir = "D:/dl"\n'
        )
        self.assertIn("ftp://h", browser.call_args.kwargs["config_error"])
        draft = browser.call_args.kwargs["config_draft"]
        self.assertEqual((draft.server_url, draft.api_key, draft.download_dir),
                         ("ftp://h", "the-key", "D:/dl"))

    def test_no_file_is_a_plain_first_run(self):
        browser = self.start_app(None)
        self.assertIsNone(browser.call_args.kwargs["config_error"])


def _rejected() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://test/manifest")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)


class FakeServer:
    """The calls the window makes, with the server's rules, on a real ManifestDB."""

    def __init__(self, path: Path):
        self.db = ManifestDB(path)
        self.fail: Exception | None = None  # raised by fetch_manifest when set
        self.refuse_folders: set[str] = set()  # create_folder fails for these

    def add_file(self, path: str) -> None:
        self.db.upsert(path, "0" * 64, 1, time.time())

    def live(self) -> list[str]:
        return [e.path for e in self.db.list_all() if not e.deleted]

    def fetch_manifest(self):
        if self.fail is not None:
            raise self.fail
        return self.db.list_all(), self.db.list_folders()

    def move(self, src: str, dst: str):
        if dst in self.live():
            raise RuntimeError(f"a file named '{dst}' already exists")
        return self.db.move_file(src, dst)

    def create_folder(self, path: str) -> list[str]:
        if path in self.refuse_folders:
            raise RuntimeError("connection reset")
        if path in self.live():
            raise RuntimeError(f"a file named '{path}' already exists")
        self.db.create_folder(path)
        return self.db.list_folders()

    def delete_folder(self, path: str) -> list[str]:
        prefix = path + "/"
        if any(p.startswith(prefix) for p in self.live()) or any(
            f.startswith(prefix) for f in self.db.list_folders()
        ):
            raise RuntimeError("folder is not empty")
        self.db.delete_folder(path)
        return self.db.list_folders()

    def close(self) -> None:
        pass


class _SyncThread:
    """Stands in for threading.Thread in client.gui: runs the work at once."""

    def __init__(self, target, daemon=None):
        self._target = target

    def start(self) -> None:
        self._target()


@unittest.skipIf(gui is None, "tkinter is not available")
class WindowTest(unittest.TestCase):
    """The real FileBrowser in a hidden window, against FakeServer.

    Background work is run inline on the test thread, so what the window shows
    and what reaches the server can be checked directly. Covers the wiring the
    planning tests above can't see (F-033).
    """

    def setUp(self) -> None:
        try:
            self.root = gui.tk.Tk()
        except gui.tk.TclError as exc:  # no display
            self.skipTest(f"no Tk display: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = FakeServer(Path(self._tmp.name) / "manifest.db")
        self.addCleanup(self.server.db._conn.close)
        self.dialogs: list[str] = []
        sync = types.SimpleNamespace(Thread=_SyncThread, Event=threading.Event)
        for patch in (
            mock.patch.object(gui, "_HAS_DND", False),
            mock.patch.object(gui, "threading", sync),
            mock.patch.object(gui, "StorageAPI", lambda *a, **k: self.server),
            mock.patch.object(gui, "save_config", lambda *a, **k: Path("config.toml")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("showinfo", "showwarning", "showerror"):
            p = mock.patch.object(
                gui.messagebox, name, lambda title, *a, **k: self.dialogs.append(title)
            )
            p.start()
            self.addCleanup(p.stop)

    def open_window(self):
        config = ClientConfig("http://test", "k" * 20, self._tmp.name)
        self.app = gui.FileBrowser(self.root, config)
        self.addCleanup(self.app.close)
        self.flush()
        return self.app

    def flush(self) -> None:
        for _ in range(5):
            self.root.update()

    def run_ops_inline(self) -> None:
        self.app._enqueue_op = lambda label, body: body(threading.Event())

    def test_rejected_key_is_named_in_the_status_line(self):
        # F-005: the first load's error reaches the status line.
        self.server.fail = _rejected()
        app = self.open_window()
        self.assertIn("API key", app.status.get())

    def test_refresh_failure_names_the_reason(self):
        # F-005: so does a failed manual Refresh.
        app = self.open_window()
        self.server.fail = _rejected()
        app._poll_now()
        self.flush()
        self.assertIn("API key", app.status.get())

    def test_folder_move_reaches_the_server_with_its_sub_folders(self):
        # F-009: the folder and its empty sub-folder move; no shell is left.
        for f in ("a", "a/empty", "c"):
            self.server.db.create_folder(f)
        self.server.add_file("a/x.txt")
        app = self.open_window()
        self.run_ops_inline()
        app._move_selection([], ["a"], "c")
        self.flush()
        self.assertEqual(self.server.db.list_folders(), ["c", "c/a", "c/a/empty"])
        self.assertEqual(self.server.live(), ["c/a/x.txt"])

    def test_trash_and_restore_keep_empty_sub_folders(self):
        # F-030: before, both the folder and its empty sub-folder were lost.
        for f in ("a", "a/empty"):
            self.server.db.create_folder(f)
        self.server.add_file("a/x.txt")
        app = self.open_window()
        self.run_ops_inline()
        app._delete_folder_contents("a")
        self.flush()
        self.assertIn("Trash/a/empty", self.server.db.list_folders())
        self.assertNotIn("a/empty", self.server.db.list_folders())
        self.assertEqual(self.server.live(), ["Trash/a/x.txt"])
        app._restore_folder("Trash/a")
        self.flush()
        folders = self.server.db.list_folders()
        self.assertIn("a/empty", folders)
        self.assertNotIn("Trash/a/empty", folders)
        self.assertEqual(self.server.live(), ["a/x.txt"])
        self.assertEqual(self.dialogs, [])

    def test_folder_move_keeps_a_folder_whose_copy_failed(self):
        # F-039: a/empty used to be dropped although c/a/empty was never made.
        for f in ("a", "a/empty", "c"):
            self.server.db.create_folder(f)
        self.server.add_file("a/x.txt")
        self.server.refuse_folders = {"c/a/empty"}
        app = self.open_window()
        self.run_ops_inline()
        app._move_selection([], ["a"], "c")
        self.flush()
        self.assertIn("a/empty", self.server.db.list_folders())
        self.assertEqual(self.server.live(), ["c/a/x.txt"])

    def test_trash_keeps_a_folder_whose_copy_failed(self):
        # F-046: _carry_folders keeps an original whose Trash copy failed.
        for f in ("a", "a/empty"):
            self.server.db.create_folder(f)
        self.server.add_file("a/x.txt")
        self.server.refuse_folders = {"Trash/a/empty"}
        app = self.open_window()
        self.run_ops_inline()
        app._delete_folder_contents("a")
        self.flush()
        self.assertIn("a/empty", self.server.db.list_folders())
        self.assertEqual(self.server.live(), ["Trash/a/x.txt"])

    def test_restore_brings_a_numbered_file_back_under_its_name(self):
        # F-040, through the window and the real manifest's moved_from.
        self.server.add_file("a.txt")
        app = self.open_window()
        self.run_ops_inline()
        app._delete_selection(["a.txt"], [])
        self.flush()
        self.server.add_file("a.txt")
        app._poll_now()
        self.flush()
        app._delete_selection(["a.txt"], [])
        self.flush()
        self.assertEqual(self.server.live(), ["Trash/a (1).txt", "Trash/a.txt"])
        app._restore(["Trash/a (1).txt"])
        self.flush()
        self.assertEqual(self.server.live(), ["Trash/a.txt", "a.txt"])

    def test_delete_is_refused_while_a_top_level_trash_exists(self):
        # F-048: every move into Trash would get 409; say why, move nothing.
        self.server.db.create_folder("trash")
        self.server.add_file("a.txt")
        app = self.open_window()
        self.run_ops_inline()
        app._delete_selection(["a.txt"], [])
        app._delete_folder_contents("trash")
        self.flush()
        self.assertEqual(self.dialogs, ["Can't move to Trash", "Can't move to Trash"])
        self.assertEqual(self.server.live(), ["a.txt"])
        self.assertEqual(self.server.db.list_folders(), ["trash"])

    def test_trash_name_is_reserved_in_any_case(self):
        # F-048: New folder and Move refuse "trash" before it can block Trash.
        self.server.add_file("a.txt")
        app = self.open_window()
        self.run_ops_inline()
        with mock.patch.object(gui.simpledialog, "askstring", return_value="trash"):
            app._new_folder()
        app._move_selection(["a.txt"], [], "TRASH/inbox")
        self.flush()
        self.assertEqual(self.dialogs, ["Invalid name", "Invalid folder"])
        self.assertEqual(self.server.db.list_folders(), [])
        self.assertEqual(self.server.live(), ["a.txt"])

    def test_a_config_that_did_not_load_is_explained_and_prefilled(self):
        # F-053: the status line and the settings screen give the reason, and
        # the screen starts from what the file held.
        draft = ClientConfig("ftp://h", "k" * 20, self._tmp.name)
        with mock.patch.object(self.root, "after"):  # no automatic Settings
            app = gui.FileBrowser(
                self.root, None, config_error="Couldn't use config.toml: bad URL",
                config_draft=draft,
            )
        self.addCleanup(app.close)
        self.assertIn("Couldn't use config.toml", app.status.get())
        dialog = mock.Mock(return_value=None)
        app._settings_dialog = dialog
        app._open_settings(first_run=True)
        args, kwargs = dialog.call_args
        self.assertEqual(args[:3], ("ftp://h", "k" * 20, self._tmp.name))
        self.assertEqual(kwargs["notice"], "Couldn't use config.toml: bad URL")
        self.assertIn("Couldn't use config.toml", app.status.get())

    def test_saving_over_a_broken_config_connects(self):
        # F-053: the draft prefills the screen but isn't a live connection, so
        # saving its own values must still connect.
        draft = ClientConfig("http://test", "k" * 20, self._tmp.name)
        with mock.patch.object(self.root, "after"):
            app = gui.FileBrowser(self.root, None, config_error="bad", config_draft=draft)
        self.addCleanup(app.close)
        app._settings_dialog = mock.Mock(
            return_value=("http://test", "k" * 20, self._tmp.name)
        )
        app._open_settings(first_run=True)
        self.flush()
        self.assertIs(app.api, self.server)
        self.assertIsNone(app._config_error)

    def test_settings_are_locked_while_a_job_runs(self):
        # F-031: changing the server mid-job would switch the job to it.
        app = self.open_window()
        gate = threading.Event()
        self.addCleanup(gate.set)
        app._enqueue_transfer("Download 2 files", lambda cancelled: gate.wait(10))
        self.assertEqual(str(app.settings_btn["state"]), "disabled")
        dialog = mock.Mock(return_value=("http://other:8000", "k" * 20, self._tmp.name))
        app._settings_dialog = dialog
        app._open_settings()
        dialog.assert_not_called()
        self.assertIs(app.api, self.server)
        gate.set()  # let the job end before the window goes
        deadline = time.monotonic() + 10
        while app._transfers.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.flush()


if __name__ == "__main__":
    unittest.main()
