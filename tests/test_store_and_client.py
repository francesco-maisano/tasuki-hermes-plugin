import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from _tasuki_hermes_core import client, store
from _tasuki_hermes_core.store import MemoryStore, Pairing, PairingStore, TOKEN_KEY


class StoreTests(unittest.TestCase):
    def test_token_only_in_secret_store(self):
        with tempfile.TemporaryDirectory() as d:
            sec = MemoryStore()
            ps = PairingStore(sec, Path(d) / "state")
            ps.save(Pairing("https://x.example", "ts_SECRETTOKEN123", "agent_1", ["delegate"]))
            self.assertEqual(sec.get(TOKEN_KEY), "ts_SECRETTOKEN123")
            for root, _, files in os.walk(d):          # no file under the state dir contains the token
                for f in files:
                    self.assertNotIn("SECRETTOKEN123", (Path(root) / f).read_text())
            self.assertEqual(stat.S_IMODE(os.stat(ps.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(ps.dir).st_mode), 0o700)
            p = ps.load()
            self.assertEqual((p.server, p.token, p.agent_id, p.scopes), ("https://x.example", "ts_SECRETTOKEN123", "agent_1", ["delegate"]))
            self.assertNotIn("SECRETTOKEN123", repr(p))         # repr never shows it

    def test_clear_and_partial_state_is_unpaired(self):
        with tempfile.TemporaryDirectory() as d:
            sec = MemoryStore()
            ps = PairingStore(sec, Path(d))
            ps.save(Pairing("https://x.example", "t", "a"))
            ps.clear()
            self.assertIsNone(ps.load())
            self.assertIsNone(sec.get(TOKEN_KEY))
            sec.set(TOKEN_KEY, "t")                              # token without metadata: not paired
            self.assertIsNone(ps.load())

    def test_hermes_env_store_writes_dotenv_not_plugin_files(self):
        """With a real HERMES_HOME the token lands in .env (Hermes' secret store) and nowhere in the plugin tree."""
        try:
            import hermes_cli.config  # noqa: F401
        except ImportError:
            self.skipTest("hermes not importable here; covered by integration_isolated.sh")


class ClientTests(unittest.TestCase):
    def test_normalise_server(self):
        self.assertEqual(client.normalise_server("https://app.usetasuki.com/"), "https://app.usetasuki.com")
        self.assertEqual(client.normalise_server("http://127.0.0.1:8765"), "http://127.0.0.1:8765")
        for bad in ("http://example.com", "ftp://x", "https://u:p@x.com", "https://x.com?a=1", "javascript:alert(1)", "", "https://"):
            with self.assertRaises(client.TasukiError, msg=bad):
                client.normalise_server(bad)

    def test_bridge_allowlist(self):
        ok = [("GET", "/mail/conversations"), ("GET", "/mail/threads/abc%3D"), ("GET", "/mail/messages/AAQ%2F"),
              ("POST", "/mail/messages/AAQ/draft-reply"), ("GET", "/calendar/events"), ("GET", "/calendar/events/E1/prep"),
              ("GET", "/calendar/free"), ("GET", "/calendar/summary"), ("GET", "/food/day"), ("GET", "/food/week"), ("GET", "/food/recent")]
        for m, p in ok:
            self.assertTrue(client.bridge_allowed(m, p), (m, p))
        bad = [("POST", "/mail/messages/AAQ/send"), ("POST", "/mail/messages/AAQ/forward"), ("DELETE", "/mail/messages/AAQ"),
               ("POST", "/mail/messages/AAQ/move"), ("POST", "/mail/messages/AAQ/read"), ("PATCH", "/mail/drafts/D1"),
               ("POST", "/calendar/blocks"), ("DELETE", "/calendar/events/E1"), ("POST", "/food/log"), ("POST", "/food/add"),
               ("GET", "/mail/messages/../../admin"), ("POST", "/mail/conversations"), ("GET", "/mail/messages/A/attachments/x"),
               ("PUT", "/food/goals"), ("GET", "/me")]
        for m, p in bad:
            self.assertFalse(client.bridge_allowed(m, p), (m, p))
        with self.assertRaises(client.TasukiError):
            client.HttpClient("http://127.0.0.1:1", "t").bridge("POST", "/mail/messages/A/send", body={})

    def test_no_redirect_leaks_bearer(self):
        import http.server, threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/start"):
                    self.send_response(302); self.send_header("Location", "/other"); self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = client.HttpClient(f"http://127.0.0.1:{srv.server_address[1]}", "ts_tok")
            with self.assertRaises(client.TasukiError):
                c.request("GET", "/start")
            self.assertEqual([p for p, _ in seen], ["/start"])
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
