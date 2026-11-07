"""Process-wide service: pairing, status, and the one delegation worker.

The worker only ever starts from the dashboard API module (the process that also serves ``/api/ws``), never from
``register()``, so the gateway process and CLI sessions that merely load the plugin do not poll.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from . import CAPABILITIES, PROTOCOL_VERSION, VERSION
from .client import HttpClient, TasukiError, normalise_server
from .hermes_ws import HermesBridge
from .mirror import DirError, Mirror, STATE_NAME, Tree, new_state
from .store import Pairing, PairingStore, hermes_home
from .tools import Tools
from .worker import DelegationWorker

log = logging.getLogger("tasuki.service")


PLUGIN_DIR = Path(__file__).resolve().parent.parent


class Service:
    def __init__(self, store: Optional[PairingStore] = None, gateway_url: Optional[Callable[[], str]] = None,
                 connect=None, worker_kwargs: Optional[dict] = None, mirror_kwargs: Optional[dict] = None,
                 default_mirror_dir: Optional[Path] = None) -> None:
        self.store = store or PairingStore()
        self.gateway_url = gateway_url          # set by the dashboard API module: () -> ws url with credential
        self._connect = connect
        self._worker_kwargs = worker_kwargs or {}
        self._worker: Optional[DelegationWorker] = None
        self._lock = threading.Lock()
        self._default_mirror_dir = default_mirror_dir
        self.mirror = Mirror(self.pairing, self.make_client, self.mirror_dir, **(mirror_kwargs or {}))
        self.tools = Tools(self.tool_client, self.tasks_dir_info)

    # ---- plugin settings (non-secret): $HERMES_HOME/state/tasuki/config.json ----
    @property
    def config_path(self) -> Path:
        return self.store.dir / "config.json"

    def _config(self) -> dict:
        try:
            c = json.loads(self.config_path.read_text("utf-8"))
            return c if isinstance(c, dict) else {}
        except (OSError, ValueError):
            return {}

    def default_mirror_dir(self) -> Path:
        return self._default_mirror_dir or hermes_home() / "tasuki"

    def mirror_dir(self) -> Path:
        d = self._config().get("mirror_dir")
        return Path(d) if isinstance(d, str) and d else self.default_mirror_dir()

    def set_mirror_dir(self, raw: Optional[str]) -> dict:
        """Change (or reset, with None/'') the mirror directory. Refuses anything that could clobber other data: it must be an absolute
        path, outside the plugin directory and the Hermes state, and empty (or already a mirror)."""
        if raw in (None, ""):
            new = None
        else:
            if not isinstance(raw, str) or len(raw) > 500 or "\x00" in raw:
                raise TasukiError("mirror_dir must be a path", 400, "bad_dir")
            p = Path(os.path.expanduser(raw))
            if not p.is_absolute():
                raise TasukiError("mirror_dir must be an absolute path", 400, "bad_dir")
            new = str(Path(os.path.normpath(p)))
            real = Path(os.path.realpath(new))
            home = Path(os.path.realpath(hermes_home()))
            forbidden = [Path(os.path.realpath(PLUGIN_DIR)), home / "plugins", Path(os.path.realpath(self.store.dir)), home / ".env"]
            if real == Path(real.anchor) or len(real.parts) < 3 or real == Path.home() or real == home or home.is_relative_to(real):
                raise TasukiError("that directory is too broad: pick a dedicated folder", 400, "bad_dir")
            if any(real == f or f in real.parents or real in f.parents for f in forbidden):
                raise TasukiError("the mirror cannot live inside the plugin or the Hermes state directory", 400, "bad_dir")
            if real.exists():
                if not real.is_dir():
                    raise TasukiError("that path is a file", 400, "bad_dir")
                if any(real.iterdir()) and not (real / STATE_NAME).exists():
                    raise TasukiError("that directory is not empty and is not a tasuki mirror", 400, "dir_not_empty")
        self.store.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.store.dir, 0o700)
        cfg = self._config()
        if new:
            cfg["mirror_dir"] = new
        else:
            cfg.pop("mirror_dir", None)
        fd, tmp = tempfile.mkstemp(dir=self.store.dir, prefix=".config-")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.config_path)
        try:
            Tree(self.mirror_dir(), new_state()).ensure_root()
        except DirError as e:
            self.mirror.error = str(e.args[0])
        else:
            self.mirror.error = None
        self.mirror.resync()
        return self.mirror_status()

    def mirror_status(self) -> dict:
        st = self.mirror.status()
        p = self.pairing()
        if p is not None and "read" not in (p.scopes or []):
            st["errors"]["last"] = st["errors"]["last"] or "missing_scope:read"
        return st

    def tasks_dir_info(self) -> dict:
        d = self.mirror_dir()
        st = self.mirror.status()
        return {"dir": str(d), "exists": d.is_dir(), "last_sync_at": st["last_sync_at"], "tasks": st["tasks"], "error": st["errors"]["last"]}

    # ---- pairing state ----
    def pairing(self) -> Optional[Pairing]:
        return self.store.load()

    def make_client(self, p: Pairing) -> HttpClient:
        return HttpClient(p.server, p.token)

    def tool_client(self) -> Optional[HttpClient]:
        p = self.pairing()
        return self.make_client(p) if p else None

    # ---- worker ----
    def worker(self) -> DelegationWorker:
        with self._lock:
            if self._worker is None:
                def bridge() -> HermesBridge:
                    if self.gateway_url is None:
                        raise RuntimeError("no Hermes gateway endpoint")
                    return HermesBridge(self.gateway_url, self._connect)
                kw = dict(lock_path=str(self.store.dir / "worker.lock"), cwd=os.path.expanduser("~"))
                kw.update(self._worker_kwargs)
                self._worker = DelegationWorker(self.pairing, self.make_client, bridge, **kw)
            return self._worker

    def start_worker_if_paired(self) -> bool:
        if not self.pairing():
            return False
        self._start_mirror()
        return self.worker().start()

    def _start_mirror(self) -> None:
        """The mirror must never break pairing or the delegation worker: any failure is only reported in status."""
        try:
            self.mirror.start()
        except Exception as e:
            self.mirror.error = f"crashed:{type(e).__name__}"
            log.error("mirror did not start: %s", type(e).__name__)

    def shutdown(self) -> None:
        self.mirror.stop()
        if self._worker:
            self._worker.stop()

    # ---- routes' logic ----
    def agent_name(self) -> str:
        return f"Hermes on {socket.gethostname()}"[:80]

    def pair(self, server_url: str, code: str) -> dict:
        code = (code or "").strip()
        if not code or len(code) > 64:
            raise TasukiError("pairing code is required", 400, "bad_code")
        server = normalise_server(server_url)
        try:
            r = HttpClient(server).pair(code, self.agent_name())
        except TasukiError as e:
            if e.code in ("invalid_code", "http_400") and e.status == 400:
                raise TasukiError("That pairing code is not valid or has expired.", 400, "bad_code") from None
            raise
        info = (r or {}).get("server") or {}
        if not isinstance(r, dict) or not r.get("token") or not r.get("agent_id"):
            raise TasukiError("tasuki server answered with an unexpected pairing reply", 502, "bad_response")
        if int(info.get("protocol_version") or 0) != PROTOCOL_VERSION:
            raise TasukiError(f"tasuki server speaks protocol {info.get('protocol_version')}, this plugin speaks "
                              f"{PROTOCOL_VERSION}; update the plugin or the server", 409, "protocol_mismatch")
        if self._worker:
            self._worker.stop()
        self.store.save(Pairing(server=server, token=str(r["token"]), agent_id=str(r["agent_id"]),
                                scopes=[str(s) for s in (r.get("scopes") or [])]))
        self.mirror.error = None
        self._start_mirror()
        started = self.worker().start()
        return {"paired": True, "agent_id": str(r["agent_id"]), "server": {"origin": server, "protocol_version": PROTOCOL_VERSION,
                                                                           "origin_reported": info.get("origin")},
                "scopes": [str(s) for s in (r.get("scopes") or [])], "worker": {"running": started}}

    def status(self) -> dict:
        p = self.pairing()
        w = self.worker().status() if (self._worker or p) else {"running": False, "last_claim_at": None, "last_error": None}
        return {"version": VERSION, "protocol_version": PROTOCOL_VERSION, "paired": bool(p),
                "server": {"origin": p.server} if p else None, "agent_id": p.agent_id if p else None,
                "scopes": p.scopes if p else [], "worker": w, "capabilities": CAPABILITIES,
                "mirror": self.mirror_status(), "config": {"mirror_dir": str(self.mirror_dir()), "mirror_dir_default": str(self.default_mirror_dir())}}

    def unpair(self) -> dict:
        """Stop the worker and the mirror, tell tasuki to revoke this agent (DELETE /api/v1/agents/me), forget the token locally.
        `server_revoked` is true when tasuki confirmed (or already considered the token dead); false when it could not be reached,
        in which case revoke the agent in the tasuki app. The mirror folder is left in place: it is the user's data."""
        p = self.pairing()
        had = p is not None
        self.mirror.stop()
        if self._worker:
            self._worker.stop()
        revoked = False
        if p is not None:
            try:
                self.make_client(p).revoke_self()
                revoked = True
            except TasukiError as e:
                revoked = e.status == 401        # the token was already dead server-side: nothing left to revoke
                if not revoked:
                    log.warning("unpair: server revoke failed (%s)", e.code or e.status)
        self.store.clear()
        return {"paired": False, "was_paired": had, "server_revoked": revoked}


_service: Optional[Service] = None
_service_lock = threading.Lock()


def get_service() -> Service:
    global _service
    with _service_lock:
        if _service is None:
            _service = Service()
        return _service
