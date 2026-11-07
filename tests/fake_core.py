"""An in-process fake of the tasuki core's task REST routes and change feed, for the mirror and inbox tests.

It answers the calls the mirror makes through ``HttpClient.request(method, path, body, query, timeout, auth, headers)``:
  GET /api/v1/changes?format=events&since=now|c_N&limit&view&wait   (events in cursor order, has_more, reset)
  GET /api/v1/today, GET /api/v1/tasks (status, sort, order, limit, cursor), GET /api/v1/tasks/{id}, GET /api/v1/tasks/{id}/comments
  POST /api/v1/owner/claim, POST /api/v1/agents/me/device-codes (claim_logic.ClaimLogic: wrong secret, lockout, rate limits)
  POST /api/v1/tasks (the inbox) with Idempotency-Key replay and the core's validation errors
Mutators (``create``, ``update``, ``complete``, ``reopen``, ``delete``, ``comment``) append feed events shaped like docs CHANGES_FEED.md
(``after`` is the task WITHOUT comment_count, like the real stored snapshot; deletes are tombstones).
"""
from __future__ import annotations

import copy
import json
import threading
from typing import Optional

from claim_logic import ClaimLogic
import helpers  # noqa: F401  (loads the core under its module name)
from _tasuki_hermes_core.client import TasukiError


