#!/usr/bin/env python3
"""A minimal OpenAI-compatible chat-completions stub, so an ISOLATED Hermes can run a turn with no real model.

  python3 stub_model.py --port 8791

Behaviour by the last user message:
  contains "calendar"  -> narration, then tool_search -> tool_call tasuki_calendar_summary (Hermes defers plugin tools
                          behind tool_search/tool_call), then a final answer built from the tool result
  contains "inbox"     -> tasuki_mail_conversations -> tasuki_mail_thread (first conversation) -> tasuki_mail_read (its newest message),
                          then a final answer of COUNTS ONLY ("RESULT mail ok=True convs=N thread_msgs=N read_chars=N trunc=...");
                          real mail text never reaches the reply (the real-chain test reads a real mailbox)
  contains "calsum"    -> tasuki_calendar_summary, then "RESULT calendar ok=True keys=N" (counts only)
  contains "taskdir"   -> tasuki_tasks_dir, then "RESULT taskdir ok=... has_note=..."
  contains "SLOW"      -> streams one word per second for 60 s (for cancel tests)
  anything else        -> "stub reply: <first 40 chars>"
Never makes a network call. Requests are counted at GET /_count and the last tool list / prompt seen is at GET /_last (no auth).
"""
import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNT = {"chat": 0}
LAST = {"tools": [], "user": "", "n": 0}


def sse(h, obj):
    h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
    h.wfile.flush()


