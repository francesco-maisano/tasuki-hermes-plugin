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
import stat
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from . import CAPABILITIES, FEATURES, PROTOCOL_VERSION, VERSION
from .client import HttpClient, TasukiError, normalise_server
from .hermes_ws import HermesBridge
from .mirror import DirError, Mirror, STATE_NAME, Tree, new_state
from .store import CLAIM_KEY, Pairing, PairingStore, hermes_home
from .tools import Tools
from .worker import DelegationWorker

log = logging.getLogger("tasuki.service")


PLUGIN_DIR = Path(__file__).resolve().parent.parent

CLAIM_NEEDED = ("tasuki needs its claim secret: it is on your tasuki server in claim.secret; paste it here once")
CLAIM_MESSAGES = {
    "invalid_claim_secret": "That claim secret is not right. Copy it again from claim.secret on your tasuki server.",
    "locked_out": "tasuki has locked claim attempts after too many wrong secrets. Wait for the lockout to end, then try again.",
    "rate_limited": "Too many attempts. Wait a minute, then try again.",
}
ENROLL_LIMIT, ENROLL_WINDOW_S = 5, 600          # enrol-device calls per window (the core adds its own limit, 3 per 10 minutes)
CLAIM_FILE_MAX = 4096


def default_claim_files() -> list:
    """Where a claim.secret may already be on this machine (the tasuki server's own data directory, when Hermes runs beside it).
    TASUKI_CLAIM_FILES (paths separated by the OS path separator) replaces the defaults: for tests and unusual installs."""
    env = os.environ.get("TASUKI_CLAIM_FILES")
    if env is not None:
        return [Path(x) for x in env.split(os.pathsep) if x]
    return [Path(os.path.expanduser("~")) / "tasuki-data" / "claim.secret", Path("/var/lib/tasuki/claim.secret"),
            Path("/etc/tasuki/claim.secret")]


