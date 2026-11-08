import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import FakeHermes, wait_for
from fake_tasuki import FakeTasuki
from _tasuki_hermes_core import CAPABILITIES, PROTOCOL_VERSION, VERSION
from _tasuki_hermes_core import service as svc
from _tasuki_hermes_core.client import TasukiError
from _tasuki_hermes_core.store import MemoryStore, PairingStore, TOKEN_KEY

ROOT = helpers.ROOT


def make_service(tmp):
    sec = MemoryStore()
    fh = FakeHermes()
    s = svc.Service(PairingStore(sec, Path(tmp)), gateway_url=lambda: "ws://fake/api/ws?token=X", connect=fh.connect,
                    worker_kwargs=dict(heartbeat_s=0.2, poll_wait=1, lock_path=None))
    return s, sec, fh


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = FakeTasuki(max_wait=0.3).start()
        self.s, self.sec, self.fh = make_service(self.tmp.name)

    def tearDown(self):
        self.s.shutdown()
        self.srv.stop()
        self.tmp.cleanup()

    def test_pair_status_delegate_unpair(self):
        self.assertFalse(self.s.status()["paired"])
        r = self.s.pair(self.srv.url + "/", "PAIR-1234")
        self.assertEqual((r["paired"], r["agent_id"], r["server"]["protocol_version"]), (True, self.srv.agent_id, 1))
        self.assertIn("delegate", r["scopes"])
        body = self.srv.pair_bodies[0]
        self.assertEqual(body["agent"]["kind"], "hermes")
        self.assertEqual(body["agent"]["version"], VERSION)
        self.assertEqual(body["agent"]["capabilities"], CAPABILITIES)
        self.assertTrue(body["agent"]["name"].startswith("Hermes on "))
        self.assertEqual(self.sec.get(TOKEN_KEY), self.srv.token)                # token in the secret store
        st = self.s.status()
        self.assertEqual((st["version"], st["protocol_version"], st["paired"], st["agent_id"]), (VERSION, PROTOCOL_VERSION, True, self.srv.agent_id))
        self.assertEqual(st["server"], {"origin": self.srv.url})
        self.assertTrue(st["worker"]["running"])
        self.assertEqual(st["capabilities"], CAPABILITIES)
        self.assertNotIn(self.srv.token, json.dumps(st))
        did = self.srv.push("hello there")                                       # a paired worker serves delegations
        self.assertEqual(self.srv.wait_events(did)[-1]["type"], "final")
        self.assertIsNotNone(self.s.status()["worker"]["last_claim_at"])
        u = self.s.unpair()
        self.assertEqual(u, {"paired": False, "was_paired": True, "server_revoked": True})
        self.assertIsNone(self.sec.get(TOKEN_KEY))
        st = self.s.status()
        self.assertFalse(st["paired"])
        self.assertFalse(st["worker"]["running"])
        self.assertFalse(self.s.tools.paired())

    def test_bad_code_and_urls(self):
        with self.assertRaises(TasukiError) as c:
            self.s.pair(self.srv.url, "WRONG")
        self.assertEqual(c.exception.code, "bad_code")
        self.assertFalse(self.s.status()["paired"])
        for url in ("http://example.com", "not a url"):
            with self.assertRaises(TasukiError):
                self.s.pair(url, "PAIR-1234")
        with self.assertRaises(TasukiError):
            self.s.pair(self.srv.url, "")
        with self.assertRaises(TasukiError) as c:
            self.s.pair("http://127.0.0.1:1", "PAIR-1234")
        self.assertEqual(c.exception.code, "unreachable")

    def test_version_negotiation_refuses_other_protocol(self):
        self.srv.protocol = 2
        with self.assertRaises(TasukiError) as c:
            self.s.pair(self.srv.url, "PAIR-1234")
        self.assertEqual((c.exception.code, c.exception.status), ("protocol_mismatch", 409))
        self.assertFalse(self.s.status()["paired"])
        self.assertIsNone(self.sec.get(TOKEN_KEY))                               # nothing stored on a mismatch

    def test_repair_replaces_pairing(self):
        self.s.pair(self.srv.url, "PAIR-1234")
        old = self.srv.token
        self.srv.token = "ts_new"
        self.s.pair(self.srv.url, "PAIR-1234")
        self.assertEqual(self.sec.get(TOKEN_KEY), "ts_new")
        self.assertNotEqual(old, "ts_new")
        self.assertTrue(self.s.status()["worker"]["running"])

    def test_status_when_server_revokes(self):
        self.s.pair(self.srv.url, "PAIR-1234")
        self.srv.revoke()
        self.assertTrue(wait_for(lambda: self.s.status()["worker"]["last_error"] == "unauthorized", 8))
        self.assertTrue(self.s.status()["paired"])                               # still "paired" locally until the user unpairs


