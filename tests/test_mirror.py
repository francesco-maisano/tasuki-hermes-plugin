"""Task mirror and inbox: unit tests against an in-process fake of the core's feed (tests/fake_core.py)."""
import json
import os
import re
import stat
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import wait_for
from fake_core import FakeCore
from _tasuki_hermes_core import mirror as M
from _tasuki_hermes_core import inbox as IB
from _tasuki_hermes_core.client import TasukiError

NOW = 1794038400.0          # 2026-11-07T08:00:00Z = 16:00 Perth
SCOPES = ["read", "suggest", "write", "comment", "complete", "delete", "undo", "delegate"]


def pairing(scopes=None, server="http://x", agent="ag_test", token="ts_secret"):
    return types.SimpleNamespace(server=server, agent_id=agent, scopes=SCOPES if scopes is None else scopes, token=token)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "mirror"
        self.core = FakeCore()
        self.p = pairing()
        self.mk()

    def mk(self, **kw):
        kw.setdefault("clock", lambda: NOW)
        kw.setdefault("inbox_settle_s", 0)
        self.m = M.Mirror(lambda: self.p, lambda p: self.core, lambda: self.dir, **kw)
        return self.m

    def tearDown(self):
        self.m.stop()
        self.tmp.cleanup()

    def sync(self):
        return self.m.sync_once(self.core)

    def tasks(self):
        return sorted(p.name for p in (self.dir / "tasks").glob("*.md"))

    def done(self):
        return sorted(str(p.relative_to(self.dir / "tasks/done")) for p in (self.dir / "tasks/done").rglob("*.md"))

    def read(self, rel):
        return (self.dir / rel).read_text("utf-8")

    def inbox_scan(self, scopes=None, agent="ag_test"):
        return IB.InboxWatcher(self.m).scan(self.core, SCOPES if scopes is None else scopes, agent)

    def drop(self, name, content):
        p = self.dir / "inbox" / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        os.utime(p, (time.time() - 100, time.time() - 100))      # old enough: settled
        return p