def read_claim_file(path: Path) -> tuple:
    """-> (secret or None, reason or None). Read only, never follows a symlink, never logs or returns anything but the stripped
    first line. Refused: not a regular file, writable by others, readable by everyone while owned by someone else than the Hermes user.
    World-readable but owned by the Hermes user is allowed with a warning (chmod 600 it)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, "symlink_or_unreadable" if e.errno in (40, 13) else "unreadable"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not_a_file"
        if st.st_mode & 0o002:
            return None, "world_writable"
        if st.st_mode & 0o004:
            if st.st_uid != os.geteuid():
                return None, "world_readable_not_yours"
            log.warning("claim file %s is world-readable; run chmod 600 on it", path.name)
        if st.st_size > CLAIM_FILE_MAX:
            return None, "too_large"
        raw = os.read(fd, CLAIM_FILE_MAX + 1)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    lines = raw.decode("utf-8", "replace").strip().splitlines()
    s = lines[0].strip() if lines else ""
    if not s or len(s) > 256 or any(c.isspace() or ord(c) < 32 for c in s):
        return None, "empty_or_malformed"
    return s, None


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
        self._connect_lock = threading.Lock()
        self._enroll_times: deque = deque()
        self._now = time.time
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

    def claim_files(self) -> list:
        """Candidate claim.secret files: config key `claim_file` (a path, in state/tasuki/config.json) first, then the defaults."""
        c = self._config().get("claim_file")
        files = [Path(os.path.expanduser(c))] if isinstance(c, str) and c and "\x00" not in c else []
        return files + [f for f in default_claim_files() if f not in files]

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

    # ---- connect with the owner claim secret (no pairing code, no tasuki sign-in) ----
    def find_claim_secret(self, body_secret: Optional[str]) -> tuple:
        """-> (secret, source, refused). Order: request body, Hermes secret store, a local claim.secret file."""
        s = (body_secret or "").strip()
        if s:
            return s, "body", []
        try:
            s = (self.store.secrets.get(CLAIM_KEY) or "").strip()
        except Exception:
            s = ""
        if s:
            return s, "secret_store", []
        refused = []
        for f in self.claim_files():
            s, why = read_claim_file(f)
            if s:
                return s, "file", refused
            if why:
                refused.append({"path": str(f), "reason": why})
        return None, None, refused

    def connect(self, server_url: str, claim_secret: Optional[str] = None, remember: bool = False) -> dict:
        server = normalise_server(server_url)
        secret, source, refused = self.find_claim_secret(claim_secret)
        if not secret:
            raise TasukiError(CLAIM_NEEDED, 409, "claim_secret_needed", {"refused": refused} if refused else None)
        with self._connect_lock:
            try:
                r = HttpClient(server).claim(secret, self.agent_name())
            except TasukiError as e:
                msg = CLAIM_MESSAGES.get(e.code) or str(e).replace(secret, "***")
                if e.code in ("invalid_claim_secret", "invalid_secret"):    # the core answers 401 invalid_claim_secret; a 401 from THIS route would look like a lost dashboard login
                    raise TasukiError(CLAIM_MESSAGES["invalid_claim_secret"], 400, "invalid_secret") from None
                raise TasukiError(msg, e.status, e.code) from None
            info = (r or {}).get("server") or {} if isinstance(r, dict) else {}
            if not isinstance(r, dict) or not r.get("token") or not r.get("agent_id"):
                raise TasukiError("tasuki server answered with an unexpected claim reply", 502, "bad_response")
            if int(info.get("protocol_version") or 0) != PROTOCOL_VERSION:
                try:                                   # do not leave an agent behind that this plugin will never use
                    HttpClient(server, str(r["token"])).revoke_self()
                except TasukiError:
                    pass
                raise TasukiError(f"tasuki server speaks protocol {info.get('protocol_version')}, this plugin speaks "
                                  f"{PROTOCOL_VERSION}; update the plugin or the server", 409, "protocol_mismatch")
            old = self.pairing()
            if self._worker:
                self._worker.stop()
            self.store.save(Pairing(server=server, token=str(r["token"]), agent_id=str(r["agent_id"]),
                                    scopes=[str(x) for x in (r.get("scopes") or [])], claimed=True))
            if remember and source == "body":
                try:
                    self.store.secrets.set(CLAIM_KEY, secret)
                except Exception as e:
                    log.warning("connect: could not remember the claim secret (%s)", type(e).__name__)
            self.mirror.error = None
            self._start_mirror()
            self.worker().start()
            if old is not None and old.token != str(r["token"]) and old.server == server and old.agent_id != str(r["agent_id"]):
                # Same name + kind: the core replaced the token IN PLACE (same agent id, old token already dead), so there is nothing to revoke, and a
                # revoke with the old token would only 401. A DIFFERENT agent id means an agent paired the old way: revoke it so it is not left listed.
                try:
                    self.make_client(old).revoke_self()
                except TasukiError:
                    pass
            log.info("connected: agent %s via %s", str(r["agent_id"])[:40], source)
        st = self.status()
        st["claim_source"] = source
        return st

    def enroll_device(self, device_name: Optional[str] = None) -> dict:
        p = self.pairing()
        if p is None:
            raise TasukiError("Connect tasuki first: this Hermes is not connected to a tasuki server.", 409, "not_paired")
        if "enroll_device" not in (p.scopes or []):
            raise TasukiError("This connection cannot enrol phones. Connect with the claim secret instead of a pairing code.", 409, "missing_scope")
        name = "".join(c for c in (device_name or "") if c.isprintable()).strip()[:80]
        with self._lock:
            now = self._now()
            while self._enroll_times and now - self._enroll_times[0] > ENROLL_WINDOW_S:
                self._enroll_times.popleft()
            if len(self._enroll_times) >= ENROLL_LIMIT:
                wait = int(ENROLL_WINDOW_S - (now - self._enroll_times[0])) + 1
                raise TasukiError(f"Too many enrolment codes requested. Try again in {wait} seconds.", 429, "rate_limited", {"retry_after": wait})
            self._enroll_times.append(now)
        try:
            r = self.make_client(p).device_code(name or None)
        except TasukiError as e:
            if e.status == 401:
                raise TasukiError("tasuki no longer accepts this connection. Connect again.", 409, "agent_revoked") from None
            if e.status == 403:
                raise TasukiError("This connection cannot enrol phones. Connect with the claim secret.", 409, "missing_scope") from None
            if e.status == 429:
                raise TasukiError("tasuki is limiting enrolment codes (3 per 10 minutes). Wait a few minutes.", 429, "rate_limited") from None
            raise
        code, exp = (r or {}).get("code"), (r or {}).get("expires_at")
        if not isinstance(code, str) or not code or not exp:
            raise TasukiError("tasuki server answered with an unexpected enrolment reply", 502, "bad_response")
        log.info("enroll-device: code issued for agent %s (%d in the last %d min)", p.agent_id[:40], len(self._enroll_times), ENROLL_WINDOW_S // 60)
        return {"code": code, "expires_at": exp, "server_url": p.server}

    def status(self) -> dict:
        p = self.pairing()
        w = self.worker().status() if (self._worker or p) else {"running": False, "last_claim_at": None, "last_error": None}
        return {"version": VERSION, "protocol_version": PROTOCOL_VERSION, "paired": bool(p),
                "claimed": bool(p and p.claimed), "enroll_available": bool(p and "enroll_device" in (p.scopes or [])),
                "claim_secret_remembered": self._claim_remembered(), "features": FEATURES,
                "server": {"origin": p.server} if p else None, "agent_id": p.agent_id if p else None,
                "scopes": p.scopes if p else [], "worker": w, "capabilities": CAPABILITIES,
                "mirror": self.mirror_status(), "config": {"mirror_dir": str(self.mirror_dir()), "mirror_dir_default": str(self.default_mirror_dir())}}

    def _claim_remembered(self) -> bool:
        try:
            return bool(self.store.secrets.get(CLAIM_KEY))
        except Exception:
            return False

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
