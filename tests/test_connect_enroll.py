"""0.3.0 onboarding: connect with the owner claim secret (body / secret store / local file) and enrol a phone with a device code."""
import importlib.util
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import wait_for
from fake_tasuki import FakeTasuki
from fake_core import FakeCore
from test_service_api import make_service
from _tasuki_hermes_core import FEATURES, VERSION
from _tasuki_hermes_core import service as svc
from _tasuki_hermes_core.client import HttpClient, TasukiError
from _tasuki_hermes_core.store import CLAIM_KEY, TOKEN_KEY
from _tasuki_hermes_core.worker import DelegationWorker

ROOT = helpers.ROOT
MARK = "CLAIMMARKER-9f3a7c-DONOTLEAK"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.srv = FakeTasuki(max_wait=0.3).start()
        self.srv.claim.claim_secret = MARK
        self.s, self.sec, self.fh = make_service(self.dir / "state")
        self.defaults = self.dir / "defaults"
        self.defaults.mkdir()
        self.default_files = [self.defaults / "a" / "claim.secret", self.defaults / "b" / "claim.secret"]
        p = mock.patch.object(svc, "default_claim_files", lambda: list(self.default_files))   # never read the real machine's claim.secret
        p.start()
        self.addCleanup(p.stop)
        self.logs = []

        class H(logging.Handler):
            def emit(h, rec):
                self.logs.append(rec.getMessage() + " " + str(rec.args))
        self.h = H(level=logging.DEBUG)
        logging.getLogger().addHandler(self.h)
        self.old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger().removeHandler(self.h)
        logging.getLogger().setLevel(self.old_level)
        self.s.shutdown()
        self.srv.stop()
        self.tmp.cleanup()

    def write_claim(self, path: Path, secret=MARK, mode=0o600):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret + "\n")
        os.chmod(path, mode)
        return path

    def assertNoSecret(self, *things):
        for t in things:
            self.assertNotIn(MARK, t if isinstance(t, str) else json.dumps(t, default=str))
        self.assertNotIn(MARK, "\n".join(self.logs))


