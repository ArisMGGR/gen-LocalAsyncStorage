"""Server endpoint tests, run against the ASGI app in-process.

Run from the project root with ``python -m unittest discover tests``. Skipped
when fastapi isn't installed (it's only needed on the server machine).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import fastapi  # noqa: F401
    import httpx
except ImportError:  # client-only install
    httpx = None

KEY = "test-key-" + "x" * 24


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _headers(data: bytes, length: int | None = None, mtime: float | str | None = None) -> dict:
    return {
        "X-Content-SHA256": _sha(data),
        "X-Mtime": str(time.time() if mtime is None else mtime),
        "Content-Length": str(len(data) if length is None else length),
    }


@unittest.skipIf(httpx is None, "fastapi is not installed")
class ServerTest(unittest.TestCase):
    max_total_bytes = 100

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data_dir = Path(self._tmp.name) / "data"
        env = {
            "STORAGE_SERVER_CONFIG": str(Path(self._tmp.name) / "absent.toml"),
            "STORAGE_API_KEY": KEY,
            "STORAGE_DATA_DIR": str(self.data_dir),
            "STORAGE_LOG_DIR": str(self.data_dir / "logs"),
            "STORAGE_MAX_TOTAL_BYTES": str(self.max_total_bytes),
        }
        self._saved_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        # server.main builds its app, database and limits at import: reload it
        # so every test starts from an empty store.
        for name in ("server.main", "server.auth"):
            sys.modules.pop(name, None)
        self.main = importlib.import_module("server.main")

    def tearDown(self) -> None:
        self.main.db._conn.close()
        for name in ("server.main", "server.auth"):
            sys.modules.pop(name, None)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def run_client(self, scenario, key: str | bytes = KEY):
        """Run ``scenario(client)`` on a fresh event loop and return its result."""

        async def go():
            transport = httpx.ASGITransport(app=self.main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", headers={"X-API-Key": key}
            ) as client:
                return await scenario(client)

        return asyncio.run(go())

    @staticmethod
    async def upload(client, path: str, data: bytes):
        return await client.put(
            "/upload", params={"path": path}, content=data, headers=_headers(data)
        )

    # -- moves and uploads in flight (F-001) --

    def test_move_onto_a_name_being_uploaded_is_refused(self):
        async def scenario(c):
            self.assertEqual((await self.upload(c, "b", b"bee")).status_code, 200)
            gate = asyncio.Event()

            async def slow_body():
                yield b"new"
                await gate.wait()

            up = asyncio.create_task(
                c.put("/upload", params={"path": "a"}, content=slow_body(),
                      headers=_headers(b"new"))
            )
            await asyncio.sleep(0.1)  # the upload has claimed "a" and is streaming
            moved = await c.post("/move", params={"src": "b", "dst": "a"})
            gate.set()
            uploaded = await up
            b = await c.get("/download", params={"path": "b"})
            a = await c.get("/download", params={"path": "a"})
            return moved, uploaded, b, a

        moved, uploaded, b, a = self.run_client(scenario)
        self.assertEqual(moved.status_code, 409)
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(b.content, b"bee")  # the file that would have moved is intact
        self.assertEqual(a.content, b"new")

    def test_nothing_goes_beneath_a_name_being_uploaded(self):
        # F-045: F-001's ancestor half — "a" is still uploading, so "a/x" can't
        # be a folder path (the upload would then land on a directory).
        async def scenario(c):
            gate = asyncio.Event()

            async def slow_body():
                yield b"new"
                await gate.wait()

            up = asyncio.create_task(
                c.put("/upload", params={"path": "a"}, content=slow_body(),
                      headers=_headers(b"new"))
            )
            await asyncio.sleep(0.1)
            under = await self.upload(c, "a/x", b"x")
            folder = await c.post("/folder", params={"path": "a/sub"})
            gate.set()
            return under, folder, await up

        under, folder, uploaded = self.run_client(scenario)
        self.assertEqual(under.status_code, 409)
        self.assertEqual(folder.status_code, 409)
        self.assertEqual(uploaded.status_code, 200)

    def test_move_refuses_existing_file_and_folder(self):
        async def scenario(c):
            await self.upload(c, "a", b"1")
            await self.upload(c, "b", b"2")
            await c.post("/folder", params={"path": "dir"})
            return (
                await c.post("/move", params={"src": "a", "dst": "b"}),
                await c.post("/move", params={"src": "a", "dst": "dir"}),
                await c.post("/move", params={"src": "a", "dst": "b/inside"}),
                await c.post("/move", params={"src": "a", "dst": "dir/a"}),
            )

        onto_file, onto_folder, under_file, ok = self.run_client(scenario)
        self.assertEqual(onto_file.status_code, 409)
        self.assertEqual(onto_folder.status_code, 409)
        self.assertEqual(under_file.status_code, 409)
        self.assertEqual(ok.status_code, 200)

    # -- quota across concurrent uploads (F-002) --

    def test_concurrent_uploads_cannot_exceed_quota(self):
        async def scenario(c):
            gate = asyncio.Event()

            async def slow_body():
                yield b"x" * 10
                await gate.wait()
                yield b"x" * 50

            first = asyncio.create_task(
                c.put("/upload", params={"path": "one"}, content=slow_body(),
                      headers=_headers(b"x" * 60))
            )
            await asyncio.sleep(0.1)  # "one" holds 60 of the 100 bytes
            second = await self.upload(c, "two", b"y" * 60)
            gate.set()
            return await first, second, await c.get("/manifest")

        first, second, manifest = self.run_client(scenario)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 413)
        total = sum(f["size"] for f in manifest.json()["files"] if not f["deleted"])
        self.assertLessEqual(total, self.max_total_bytes)

    def test_concurrent_uploads_that_fit_both_succeed(self):
        async def scenario(c):
            return await asyncio.gather(
                self.upload(c, "one", b"a" * 40), self.upload(c, "two", b"b" * 40)
            )

        results = self.run_client(scenario)
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertEqual(self.main._reserved_bytes, 0)

    def test_quota_released_after_failed_upload(self):
        async def scenario(c):
            bad = await c.put(
                "/upload", params={"path": "bad"}, content=b"z" * 50,
                headers={**_headers(b"z" * 50), "X-Content-SHA256": "0" * 64},
            )
            good = await self.upload(c, "good", b"g" * 90)
            return bad, good

        bad, good = self.run_client(scenario)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.main._reserved_bytes, 0)

    # -- folders vs files (F-008) --

    def test_folder_cannot_take_a_file_name(self):
        async def scenario(c):
            await self.upload(c, "notes", b"text")
            return (
                await c.post("/folder", params={"path": "notes"}),
                await c.post("/folder", params={"path": "notes/sub"}),
                await c.post("/folder", params={"path": "docs"}),
                await c.post("/folder", params={"path": "docs"}),
            )

        same, under, new, again = self.run_client(scenario)
        self.assertEqual(same.status_code, 409)
        self.assertEqual(under.status_code, 409)
        self.assertEqual(new.status_code, 200)
        self.assertEqual(again.status_code, 200)  # creating a folder is idempotent
        self.assertEqual(again.json()["folders"], ["docs"])

    def test_upload_never_replaces(self):
        async def scenario(c):
            await self.upload(c, "a", b"1")
            await c.post("/folder", params={"path": "dir"})
            return await self.upload(c, "a", b"2"), await self.upload(c, "dir", b"3")

        file_clash, folder_clash = self.run_client(scenario)
        self.assertEqual(file_clash.status_code, 409)
        self.assertEqual(folder_clash.status_code, 409)

    # -- authentication (F-011) --

    def test_wrong_and_non_ascii_keys_get_401(self):
        async def scenario(c):
            return await c.get("/manifest")

        self.assertEqual(self.run_client(scenario, key="wrong").status_code, 401)
        self.assertEqual(self.run_client(scenario, key="é".encode("utf-8")).status_code, 401)
        self.assertEqual(self.run_client(scenario).status_code, 200)

    def test_health_hides_count_from_a_wrong_key(self):
        async def scenario(c):
            return (await c.get("/health")).json()

        self.assertNotIn("files", self.run_client(scenario, key="é".encode("utf-8")))
        self.assertIn("files", self.run_client(scenario))

    # -- mtime validation (F-012) --

    def test_non_finite_mtime_rejected_without_leaving_a_blob(self):
        async def scenario(c):
            out = []
            for bad in ("nan", "inf", "-1"):
                out.append(await c.put(
                    "/upload", params={"path": "x"}, content=b"data",
                    headers=_headers(b"data", mtime=bad),
                ))
            return out, await c.get("/manifest")

        responses, manifest = self.run_client(scenario)
        self.assertEqual([r.status_code for r in responses], [400, 400, 400])
        self.assertEqual(manifest.json()["files"], [])
        self.assertFalse((self.data_dir / "storage" / "x").exists())

    def test_manifest_reports_the_upload_limit(self):
        async def scenario(c):
            return (await c.get("/manifest")).json()

        self.assertEqual(self.run_client(scenario)["max_file_bytes"], self.main.config.max_file_bytes)

    def test_manifest_reports_the_data_disk(self):
        async def scenario(c):
            return (await c.get("/manifest")).json()

        disk = self.run_client(scenario)["disk"]
        self.assertGreater(disk["total"], 0)
        self.assertLessEqual(disk["used"], disk["total"])
        self.assertLessEqual(disk["free"], disk["total"])

    # -- health count (F-014) --

    def test_health_counts_live_files_only(self):
        async def scenario(c):
            await self.upload(c, "a", b"1")
            await self.upload(c, "b", b"2")
            await c.post("/delete", params={"path": "a"})
            return (await c.get("/health")).json()

        self.assertEqual(self.run_client(scenario)["files"], 1)

    # -- issues are logged --

    def test_refused_requests_are_logged_with_their_reason(self):
        async def scenario(c):
            await c.get("/download", params={"path": "missing"})
            await c.get("/manifest", headers={"X-API-Key": "wrong-key-wrong-key"})
            await c.put("/upload", params={"path": "a"}, content=b"x")  # no headers

        with self.assertLogs("server", level="WARNING") as logs:
            self.run_client(scenario)
        out = "\n".join(logs.output)
        self.assertIn("404 on GET /download?path=missing", out)
        self.assertIn("401 on GET /manifest", out)
        self.assertIn("422 on PUT /upload?path=a", out)
        self.assertNotIn("wrong-key", out)  # headers, and so keys, are never logged
        self.assertNotIn(KEY, out)

    def test_unhandled_errors_are_logged_with_a_traceback(self):
        def boom():
            raise RuntimeError("manifest exploded")

        self.main.db.list_all = boom

        async def go():
            transport = httpx.ASGITransport(app=self.main.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", headers={"X-API-Key": KEY}
            ) as client:
                return await client.get("/manifest")

        with self.assertLogs("server", level="ERROR") as logs:
            resp = asyncio.run(go())
        self.assertEqual(resp.status_code, 500)
        self.assertIn("500 on GET /manifest", logs.output[0])
        self.assertIn("RuntimeError: manifest exploded", logs.output[0])

    # -- "All is fine" heartbeat --

    def heartbeat_messages(self, during=None) -> list[str]:
        """Run one heartbeat tick, calling ``during()`` mid-interval, and return
        what the server logged. Collected with a plain handler rather than
        assertLogs, which stops records reaching the root issue counter."""
        import logging

        self.main._HEARTBEAT_INTERVAL = 0.2
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        server_log = logging.getLogger("server")
        server_log.addHandler(handler)
        self.addCleanup(server_log.removeHandler, handler)

        async def go():
            task = asyncio.create_task(self.main._heartbeat())
            await asyncio.sleep(0.05)
            if during:
                during()
            await asyncio.sleep(0.25)
            task.cancel()

        asyncio.run(go())
        return [r.getMessage() for r in records]

    def test_heartbeat_says_all_is_fine_when_nothing_went_wrong(self):
        self.assertEqual(self.heartbeat_messages(), ["All is fine"])

    def test_heartbeat_stays_quiet_after_an_issue(self):
        import logging

        out = self.heartbeat_messages(lambda: logging.getLogger("x").warning("disk slow"))
        self.assertNotIn("All is fine", out)

    def test_heartbeat_reports_a_failed_health_check(self):
        def boom():
            raise RuntimeError("manifest exploded")

        self.main.db.count_live = boom
        out = self.heartbeat_messages()
        self.assertNotIn("All is fine", out)
        self.assertIn("manifest exploded", out[0])

    # -- names differing only in case (F-028) --

    def test_case_variant_of_a_taken_name_is_refused(self):
        async def scenario(c):
            await self.upload(c, "a.txt", b"ORIGINAL")
            await self.upload(c, "b", b"bee")
            await c.post("/folder", params={"path": "Docs"})
            return (
                await self.upload(c, "A.txt", b"replacement"),
                await c.post("/move", params={"src": "b", "dst": "A.TXT"}),
                await c.post("/folder", params={"path": "docs"}),
                await self.upload(c, "DOCS", b"x"),
                await self.upload(c, "A.TXT/inside", b"x"),
                await c.get("/download", params={"path": "a.txt"}),
            )

        upload, move, folder, onto_folder, under_file, original = self.run_client(scenario)
        for r in (upload, move, folder, onto_folder, under_file):
            self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(original.content, b"ORIGINAL")

    def test_move_may_change_only_the_case_of_its_own_name(self):
        async def scenario(c):
            await self.upload(c, "a.txt", b"data")
            moved = await c.post("/move", params={"src": "a.txt", "dst": "A.txt"})
            return moved, await c.get("/download", params={"path": "A.txt"}), await c.get("/manifest")

        moved, renamed, manifest = self.run_client(scenario)
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(renamed.content, b"data")
        live = [f["path"] for f in manifest.json()["files"] if not f["deleted"]]
        self.assertEqual(live, ["A.txt"])

    def test_deleting_an_unknown_case_variant_keeps_the_file(self):
        async def scenario(c):
            await self.upload(c, "a.txt", b"keep me")
            gone = await c.post("/delete", params={"path": "A.txt"})
            return gone, await c.get("/download", params={"path": "a.txt"})

        gone, kept = self.run_client(scenario)
        self.assertEqual(gone.status_code, 200)
        self.assertEqual(kept.content, b"keep me")

    def test_case_variant_of_a_folder_on_the_way_is_refused(self):
        # F-041: "docs/x.txt" beside folder "Docs" would make a second folder.
        async def scenario(c):
            await c.post("/folder", params={"path": "Docs"})
            return (
                await self.upload(c, "docs/x.txt", b"x"),
                await c.post("/folder", params={"path": "docs/sub"}),
                await self.upload(c, "Docs/x.txt", b"x"),
                await c.get("/manifest"),
            )

        under_variant, folder_variant, exact, manifest = self.run_client(scenario)
        self.assertEqual(under_variant.status_code, 409, under_variant.text)
        self.assertEqual(folder_variant.status_code, 409, folder_variant.text)
        self.assertEqual(exact.status_code, 200)
        self.assertEqual([f["path"] for f in manifest.json()["files"]], ["Docs/x.txt"])

    # -- names a Windows disk can't hold as given (F-037) --

    def test_names_windows_cannot_hold_are_refused(self):
        async def scenario(c):
            await self.upload(c, "a.txt", b"ORIGINAL")
            await self.upload(c, "b.txt", b"bee")
            bad = [
                await self.upload(c, name, b"replacement")
                for name in ("a.txt.", "a.txt ", "dir./x", "NUL", "nul.txt", "com1", "a:b", "q?")
            ]
            bad.append(await c.post("/move", params={"src": "b.txt", "dst": "a.txt "}))
            bad.append(await c.post("/folder", params={"path": "folder."}))
            return bad, await c.get("/download", params={"path": "a.txt"}), await c.get("/manifest")

        bad, original, manifest = self.run_client(scenario)
        for r in bad:
            self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(original.content, b"ORIGINAL")
        live = sorted(f["path"] for f in manifest.json()["files"] if not f["deleted"])
        self.assertEqual(live, ["a.txt", "b.txt"])
        self.assertEqual(manifest.json()["folders"], [])

    def test_a_stored_unsafe_name_can_still_be_deleted(self):
        # Only new names are checked: one stored before can still go.
        self.main.db.upsert("old.", "0" * 64, 1, time.time())

        async def scenario(c):
            return await c.post("/delete", params={"path": "old."})

        self.assertEqual(self.run_client(scenario).status_code, 200)
        self.assertTrue(self.main.db.get("old.").deleted)

    # -- a name the disk can't hold (F-050) --

    def test_disk_error_on_upload_is_reported_not_a_500(self):
        name = "n" * 300 + ".txt"  # longer than any disk's name limit

        async def scenario(c):
            up = await self.upload(c, name, b"x")
            await self.upload(c, "a.txt", b"a")
            moved = await c.post("/move", params={"src": "a.txt", "dst": name})
            return up, moved, await c.get("/manifest")

        up, moved, manifest = self.run_client(scenario)
        self.assertEqual(up.status_code, 409, up.text)
        self.assertIn("cannot store that path", up.json()["detail"])
        self.assertEqual(moved.status_code, 409)
        self.assertIn("cannot move to that path", moved.json()["detail"])
        # No server paths (F-054, F-056). The temp folder's own name has no
        # separator in it, so it's found however an error spells the path
        # (Windows error text doubles every backslash).
        marker = Path(self._tmp.name).name
        for r in (up, moved):
            self.assertNotIn(marker, r.json()["detail"])
            self.assertNotIn("storage", r.json()["detail"])
        self.assertEqual([f["path"] for f in manifest.json()["files"]], ["a.txt"])
        self.assertEqual(self.main._reserved_bytes, 0)
        self.assertEqual(self.main._uploads_in_flight, set())
        self.assertEqual(
            [p.name for p in (self.data_dir / "storage").iterdir()], ["a.txt"]
        )

    # -- the clients' Trash and Restore plans pass the case rule (F-048) --

    def test_trash_and_restore_plans_pass_the_case_rule(self):
        try:
            from client import gui
        except ImportError:  # no tkinter in this Python
            self.skipTest("tkinter is not available")
        from common.hashing import FileEntry

        async def state(c):
            m = (await c.get("/manifest")).json()
            live = [FileEntry.from_dict(f) for f in m["files"] if not f["deleted"]]
            return live, [e.path for e in live], m["folders"]

        async def apply(c, moves):
            return [(await c.post("/move", params={"src": s, "dst": d})).status_code
                    for s, d in moves]

        async def scenario(c):
            # Trash holds "Docs"; deleting docs/y.txt goes into it, not beside it.
            await self.upload(c, "Docs/a.txt", b"a")
            _, files, folders = await state(c)
            await apply(c, gui.plan_trash([], ["Docs"], files, folders)[0])
            await self.upload(c, "docs/y.txt", b"y")
            _, files, folders = await state(c)
            trashed = gui.plan_trash(["docs/y.txt"], [], files, folders)[0]
            out = [trashed, await apply(c, trashed)]
            # "notes" now exists; restoring Trash/Notes/n.txt goes into it.
            await self.upload(c, "Notes/n.txt", b"n")
            _, files, folders = await state(c)
            await apply(c, gui.plan_trash([], ["Notes"], files, folders)[0])
            await c.post("/folder", params={"path": "notes/sub"})
            await c.post("/folder", params={"path": "Trash/Notes/empty"})
            live, files, folders = await state(c)
            origins = gui.trash_origins(live)
            restored = gui.plan_restore(["Trash/Notes/n.txt"], files, folders, origins)
            out += [restored, await apply(c, restored)]
            back = gui.plan_restore_folders(
                ["Trash/Notes/empty", "Trash/Notes"], origins,
                [*files, *(d for _s, d in restored)], folders,
            )
            out += [back, [(await c.post("/folder", params={"path": d})).status_code
                           for _s, d in back]]
            return out

        trashed, t_codes, restored, r_codes, back, f_codes = self.run_client(scenario)
        self.assertEqual(trashed, [("docs/y.txt", "Trash/Docs/y.txt")])
        self.assertEqual(restored, [("Trash/Notes/n.txt", "notes/n.txt")])
        self.assertEqual(back, [("Trash/Notes/empty", "notes/empty"), ("Trash/Notes", "notes")])
        self.assertEqual(t_codes + r_codes + f_codes, [200, 200, 200, 200])

    # -- moves record where they came from (F-040) --

    def test_move_records_its_source_and_upload_clears_it(self):
        # F-049: b's tombstone still says it came from a.txt, so the upload over
        # it must clear that, or Restore would send the new b to a.txt.
        async def scenario(c):
            await self.upload(c, "a.txt", b"1")
            await c.post("/move", params={"src": "a.txt", "dst": "Trash/a.txt"})
            await c.post("/move", params={"src": "Trash/a.txt", "dst": "b"})
            await c.post("/delete", params={"path": "b"})
            tombstone = {f["path"]: f for f in (await c.get("/manifest")).json()["files"]}["b"]
            await self.upload(c, "b", b"2")
            return tombstone, (await c.get("/manifest")).json()["files"]

        tombstone, files = self.run_client(scenario)
        rows = {f["path"]: f for f in files}
        self.assertEqual(rows["b"]["moved_from"], "")
        self.assertTrue(tombstone["deleted"])
        self.assertEqual(tombstone["moved_from"], "Trash/a.txt")  # what the upload clears
        self.assertEqual(rows["Trash/a.txt"]["moved_from"], "a.txt")


class ConfigTest(unittest.TestCase):
    """server.config needs no fastapi, so these always run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        names = (
            "STORAGE_HOST", "STORAGE_API_KEY", "STORAGE_PORT", "STORAGE_DATA_DIR",
            "STORAGE_LOG_DIR",
        )
        self._saved_env = {k: os.environ.pop(k, None) for k in names}
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v

    def load(self, server_table: str):
        from server.config import load_config

        path = Path(self._tmp.name) / "server-config.toml"
        path.write_text(f'[server]\napi_key = "{KEY}"\n{server_table}', encoding="utf-8")
        return load_config(path)

    def test_blank_values_mean_the_defaults(self):
        # F-035: host = "" used to reach the server as "", i.e. every interface.
        config = self.load('host = ""\nport = ""\n')
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)

    def test_logs_default_to_a_logs_folder(self):
        self.assertEqual(self.load("").log_dir, Path("logs").resolve())

    def test_values_are_kept(self):
        self.assertEqual(self.load('host = "100.1.2.3"\n').host, "100.1.2.3")