def chunk(delta, finish=None):
    return {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def find_envelopes(obj, depth=0):
    """Every tasuki tool envelope ({"ok":..,"data":..}) inside a tool result, however Hermes wrapped it."""
    out = []
    if depth > 6:
        return out
    if isinstance(obj, str):
        try:
            return find_envelopes(json.loads(obj), depth + 1) if obj.lstrip()[:1] in "{[" else out
        except ValueError:
            return out
    if isinstance(obj, dict):
        if "ok" in obj and ("data" in obj or "error" in obj or "dir" in obj):
            out.append(obj)
        for v in obj.values():
            out += find_envelopes(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            out += find_envelopes(v, depth + 1)
    return out


def turn_results(msgs):
    """(tool name, content) for each tool_call result since the last user message; plus whether tool_search already ran."""
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    names, results, searched = {}, [], False
    for m in msgs[last_user + 1:]:
        for tc in m.get("tool_calls") or []:
            names[tc.get("id")] = (tc.get("function") or {}).get("name")
        if m.get("role") == "tool":
            nm = names.get(m.get("tool_call_id"))
            if nm == "tool_search":
                searched = True
            else:
                results.append((nm, m.get("content") or ""))
    return results, searched


def plan_for(text, results):
    """The next action for a scripted flow: ("call", tool, args) or ("final", text)."""
    low = text.lower()
    envs = [find_envelopes(c) for _, c in results]
    if "inbox" in low:
        n = len(results)
        if n == 0:
            return ("call", "tasuki_mail_conversations", {"limit": 3, "account": "all"})
        d1 = (envs[0][0].get("data") if envs[0] else None) or {}
        convs = d1.get("conversations") or []
        if n == 1:
            if not convs or not (convs[0].get("account") or {}).get("id"):
                return ("final", f"RESULT mail ok={bool(envs[0] and envs[0][0].get('ok'))} convs={len(convs)} thread_msgs=0 read_chars=0 trunc=False")
            return ("call", "tasuki_mail_thread", {"id": convs[0]["conversation_id"], "account": convs[0]["account"]["id"]})
        d2 = (envs[1][0].get("data") if len(envs) > 1 and envs[1] else None) or {}
        msgs = d2.get("messages") or []
        if n == 2:
            if not msgs:
                return ("final", f"RESULT mail ok=True convs={len(convs)} thread_msgs=0 read_chars=0 trunc=False")
            return ("call", "tasuki_mail_read", {"id": msgs[-1]["id"], "account": convs[0]["account"]["id"]})
        d3 = (envs[2][0].get("data") if len(envs) > 2 and envs[2] else None) or {}
        tr = any((e[0].get("truncated") for e in envs if e))
        return ("final", f"RESULT mail ok={all(bool(e and e[0].get('ok')) for e in envs)} convs={len(convs)} thread_msgs={len(msgs)} "
                         f"read_chars={len(d3.get('body_text') or '')} trunc={bool(tr)}")
    if "calsum" in low:
        if not results:
            return ("call", "tasuki_calendar_summary", {})
        e = envs[0][0] if envs[0] else {}
        return ("final", f"RESULT calendar ok={bool(e.get('ok'))} keys={len((e.get('data') or {}))}")
    if "taskdir" in low:
        if not results:
            return ("call", "tasuki_tasks_dir", {})
        e = envs[0][0] if envs[0] else {}
        return ("final", f"RESULT taskdir ok={bool(e.get('ok'))} has_note={'READ-ONLY' in str(e.get('note') or '')}")
    return None


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
        if self.path == "/_count":
            return self._json(200, COUNT)
        if self.path == "/_last":
            return self._json(200, LAST)
        self._json(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        COUNT["chat"] += 1
        msgs = body.get("messages") or []
        last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
        text = last_user.get("content") or ""
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        after_tool = bool(msgs) and msgs[-1].get("role") == "tool"
        tool_names = [t.get("function", {}).get("name") for t in (body.get("tools") or [])]
        stream = bool(body.get("stream"))
        if "tools" in body or stream:   # the agent turn, not an auxiliary call (titles etc.)
            LAST.update({"tools": sorted(t for t in tool_names if t), "user": str(text)[:300], "n": len(msgs), "keys": sorted(body.keys()), "ntools": len(body.get("tools") or [])})
        if "SLOW" in text and stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked") if False else None
            self.end_headers()
            try:
                for i in range(60):
                    sse(self, chunk({"content": f"slow{i} "}))
                    time.sleep(1)
                sse(self, chunk({}, "stop"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        results, searched = turn_results(msgs)
        plan = plan_for(str(text), results) if any(k in str(text).lower() for k in ("inbox", "taskdir", "calsum")) else None
        if plan is not None:
            if plan[0] == "final":
                return self._reply(stream, {"role": "assistant", "content": plan[1]}, "stop")
            _, tool, targs = plan
            if tool in tool_names:
                return self._call("", tool, targs, stream)
            if "tool_search" in tool_names and not searched:
                return self._call("", "tool_search", {"queries": [tool.replace("_", " ")]}, stream)
            return self._call("", "tool_call", {"calls": [{"name": tool, "arguments": targs}]}, stream)
        wants_calendar = "calendar" in text.lower()
        tool_content = (msgs[-1].get("content") or "") if after_tool else ""
        if wants_calendar and not after_tool and "tool_search" in tool_names:
            # Hermes defers plugin tools behind tool_search / tool_call: follow that real path.
            return self._call("Let me check your calendar.", "tool_search", {"queries": ["tasuki calendar summary"]}, stream)
        if wants_calendar and not after_tool and "tasuki_calendar_summary" in tool_names:
            return self._call("Let me check your calendar.", "tasuki_calendar_summary", {}, stream)
        if after_tool and "tasuki_calendar_summary" in tool_content and "Moora" not in tool_content and "tool_call" in tool_names:
            return self._call("", "tool_call", {"calls": [{"name": "tasuki_calendar_summary", "arguments": {}}]}, stream)
        if after_tool:
            reply = "Your next meeting is Moora QBR." if "Moora" in tool_content else f"Tool said: {str(tool_content)[:80]}"
        else:
            reply = f"stub reply: {str(text).strip()[:40]}"
        self._reply(stream, {"role": "assistant", "content": reply}, "stop")

    def _call(self, narration, name, args, stream):
        msg = {"role": "assistant", "content": narration or None,
               "tool_calls": [{"id": f"call_{COUNT['chat']}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return self._reply(stream, msg, "tool_calls")

    def _reply(self, stream, msg, finish):
        if not stream:
            return self._json(200, {"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": "stub-model",
                                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        sse(self, chunk({"role": "assistant"}))
        if msg.get("content"):
            sse(self, chunk({"content": msg["content"]}))
        for i, tc in enumerate(msg.get("tool_calls") or []):
            sse(self, chunk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                              "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}))
        sse(self, chunk({}, finish))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