class Files(Base):
    def test_full_sync_then_incremental_create_update_complete_delete_rename(self):
        a = self.core.create("Send quote to Acme", notes="Hello notes\nline two", due_date="2026-11-10", project="Acme", labels=["quote", "urgent"],
                             priority=2, effort="30", next_action="Email it", next_actor="human", refs={"client": "acme"})
        self.sync()
        name = f"{a['id']}-send-quote-to-acme.md"
        self.assertEqual(self.tasks(), [name])
        text = self.read("tasks/" + name)
        for needle in (f"id: {a['id']}", 'title: "Send quote to Acme"', "status: todo", "due: 2026-11-10", 'project: "Acme"', 'labels: ["quote", "urgent"]',
                       "client: acme", "priority: 2", 'effort: "30"', 'next: "Email it"', "next_actor: human", "# Send quote to Acme", "Hello notes\nline two"):
            self.assertIn(needle, text)
        self.assertTrue((self.dir / "README.md").exists() and (self.dir / "inbox/README.md").exists())
        st = json.loads(self.read(".tasuki-mirror.json"))
        self.assertEqual((st["schema"], st["cursor"]), (1, "c_1"))
        # update + waiting
        self.core.update(a["id"], waiting_on="supplier", next_actor="other", next_actor_name="Tim")
        self.sync()
        self.assertIn('waiting_on: "supplier"', self.read("tasks/" + name))
        # rename slug: the old file goes, the id is stable
        self.core.update(a["id"], title="Quote for Acme (final)")
        self.sync()
        self.assertEqual(self.tasks(), [f"{a['id']}-quote-for-acme-final.md"])
        # complete: moves to done/YYYY-MM with evidence
        self.core.complete(a["id"], evidence={"text": "Sent and acknowledged", "links": ["https://example.com/q"]})
        self.sync()
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.done(), [f"2026-11/{a['id']}-quote-for-acme-final.md"])
        d = self.read(f"tasks/done/2026-11/{a['id']}-quote-for-acme-final.md")
        self.assertIn("status: done", d)
        self.assertIn("## Evidence", d)
        self.assertIn("Sent and acknowledged", d)
        # reopen moves it back; delete removes it
        self.core.reopen(a["id"])
        self.sync()
        self.assertEqual((len(self.tasks()), self.done()), (1, []))
        self.core.delete(a["id"])
        self.sync()
        self.assertEqual((self.tasks(), self.done()), ([], []))
        self.assertEqual(self.m.status()["tasks"], 0)

    def test_baseline_takes_the_cursor_first_and_reads_open_and_recent_done_with_comments(self):
        a = self.core.create("Open one")
        b = self.core.create("Done one")
        self.core.complete(b["id"])
        self.core.comment(a["id"], "first comment\nsecond line")
        n0 = len(self.core.calls)
        self.sync()
        paths = [c[1] for c in self.core.calls[n0:]]
        self.assertEqual(paths[0], "/api/v1/changes")                       # cursor BEFORE the lists
        self.assertEqual(self.core.calls[n0][2]["since"], "now")
        self.assertEqual(len(self.tasks()), 1)
        self.assertEqual(len(self.done()), 1)
        text = self.read("tasks/" + self.tasks()[0])
        self.assertIn("## Comments", text)
        self.assertRegex(text, r"- 2026-11-07T02:00:00Z · Hermes \(agent\): first comment\n  second line")

    def test_comment_event_refreshes_and_a_later_task_event_keeps_the_comments(self):
        a = self.core.create("Has comments")
        self.sync()
        self.core.comment(a["id"], "hello there")
        self.sync()
        fn = "tasks/" + self.tasks()[0]
        self.assertIn("hello there", self.read(fn))
        self.core.update(a["id"], next_action="Do the thing")                 # the event carries no comments: the section must survive
        self.sync()
        t = self.read(fn)
        self.assertIn("hello there", t)
        self.assertIn('next: "Do the thing"', t)

    def test_burst_of_500_changes_in_pages_and_bounded_pages(self):
        self.sync()
        for i in range(500):
            self.core.create(f"Task number {i}")
        n0 = len(self.core.calls)
        applied = self.sync()
        self.assertEqual(applied, 500)
        self.assertEqual(len(self.tasks()), 500)
        feed = [c for c in self.core.calls[n0:] if c[1] == "/api/v1/changes"]
        self.assertEqual(len(feed), 3)                                         # 200 + 200 + 100, never one huge page
        self.assertTrue(all(int(c[2]["limit"]) <= 200 for c in feed))
        self.assertEqual(json.loads(self.read(".tasuki-mirror.json"))["cursor"], "c_500")
        t = next(iter(self.core.tasks))                                          # a burst of updates to ONE task is still one file
        for i in range(50):
            self.core.update(t, notes=f"rev {i}")
        self.sync()
        self.assertEqual(len(self.tasks()), 500)
        self.assertIn("rev 49", self.read("tasks/" + [n for n in self.tasks() if n.startswith(t)][0]))

    def test_unchanged_content_is_not_rewritten(self):
        self.core.create("Stable")
        self.sync()
        n_writes = []
        real = M.Tree._atomic

        def counting(tree, rel, data):
            n_writes.append(rel)
            return real(tree, rel, data)
        with mock.patch.object(M.Tree, "_atomic", counting):
            self.sync()                                                         # nothing changed: no file touched
            self.core._emit("task", next(iter(self.core.tasks)), "updated", self.core.tasks[next(iter(self.core.tasks))])   # an event that renders identically
            self.sync()
        self.assertEqual(n_writes, [])
        mt = (self.dir / "tasks" / self.tasks()[0]).stat().st_mtime_ns
        self.sync()
        self.assertEqual(mt, (self.dir / "tasks" / self.tasks()[0]).stat().st_mtime_ns)

    def test_file_modes_and_confinement(self):
        self.core.create("Private")
        self.sync()
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        for sub in ("tasks", "views", "inbox", "tasks/done"):
            self.assertEqual(stat.S_IMODE(os.stat(self.dir / sub).st_mode), 0o700, sub)
        for f in [*self.dir.rglob("*.md"), self.dir / ".tasuki-mirror.json"]:
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600, str(f))
        with self.assertRaises(M.DirError):
            self.m.tree._path("../escape.md")
        with self.assertRaises(M.DirError):
            self.m.tree._path("/etc/passwd")

    def test_crash_mid_write_leaves_old_files_intact_and_resume_converges(self):
        a = self.core.create("First")
        self.sync()
        old = self.read("tasks/" + self.tasks()[0])
        self.core.update(a["id"], notes="changed while we crash")
        self.core.create("Second")
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst, *x, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated crash mid-write")
            return real(src, dst, *x, **k)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(OSError):
                self.sync()
        self.assertEqual(list(self.dir.rglob(".tmp-*.part")), [], "the temp file of a failed write is removed")
        self.assertEqual(json.loads(self.read(".tasuki-mirror.json"))["cursor"], "c_1")   # not advanced: a restart re-reads the page
        self.m.stop()
        m2 = self.mk()                                                                      # "restart": a NEW Mirror on the same dir
        m2.sync_once(self.core)
        self.assertEqual(len(self.tasks()), 2)
        new = self.read("tasks/" + [n for n in self.tasks() if n.startswith(a["id"])][0])
        self.assertIn("changed while we crash", new)
        self.assertNotEqual(old, new)
        self.assertEqual(m2.status()["external_edits"], 0)                                  # our own crash is not an "external edit"

    def test_a_leftover_temp_file_from_a_hard_crash_is_cleaned_on_start(self):
        self.core.create("x")
        self.sync()
        left = self.dir / "tasks" / ".tmp-deadbeef00.part"
        left.write_text("half")
        self.m.stop()
        self.mk().sync_once(self.core)
        self.assertFalse(left.exists())

    def test_resume_from_cursor_does_not_baseline_again(self):
        self.core.create("One")
        self.sync()
        self.m.stop()
        m2 = self.mk()
        n0 = len(self.core.calls)
        m2.sync_once(self.core)
        self.assertEqual([c[2].get("since") for c in self.core.calls[n0:] if c[1] == "/api/v1/changes"], ["c_1"])
        self.assertFalse([c for c in self.core.calls[n0:] if c[1] == "/api/v1/tasks"])
        self.core.create("Two")
        m2.sync_once(self.core)
        self.assertEqual(len(self.tasks()), 2)

    def test_reset_from_server_runs_a_fresh_baseline(self):
        a = self.core.create("A")
        self.sync()
        self.core.delete(a["id"])
        self.core.create("B")
        self.core.reset_next = True
        self.sync()
        self.assertEqual([n.split("-", 1)[1] for n in self.tasks()], ["b.md"])

    def test_views(self):
        t = self.core.create("Today thing", due_date="2026-11-07", priority=1)
        self.core.create("Late thing", due_date="2026-11-01", next_actor="human")
        self.core.create("Mine", next_actor="human")
        self.core.create("Theirs", next_actor="other", next_actor_name="Tim")
        self.core.create("Waiting", waiting_on="a quote")
        d = self.core.create("In progress")
        self.core.update(d["id"], status="doing")
        self.sync()
        today, over = self.read("views/today.md"), self.read("views/overdue.md")
        mine, theirs = self.read("views/waiting-on-me.md"), self.read("views/waiting-on-others.md")
        self.assertIn("# Today (2026-11-07)", today)
        self.assertIn("Today thing", today)
        self.assertIn("In progress", today)
        self.assertNotIn("Late thing", today)
        self.assertIn("Late thing", over)
        self.assertNotIn("Today thing", over)
        self.assertIn("Mine", mine)
        self.assertIn("Late thing", mine)
        self.assertNotIn("Theirs", mine)
        self.assertIn("Theirs", theirs)
        self.assertIn("next: other (Tim)", theirs)
        self.assertIn("waiting on a quote", theirs)
        self.assertRegex(today, rf"\(\.\./tasks/{t['id']}-today-thing\.md\)")
        self.assertNotRegex(today + over + mine + theirs, r"\d{2}:\d{2}:\d{2}")        # nothing clock-dependent: no churn

    def test_unknown_event_types_and_proposals_are_ignored_or_removed(self):
        self.core.create("Real")
        self.sync()
        self.core._emit("project", "p1", "updated", {"id": "p1"})
        self.core._emit("future-thing", "x", "frobbed", {"id": "x"})
        self.core._emit("agent-suggestion", "t_zzz", "proposed", {"id": "t_zzz", "status": "proposed", "title": "Suggestion"})
        self.sync()
        self.assertEqual(len(self.tasks()), 1)
        self.core._emit("agent-suggestion", "t_yyy", "accepted", {"id": "t_yyy", "title": "Accepted one", "status": "todo", "version": 2})
        self.sync()
        self.assertEqual(len(self.tasks()), 2)
        self.assertEqual(self.m.status()["errors"]["count"], 0)

    def test_resync_event_triggers_a_baseline(self):
        self.core.create("A")
        self.sync()
        self.core.tasks["t_000001"]["project"] = "Renamed project"           # a project rename changes many tasks without one event each
        self.core._emit("project", "p1", "updated", {"id": "p1"}, resync="tasks")
        self.sync()
        self.assertIn('project: "Renamed project"', self.read("tasks/" + self.tasks()[0]))


