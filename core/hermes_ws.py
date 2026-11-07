"""Run prompts in Hermes over its own JSON-RPC gateway websocket (``/api/ws``), the wire tasuki voice and Hermes Desktop use.

Verified wire: ``client.capabilities`` -> ``session.create {source,title,cwd,close_on_disconnect}`` -> ``prompt.submit
{session_id,text,surface,voice_context}`` -> events ``message.delta {text,segment}`` / ``tool.start {name}`` /
``message.complete {text,status}`` / ``error``; ``session.interrupt``; ``session.close``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

log = logging.getLogger("tasuki.hermes")


class HermesError(Exception):
    pass


@dataclass
class TurnEvent:
    kind: str            # 'delta' | 'tool' | 'complete' | 'error'
    text: str = ""
    segment: int = 0
    name: str = ""
    status: str = ""


class _WS:
    """Minimal duck type both backends are adapted to: ``send(str)``, async iteration of str frames, ``close()``."""


async def default_connect(url: str):
    """websockets if installed, else aiohttp (Hermes ships at least one: its own gateway needs a websocket stack)."""
    try:
        import websockets
    except ImportError:
        websockets = None
    if websockets is not None:
        ws = await asyncio.wait_for(websockets.connect(url, max_size=None, open_timeout=8), 10)

        class _W(_WS):
            async def send(self, s):
                await ws.send(s)

            def __aiter__(self):
                return ws.__aiter__()

            async def close(self):
                await ws.close()
        return _W()
    import aiohttp
    session = aiohttp.ClientSession()
    try:
        raw = await asyncio.wait_for(session.ws_connect(url, max_msg_size=0, heartbeat=20), 10)
    except BaseException:
        await session.close()
        raise

    class _A(_WS):
        async def send(self, s):
            await raw.send_str(s)

        async def _gen(self):
            async for m in raw:
                if m.type == aiohttp.WSMsgType.TEXT:
                    yield m.data
                elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return

        def __aiter__(self):
            return self._gen()

        async def close(self):
            await raw.close()
            await session.close()
    return _A()


class HermesBridge:
    """One long-lived gateway connection; many Hermes sessions. All methods run on one event loop."""

    def __init__(self, url_factory: Callable[[], str], connect: Optional[Callable[[str], Awaitable[Any]]] = None) -> None:
        self._url_factory = url_factory      # called per (re)connect: the URL carries a credential, it is never stored or logged
        self._connect = connect or default_connect
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._next = 0
        self._pending: dict = {}
        self._subs: dict = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def _ensure(self) -> None:
        async with self._lock:
            if self._ws is not None:
                return
            try:
                ws = await self._connect(self._url_factory())
            except Exception as exc:
                raise HermesError(f"Hermes is not reachable ({type(exc).__name__})") from None
            self._ws = ws
            self._reader = asyncio.create_task(self._read(ws))
        await self._call_raw("client.capabilities", {"server_requests": False})

    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                if "id" in m and ("result" in m or "error" in m):
                    fut = self._pending.pop(m["id"], None)
                    if fut and not fut.done():
                        fut.set_result(m)
                elif m.get("method") == "event":
                    p = m.get("params") or {}
                    q = self._subs.get(p.get("session_id"))
                    if q is not None:
                        if q.qsize() < 5000:           # bounded: a stuck consumer cannot grow memory without limit
                            q.put_nowait(p)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("hermes connection ended: %s", type(exc).__name__)
        finally:
            if self._ws is ws:
                self._ws = None
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(HermesError("Hermes connection lost"))
            self._pending.clear()
            for q in self._subs.values():
                q.put_nowait({"type": "_disconnected"})

    async def _call_raw(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        ws = self._ws
        if ws is None:
            raise HermesError("Hermes connection lost")
        self._next += 1
        i = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}))
        try:
            m = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(i, None)
            raise HermesError(f"Hermes did not answer {method}") from None
        if "error" in m:
            raise HermesError(str((m["error"] or {}).get("message") or "rpc error")[:200])
        return m.get("result") or {}

    async def call(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        await self._ensure()
        return await self._call_raw(method, params, timeout)

    async def create_session(self, title: str, cwd: str) -> str:
        r = await self.call("session.create", {"source": "desktop", "title": title, "cwd": cwd, "close_on_disconnect": False})
        sid = r.get("session_id")
        if not sid:
            raise HermesError("Hermes did not create a session")
        self._subs[sid] = asyncio.Queue()
        return str(sid)

    def known(self, sid: str) -> bool:
        return sid in self._subs and self._ws is not None

    async def interrupt(self, sid: str) -> None:
        try:
            await self.call("session.interrupt", {"session_id": sid}, timeout=10)
        except HermesError as exc:
            log.warning("interrupt failed: %s", exc)

    async def settle(self, sid: str, timeout: float) -> None:
        """After an interrupt: wait for the old turn's message.complete so the next prompt is not queued behind it."""
        q = self._subs.get(sid)
        if q is None:
            return
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            left = end - loop.time()
            if left <= 0:
                return
            try:
                p = await asyncio.wait_for(q.get(), left)
            except asyncio.TimeoutError:
                return
            if p.get("type") in ("message.complete", "_disconnected"):
                return

    async def close_session(self, sid: str) -> None:
        self._subs.pop(sid, None)
        try:
            if self._ws is not None:
                await self._call_raw("session.close", {"session_id": sid}, timeout=10)
        except Exception:
            pass

    async def turn(self, sid: str, text: str, surface: Optional[str] = None,
                   voice_context: Optional[str] = None) -> AsyncIterator[TurnEvent]:
        q = self._subs.get(sid)
        if q is None or self._ws is None:
            raise HermesError("Hermes session is gone")
        while not q.empty():
            q.get_nowait()
        params: dict = {"session_id": sid, "text": text}
        if surface:
            params["surface"] = surface
        if voice_context:
            params["voice_context"] = voice_context
        await self.call("prompt.submit", params, timeout=20)
        while True:
            p = await q.get()
            t, pl = p.get("type"), p.get("payload") or {}
            if t == "_disconnected":
                raise HermesError("Hermes connection lost")
            if t == "message.delta":
                if pl.get("text"):
                    yield TurnEvent("delta", text=pl["text"], segment=int(pl.get("segment") or 0))
            elif t == "tool.start":
                yield TurnEvent("tool", name=str(pl.get("name") or ""), text=str(pl.get("context") or pl.get("preview") or ""))
            elif t == "message.complete":
                yield TurnEvent("complete", text=str(pl.get("text") or ""), status=str(pl.get("status") or ""))
                return
            elif t == "error":
                yield TurnEvent("error", text=str(pl.get("message") or "Hermes error"))
                return

    async def aclose(self) -> None:
        for sid in list(self._subs):
            await self.close_session(sid)
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._reader:
            self._reader.cancel()