class ClaimSourceTests(Base):
    def test_body_source_connects_and_stores_token_only(self):
        st = self.s.connect(self.srv.url + "/", claim_secret=MARK)
        self.assertEqual((st["paired"], st["claimed"], st["claim_source"], st["agent_id"]), (True, True, "body", self.srv.agent_id))
        self.assertTrue(st["enroll_available"] and st["worker"]["running"])
        self.assertEqual(st["server"], {"origin": self.srv.url})
        self.assertIn("claim", st["features"])
        self.assertIn("enroll_device", st["features"])
        self.assertEqual(st["features"], FEATURES)
        self.assertEqual(self.sec.get(TOKEN_KEY), self.srv.token)
        body = self.srv.claim.claim_attempts[0]
        self.assertEqual((body["claim_secret"], body["agent"]["kind"], body["agent"]["version"]), (MARK, "hermes", VERSION))
        self.assertTrue(body["agent"]["name"].startswith("Hermes on "))
        self.assertIsNone(self.sec.get(CLAIM_KEY))                 # not remembered by default
        self.assertFalse(st["claim_secret_remembered"])
        self.assertNoSecret(st)
        for f in self.dir.rglob("*"):                              # no plaintext secret anywhere on disk
            if f.is_file():
                self.assertNotIn(MARK.encode(), f.read_bytes(), str(f))
        did = self.srv.push("hello over a claimed connection")
        self.assertEqual(self.srv.wait_events(did)[-1]["type"], "final")

    def test_remember_true_stores_in_secret_store_only_from_body(self):
        st = self.s.connect(self.srv.url, claim_secret=MARK, remember=True)
        self.assertEqual(self.sec.get(CLAIM_KEY), MARK)
        self.assertTrue(st["claim_secret_remembered"])
        self.assertNoSecret(st)
        for f in self.dir.rglob("*"):
            if f.is_file():
                self.assertNotIn(MARK.encode(), f.read_bytes(), str(f))
        # a file-sourced secret is not copied into the store even with remember:true
        self.s.unpair()
        self.sec.delete(CLAIM_KEY)
        self.write_claim(self.default_files[0])
        st = self.s.connect(self.srv.url, remember=True)
        self.assertEqual(st["claim_source"], "file")
        self.assertIsNone(self.sec.get(CLAIM_KEY))

    def test_wrong_secret_with_remember_is_not_stored(self):
        with self.assertRaises(TasukiError):
            self.s.connect(self.srv.url, claim_secret="nope", remember=True)
        self.assertIsNone(self.sec.get(CLAIM_KEY))

    def test_secret_store_source(self):
        self.sec.set(CLAIM_KEY, MARK)
        st = self.s.connect(self.srv.url)
        self.assertEqual((st["claimed"], st["claim_source"]), (True, "secret_store"))
        self.assertTrue(st["claim_secret_remembered"])
        self.assertNoSecret(st)

    def test_file_source_default_candidates_in_order(self):
        self.write_claim(self.default_files[1])
        self.assertEqual(self.s.connect(self.srv.url)["claim_source"], "file")      # only the 2nd candidate exists
        self.s.unpair()
        self.write_claim(self.default_files[0], "OTHER-SECRET-FIRST")
        with self.assertRaises(TasukiError) as c:                                    # first existing candidate wins: its (wrong) secret is used
            self.s.connect(self.srv.url)
        self.assertEqual(c.exception.code, "invalid_secret")

    def test_config_claim_file_goes_first(self):
        cf = self.write_claim(self.dir / "cfg" / "mine.secret")
        self.write_claim(self.default_files[0], "OTHER-SECRET-FIRST")
        self.s.store.dir.mkdir(parents=True, exist_ok=True)
        self.s.config_path.write_text(json.dumps({"claim_file": str(cf)}))
        self.assertEqual(self.s.connect(self.srv.url)["claim_source"], "file")

    def test_precedence_body_over_store_over_file(self):
        self.sec.set(CLAIM_KEY, "STORE-SECRET-WRONG")
        self.write_claim(self.default_files[0], "FILE-SECRET-WRONG")
        self.assertEqual(self.s.connect(self.srv.url, claim_secret=MARK)["claim_source"], "body")   # body beats a wrong store + file
        self.s.unpair()
        with self.assertRaises(TasukiError) as c:                                                  # store beats file: its wrong secret is what is tried
            self.s.connect(self.srv.url)
        self.assertEqual(c.exception.code, "invalid_secret")
        self.assertEqual(self.srv.claim.claim_attempts[-1]["claim_secret"], "STORE-SECRET-WRONG")
        self.sec.delete(CLAIM_KEY)
        with self.assertRaises(TasukiError):
            self.s.connect(self.srv.url)
        self.assertEqual(self.srv.claim.claim_attempts[-1]["claim_secret"], "FILE-SECRET-WRONG")

    def test_no_secret_anywhere_is_409_claim_secret_needed(self):
        with self.assertRaises(TasukiError) as c:
            self.s.connect(self.srv.url)
        e = c.exception
        self.assertEqual((e.status, e.code), (409, "claim_secret_needed"))
        self.assertEqual(str(e), "tasuki needs its claim secret: it is on your tasuki server in claim.secret; paste it here once")
        self.assertEqual(self.srv.claim.claim_attempts, [])                    # the server was never called
        self.assertFalse(self.s.status()["paired"])
        for blank in ("", "   ", None):
            with self.assertRaises(TasukiError):
                self.s.connect(self.srv.url, claim_secret=blank)

    def test_wrong_secret_passes_the_cores_400_as_friendly_invalid_secret(self):
        with self.assertRaises(TasukiError) as c:
            self.s.connect(self.srv.url, claim_secret="definitely-wrong")
        e = c.exception
        self.assertEqual((e.status, e.code), (400, "invalid_secret"))
        self.assertIn("claim.secret", str(e))
        self.assertNotIn("definitely-wrong", str(e))
        self.assertFalse(self.s.status()["paired"])
        self.assertIsNone(self.sec.get(TOKEN_KEY))

    def test_lockout_and_rate_limit_pass_through(self):
        self.srv.claim.lock_after = 2
        for _ in range(2):
            with self.assertRaises(TasukiError):
                self.s.connect(self.srv.url, claim_secret="wrong")
        with self.assertRaises(TasukiError) as c:                              # locked even for the right secret
            self.s.connect(self.srv.url, claim_secret=MARK)
        self.assertEqual((c.exception.status, c.exception.code), (429, "locked_out"))
        self.assertIn("locked", str(c.exception))
        self.srv.claim.unlock()
        self.srv.claim.claim_rate_limited = True
        with self.assertRaises(TasukiError) as c:
            self.s.connect(self.srv.url, claim_secret=MARK)
        self.assertEqual((c.exception.status, c.exception.code), (429, "rate_limited"))
        self.srv.claim.claim_rate_limited = False
        self.assertTrue(self.s.connect(self.srv.url, claim_secret=MARK)["paired"])

    def test_protocol_mismatch_stores_nothing_and_revokes_the_new_agent(self):
        self.srv.protocol = 2
        with self.assertRaises(TasukiError) as c:
            self.s.connect(self.srv.url, claim_secret=MARK)
        self.assertEqual((c.exception.status, c.exception.code), (409, "protocol_mismatch"))
        self.assertIsNone(self.sec.get(TOKEN_KEY))
        self.assertEqual(self.srv.self_revokes, 1)

    def test_bad_server_urls(self):
        for url, code in (("http://example.com", "insecure_server_url"), ("not a url", "bad_server_url")):
            with self.assertRaises(TasukiError) as c:
                self.s.connect(url, claim_secret=MARK)
            self.assertEqual(c.exception.code, code)
        with self.assertRaises(TasukiError) as c:
            self.s.connect("http://127.0.0.1:1", claim_secret=MARK)
        self.assertEqual(c.exception.code, "unreachable")
        self.assertNoSecret(str(c.exception))

    def test_secret_never_in_logs_errors_status_or_exceptions(self):
        outputs = []
        for kw in (dict(claim_secret=MARK + "x"), dict()):
            try:
                self.s.connect(self.srv.url, **kw)
            except TasukiError as e:
                outputs += [str(e), e.code, json.dumps(e.extra)]
        self.s.connect(self.srv.url, claim_secret=MARK, remember=True)
        outputs += [json.dumps(self.s.status()), repr(self.s.pairing())]
        self.s.enroll_device("Pixel")
        self.s.unpair()
        self.assertNoSecret(*outputs)
        self.assertTrue(self.logs)                                             # the capture really saw log lines