class ExternalEdits(Base):
    def test_a_hand_edit_is_never_pushed_noted_and_restored(self):
        a = self.core.create("Guarded", notes="server version")
        self.sync()
        f = self.dir / "tasks" / self.tasks()[0]
        original = f.read_text()
        f.write_text(original.replace("server version", "I EDITED THIS"))
        n0 = len(self.core.calls)
        self.m.scan_every_s = 0
        self.sync()
        self.assertEqual(f.read_text(), original, "restored from the server")
        self.assertEqual([c for c in self.core.calls[n0:] if c[0] != "GET"], [], "nothing was written to the server")
        self.assertEqual(self.core.posts, [])
        st = json.loads(self.read(".tasuki-mirror.json"))
        self.assertEqual(st["counters"]["external_edits"], 1)
        self.assertEqual(st["notes"][-1]["kind"], "modified")
        self.assertEqual(st["notes"][-1]["path"], f"tasks/{f.name}")
        self.assertIn("changed by hand", self.read("inbox/README.md"))
        self.assertNotIn("I EDITED", json.dumps(st))                          # the edit's text is not recorded
        self.assertEqual(self.core.tasks[a["id"]]["notes"], "server version")
        self.sync()                                                              # and it is quiet afterwards: one edit, one count
        self.assertEqual(json.loads(self.read(".tasuki-mirror.json"))["counters"]["external_edits"], 1)

    def test_a_deleted_mirror_file_and_an_edited_view_are_restored(self):
        self.core.create("Gone", due_date="2026-11-07")
        self.sync()
        f = self.dir / "tasks" / self.tasks()[0]
        f.unlink()
        (self.dir / "views/today.md").write_text("tampered")
        self.m.scan_every_s = 0
        self.sync()
        self.assertTrue(f.exists())
        self.assertIn("Gone", self.read("views/today.md"))
        self.assertEqual(self.m.status()["external_edits"], 2)

    def test_replacing_a_mirror_file_with_a_symlink_never_writes_through_it(self):
        self.core.create("Linked")
        self.sync()
        f = self.dir / "tasks" / self.tasks()[0]
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("precious")
        f.unlink()
        f.symlink_to(outside)
        self.m.scan_every_s = 0
        self.sync()
        self.assertEqual(outside.read_text(), "precious")
        self.assertFalse(f.is_symlink())
        self.assertIn("Linked", f.read_text())

    def test_only_files_the_mirror_wrote_are_ever_deleted(self):
        self.core.create("Mine")
        self.sync()
        stray = self.dir / "tasks" / "my-own-notes.md"
        stray.write_text("user file")
        self.core.delete(next(iter(self.core.tasks)))
        self.sync()
        self.assertEqual(stray.read_text(), "user file")
        self.assertEqual(self.tasks(), ["my-own-notes.md"])