class RegisterAndApiTests(unittest.TestCase):
    def test_register_with_fake_ctx(self):
        spec = importlib.util.spec_from_file_location("tasuki_plugin_pkg", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        calls = []

        class Ctx:
            def register_tool(self, **kw):
                calls.append(kw)

            def register_hook(self, *a, **k):
                raise AssertionError("v0 registers no hooks")
        mod.register(Ctx())
        self.assertEqual(len(calls), 14)
        for kw in calls:
            self.assertEqual(kw["toolset"], "tasuki")
            self.assertTrue(kw["name"].startswith("tasuki_"))
            self.assertEqual(kw["schema"]["name"], kw["name"])
            self.assertEqual(kw["schema"]["parameters"]["type"], "object")
            self.assertTrue(callable(kw["handler"]) and callable(kw["check_fn"]))
        out = json.loads(next(c for c in calls if c["name"] == "tasuki_food_week")["handler"]({}))
        self.assertEqual(out["error"], "tasuki is not paired")

    def test_plugin_yaml_and_manifest(self):
        import yaml
        y = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        self.assertEqual((y["name"], y["manifest_version"], y["kind"]), ("tasuki", 1, "standalone"))
        self.assertEqual(y["version"], VERSION)
        self.assertNotIn("provides_hooks", y)
        m = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        self.assertEqual((m["name"], m["api"], m["version"]), ("tasuki", "plugin_api.py", VERSION))
        self.assertTrue(m["tab"]["hidden"])
        self.assertTrue((ROOT / "LICENSE").read_text().startswith("MIT License"))
        self.assertEqual(m["version"], VERSION)                     # plugin.yaml, manifest.json and core agree

    def test_routes(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("fastapi test client unavailable")
        tmp = tempfile.TemporaryDirectory()
        s, sec, fh = make_service(tmp.name)
        svc._service = s
        spec = importlib.util.spec_from_file_location("tasuki_plugin_api_under_test", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/tasuki")
        c = TestClient(app)
        srv = FakeTasuki(max_wait=0.3).start()
        try:
            st = c.get("/api/plugins/tasuki/status").json()
            self.assertEqual((st["paired"], st["protocol_version"], st["version"]), (False, 1, VERSION))
            r = c.post("/api/plugins/tasuki/pair", json={"server_url": srv.url, "code": "nope"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"]["code"], "bad_code")
            r = c.post("/api/plugins/tasuki/pair", json={"server_url": "http://example.com", "code": "x"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"]["code"], "insecure_server_url")
            self.assertEqual(c.post("/api/plugins/tasuki/pair", json={"server_url": srv.url}).status_code, 422)
            r = c.post("/api/plugins/tasuki/pair", json={"server_url": srv.url, "code": "PAIR-1234"})
            self.assertEqual(r.status_code, 200, r.text)
            j = r.json()
            self.assertEqual((j["paired"], j["agent_id"], j["server"]["protocol_version"]), (True, srv.agent_id, 1))
            self.assertNotIn(srv.token, r.text)
            st = c.get("/api/plugins/tasuki/status").json()
            self.assertTrue(st["paired"] and st["worker"]["running"])
            self.assertNotIn(srv.token, json.dumps(st))
            srv.protocol = 9
            r = c.post("/api/plugins/tasuki/pair", json={"server_url": srv.url, "code": "PAIR-1234"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "protocol_mismatch"))
            r = c.post("/api/plugins/tasuki/unpair")
            self.assertEqual(r.json(), {"paired": False, "was_paired": True, "server_revoked": True})
            self.assertFalse(c.get("/api/plugins/tasuki/status").json()["paired"])
        finally:
            s.shutdown()
            srv.stop()
            tmp.cleanup()

    def test_gateway_url_dials_the_bound_interface_with_the_right_credential(self):
        import types
        tmp = tempfile.TemporaryDirectory()
        s, _, _ = make_service(tmp.name)
        svc._service = s
        spec = importlib.util.spec_from_file_location("tasuki_plugin_api_gw", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        state = types.SimpleNamespace(bound_port=9119, bound_host="0.0.0.0", auth_required=True)
        ws_mod = types.ModuleType("hermes_cli.web_server")
        ws_mod._SESSION_TOKEN, ws_mod.app = "SESSIONTOK", types.SimpleNamespace(state=state)
        tick = types.ModuleType("hermes_cli.dashboard_auth.ws_tickets")
        tick.internal_ws_credential = lambda: "INTERNALCRED"
        saved = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.web_server", "hermes_cli.dashboard_auth", "hermes_cli.dashboard_auth.ws_tickets")}
        sys.modules.update({"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.web_server": ws_mod,
                            "hermes_cli.dashboard_auth": types.ModuleType("hermes_cli.dashboard_auth"), "hermes_cli.dashboard_auth.ws_tickets": tick})
        try:
            self.assertEqual(api.gateway_url(), "ws://127.0.0.1:9119/api/ws?internal=INTERNALCRED")      # gated + wildcard bind
            state.auth_required, state.bound_host = False, "127.0.0.1"
            self.assertEqual(api.gateway_url(), "ws://127.0.0.1:9119/api/ws?token=SESSIONTOK")            # loopback dashboard
            state.auth_required, state.bound_host = True, "127.0.0.2"
            self.assertTrue(api.gateway_url().startswith("ws://127.0.0.2:9119/"))                        # a concrete bind is dialled as bound
            state.bound_port = None
            with self.assertRaises(RuntimeError):
                api.gateway_url()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            tmp.cleanup()

    def test_routes_fail_closed_without_dashboard_auth(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("fastapi test client unavailable")
        tmp = tempfile.TemporaryDirectory()
        s, _, _ = make_service(tmp.name)
        svc._service = s
        spec = importlib.util.spec_from_file_location("tasuki_plugin_api_failclosed", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(api.router)
        old = os.environ.pop("TASUKI_PLUGIN_TEST")
        try:
            r = TestClient(app).get("/status")
            self.assertEqual(r.status_code, 503)                                 # no hermes auth guard importable: refuse
            self.assertEqual(TestClient(app).post("/unpair").status_code, 503)
        finally:
            os.environ["TASUKI_PLUGIN_TEST"] = old
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