class DailyFileHandlerTest(unittest.TestCase):
    def test_writes_to_a_dated_log_file(self):
        import logging
        from datetime import date

        from server.logging_setup import DailyFileHandler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        handler = DailyFileHandler(Path(tmp.name) / "logs")
        self.addCleanup(handler.close)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        handler.emit(logging.makeLogRecord({"levelname": "ERROR", "msg": "disk full"}))

        path = Path(tmp.name) / "logs" / f"{date.today().isoformat()}.log"
        self.assertEqual(path.read_text(encoding="utf-8"), "ERROR disk full\n")


class ManifestDBTest(unittest.TestCase):
    """server.db is stdlib only, so these always run."""

    def test_old_manifest_gains_moved_from(self):
        import sqlite3

        from server.db import ManifestDB

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "manifest.db"
        old = sqlite3.connect(path)  # the schema before moved_from existed
        old.execute(
            "CREATE TABLE manifest (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
            "size INTEGER NOT NULL, mtime REAL NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0)"
        )
        old.execute("INSERT INTO manifest (path, sha256, size, mtime) VALUES ('a', 'x', 1, 1)")
        old.commit()
        old.close()

        db = ManifestDB(path)
        self.addCleanup(db._conn.close)
        self.assertEqual(db.get("a").moved_from, "")
        self.assertEqual(db.move_file("a", "b").moved_from, "a")


if __name__ == "__main__":
    unittest.main()