class Inbox(Base):
    def setUp(self):
        super().setUp()
        self.sync()          # lays out the folders

    def test_happy_path_front_matter_heading_idempotency_and_source(self):
        raw = ("---\ndue: 2026-11-20\nproject: Acme\nclient: acme-pty\nlabels: [quote, urgent]\npriority: 2\neffort: 30\nnext: Email the quote\n---\n"
               "# Send quote to Acme\n\nNotes line 1\nNotes line 2\n")
        self.drop("quote.md", raw)
        r = self.inbox_scan()
        self.assertEqual((r["processed"], r["rejected"]), (1, 0))
        body = self.core.posts[0]
        self.assertEqual({k: body[k] for k in ("title", "due_date", "project", "labels", "priority", "effort", "next", "refs")},
                         {"title": "Send quote to Acme", "due_date": "2026-11-20", "project": "Acme", "labels": ["quote", "urgent"], "priority": 2,
                          "effort": "30", "next": "Email the quote", "refs": {"client": "acme-pty"}})
        self.assertEqual(body["notes"], "Notes line 1\nNotes line 2")
        self.assertEqual(body["source"], {"label": "agent:ag_test"})
        self.assertFalse((self.dir / "inbox/quote.md").exists())
        out = (self.dir / "inbox/processed/quote.md").read_text()
        tid = list(self.core.tasks)[0]
        self.assertIn(f"task_id: {tid}", out)
        self.assertIn("# Send quote to Acme", out)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "inbox/processed/quote.md").st_mode), 0o600)
        self.sync()                                                              # the mirror shows the new task
        self.assertEqual(len(self.tasks()), 1)

    def test_title_from_filename_and_no_front_matter(self):
        self.drop("buy-new-monitors.md", "just some notes, no heading\n")
        self.inbox_scan()
        self.assertEqual(self.core.posts[0]["title"], "buy new monitors")
        self.assertEqual(self.core.posts[0]["notes"], "just some notes, no heading")

    def test_re_drop_of_the_same_file_is_one_task(self):
        content = "# Same file\n\nbody\n"
        self.drop("a.md", content)
        self.inbox_scan()
        self.drop("again.md", content)
        self.assertEqual(self.inbox_scan()["processed"], 1)
        self.assertEqual(self.core.post_attempts, 1, "the second drop never reached the server")
        self.assertEqual(len(self.core.tasks), 1)
        a = (self.dir / "inbox/processed/a.md").read_text()
        b = (self.dir / "inbox/processed/again.md").read_text()
        self.assertEqual(re.search(r"task_id: (\S+)", a).group(1), re.search(r"task_id: (\S+)", b).group(1))
        self.m.stop()                                                            # and after a restart the hash table is in the state file
        self.mk().sync_once(self.core)
        self.drop("third.md", content)
        IB.InboxWatcher(self.m).scan(self.core, SCOPES, "ag_test")
        self.assertEqual(self.core.post_attempts, 1)

    def test_idempotency_key_is_the_file_hash_and_server_replay_is_one_task(self):
        import hashlib
        raw = b"# Keyed\n"
        self.drop("k.md", raw)
        seen = {}
        real = self.core.request

        def spy(method, path, body=None, query=None, timeout=None, auth=True, headers=None):
            if method == "POST":
                seen.update(headers or {})
            return real(method, path, body, query, timeout, auth, headers)
        self.core.request = spy
        self.inbox_scan()
        self.assertEqual(seen["Idempotency-Key"], "inbox-" + hashlib.sha256(raw).hexdigest()[:40])
        # same bytes again with the local table forgotten (the server's 24 h replay still answers with the one task)
        self.m.state["inbox_hashes"].clear()
        self.drop("k2.md", raw)
        self.inbox_scan()
        self.assertEqual(len(self.core.tasks), 1)

    def rejected(self):
        d = self.dir / "inbox/rejected"
        return {p.name: (p.read_text() if p.name.endswith(".reason.txt") else None) for p in d.iterdir()}

    def test_rejects_bad_front_matter_with_a_reason_file(self):
        self.drop("bad.md", "---\ncolour: red\n---\n# T\n")
        self.drop("unclosed.md", "---\ndue: 2026-11-01\n# T\n")
        self.drop("baddue.md", "---\ndue: tomorrow\n---\n# T\n")
        self.drop("badprio.md", "---\npriority: 9\n---\n# T\n")
        self.drop("_.md", "")
        self.drop("binary.md", b"\xff\xfe\x00\x01")
        r = self.inbox_scan()
        self.assertEqual((r["processed"], r["rejected"]), (0, 6))
        rej = self.rejected()
        self.assertIn("unknown_key", rej["bad.md.reason.txt"])
        self.assertIn("colour", rej["bad.md.reason.txt"])
        self.assertIn("bad_front_matter", rej["unclosed.md.reason.txt"])
        self.assertIn("bad_due", rej["baddue.md.reason.txt"])
        self.assertIn("bad_priority", rej["badprio.md.reason.txt"])
        self.assertIn("not_utf8", rej["binary.md.reason.txt"])
        self.assertIn("bad.md", rej)
        self.assertEqual(self.core.post_attempts, 0)

    def test_rejects_oversize_without_reading_it(self):
        self.drop("big.md", "# Big\n" + "x" * (IB.MAX_BYTES + 10))
        orig_open = os.open
        opened = []

        def spy(path, flags, *a, **k):
            opened.append(str(path))
            return orig_open(path, flags, *a, **k)
        with mock.patch("os.open", spy):
            r = self.inbox_scan()
        self.assertEqual(r["rejected"], 1)
        self.assertNotIn("big.md", opened, "an oversize file is rejected on its size alone")
        self.assertIn("too_large", self.rejected()["big.md.reason.txt"])
        self.assertEqual(self.core.post_attempts, 0)
        self.drop("ok.md", "# Ok\n" + "y" * 19000)
        self.assertEqual(self.inbox_scan()["processed"], 1)
        self.drop("long-notes.md", "# Long\n" + "z" * 30000)                       # under 64 KB but over the core's 20000-character notes limit
        self.assertEqual(self.inbox_scan()["rejected"], 1)
        self.assertIn("notes_too_long", self.rejected()["long-notes.md.reason.txt"])

    def test_symlink_is_never_followed_and_never_read(self):
        secret = Path(self.tmp.name) / "secret.md"
        secret.write_text("# Stolen title\n\nTOP SECRET\n")
        (self.dir / "inbox" / "innocent.md").symlink_to(secret)
        d = Path(self.tmp.name) / "elsewhere"
        d.mkdir()
        (d / "x.md").write_text("# Dir contents\n")
        (self.dir / "inbox" / "dirlink.md").symlink_to(d)
        r = self.inbox_scan()
        self.assertEqual((r["processed"], r["rejected"]), (0, 2))
        self.assertEqual(self.core.post_attempts, 0)
        self.assertEqual(secret.read_text(), "# Stolen title\n\nTOP SECRET\n")           # untouched, not moved
        self.assertEqual((d / "x.md").read_text(), "# Dir contents\n")
        self.assertTrue((self.dir / "inbox/rejected/innocent.md").is_symlink(), "the LINK was moved aside, not its target")
        self.assertIn("symlink", self.rejected()["innocent.md.reason.txt"])
        import shutil                                                                      # the inbox folder itself being a symlink is not followed
        shutil.rmtree(self.dir / "inbox")
        os.symlink(d, self.dir / "inbox")
        (d / "evil.md").write_text("# Evil\n")
        os.utime(d / "evil.md", (time.time() - 100, time.time() - 100))
        self.assertEqual(self.inbox_scan(), {"processed": 0, "rejected": 0, "pending": 0})
        self.assertEqual(self.core.post_attempts, 0)

    def test_missing_scope_is_rejected_with_a_reason_and_nothing_is_sent(self):
        self.drop("n.md", "# Needs write\n")
        self.assertEqual(self.inbox_scan(scopes=["read", "delegate"])["rejected"], 1)
        self.assertIn("missing_scope", self.rejected()["n.md.reason.txt"])
        self.assertEqual(self.core.post_attempts, 0)

    def test_server_validation_error_rejects_but_trouble_leaves_the_file(self):
        self.drop("a.md", "# A\n")
        self.core.fail, self.core.fail_times = TasukiError("title is too long", 400, "http_400"), 1
        self.assertEqual(self.inbox_scan()["rejected"], 1)
        self.assertIn("title is too long", self.rejected()["a.md.reason.txt"])
        self.drop("b.md", "# B\n")
        self.core.fail, self.core.fail_times = TasukiError("boom", 503, "http_503"), 1
        r = self.inbox_scan()
        self.assertEqual((r["processed"], r["rejected"], r["pending"]), (0, 0, 1))
        self.assertTrue((self.dir / "inbox/b.md").exists(), "left for the next scan")
        self.assertEqual(self.inbox_scan()["processed"], 1)
        self.drop("c.md", "# C\n")
        self.core.fail, self.core.fail_times = TasukiError("down", 0, "unreachable"), 1
        self.inbox_scan()
        self.assertTrue((self.dir / "inbox/c.md").exists())

    def test_rate_limit_30_a_minute_and_the_rest_waits(self):
        for i in range(35):
            self.drop(f"f{i:02d}.md", f"# Task {i}\n")
        r = self.inbox_scan()
        self.assertEqual((r["processed"], r["pending"]), (30, 5))
        self.assertEqual(len(list((self.dir / "inbox").glob("f*.md"))), 5)
        self.m.inbox_bucket[:] = [t - 61 for t in self.m.inbox_bucket]               # a minute later
        self.assertEqual(self.inbox_scan()["processed"], 5)

    def test_a_file_still_being_written_waits_and_temp_names_are_ignored(self):
        (self.dir / "inbox" / "fresh.md").write_text("# Fresh\n")
        self.m.inbox_settle_s = 600
        self.assertEqual(self.inbox_scan()["pending"], 1)
        self.drop(".hidden.md", "# Hidden\n")
        self.drop("draft.md.swp", "x")
        self.drop("note.md~", "x")
        self.drop("README.md", "# readme\n")
        os.utime(self.dir / "inbox" / "fresh.md", (time.time() - 1000, time.time() - 1000))
        self.assertEqual(self.inbox_scan()["processed"], 1)
        self.assertEqual(self.core.post_attempts, 1)

    def test_not_markdown_is_rejected(self):
        self.drop("notes.txt", "hello")
        self.assertEqual(self.inbox_scan()["rejected"], 1)
        self.assertIn("not_markdown", self.rejected()["notes.txt.reason.txt"])