class ClaimFileTests(Base):
    def test_0600_ok_no_warning(self):
        f = self.write_claim(self.default_files[0])
        self.assertEqual(svc.read_claim_file(f), (MARK, None))
        self.assertFalse(any("world-readable" in m for m in self.logs))

    def test_world_readable_but_mine_is_allowed_with_a_warning(self):
        f = self.write_claim(self.default_files[0], mode=0o644)
        self.assertEqual(svc.read_claim_file(f), (MARK, None))
        self.assertTrue(any("world-readable" in m for m in self.logs))
        self.assertNoSecret()

    def test_world_readable_owned_by_someone_else_is_refused(self):
        f = self.write_claim(self.default_files[0], mode=0o644)
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(svc.read_claim_file(f), (None, "world_readable_not_yours"))
        os.chmod(f, 0o640)                                                     # group-readable only: fine even if another user owns it
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(svc.read_claim_file(f), (MARK, None))

    def test_world_writable_symlink_dir_and_garbage_are_refused(self):
        f = self.write_claim(self.default_files[0], mode=0o666)
        self.assertEqual(svc.read_claim_file(f), (None, "world_writable"))
        real = self.write_claim(self.dir / "real.secret")
        link = self.dir / "link.secret"
        link.symlink_to(real)
        self.assertIsNone(svc.read_claim_file(link)[0])                        # never follows a symlink
        self.assertEqual(svc.read_claim_file(self.dir), (None, "not_a_file"))
        self.assertEqual(svc.read_claim_file(self.dir / "missing"), (None, None))
        for junk in ("", "two words", "x" * 400):
            self.assertEqual(svc.read_claim_file(self.write_claim(self.dir / "j.secret", junk))[0], None)
        self.assertEqual(svc.read_claim_file(self.write_claim(self.dir / "j.secret", MARK + "\nsecond line"))[0], MARK)

    def test_refused_file_is_reported_in_the_409_without_the_secret(self):
        self.write_claim(self.default_files[0], mode=0o666)
        with self.assertRaises(TasukiError) as c:
            self.s.connect(self.srv.url)
        self.assertEqual(c.exception.code, "claim_secret_needed")
        self.assertEqual(c.exception.extra["refused"][0]["reason"], "world_writable")
        self.assertNoSecret(json.dumps(c.exception.extra))
        self.assertEqual(self.srv.claim.claim_attempts, [])


