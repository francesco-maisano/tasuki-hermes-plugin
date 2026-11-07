import json
import re
import unittest
from pathlib import Path

import helpers
from fake_tasuki import FakeTasuki
from _tasuki_hermes_core import client as cl
from _tasuki_hermes_core import tools as T
from _tasuki_hermes_core.client import HttpClient

ROOT = Path(helpers.ROOT)
EXPECTED = {"tasuki_mail_conversations", "tasuki_mail_thread", "tasuki_mail_read", "tasuki_mail_draft_reply",
            "tasuki_calendar_events", "tasuki_calendar_free", "tasuki_calendar_summary", "tasuki_meeting_prep",
            "tasuki_food_day", "tasuki_food_week", "tasuki_food_recent", "tasuki_tasks_dir"}


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakeTasuki().start()
        cls.tools = T.Tools(lambda: HttpClient(cls.srv.url, cls.srv.token, timeout=5))
        cls.by = {n: h for n, _, _, h in T.specs(cls.tools)}

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def call(self, name, **args):
        return json.loads(self.by[name](args))

    # ---- the no-send enumeration ----
    def test_tool_list_is_exactly_the_read_set_and_has_no_send(self):
        names = {n for n, *_ in T.specs(self.tools)}
        self.assertEqual(names, EXPECTED)
        self.assertEqual(len(names), 12)
        for n in names:
            self.assertTrue(n.startswith("tasuki_"))
            self.assertFalse(re.search(r"send|delete|forward|move|archive|flag|mark|remove|cancel|create_event|accept|decline", n), n)
        yaml_names = set(re.findall(r"^  - (tasuki_\w+)$", (ROOT / "plugin.yaml").read_text(), re.M))
        self.assertEqual(yaml_names, EXPECTED)                           # plugin.yaml provides_tools matches

    def test_the_only_write_is_a_draft_and_nothing_else_reaches_the_bridge(self):
        self.srv.bridge_calls.clear()
        self.call("tasuki_mail_conversations")
        self.call("tasuki_mail_read", id="M1", account="work")
        self.call("tasuki_mail_draft_reply", id="M1", account="work", instruction="say yes")
        self.call("tasuki_calendar_summary")
        self.call("tasuki_food_day")
        writes = [(m, p) for m, p, _, _ in self.srv.bridge_calls if m != "GET"]
        self.assertEqual(writes, [("POST", "/mail/messages/M1/draft-reply")])
        src = (ROOT / "core" / "tools.py").read_text()
        paths = set(re.findall(r'f?"(/(?:mail|calendar|food)/[^"]*)"', src))
        self.assertGreaterEqual(len(paths), 8)
        for pth in paths:      # every path literal the tools use is on the client's allowlist
            norm = re.sub(r"\{[^}]*\}", "X", pth)
            self.assertTrue(any(rx.match(norm) for _, rx in cl.ALLOWED_BRIDGE), pth)
        for bad in ("/send", "/forward", "/move", "/delete", "sendMail"):
            self.assertFalse([x for x in paths if bad in x], bad)

    # ---- envelope / untrusted ----
    def test_mail_results_are_in_an_untrusted_envelope(self):
        r = self.call("tasuki_mail_conversations")
        self.assertTrue(r["ok"] and r["untrusted_content"])
        self.assertIn("UNTRUSTED", r["notice"])
        self.assertIn("Ignore previous instructions", json.dumps(r["data"]))        # present as data, inside the envelope
        self.assertEqual(r["data"]["conversations"][0]["account"]["label"], "Work")
        self.assertEqual({a["id"] for a in r["data"]["accounts"]}, {"work", "home"})
        for tool in ("tasuki_mail_conversations", "tasuki_mail_thread", "tasuki_mail_read", "tasuki_calendar_events",
                     "tasuki_calendar_free", "tasuki_calendar_summary", "tasuki_meeting_prep"):
            desc = next(d for n, d, *_ in T.specs(self.tools) if n == tool)
            self.assertIn("UNTRUSTED", desc, tool)

    def test_mail_read_never_returns_html_and_truncates(self):
        r = self.call("tasuki_mail_read", id="M1", account="work")
        d = r["data"]
        self.assertNotRegex(json.dumps(r), r"<\s*(p|script|style|html|body)\b")
        self.assertNotIn("alert(1)", json.dumps(r))
        self.assertIn("Hi Sam", d["body_text"])
        self.assertLessEqual(len(d["body_text"]), 4000)
        self.assertTrue(d["body_truncated"] and r["truncated"])
        self.assertEqual(r["account"], {"id": "work", "label": "Work (Integrated ICT)"})
        self.assertEqual(d["attachments"], [{"name": "quote.pdf", "size": 1234, "type": "application/pdf"}])
        self.assertNotIn("web_link", d)         # ?format=agent carries no link (docs/BRIDGE.md 15.5): documented deviation
        self.assertEqual(d["from"], "Brett <brett@example.com>")
        q = [c for c in self.srv.bridge_calls if c[1].startswith("/mail/messages/M1")][-1][2]
        self.assertEqual(q.get("format"), "agent")      # the plugin ALWAYS asks for the compact, untrusted-wrapped agent format

    def test_thread_budget_and_flags(self):
        r = self.call("tasuki_mail_thread", id="C1", account="work")
        self.assertTrue(r["truncated"])
        msgs = r["data"]["messages"]
        self.assertEqual(msgs[0]["text"], "Can you quote 10 laptops?")
        self.assertLessEqual(max(len(m.get("text", "")) for m in msgs), 4000)
        self.assertTrue(msgs[1]["text_truncated"])
        q = [c for c in self.srv.bridge_calls if c[1].startswith("/mail/threads/C1")][-1][2]
        self.assertEqual(q.get("format"), "agent")
        self.assertNotIn("text", q)          # the app's text=1 switch is replaced by format=agent (which implies it)

    def test_draft_reply_is_draft_only(self):
        r = self.call("tasuki_mail_draft_reply", id="M1", account="work", instruction="Tell him Tuesday works", reply_all=False)
        self.assertTrue(r["ok"])
        self.assertFalse(r["data"]["sent"])
        self.assertEqual(r["data"]["web_link"], "https://outlook.office.com/mail/drafts/D1")
        self.assertIn("Nothing was sent", r["data"]["next"])
        m, p, q, body = [c for c in self.srv.bridge_calls if c[1].endswith("/draft-reply")][-1]
        self.assertEqual((body, q.get("account")), ({"instruction": "Tell him Tuesday works", "reply_all": False}, "work"))
        self.assertNotIn("force", json.dumps(body))                       # the model cannot override the already-replied guard
        r2 = self.call("tasuki_mail_draft_reply", id="M1", account="replied", body="Yes")
        self.assertFalse(r2["ok"])
        self.assertIn("already has a reply", r2["error"])
        self.assertFalse(self.call("tasuki_mail_draft_reply", id="M1", account="work")["ok"])   # neither body nor instruction

    def test_calendar_tools(self):
        e = self.call("tasuki_calendar_events", start="2026-11-09", end="2026-11-09")
        self.assertTrue(e["untrusted_content"])
        ev = e["data"]["events"][0]
        self.assertEqual((ev["subject"], ev["online"]), ("Moora QBR", True))
        self.assertNotIn("teams.example", json.dumps(e))                  # long join links are not passed to the model
        q = [c for c in self.srv.bridge_calls if c[1] == "/calendar/events"][-1][2]
        self.assertEqual((q["start"], q["end"]), ("2026-11-09", "2026-11-09"))
        f = self.call("tasuki_calendar_free", date="2026-11-09", min_minutes=45)
        self.assertEqual(f["data"]["slots"][0]["minutes"], 90)
        self.assertEqual([c for c in self.srv.bridge_calls if c[1] == "/calendar/free"][-1][2]["min_minutes"], "45")
        s = self.call("tasuki_calendar_summary")
        self.assertEqual(s["data"]["meetings_today"], 2)
        self.assertNotIn("etag", s["data"])
        p = self.call("tasuki_meeting_prep", event_id="E1")
        self.assertEqual(p["data"]["client"], "Shire of Moora")
        self.assertEqual(p["data"]["open_tasks"][0]["title"], "Moora · renewal quote")
        self.assertFalse(self.call("tasuki_calendar_free")["ok"])         # date required

    def test_food_tools_compact_json(self):
        d = self.call("tasuki_food_day", date="2026-11-07")
        self.assertTrue(d["ok"])
        self.assertNotIn("untrusted_content", d)
        self.assertEqual([m["meal"] for m in d["data"]["meals"]], ["breakfast"])   # empty meals dropped
        self.assertEqual(d["data"]["remaining"]["calories"], 1850)
        self.assertEqual(self.call("tasuki_food_week")["data"]["logged_days"], 5)
        self.assertEqual(self.call("tasuki_food_recent")["data"]["items"][0]["name"], "Eggs")

    def test_all_results_are_compact(self):
        for name, args in (("tasuki_mail_conversations", {}), ("tasuki_calendar_events", {}), ("tasuki_food_day", {})):
            self.assertLess(len(self.by[name](args)), 6000, name)

    # ---- gating and errors ----
    def test_unpaired_says_so_for_every_tool(self):
        t = T.Tools(lambda: None)
        self.assertFalse(t.paired())
        for name, _, _, h in T.specs(t):
            r = json.loads(h({"id": "x", "account": "a", "event_id": "e", "date": "2026-11-09"}))
            self.assertEqual(r, {"ok": False, "error": "tasuki is not paired"}, name)

    def test_errors_are_short_and_never_raise(self):
        t = T.Tools(lambda: HttpClient("http://127.0.0.1:1", "tok", timeout=1))
        for name, _, _, h in T.specs(t):
            raw = h({"id": "x", "account": "a", "event_id": "e", "date": "2026-11-09", "body": "b"})
            r = json.loads(raw)
            self.assertFalse(r["ok"], name)
            self.assertLess(len(r["error"]), 240)
            self.assertNotIn("tok", r["error"])
        boom = T.Tools(lambda: (_ for _ in ()).throw(RuntimeError("secret detail")))
        self.assertEqual(json.loads(boom.mail_read({"id": "1", "account": "a"}))["error"], "tasuki tool failed (RuntimeError)")

    def test_bad_args(self):
        self.assertFalse(self.call("tasuki_mail_read", id="M1")["ok"])
        self.assertFalse(self.call("tasuki_mail_conversations", filter="delete")["ok"])

    def test_html_to_text(self):
        self.assertEqual(T.html_to_text("<p>a</p><style>b{}</style><script>evil()</script><p>c &amp; d</p>"), "a\n\nc & d")


if __name__ == "__main__":
    unittest.main()