class Loop(Base):
    def test_unpaired_or_without_read_scope_does_nothing(self):
        self.p = None
        self.m.start()
        time.sleep(0.4)
        self.assertEqual(self.core.calls, [])
        self.assertEqual(self.m.status()["errors"]["last"], "not_paired")
        self.m.stop()
        self.p = pairing(scopes=["delegate"])
        self.m.start()
        time.sleep(0.4)
        self.assertEqual(self.core.calls, [])
        self.assertEqual(self.m.status()["errors"]["last"], "missing_scope:read")

    def test_revoked_token_stops_polling_until_paired_again(self):
        self.core.create("x")
        self.core.fail, self.core.fail_times = TasukiError("revoked", 401, "http_401"), -1
        self.mk(poll_wait=0)
        self.m.start()
        self.assertTrue(wait_for(lambda: self.m.status()["errors"]["last"] == "unauthorized", 5))
        n = len(self.core.calls)
        time.sleep(0.6)
        self.assertEqual(len(self.core.calls), n, "no hammering a revoked token")
        self.core.fail_times = 0
        self.p = pairing(token="ts_new_token")                                       # a new pairing wakes it
        self.assertTrue(wait_for(lambda: self.m.status()["tasks"] == 1, 15))

    def test_unreachable_server_backs_off_then_recovers(self):
        self.core.create("x")
        self.core.fail, self.core.fail_times = TasukiError("down", 0, "unreachable"), 2
        sleeps = []
        self.mk(poll_wait=0)
        self.m._backoff_sleep = lambda s: sleeps.append(s)
        self.m.start()
        self.assertTrue(wait_for(lambda: self.m.status()["tasks"] == 1 and self.m.status()["last_sync_at"] and not self.m.status()["errors"]["last"], 10))
        self.assertEqual(sleeps[:2], [1, 2])                                         # 1 s, 2 s, ... capped at 30
        self.assertIsNone(self.m.status()["errors"]["last"])
        self.assertEqual(self.m.status()["errors"]["count"], 2)

    def test_unwritable_dir_is_reported_and_does_not_raise(self):
        blocker = Path(self.tmp.name) / "afile"
        blocker.write_text("x")
        self.dir = blocker / "mirror"                                                # a path below a regular file can never be created
        self.mk(poll_wait=0)
        self.m._backoff_sleep = lambda s: time.sleep(0.05)
        self.m.start()
        self.assertTrue(wait_for(lambda: self.m.status()["errors"]["last"] == "dir_unwritable", 5))
        self.assertTrue(self.m.status()["errors"]["count"] >= 1)
        self.assertTrue(self.m.running)

    def test_logs_never_carry_task_text(self):
        import logging
        self.core.create("TOP-SECRET-TITLE-ZEBRA", notes="TOP-SECRET-NOTES-ZEBRA")
        self.core.fail, self.core.fail_times = TasukiError("TOP-SECRET-ZEBRA in the error", 500, "http_500"), 1
        self.mk(poll_wait=0)
        self.m._backoff_sleep = lambda s: None
        with self.assertLogs("tasuki", level="DEBUG") as cm:
            logging.getLogger("tasuki.mirror").info("start")
            self.m.start()
            wait_for(lambda: self.m.status()["tasks"] == 1, 10)
            self.drop("z.md", "# TOP-SECRET-INBOX-ZEBRA\n")
            self.inbox_scan()
        self.assertNotIn("ZEBRA", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
