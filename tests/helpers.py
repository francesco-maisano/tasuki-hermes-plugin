"""Test helpers: load the plugin core by path, a fake Hermes gateway (JSON-RPC over a fake websocket) and polling."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
os_env_flag = __import__("os").environ.setdefault("TASUKI_PLUGIN_TEST", "1")


def load_core():
    name = "_tasuki_hermes_core"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "core" / "__init__.py", submodule_search_locations=[str(ROOT / "core")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


load_core()


def wait_for(pred, timeout=10.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


class FakeWS:
    def __init__(self, hermes: "FakeHermes") -> None:
        self.h = hermes
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        m = json.loads(raw)
        asyncio.get_running_loop().create_task(self.h.handle(self, m))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while True:
            item = await self.q.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self.closed = True
        self.q.put_nowait(None)


class FakeHermes:
    """script(text, session) -> list of (delay_s, event_type, payload); the default echoes `echo: <text>`."""

    def __init__(self, script=None) -> None:
        self.script = script or (lambda text, sid: [(0, "message.delta", {"text": f"echo: {text[:40]}", "segment": 0}),
                                                    (0, "message.complete", {"text": f"echo: {text[:40]}", "status": "complete"})])
        self.sessions: dict = {}
        self.prompts: list = []        # (session_id, params)
        self.interrupts: list = []
        self.created: list = []
        self.closed: list = []
        self.urls: list = []
        self.connects = 0
        self.fail_connect = 0
        self.active: dict = {}         # sid -> concurrent turns now
        self.max_active: dict = {}
        self._tasks: dict = {}
        self._n = 0

    async def connect(self, url):
        self.urls.append(url)
        if self.fail_connect > 0:
            self.fail_connect -= 1
            raise ConnectionRefusedError()
        self.connects += 1
        return FakeWS(self)

    async def handle(self, ws, m):
        mid, method, p = m.get("id"), m.get("method"), m.get("params") or {}

        def reply(result):
            ws.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}))

        def emit(sid, typ, payload):
            ws.q.put_nowait(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"session_id": sid, "type": typ, "payload": payload}}))

        if method == "client.capabilities":
            return reply({})
        if method == "session.create":
            self._n += 1
            sid = f"sess{self._n}"
            self.sessions[sid] = p
            self.created.append(p)
            return reply({"session_id": sid, "stored_session_id": f"stored{self._n}"})
        if method == "session.close":
            self.closed.append(p.get("session_id"))
            return reply({})
        if method == "session.interrupt":
            sid = p.get("session_id")
            self.interrupts.append(sid)
            t = self._tasks.get(sid)
            if t:
                t.cancel()
                emit(sid, "message.complete", {"text": "", "status": "interrupted"})
            return reply({"ok": True})
        if method == "prompt.submit":
            sid = p["session_id"]
            self.prompts.append((sid, p))
            self.active[sid] = self.active.get(sid, 0) + 1
            self.max_active[sid] = max(self.max_active.get(sid, 0), self.active[sid])
            reply({"status": "started"})

            async def run():
                try:
                    for delay, typ, payload in self.script(p["text"], sid):
                        if delay:
                            await asyncio.sleep(delay)
                        emit(sid, typ, payload)
                except asyncio.CancelledError:
                    pass
                finally:
                    self.active[sid] -= 1
            self._tasks[sid] = asyncio.get_running_loop().create_task(run())
            return
        ws.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}}))
