import tempfile
import time
import unittest
from pathlib import Path

import helpers
from helpers import FakeHermes, wait_for
from fake_tasuki import FakeTasuki
from _tasuki_hermes_core.client import HttpClient
from _tasuki_hermes_core.hermes_ws import HermesBridge
from _tasuki_hermes_core.store import Pairing
from _tasuki_hermes_core.worker import DelegationWorker


class Harness:
    def __init__(self, script=None, **wk):
        self.srv = FakeTasuki(max_wait=0.3).start()
        self.hermes = FakeHermes(script)
        self.pairing = Pairing(self.srv.url, self.srv.token, self.srv.agent_id)
        kw = dict(heartbeat_s=0.15, poll_wait=1, timeout_s=20)
        kw.update(wk)
        self.w = DelegationWorker(lambda: self.pairing, lambda p: HttpClient(p.server, p.token, timeout=5),
                                  lambda: HermesBridge(lambda: "ws://fake/api/ws?token=SECRETCRED", self.hermes.connect), **kw)

    def __enter__(self):
        self.w.start()
        return self

    def __exit__(self, *a):
        self.w.stop()
        self.srv.stop()


def types(evs):
    return [e["type"] for e in evs]


class WorkerTests(unittest.TestCase):
    def test_claim_stream_final_with_voice_surface(self):
        def script(text, sid):
            return [(0, "message.delta", {"text": "Let me check your calendar.", "segment": 0}),
                    (0, "tool.start", {"name": "tasuki_calendar_events", "context": "x"}),
                    (0.05, "message.delta", {"text": "You have **one** meeting at ten, see https://x.example/a.", "segment": 1}),
                    (0, "message.complete", {"text": "You have **one** meeting at ten, see https://x.example/a.", "status": "complete"})]
        with Harness(script) as h:
            did = h.srv.push("what is on today", context="user: hi", screen_context="Tasks tab", session_key="k1")
            evs = h.srv.wait_events(did)
            self.assertEqual(types(evs), ["speakable", "commentary", "final"], evs)
            self.assertEqual(evs[0]["text"], "Checking your calendar.")
            self.assertEqual([e["seq"] for e in evs], [1, 2, 3])
            self.assertEqual(evs[1]["text"], "You have one meeting at ten, see.")      # sanitised for speech; pre-tool narration dropped
            self.assertNotIn("Let me check", " ".join(e.get("text", "") for e in evs))
            self.assertEqual(evs[2]["data"]["source"], "hermes")
            sid, p = h.hermes.prompts[0]
            self.assertEqual(p["surface"], "voice-live")
            self.assertEqual(p["voice_context"], "user: hi")
            self.assertIn("Voice reply rules", p["text"])                              # versioned brevity note appended
            self.assertIn("Tasks tab", p["text"])
            self.assertRegex(h.hermes.created[0]["title"], r"^tasuki voice \d\d:\d\d$")
            self.assertFalse(h.hermes.created[0]["close_on_disconnect"])
            self.assertIsNotNone(h.w.status()["last_claim_at"])
            self.assertTrue(h.w.status()["running"])

    def test_non_voice_surface_gets_raw_reply_no_note(self):
        with Harness(lambda t, s: [(0, "message.complete", {"text": "## Hi\n* a", "status": "complete"})]) as h:
            did = h.srv.push("hello", surface="chat", context="ctx here")
            evs = h.srv.wait_events(did)
            self.assertEqual(evs[-1]["text"], "## Hi\n* a")
            p = h.hermes.prompts[0][1]
            self.assertNotIn("surface", p)
            self.assertNotIn("Voice reply rules", p["text"])
            self.assertTrue(p["text"].startswith("[Recent conversation"))

    def test_progress_lines_deduped_and_capped(self):
        script = lambda t, s: [(0, "tool.start", {"name": "calendar_x"}), (0, "tool.start", {"name": "calendar_y"}),
                               (0, "tool.start", {"name": "mail_search"}), (0, "message.complete", {"text": "Done.", "status": "complete"})]
        with Harness(script) as h:
            did = h.srv.push("go", surface="chat")
            evs = h.srv.wait_events(did)
            self.assertEqual(types(evs), ["progress", "progress", "commentary", "final"])
            self.assertEqual([e["text"] for e in evs[:2]], ["Checking your calendar.", "Checking your email."])

    def test_titles_are_unique_within_a_minute(self):
        with Harness(None) as h:
            for i in range(3):
                h.srv.wait_events(h.srv.push(f"q{i}", session_key=f"t{i}"))
            titles = [c["title"] for c in h.hermes.created]
            self.assertEqual(len(set(titles)), 3, titles)
            self.assertTrue(all(t.startswith("tasuki voice ") for t in titles))

    def test_hold_to_ask_session_is_titled_from_what_was_said(self):
        with Harness(None) as h:
            h.srv.wait_events(h.srv.push("move the Northwind proposal to Friday and let Priya know about the new install window", surface="hold-to-ask"))
            title = h.hermes.created[0]["title"]
            self.assertTrue(title.startswith("tasuki: move the Northwind proposal"), title)
            self.assertLessEqual(len(title), 60)
            p = h.hermes.prompts[0][1]
            self.assertNotIn("Voice reply rules", p["text"])                            # an ordinary prompt, not a voice turn

    def test_hermes_tool_lookup_is_not_announced(self):
        script = lambda t, s: [(0, "tool.start", {"name": "tool_search"}), (0, "tool.start", {"name": "tool_describe"}),
                               (0, "tool.start", {"name": "tool_call", "context": "tasuki_calendar_summary"}),
                               (0, "message.complete", {"text": "Done.", "status": "complete"})]
        with Harness(script) as h:
            evs = h.srv.wait_events(h.srv.push("go", surface="voice-live"))
            self.assertEqual(types(evs), ["speakable", "commentary", "final"])
            self.assertEqual(evs[0]["text"], "Checking your calendar.")

    def test_proposal_event(self):
        text = 'Found them.\nPROPOSAL: {"action":"delete","summary":"Delete these 2 tasks","items":["A","B"]}'
        with Harness(lambda t, s: [(0, "message.complete", {"text": text, "status": "complete"})]) as h:
            evs = h.srv.wait_events(h.srv.push("delete old tasks"))
            self.assertEqual(types(evs), ["proposal", "final"])
            self.assertEqual(evs[0]["data"]["items"], ["A", "B"])
            self.assertEqual(evs[0]["data"]["action"], "delete")
            self.assertTrue(evs[1]["data"]["awaiting_confirmation"])
            self.assertNotIn("PROPOSAL", evs[1]["text"])

    def test_cancel_via_heartbeat_interrupts_turn(self):
        script = lambda t, s: [(0, "tool.start", {"name": "web_search"}), (30, "message.complete", {"text": "never", "status": "complete"})]
        with Harness(script) as h:
            did = h.srv.push("long job")
            wait_for(lambda: h.hermes.prompts)
            time.sleep(0.1)
            h.srv.cancel(did)
            evs = h.srv.wait_events(did)
            self.assertEqual(evs[-1]["type"], "error")
            self.assertEqual(evs[-1]["data"]["code"], "cancelled")
            self.assertEqual(h.hermes.interrupts, ["sess1"])
            self.assertGreaterEqual(h.srv.heartbeats.get(did, 0), 1)

    def test_heartbeat_runs_while_working(self):
        script = lambda t, s: [(0.8, "message.complete", {"text": "ok", "status": "complete"})]
        with Harness(script, heartbeat_s=0.1) as h:
            did = h.srv.push("slow")
            h.srv.wait_events(did)
            self.assertGreaterEqual(h.srv.heartbeats.get(did, 0), 4)

    def test_one_turn_at_a_time_per_session_key(self):
        script = lambda t, s: [(0.25, "message.complete", {"text": f"done {t[:5]}", "status": "complete"})]
        with Harness(script) as h:
            a = h.srv.push("first one", session_key="same")
            b = h.srv.push("second one", session_key="same")
            self.assertTrue(h.srv.wait_events(a) and h.srv.wait_events(b))
            self.assertEqual(h.hermes.max_active, {"sess1": 1})
            self.assertEqual(len(h.hermes.created), 1)                              # the session was reused

    def test_different_keys_run_in_parallel_and_bounded(self):
        script = lambda t, s: [(0.4, "message.complete", {"text": "x", "status": "complete"})]
        with Harness(script, max_concurrent=2) as h:
            ids = [h.srv.push(f"job {i}", session_key=f"k{i}") for i in range(4)]
            wait_for(lambda: len(h.hermes.prompts) >= 2)
            time.sleep(0.15)
            self.assertEqual(len(h.hermes.prompts), 2)                               # never more than max_concurrent in flight
            for i in ids:
                self.assertTrue(h.srv.wait_events(i, timeout=15))
            self.assertEqual(len(h.hermes.prompts), 4)

    def test_sessions_bounded_lru(self):
        with Harness(None, max_sessions=2) as h:
            for i in range(4):
                h.srv.wait_events(h.srv.push(f"q{i}", session_key=f"k{i}"))
            self.assertEqual(len(h.hermes.created), 4)
            self.assertGreaterEqual(len(h.hermes.closed), 2)
            self.assertLessEqual(len(h.w._sessions), 2)

    def test_events_retry_is_idempotent(self):
        with Harness(None) as h:
            h.srv.fail_next_events = 2
            evs = h.srv.wait_events(h.srv.push("hi"), timeout=15)
            self.assertEqual(types(evs)[-1], "final")
            self.assertEqual(len({e["seq"] for e in evs}), len(evs))

    def test_server_down_backs_off_then_recovers(self):
        h = Harness(None)
        port = h.srv.port
        h.srv.stop()                                   # nothing listening
        h.w.start()
        try:
            self.assertTrue(wait_for(lambda: h.w.status()["last_error"] == "unreachable", 8))
            self.assertTrue(h.w.running)
            srv2 = FakeTasuki(port=port, max_wait=0.3).start()
            try:
                h.pairing = Pairing(srv2.url, srv2.token, srv2.agent_id)
                did = srv2.push("after outage")
                self.assertEqual(srv2.wait_events(did, timeout=20)[-1]["type"], "final")
                self.assertIsNone(h.w.status()["last_error"])
            finally:
                srv2.stop()
        finally:
            h.w.stop()

    def test_revoked_token_pauses_polling_until_new_pairing(self):
        with Harness(None) as h:
            h.srv.revoke()
            self.assertTrue(wait_for(lambda: h.w.status()["last_error"] == "unauthorized", 8))
            n = len(h.srv.requests)
            time.sleep(1.0)
            self.assertLessEqual(len(h.srv.requests) - n, 1)                        # stopped hammering the server
            self.assertTrue(h.w.running)

    def test_unpaired_idles_without_calls(self):
        with Harness(None) as h:
            h.pairing = None
            time.sleep(0.5)
            n = len(h.srv.requests)
            time.sleep(0.8)
            self.assertEqual(len(h.srv.requests), n)
            self.assertEqual(h.w.status()["last_error"], "not_paired")

    def test_hermes_unreachable_reports_error(self):
        h = Harness(None)
        h.hermes.fail_connect = 99
        with h:
            evs = h.srv.wait_events(h.srv.push("hello"))
            self.assertEqual(evs[-1]["type"], "error")
            self.assertEqual(evs[-1]["data"]["code"], "hermes_unreachable")

    def test_hermes_reconnect_after_gateway_restart(self):
        with Harness(None) as h:
            h.srv.wait_events(h.srv.push("one", session_key="a"))
            import asyncio
            fut = asyncio.run_coroutine_threadsafe(h.w._bridge._ws.close(), h.w._loop)
            fut.result(3)
            time.sleep(0.2)
            evs = h.srv.wait_events(h.srv.push("two", session_key="a"))
            self.assertEqual(evs[-1]["type"], "final")
            self.assertEqual(h.hermes.connects, 2)
            self.assertEqual(len(h.hermes.created), 2)                               # a new Hermes session after reconnect

    def test_timeout_interrupts(self):
        script = lambda t, s: [(30, "message.complete", {"text": "never", "status": "complete"})]
        with Harness(script, timeout_s=0.5) as h:
            evs = h.srv.wait_events(h.srv.push("stuck"))
            self.assertEqual(evs[-1]["data"]["code"], "timeout")
            self.assertTrue(h.hermes.interrupts)

    def test_clean_shutdown_and_restart(self):
        h = Harness(None)
        h.w.start()
        h.srv.wait_events(h.srv.push("a"))
        t0 = time.time()
        h.w.stop()
        self.assertLess(time.time() - t0, 6)
        self.assertFalse(h.w.running)
        h.w.start()                                                                  # worker restart works
        try:
            did = h.srv.push("after restart")
            self.assertEqual(h.srv.wait_events(did)[-1]["type"], "final")
        finally:
            h.w.stop()
            h.srv.stop()

    def test_stop_with_a_poll_in_flight_then_immediate_restart_leaves_exactly_one_worker(self):
        import threading
        h = Harness(None, poll_wait=2)
        h.srv.max_wait = 2.0                                                          # the server holds a poll for the whole wait
        try:
            h.w.start()
            time.sleep(0.4)                                                           # a long-poll is now in flight
            h.w.stop()
            self.assertFalse(h.w.running)
            h.w.start()                                                               # unpair then pair again, straight away
            time.sleep(2.6)                                                           # longer than the old poll could live
            alive = [t for t in threading.enumerate() if t.name == "tasuki-worker" and t.is_alive()]
            self.assertEqual(len(alive), 1)
            self.assertIsNone(h.w.status()["last_error"])                             # no 'crashed:RuntimeError (different event loop)'
            did = h.srv.push("served by the one worker")
            self.assertEqual(h.srv.wait_events(did)[-1]["type"], "final")
        finally:
            h.w.stop()
            h.srv.stop()

    def test_shutdown_mid_turn_interrupts_and_returns(self):
        script = lambda t, s: [(30, "message.complete", {"text": "never", "status": "complete"})]
        h = Harness(script)
        h.w.start()
        h.srv.push("busy")
        wait_for(lambda: h.hermes.prompts)
        t0 = time.time()
        h.w.stop()
        self.assertLess(time.time() - t0, 8)
        h.srv.stop()

    def test_lease_lost_stops_turn(self):
        script = lambda t, s: [(30, "message.complete", {"text": "never", "status": "complete"})]
        with Harness(script) as h:
            did = h.srv.push("lease")
            wait_for(lambda: h.hermes.prompts)
            h.srv.lease_lost.add(did)
            self.assertTrue(wait_for(lambda: h.hermes.interrupts, 5))

    def test_already_cancelled_delegation_never_reaches_hermes(self):
        with Harness(None) as h:
            did = h.srv.push("x")
            h.srv.queue[0]["cancelled"] = True
            evs = h.srv.wait_events(did)
            self.assertEqual(evs[-1]["data"]["code"], "cancelled")
            self.assertEqual(h.hermes.prompts, [])

    def test_second_worker_in_same_dir_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            lock = str(Path(d) / "w.lock")
            a, b = Harness(None, lock_path=lock), Harness(None, lock_path=lock)
            try:
                self.assertTrue(a.w.start())
                self.assertFalse(b.w.start())
                self.assertEqual(b.w.status()["last_error"], "another_process_runs_the_worker")
            finally:
                a.w.stop(); a.srv.stop(); b.srv.stop()

    def test_no_text_or_secrets_in_logs(self):
        import logging
        records = []

        class Cap(logging.Handler):
            def emit(self, r): records.append(r.getMessage())
        logging.getLogger("tasuki").addHandler(Cap())
        logging.getLogger("tasuki").setLevel(logging.DEBUG)
        script = lambda t, s: [(0, "message.complete", {"text": "TOPSECRETREPLY", "status": "complete"})]
        with Harness(script) as h:
            h.srv.wait_events(h.srv.push("MYPRIVATEQUESTION"))
            time.sleep(0.2)
        blob = "\n".join(records)
        for needle in ("MYPRIVATEQUESTION", "TOPSECRETREPLY", h.srv.token, "SECRETCRED"):
            self.assertNotIn(needle, blob)


if __name__ == "__main__":
    unittest.main()
