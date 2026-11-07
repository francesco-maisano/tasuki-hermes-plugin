"""The mirror inside the plugin service: status, config, the tasks_dir tool, pairing/unpair lifecycle, and 'never breaks the worker'."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import FakeHermes, wait_for
from fake_tasuki import FakeTasuki
from _tasuki_hermes_core import service as svc
from _tasuki_hermes_core.client import TasukiError
from _tasuki_hermes_core.store import MemoryStore, PairingStore


def make(tmp, **mk):
    fh = FakeHermes()
    default = Path(tmp) / "hermes_home" / "tasuki"
    s = svc.Service(PairingStore(MemoryStore(), Path(tmp) / "state"), gateway_url=lambda: "ws://fake/api/ws?token=X", connect=fh.connect,
                    worker_kwargs=dict(heartbeat_s=0.2, poll_wait=1, lock_path=None), mirror_kwargs=dict(poll_wait=0, **mk), default_mirror_dir=default)
    return s, default


class MirrorService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = FakeTasuki(max_wait=0.3).start()

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def test_status_carries_mirror_and_config_and_the_default_dir_is_under_hermes_home(self):
        s, default = make(self.tmp.name)
        st = s.status()
        self.assertEqual(st["config"], {"mirror_dir": str(default), "mirror_dir_default": str(default)})
        self.assertEqual(set(st["mirror"]), {"dir", "running", "last_sync_at", "tasks", "errors", "cursor", "external_edits", "recovered", "resyncs", "schema", "inbox"})
        self.assertEqual((st["mirror"]["dir"], st["mirror"]["tasks"], st["mirror"]["running"]), (str(default), 0, False))
        self.assertNotIn("plugins", str(default))
        s.shutdown()

    def test_pairing_starts_the_mirror_unpair_stops_it_and_revokes_on_the_server(self):
        s, default = make(self.tmp.name)
        s.pair(self.srv.url, "PAIR-1234")
        self.assertTrue(wait_for(lambda: s.status()["mirror"]["running"], 5))
        u = s.unpair()
        self.assertEqual(u, {"paired": False, "was_paired": True, "server_revoked": True})
        self.assertEqual(self.srv.self_revokes, 1)
        self.assertFalse(s.status()["mirror"]["running"])
        self.assertFalse(s.status()["worker"]["running"])
        self.assertIsNone(s.pairing())
        s.shutdown()

    def test_unpair_when_the_server_is_unreachable_still_forgets_the_token(self):
        s, _ = make(self.tmp.name)
        s.pair(self.srv.url, "PAIR-1234")
        self.srv.fail_revoke = True
        u = s.unpair()
        self.assertEqual((u["paired"], u["server_revoked"]), (False, False))
        self.assertIsNone(s.pairing())
        s.shutdown()

    def test_a_dead_token_on_unpair_counts_as_revoked(self):
        s, _ = make(self.tmp.name)
        s.pair(self.srv.url, "PAIR-1234")
        self.srv.revoke()                                           # already revoked in the app
        self.assertEqual(s.unpair()["server_revoked"], True)
        s.shutdown()

    def test_bad_code_is_mapped_from_the_cores_invalid_code(self):
        s, _ = make(self.tmp.name)
        with self.assertRaises(TasukiError) as cm:
            s.pair(self.srv.url, "WRONG-CODE")
        self.assertEqual((cm.exception.status, cm.exception.code), (400, "bad_code"))
        s.shutdown()

    def test_an_unwritable_mirror_dir_never_stops_the_delegation_worker(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        s, _ = make(self.tmp.name)
        s.mirror_dir = lambda: blocker / "mirror"                    # cannot be created
        s.mirror._get_dir = s.mirror_dir
        s.pair(self.srv.url, "PAIR-1234")
        self.assertTrue(wait_for(lambda: s.status()["mirror"]["errors"]["last"] == "dir_unwritable", 5))
        did = self.srv.push("hello while the mirror is broken")
        self.assertEqual(self.srv.wait_events(did)[-1]["type"], "final")
        self.assertTrue(s.status()["worker"]["running"])
        s.shutdown()

    def test_a_server_without_the_feed_does_not_break_the_worker_either(self):
        s, _ = make(self.tmp.name)                                   # the fake tasuki has no /changes: 404 for the mirror
        s.pair(self.srv.url, "PAIR-1234")
        self.assertTrue(wait_for(lambda: s.status()["mirror"]["errors"]["count"] >= 1, 8))
        did = self.srv.push("hello")
        self.assertEqual(self.srv.wait_events(did)[-1]["type"], "final")
        s.shutdown()

    def test_set_mirror_dir_validations_and_persistence(self):
        s, default = make(self.tmp.name)
        good = Path(self.tmp.name) / "my-tasks"
        r = s.set_mirror_dir(str(good))
        self.assertEqual(r["dir"], str(good))
        self.assertTrue(good.is_dir())
        self.assertEqual(oct(os.stat(good).st_mode & 0o777), "0o700")
        self.assertEqual(s.status()["config"]["mirror_dir"], str(good))
        self.assertEqual(json.loads((Path(self.tmp.name) / "state" / "config.json").read_text()), {"mirror_dir": str(good)})
        self.assertEqual(oct(os.stat(Path(self.tmp.name) / "state" / "config.json").st_mode & 0o777), "0o600")
        s2, _ = make(self.tmp.name)                                  # survives a restart
        self.assertEqual(s2.mirror_dir(), good)
        self.assertEqual(s.set_mirror_dir(None)["dir"], str(default))   # reset
        for bad, code in (("relative/path", "bad_dir"), ("/", "bad_dir"), (str(Path.home()), "bad_dir"), ("/etc", "bad_dir"),
                          (str(svc.PLUGIN_DIR), "bad_dir"), (str(svc.PLUGIN_DIR / "inside"), "bad_dir"), ("a\x00b", "bad_dir")):
            with self.assertRaises(TasukiError, msg=bad) as cm:
                s.set_mirror_dir(bad)
            self.assertEqual(cm.exception.code, code, bad)
        full = Path(self.tmp.name) / "full"
        full.mkdir()
        (full / "precious.txt").write_text("keep")
        with self.assertRaises(TasukiError) as cm:
            s.set_mirror_dir(str(full))
        self.assertEqual(cm.exception.code, "dir_not_empty")
        self.assertEqual((full / "precious.txt").read_text(), "keep")
        s.shutdown()
        s2.shutdown()

    def test_tasks_dir_tool_returns_the_path_and_the_note_and_needs_pairing(self):
        s, default = make(self.tmp.name)
        by = {n: h for n, _, _, h in __import__("_tasuki_hermes_core.tools", fromlist=["specs"]).specs(s.tools)}
        self.assertEqual(json.loads(by["tasuki_tasks_dir"]({})), {"ok": False, "error": "tasuki is not paired"})
        s.pair(self.srv.url, "PAIR-1234")
        r = json.loads(by["tasuki_tasks_dir"]({}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["dir"], str(default))
        self.assertIn("read it directly", r["note"].lower().replace("read it directly with your file tools", "read it directly"))
        self.assertIn("READ-ONLY", r["note"])
        self.assertIn("inbox/", r["note"])
        s.shutdown()

    def test_the_mirror_dir_is_never_inside_the_plugin_directory_by_default(self):
        s = svc.Service(PairingStore(MemoryStore(), Path(self.tmp.name) / "state"))
        os.environ["HERMES_HOME"] = str(Path(self.tmp.name) / "hh")
        try:
            self.assertEqual(s.default_mirror_dir(), Path(self.tmp.name) / "hh" / "tasuki")
            self.assertNotIn(str(svc.PLUGIN_DIR), str(s.default_mirror_dir()))
        finally:
            os.environ.pop("HERMES_HOME", None)

    def test_routes_config_and_resync(self):
        try:
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
        except Exception:
            self.skipTest("fastapi test client unavailable")
        import importlib.util
        s, default = make(self.tmp.name)
        svc._service = s
        spec = importlib.util.spec_from_file_location("tasuki_plugin_api_mirror_routes", helpers.ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/tasuki")
        c = TestClient(app)
        try:
            r = c.post("/api/plugins/tasuki/mirror/resync")
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "not_paired"))
            self.assertEqual(c.get("/api/plugins/tasuki/status").json()["mirror"]["dir"], str(default))
            new = Path(self.tmp.name) / "elsewhere"
            r = c.post("/api/plugins/tasuki/config", json={"mirror_dir": str(new)})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["config"]["mirror_dir"], str(new))
            self.assertEqual(r.json()["mirror"]["dir"], str(new))
            r = c.post("/api/plugins/tasuki/config", json={"mirror_dir": "relative"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "bad_dir"))
            r = c.post("/api/plugins/tasuki/config", json={"mirror_dir": None})
            self.assertEqual(r.json()["config"]["mirror_dir"], str(default))
            c.post("/api/plugins/tasuki/pair", json={"server_url": self.srv.url, "code": "PAIR-1234"})
            r = c.post("/api/plugins/tasuki/mirror/resync")
            self.assertEqual((r.status_code, r.json()["queued"]), (200, True))
        finally:
            s.shutdown()


if __name__ == "__main__":
    unittest.main()