class ReconnectTests(Base):
    def test_reconnect_replaces_token_in_place_restarts_worker_once(self):
        starts = []
        real_start = DelegationWorker.start

        def counting(w):
            starts.append(1)
            return real_start(w)
        with mock.patch.object(DelegationWorker, "start", counting):
            self.s.connect(self.srv.url, claim_secret=MARK)
            old_token, old_agent = self.srv.token, self.srv.agent_id
            self.assertEqual(len(starts), 1)
            st = self.s.connect(self.srv.url, claim_secret=MARK)
        self.assertEqual(len(starts), 2)                                       # exactly one restart per connect
        self.assertNotEqual(self.srv.token, old_token)
        self.assertEqual(self.sec.get(TOKEN_KEY), self.srv.token)
        self.assertEqual(st["agent_id"], self.srv.agent_id)
        self.assertEqual(st["agent_id"], old_agent)                           # replaced in place: same agent id as the real core
        self.assertEqual(self.srv.revoked_bearers, [])                         # nothing to revoke: the old token is already dead, the agent is alive
        self.assertTrue(st["worker"]["running"])
        self.assertTrue(wait_for(lambda: self.s.status()["worker"]["last_error"] is None, 3))
        did = self.srv.push("after reconnect")
        self.assertEqual(self.srv.wait_events(did)[-1]["type"], "final")

    def test_unpair_then_connect(self):
        self.s.connect(self.srv.url, claim_secret=MARK, remember=True)
        u = self.s.unpair()
        self.assertEqual(u, {"paired": False, "was_paired": True, "server_revoked": True})
        self.assertIsNone(self.sec.get(TOKEN_KEY))
        self.assertEqual(self.sec.get(CLAIM_KEY), MARK)                        # remembered on purpose: kept
        st = self.s.connect(self.srv.url)                                      # connects again from the secret store, no body
        self.assertEqual((st["paired"], st["claim_source"]), (True, "secret_store"))
        self.assertTrue(st["worker"]["running"])

    def test_legacy_pair_still_works_and_is_not_claimed(self):
        r = self.s.pair(self.srv.url, "PAIR-1234")
        self.assertTrue(r["paired"])
        st = self.s.status()
        self.assertEqual((st["paired"], st["claimed"], st["enroll_available"]), (True, False, False))
        with self.assertRaises(TasukiError) as c:
            self.s.enroll_device("Pixel")
        self.assertEqual((c.exception.status, c.exception.code), (409, "missing_scope"))
        self.assertEqual(self.srv.claim.device_codes, [])
        st = self.s.connect(self.srv.url, claim_secret=MARK)                   # a legacy pairing upgrades to a claim connection
        self.assertTrue(st["claimed"] and st["enroll_available"])

    def test_status_when_unpaired(self):
        st = self.s.status()
        self.assertEqual((st["paired"], st["claimed"], st["enroll_available"], st["claim_secret_remembered"]), (False, False, False, False))
        self.assertEqual(st["features"], ["claim", "enroll_device", "pair_legacy"])
        self.assertEqual(st["protocol_version"], 1)