class FakeCore:
    def __init__(self) -> None:
        self.tasks: dict = {}
        self.comments: dict = {}
        self.events: list = []            # each has an int `n`
        self.n = 0
        self.seq = 0
        self.lock = threading.Lock()
        self.calls: list = []             # (method, path, query)
        self.posts: list = []             # bodies of POST /api/v1/tasks that were accepted
        self.post_attempts = 0
        self.idem: dict = {}
        self.reset_next = False
        self.fail: Optional[TasukiError] = None
        self.fail_times = 0
        self.claim = ClaimLogic()
        self.agent_token: Optional[str] = None      # set by a claim; device-codes then needs auth=True
        self.timezone = "Australia/Perth"
        self.today = "2026-11-07"

    # ---- mutators ----
    def _emit(self, entity: str, entity_id: str, action: str, after: Optional[dict], **extra) -> None:
        self.n += 1
        ev = {"id": f"ev_{self.n}", "cursor": f"c_{self.n}", "n": self.n, "type": f"{entity}.{action}", "entity": entity,
              "entity_id": entity_id, "action": action, "after": copy.deepcopy(after), **extra}
        if after is None and entity == "task":
            ev["deleted"] = True
        self.events.append(ev)

    def create(self, title: str, **fields) -> dict:
        with self.lock:
            self.seq += 1
            tid = f"t_{self.seq:06d}"
            t = {"id": tid, "title": title, "status": "todo", "created": f"2026-11-0{1 + self.seq % 6}T01:00:00.000Z",
                 "updated": f"2026-11-0{1 + self.seq % 6}T01:00:00.000Z", "version": 1, **fields}
            self.tasks[tid] = t
            self._emit("task", tid, "created", t)
            return copy.deepcopy(t)

    def update(self, tid: str, **fields) -> None:
        with self.lock:
            t = self.tasks[tid]
            t.update(fields)
            t["version"] += 1
            t["updated"] = f"2026-11-07T0{t['version'] % 10}:00:00.000Z"
            for k in [k for k, v in fields.items() if v is None]:
                t.pop(k, None)
            self._emit("task", tid, "updated", t, changed=sorted(fields))

    def complete(self, tid: str, evidence: Optional[dict] = None) -> None:
        with self.lock:
            t = self.tasks[tid]
            t.update({"status": "done", "completed_at": "2026-11-07T03:00:00.000Z", "version": t["version"] + 1})
            if evidence:
                t["evidence"] = evidence
            self._emit("task", tid, "completed", t, changed=["completed_at", "status"])

    def reopen(self, tid: str) -> None:
        with self.lock:
            t = self.tasks[tid]
            t.update({"status": "todo", "version": t["version"] + 1})
            t.pop("completed_at", None)
            self._emit("task", tid, "reopened", t)

    def delete(self, tid: str) -> None:
        with self.lock:
            self.tasks.pop(tid)
            self._emit("task", tid, "deleted", None)

    def comment(self, tid: str, body: str, author: str = "Hermes", kind: str = "agent") -> None:
        with self.lock:
            lst = self.comments.setdefault(tid, [])
            cid = f"cm_{len(lst) + 1}"
            lst.append({"id": cid, "task_id": tid, "author_kind": kind, "author_name": author, "body": body,
                        "created_at": f"2026-11-07T02:0{len(lst)}:00.000Z"})
            self.tasks[tid]["comment_count"] = len(lst)
            self._emit("comment", cid, "created", {"id": cid}, parent={"entity": "task", "id": tid})

    # ---- the client interface ----
    def request(self, method, path, body=None, query=None, timeout=None, auth=True, headers=None):
        q = {k: v for k, v in (query or {}).items() if v not in (None, "")}
        with self.lock:
            self.calls.append((method, path, dict(q)))
            if self.fail is not None and self.fail_times != 0:
                self.fail_times -= 1
                raise self.fail
        if method == "POST" and path == "/api/v1/owner/claim":
            status, out = self.claim.claim(body or {}, self._new_agent)
            return self._answer(status, out)
        if method == "POST" and path == "/api/v1/agents/me/device-codes":
            if not auth or not self.agent_token:
                raise TasukiError("bad or missing token", 401, "unauthenticated")
            return self._answer(*self.claim.device_code(body or {}, ["enroll_device"]))
        if method == "GET" and path == "/api/v1/changes":
            return self._changes(q)
        if method == "GET" and path == "/api/v1/today":
            return {"date": self.today, "timezone": self.timezone, "counts": {}, "sections": {}}
        if method == "GET" and path == "/api/v1/tasks":
            return self._list(q)
        if method == "GET" and path.startswith("/api/v1/tasks/") and path.endswith("/comments"):
            tid = path.split("/")[4]
            return {"comments": copy.deepcopy(self.comments.get(tid, []))}
        if method == "GET" and path.startswith("/api/v1/tasks/"):
            tid = path.split("/")[4]
            if tid not in self.tasks:
                raise TasukiError("not found", 404, "http_404")
            return copy.deepcopy(self.tasks[tid])
        if method == "POST" and path == "/api/v1/tasks":
            return self._post(body or {}, headers or {})
        raise TasukiError("no such route in the fake", 404, "http_404")

    def _new_agent(self):
        self.agent_token = "ts_" + __import__("uuid").uuid4().hex
        return "agent_fake", self.agent_token, "http://fake.invalid", 1

    @staticmethod
    def _answer(status, out):
        if status >= 400:
            raise TasukiError(out["message"], status, out["code"])
        return out

    def _changes(self, q):
        since = q.get("since")
        if self.reset_next:
            self.reset_next = False
            return {"events": [], "reset": True, "next_cursor": f"c_{self.n}", "has_more": False}
        if since == "now":
            return {"events": [], "next_cursor": f"c_{self.n}", "has_more": False, "reset": False}
        start = int(str(since)[2:])
        limit = int(q.get("limit") or 100)
        evs = [e for e in self.events if e["n"] > start]
        page = evs[:limit]
        out = [{k: v for k, v in e.items() if k != "n"} for e in page]
        nxt = f"c_{page[-1]['n']}" if page else f"c_{start}"
        return {"events": copy.deepcopy(out), "next_cursor": nxt, "has_more": len(evs) > limit, "reset": False}

    def _list(self, q):
        statuses = str(q.get("status") or "todo,doing").split(",")
        rows = [t for t in self.tasks.values() if t["status"] in statuses]
        rows.sort(key=lambda t: t["id"], reverse=q.get("order") == "desc")
        off = int(str(q["cursor"])[1:]) if q.get("cursor") else 0
        lim = int(q.get("limit") or 50)
        page = rows[off:off + lim]
        out = {"tasks": copy.deepcopy(page), "total": len(rows), "limit": lim, "offset": off}
        if off + len(page) < len(rows):
            out["next_cursor"] = f"o{off + len(page)}"
        return out

    def _post(self, body, headers):
        self.post_attempts += 1
        key = headers.get("Idempotency-Key")
        fp = json.dumps(body, sort_keys=True)
        if key and key in self.idem:
            return copy.deepcopy(self.idem[key][1])
        if not str(body.get("title") or "").strip():
            raise TasukiError("title required", 400, "http_400")
        if (body.get("source") or {}).get("label", "").startswith("agent:") is False:
            raise TasukiError("source must carry the agent id", 400, "http_400")
        t = self.create(body["title"], **{k: v for k, v in body.items() if k not in ("title",)})
        self.posts.append(copy.deepcopy(body))
        if key:
            self.idem[key] = (fp, t)
        return t
