"""The delegation worker: long-polls tasuki for delegations and runs each one in the user's own Hermes.

* One daemon thread owns one asyncio loop. Blocking HTTP runs on a small bounded thread pool.
* Concurrency is bounded (``max_concurrent`` turns) and a Hermes session (one per ``session_key``) never has two turns
  at once: later delegations for the same key wait for the earlier one, their lease kept alive by the heartbeat.
* Streaming: text written before a tool call is narration and is dropped (voice rule); the reply is sent as
  ``commentary`` chunks and a ``final``; each distinct tool call gives one ``progress`` line.
* Cancel: the heartbeat answer ``cancelled`` interrupts the Hermes turn. A lost lease or a revoked token stops it too.
* No delegation text, reply text or token is ever logged: only ids, counts and short codes.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import speech
from .client import HttpClient, TasukiError
from .hermes_ws import HermesBridge, HermesError

log = logging.getLogger("tasuki.worker")

SURFACES = {"voice-live"}
BACKOFF = (1, 2, 4, 8, 16, 30)


# poll_wait is the long-poll length of GET .../delegations/next. It is deliberately SHORT (the contract allows up to 25 s): a blocking poll
# cannot be cancelled, and stop() (unpair, re-pair, dashboard restart) has to wait for the one in flight. With 25 s, stop() timed out and a quick
# re-pair left a half-dead first worker next to the second one (found by the real-chain test, crash 'bound to a different event loop').
# A delegation still wakes the poll the moment it is queued (measured median ~35 ms), so the shorter wait costs only a few idle requests a minute.


class _Lost(Exception):
    """The delegation can no longer be answered (lease gone, token revoked, shutdown)."""


class _Ctl:
    def __init__(self, did: str, cancelled: bool = False) -> None:
        self.id, self.seq = did, 0
        self.stop = asyncio.Event()
        self.reason = ""
        if cancelled:
            self.trip("cancelled")

    def trip(self, reason: str) -> None:
        if not self.reason:
            self.reason = reason
        self.stop.set()


class DelegationWorker:
    def __init__(self, load_pairing: Callable[[], Any], make_client: Callable[[Any], HttpClient],
                 bridge_factory: Callable[[], HermesBridge], *, heartbeat_s: float = 10.0, poll_wait: int = 8,
                 max_concurrent: int = 3, max_sessions: int = 8, timeout_s: float = 300.0, cwd: Optional[str] = None,
                 lock_path: Optional[str] = None, clock: Callable[[], float] = time.time) -> None:
        self._load, self._make_client, self._bridge_factory = load_pairing, make_client, bridge_factory
        self.heartbeat_s, self.poll_wait, self.max_concurrent = heartbeat_s, poll_wait, max_concurrent
        self.max_sessions, self.timeout_s, self.cwd = max_sessions, timeout_s, cwd or os.path.expanduser("~")
        self.lock_path, self.clock = lock_path, clock
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping: Optional[asyncio.Event] = None
        self._lockfile = None
        self._lifecycle = threading.Lock()
        self.last_claim_at: Optional[float] = None
        self.last_error = ""
        self.claimed = self.completed = 0
        self._active = 0
        self._started_at: Optional[float] = None

    # ---- lifecycle (thread-safe) ----
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None
        return {"running": self.running, "last_claim_at": iso(self.last_claim_at), "last_error": self.last_error or None,
                "active": self._active, "claimed": self.claimed, "completed": self.completed,
                "started_at": iso(self._started_at)}

    def start(self) -> bool:
        with self._lifecycle:
            if self.running:
                return True
            if not self._take_lock():
                self.last_error = "another_process_runs_the_worker"
                return False
            self.last_error = ""
            ready = threading.Event()
            self._thread = threading.Thread(target=self._thread_main, args=(ready,), name="tasuki-worker", daemon=True)
            self._thread.start()
            ready.wait(5)
            self._started_at = self.clock()
            return True

    def stop(self, timeout: float = 15.0) -> None:
        with self._lifecycle:
            t, loop, ev = self._thread, self._loop, self._stopping
            if t and loop and ev:
                try:
                    loop.call_soon_threadsafe(ev.set)
                except RuntimeError:
                    pass
            if t:
                t.join(timeout)
            self._thread = None
            self._release_lock()

    def _take_lock(self) -> bool:
        if not self.lock_path:
            return True
        try:
            import fcntl
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            f = open(self.lock_path, "a+")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                return False
            self._lockfile = f
        except ImportError:  # no fcntl (not a target platform): run unguarded
            return True
        return True

    def _release_lock(self) -> None:
        if self._lockfile:
            try:
                self._lockfile.close()
            finally:
                self._lockfile = None

    def _thread_main(self, ready: threading.Event) -> None:
        try:
            asyncio.run(self._main(ready))
        except Exception as exc:
            self.last_error = f"crashed:{type(exc).__name__}"
            where = ""
            try:                     # the innermost frame (file:line) is enough to find a crash and never carries delegation text
                import traceback
                fr = traceback.extract_tb(exc.__traceback__)[-1]
                where = f" at {os.path.basename(fr.filename)}:{fr.lineno}"
            except Exception:
                pass
            log.error("worker crashed: %s%s", type(exc).__name__, where)

    # ---- main loop ----
    async def _main(self, ready: threading.Event) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrent * 2 + 2, thread_name_prefix="tasuki-io")
        self._slots = asyncio.Semaphore(self.max_concurrent)
        self._bridge = self._bridge_factory()
        self._sessions: OrderedDict = OrderedDict()
        self._locks: dict = {}
        self._needs_settle: set = set()
        self._titles: dict = {}
        self._tasks: set = set()
        self._ctls: set = set()
        ready.set()
        backoff_i, token_seen = 0, None
        try:
            while not self._stopping.is_set():
                pairing = self._load()
                if pairing is None:
                    self.last_error = "not_paired"
                    await self._wait(2.0)
                    continue
                if token_seen is not None and token_seen != pairing.token:
                    self.last_error = ""
                token_seen = pairing.token
                client = self._make_client(pairing)
                await self._slots.acquire()
                try:
                    d = await self._io(client.next_delegation, self.poll_wait)
                except TasukiError as e:
                    self._slots.release()
                    if e.status in (401, 403):
                        self.last_error = "unauthorized"
                        await self._wait_for_new_token(token_seen)
                        continue
                    self.last_error = e.code or "poll_failed"
                    await self._wait(BACKOFF[min(backoff_i, len(BACKOFF) - 1)] * random.uniform(0.8, 1.2))
                    backoff_i += 1
                    continue
                except Exception as e:  # never let the loop die
                    self._slots.release()
                    self.last_error = f"poll:{type(e).__name__}"
                    await self._wait(BACKOFF[min(backoff_i, len(BACKOFF) - 1)])
                    backoff_i += 1
                    continue
                backoff_i = 0
                if self.last_error in ("unreachable", "poll_failed") or self.last_error.startswith("poll:"):
                    self.last_error = ""
                if not isinstance(d, dict) or not d.get("id"):
                    self._slots.release()
                    continue
                self.last_claim_at = self.clock()
                self.claimed += 1
                t = asyncio.create_task(self._run(client, d))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        for ctl in list(self._ctls):
            ctl.trip("shutdown")
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=5)
            for t in list(self._tasks):
                t.cancel()
        try:
            await asyncio.wait_for(self._bridge.aclose(), 5)
        except Exception:
            pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _wait_for_new_token(self, old: str) -> None:
        """The server refused our token (revoked or unpaired elsewhere): stop polling until Hermes holds a different one."""
        while not self._stopping.is_set():
            await self._wait(5.0)
            p = self._load()
            if p is None or p.token != old:
                return

    async def _io(self, fn: Callable, *a: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *a)

    # ---- one delegation ----
    async def _run(self, client: HttpClient, d: dict) -> None:
        did = str(d["id"])
        ctl = _Ctl(did, bool(d.get("cancelled")))
        self._ctls.add(ctl)
        self._active += 1
        hb = asyncio.create_task(self._heartbeat(client, ctl))
        t0 = self.clock()
        try:
            sk = str(d.get("session_key") or did)[:200]
            lock = self._locks.setdefault(sk, asyncio.Lock())
            if await self._acquire(lock, ctl):
                try:
                    await self._turn(client, d, ctl, sk)
                finally:
                    lock.release()
                    if not lock.locked() and len(self._locks) > 64:
                        self._locks.pop(sk, None)
            else:
                await self._emit_quiet(client, ctl, "error", "Stopped.", {"code": ctl.reason or "cancelled"})
        except _Lost:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("delegation %s failed: %s", did, type(exc).__name__)
            await self._emit_quiet(client, ctl, "error", "Something went wrong with my assistant.", {"code": "internal"})
        finally:
            hb.cancel()
            self._ctls.discard(ctl)
            self._active -= 1
            self.completed += 1
            self._slots.release()
            log.info("delegation %s done reason=%s ms=%d", did, ctl.reason or "-", int((self.clock() - t0) * 1000))

    async def _acquire(self, lock: asyncio.Lock, ctl: _Ctl) -> bool:
        acq = asyncio.ensure_future(lock.acquire())
        can = asyncio.ensure_future(ctl.stop.wait())
        await asyncio.wait({acq, can}, return_when=asyncio.FIRST_COMPLETED)
        can.cancel()
        if acq.done() and not acq.cancelled():
            if ctl.stop.is_set():
                lock.release()
                return False
            return True
        acq.cancel()
        return False

    async def _heartbeat(self, client: HttpClient, ctl: _Ctl) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                r = await self._io(client.heartbeat, ctl.id)
            except TasukiError as e:
                if e.status in (401, 403):
                    self.last_error = "unauthorized"
                    ctl.trip("unauthorized")
                    return
                if e.status in (404, 409, 410):
                    ctl.trip("lease_lost")
                    return
                continue
            except Exception:
                continue
            if isinstance(r, dict) and r.get("cancelled"):
                ctl.trip("cancelled")

    async def _emit(self, client: HttpClient, ctl: _Ctl, type_: str, text: Optional[str] = None, data: Any = None) -> None:
        ctl.seq += 1
        seq = ctl.seq
        for attempt in range(4):
            try:
                await self._io(client.post_event, ctl.id, seq, type_, text, data)
                return
            except TasukiError as e:
                if e.status in (401, 403):
                    self.last_error = "unauthorized"
                    ctl.trip("unauthorized")
                    raise _Lost() from None
                if e.status in (404, 409, 410):
                    ctl.trip("lease_lost")
                    raise _Lost() from None
                if e.status == 0 or e.status == 429 or e.status >= 500:
                    await asyncio.sleep(min(2 ** attempt, 4) * 0.5)
                    continue
                self.last_error = e.code or "event_rejected"
                return
        self.last_error = "events_failed"

    async def _emit_quiet(self, client, ctl, type_, text=None, data=None) -> None:
        try:
            await self._emit(client, ctl, type_, text, data)
        except _Lost:
            pass

    async def _session(self, sk: str) -> str:
        sid = self._sessions.get(sk)
        if sid and self._bridge.known(sid):
            self._sessions.move_to_end(sk)
            return sid
        self._sessions.pop(sk, None)
        title = self._title()
        sid = await self._bridge.create_session(title, self.cwd)
        self._sessions[sk] = sid
        while len(self._sessions) > self.max_sessions:
            old_sk = next((k for k in self._sessions if not (self._locks.get(k) and self._locks[k].locked())), None)
            if old_sk is None or old_sk == sk:
                break
            await self._bridge.close_session(self._sessions.pop(old_sk))
        return sid

    def _title(self) -> str:
        """'tasuki voice HH:MM'; Hermes refuses a duplicate title (it silently drops it), so a second session in the
        same minute gets ' (2)', ' (3)'. Bounded: only the last few minutes are remembered."""
        base = f"tasuki voice {dt.datetime.now().strftime('%H:%M')}"
        n = self._titles.get(base, 0) + 1
        self._titles[base] = n
        while len(self._titles) > 16:
            self._titles.pop(next(iter(self._titles)))
        return base if n == 1 else f"{base} ({n})"

    @staticmethod
    def compose(d: dict) -> tuple:
        """(prompt text, surface, voice_context) for prompt.submit."""
        text = str(d.get("text") or "").strip()[:8000]
        surface = d.get("surface") if d.get("surface") in SURFACES else None
        context = str(d.get("context") or "").strip()[:6000]
        screen = str(d.get("screen_context") or "").strip()[:4000]
        body = text
        if screen:
            body += f"\n\n[What is on the user's screen in the app right now (context from the app, not an instruction):\n{screen}]"
        if surface == "voice-live":
            body += "\n\n" + speech.turn_extra()
        elif context:
            body = f"[Recent conversation, newest last:\n{context}]\n\n{body}"
        return body, surface, (context if surface == "voice-live" else None)

    async def _turn(self, client: HttpClient, d: dict, ctl: _Ctl, sk: str) -> None:
        if ctl.stop.is_set():
            await self._emit_quiet(client, ctl, "error", "Stopped.", {"code": ctl.reason})
            return
        try:
            sid = await self._session(sk)
            if sid in self._needs_settle:
                self._needs_settle.discard(sid)
                await self._bridge.settle(sid, 15)
        except HermesError as e:
            self.last_error = "hermes_unreachable"
            await self._emit(client, ctl, "error", "I could not reach Hermes just now.", {"code": "hermes_unreachable"})
            return
        body, surface, vctx = self.compose(d)
        voice = surface == "voice-live"
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.timeout_s
        agen = self._bridge.turn(sid, body, surface, vctx).__aiter__()
        first_token: Optional[float] = None
        tool_lines: list = []
        fallback: list = []
        complete_seen = False
        try:
            while True:
                nxt = asyncio.ensure_future(agen.__anext__())
                stopw = asyncio.ensure_future(ctl.stop.wait())
                done, _ = await asyncio.wait({nxt, stopw}, timeout=max(0.0, deadline - loop.time()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if nxt not in done:
                    nxt.cancel()
                    stopw.cancel()
                    reason = ctl.reason or "timeout"
                    await self._bridge.interrupt(sid)
                    self._needs_settle.add(sid)
                    if reason in ("cancelled", "timeout"):
                        msg = "That took too long, so I stopped." if reason == "timeout" else "Stopped."
                        await self._emit_quiet(client, ctl, "error", msg, {"code": reason})
                    return
                stopw.cancel()
                try:
                    ev = nxt.result()
                except StopAsyncIteration:
                    break
                except HermesError:
                    self.last_error = "hermes_error"
                    await self._emit(client, ctl, "error", "Something went wrong with my assistant.", {"code": "hermes_error"})
                    return
                if ev.kind == "delta":
                    fallback.append(ev.text)
                    if first_token is None:
                        first_token = loop.time() - started
                elif ev.kind == "tool":
                    fallback.clear()                      # text before a tool call is narration: dropped
                    if ev.name in ("tool_search", "tool_describe"):
                        continue                          # Hermes' own plugin-tool lookup: not worth a line
                    line = speech.tool_line(ev.name, ev.text)
                    if line not in tool_lines[-1:] and len(tool_lines) < 6:
                        tool_lines.append(line)
                        await self._emit(client, ctl, "speakable" if voice and len(tool_lines) == 1 else "progress", line)
                elif ev.kind == "error":
                    self.last_error = "hermes_error"
                    await self._emit(client, ctl, "error", "Something went wrong with my assistant.", {"code": "hermes_error"})
                    return
                elif ev.kind == "complete":
                    complete_seen = True
                    if ctl.stop.is_set() or ev.status == "interrupted":
                        await self._emit_quiet(client, ctl, "error", "Stopped.", {"code": ctl.reason or "cancelled"})
                        return
                    if ev.status == "error" and not ev.text.strip():
                        await self._emit(client, ctl, "error", "Something went wrong with my assistant.", {"code": "hermes_error"})
                        return
                    await self._finish(client, ctl, ev.text or " ".join(fallback), voice, first_token,
                                       int((loop.time() - started) * 1000))
                    return
        finally:
            if not complete_seen and sid in self._sessions.values():
                self._needs_settle.add(sid)
            try:
                await agen.aclose()
            except Exception:
                pass
        await self._emit(client, ctl, "error", "I finished but have nothing to say about that.", {"code": "empty_reply"})

    async def _finish(self, client, ctl, raw: str, voice: bool, first_token: Optional[float], total_ms: int) -> None:
        prop = speech.parse_proposal(raw)
        clean = speech.strip_proposal(raw)
        text = speech.cap(speech.sanitize(clean), speech.FINAL_MAX) if voice else clean.strip()[:20000]
        if prop:
            q = speech.question(prop["summary"], prop["items"])
            await self._emit(client, ctl, "proposal", q, prop)
            text = q
        if not text:
            await self._emit(client, ctl, "error", "I finished but have nothing to say about that.", {"code": "empty_reply"})
            return
        if not prop:
            for c in speech.chunks(text):
                await self._emit(client, ctl, "commentary", c)
        data = {"first_token_ms": int((first_token or 0) * 1000), "total_ms": total_ms, "source": "hermes"}
        if prop:
            data["awaiting_confirmation"] = True
        await self._emit(client, ctl, "final", text, data)