class EnrollTests(Base):
    def test_not_paired_is_409(self):
        with self.assertRaises(TasukiError) as c:
            self.s.enroll_device("Pixel")
        self.assertEqual((c.exception.status, c.exception.code), (409, "not_paired"))
        self.assertEqual(self.srv.claim.device_code_calls, [])

    def test_issues_code_with_expiry_passthrough_and_never_reuses(self):
        self.s.connect(self.srv.url, claim_secret=MARK)
        a = self.s.enroll_device("  Sam's phone  ")
        b = self.s.enroll_device(None)
        self.assertEqual(set(a), {"code", "expires_at", "server_url"})
        self.assertRegex(a["code"], r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.assertEqual(a["server_url"], self.srv.url)
        self.assertEqual(a["expires_at"], self.srv.claim.device_codes[0]["expires_at"])    # the core's expiry, untouched
        self.assertNotEqual(a["code"], b["code"])
        self.assertEqual(self.srv.claim.device_code_calls[0][1], {"device_name": "Sam's phone"})
        self.assertEqual(self.srv.claim.device_code_calls[1][1], {})
        self.assertEqual(len(self.srv.claim.device_codes), 2)                  # each call minted a fresh code on the core
        self.assertFalse(any(a["code"] in m or b["code"] in m for m in self.logs))        # codes are never logged
        self.assertTrue(any("enroll-device" in m for m in self.logs))

    def test_core_rate_limit_is_passed_through(self):
        self.s.connect(self.srv.url, claim_secret=MARK)
        for _ in range(3):
            self.s.enroll_device("p")
        with self.assertRaises(TasukiError) as c:
            self.s.enroll_device("p")
        self.assertEqual((c.exception.status, c.exception.code), (429, "rate_limited"))
        self.assertEqual(len(self.srv.claim.device_codes), 3)

    def test_local_rate_limit_is_5_per_10_minutes(self):
        self.s.connect(self.srv.url, claim_secret=MARK)
        self.srv.claim.device_limit = 1000
        clock = [1000.0]
        self.s._now = lambda: clock[0]
        for _ in range(5):
            self.s.enroll_device("p")
        with self.assertRaises(TasukiError) as c:
            self.s.enroll_device("p")
        self.assertEqual((c.exception.status, c.exception.code), (429, "rate_limited"))
        self.assertIn("retry_after", c.exception.extra)
        self.assertEqual(len(self.srv.claim.device_codes), 5)                  # the refused call never reached the core
        clock[0] += 601
        self.assertTrue(self.s.enroll_device("p")["code"])

    def test_revoked_agent_is_409_not_401(self):
        self.s.connect(self.srv.url, claim_secret=MARK)
        self.srv.revoke()
        with self.assertRaises(TasukiError) as c:
            self.s.enroll_device("p")
        self.assertEqual((c.exception.status, c.exception.code), (409, "agent_revoked"))      # a 401 here would look like a lost dashboard login

    def test_device_name_is_cleaned_and_capped(self):
        self.s.connect(self.srv.url, claim_secret=MARK)
        self.s.enroll_device("a\x00b\nc" + "x" * 200)
        n = self.srv.claim.device_code_calls[0][1]["device_name"]
        self.assertEqual(len(n), 80)
        self.assertTrue(n.isprintable())


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
        except Exception:
            self.skipTest("fastapi test client unavailable")
        svc._service = self.s
        spec = importlib.util.spec_from_file_location("tasuki_plugin_api_connect", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/tasuki")
        self.c = TestClient(app)
        self.P = "/api/plugins/tasuki/"

    def test_connect_409_then_retry_with_secret_then_enroll(self):
        r = self.c.post(self.P + "connect", json={"server_url": self.srv.url})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "claim_secret_needed")
        self.assertIn("claim.secret", r.json()["detail"]["message"])
        r = self.c.post(self.P + "connect", json={"server_url": self.srv.url, "claim_secret": "wrong"})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_secret"))
        r = self.c.post(self.P + "connect", json={"server_url": self.srv.url, "claim_secret": MARK})
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual((j["paired"], j["claimed"], j["claim_source"], j["enroll_available"]), (True, True, "body", True))
        self.assertNotIn(MARK, r.text)
        self.assertNotIn(self.srv.token, r.text)
        self.assertIsNone(self.sec.get(CLAIM_KEY))
        r = self.c.post(self.P + "enroll-device", json={"device_name": "Pixel"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(r.json()), {"code", "expires_at", "server_url"})
        r = self.c.post(self.P + "enroll-device")                              # no body at all is fine
        self.assertEqual(r.status_code, 200, r.text)
        st = self.c.get(self.P + "status").json()
        self.assertTrue(st["claimed"] and st["enroll_available"])
        self.assertNoSecret(st)
        r = self.c.post(self.P + "unpair")
        self.assertTrue(r.json()["server_revoked"])
        r = self.c.post(self.P + "enroll-device", json={})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "not_paired"))

    def test_bad_bodies_never_echo_the_secret(self):
        r = self.c.post(self.P + "connect", json={"server_url": self.srv.url, "claim_secret": MARK * 20})      # too long
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "bad_request"))
        self.assertNotIn(MARK, r.text)
        r = self.c.post(self.P + "connect", json={"server_url": 5, "claim_secret": MARK})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(MARK, r.text)
        r = self.c.post(self.P + "connect", content=b"not json " + MARK.encode())
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(MARK, r.text)
        r = self.c.post(self.P + "connect", json={"claim_secret": MARK})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "bad_request"))
        self.assertNoSecret(r.text)

    def test_remember_flag_and_file_source_over_the_route(self):
        r = self.c.post(self.P + "connect", json={"server_url": self.srv.url, "claim_secret": MARK, "remember": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sec.get(CLAIM_KEY), MARK)
        self.assertTrue(r.json()["claim_secret_remembered"])
        self.assertNotIn(MARK, r.text)
        self.c.post(self.P + "unpair")
        self.sec.delete(CLAIM_KEY)
        self.write_claim(self.default_files[0])
        r = self.c.post(self.P + "connect", json={"server_url": self.srv.url})
        self.assertEqual((r.status_code, r.json()["claim_source"]), (200, "file"))
        self.assertNotIn(MARK, r.text)

    def test_legacy_pair_route_still_there(self):
        r = self.c.post(self.P + "pair", json={"server_url": self.srv.url, "code": "PAIR-1234"})
        self.assertEqual(r.status_code, 200, r.text)
        st = self.c.get(self.P + "status").json()
        self.assertEqual((st["paired"], st["claimed"], st["enroll_available"]), (True, False, False))


class FakeCoreTests(unittest.TestCase):
    def test_fake_core_claim_and_device_codes(self):
        fc = FakeCore()
        fc.claim.claim_secret = "S3"
        fc.claim.lock_after = 2
        for _ in range(2):
            with self.assertRaises(TasukiError) as c:
                fc.request("POST", "/api/v1/owner/claim", {"claim_secret": "bad"}, auth=False)
            self.assertIn(c.exception.code, ("invalid_claim_secret",))
        with self.assertRaises(TasukiError) as c:
            fc.request("POST", "/api/v1/owner/claim", {"claim_secret": "S3"}, auth=False)
        self.assertEqual((c.exception.status, c.exception.code), (429, "locked_out"))
        fc.claim.unlock()
        r = fc.request("POST", "/api/v1/owner/claim", {"claim_secret": "S3"}, auth=False)
        self.assertIn("enroll_device", r["scopes"])
        self.assertEqual(r["server"]["protocol_version"], 1)
        codes = {fc.request("POST", "/api/v1/agents/me/device-codes", {"device_name": "p"})["code"] for _ in range(3)}
        self.assertEqual(len(codes), 3)
        with self.assertRaises(TasukiError) as c:
            fc.request("POST", "/api/v1/agents/me/device-codes", {})
        self.assertEqual((c.exception.status, c.exception.code), (429, "rate_limited"))


if __name__ == "__main__":
    unittest.main()
