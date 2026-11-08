"""HTTP client for the tasuki server and bridge. Stdlib only, no redirects (a bearer must never follow one), TLS verified.

Bridge calls pass through an ALLOWLIST: the only write the plugin can make is a mail reply DRAFT. There is no send,
delete, forward, move, flag or calendar-write path to call, and a test enumerates this.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import CAPABILITIES, PROTOCOL_VERSION, VERSION

UA = f"tasuki-hermes-plugin/{VERSION}"


class TasukiError(Exception):
    """A short, safe-to-show failure. Never carries a token or response body."""

    def __init__(self, message: str, status: int = 0, code: str = "", extra: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status, self.code = status, code
        self.extra = extra or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def normalise_server(url: str) -> str:
    """Origin (plus optional path prefix) of a tasuki server. https only, except loopback for development."""
    u = urllib.parse.urlsplit((url or "").strip())
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise TasukiError("server_url must be a plain http(s) URL", 400, "bad_server_url")
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise TasukiError("server_url must be https (http is only allowed for localhost)", 400, "insecure_server_url")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


def _err(e: urllib.error.HTTPError) -> TasukiError:
    """Both error shapes: the gateway's ``{"error": "<code or sentence>", "message": ..., "code": ...}`` (cloud/docs/DELEGATION.md
    section 6; also what the core's older routes use) and the bridge's ``{"error": {"code", "message"}}``."""
    code = message = ""
    try:
        j = json.loads(e.read(65536).decode("utf-8", "replace"))
        err = j.get("error") if isinstance(j, dict) else None
        if isinstance(err, dict):
            code, message = str(err.get("code") or "")[:60], str(err.get("message") or "")[:200]
        elif isinstance(j, dict):
            code = str(j.get("code") or "")[:60]
            message = str(j.get("message") or (err if isinstance(err, str) else "") or "")[:200]
            if not code and isinstance(err, str) and re.fullmatch(r"[a-z][a-z0-9_]{2,40}", err):
                code = err
    except Exception:
        pass
    return TasukiError(message or f"server answered {e.code}", e.code, code or f"http_{e.code}")


class HttpClient:
    def __init__(self, server: str, token: str = "", timeout: float = 20.0, ctx: Optional[ssl.SSLContext] = None) -> None:
        self.server, self._token, self.timeout = server.rstrip("/"), token, timeout
        handlers: list = [_NoRedirect()]
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, path: str, body: Any = None, query: Optional[dict] = None, timeout: Optional[float] = None,
                auth: bool = True, headers: Optional[dict] = None) -> Any:
        url = self.server + path
        if query:
            q = {k: v for k, v in query.items() if v not in (None, "")}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if auth and self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as r:
                if r.status == 204:
                    return None
                raw = r.read(8_000_000)
        except urllib.error.HTTPError as e:
            raise _err(e) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise TasukiError(f"tasuki server not reachable ({type(reason).__name__})", 0, "unreachable") from None
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            raise TasukiError("tasuki server sent something that is not JSON", 502, "bad_response") from None

    # ---- agent gateway contract ----
    def pair(self, code: str, name: str) -> dict:
        return self.request("POST", "/api/v1/agents/pair", auth=False, body={
            "code": code, "agent": {"kind": "hermes", "name": name, "version": VERSION, "capabilities": CAPABILITIES}})

    def claim(self, secret: str, name: str) -> dict:
        """Owner claim (POST /api/v1/owner/claim, no auth): trade the server's claim secret for an agent token with the assistant
        scopes plus `enroll_device`. The secret travels only in this one request body."""
        return self.request("POST", "/api/v1/owner/claim", auth=False, body={
            "claim_secret": secret, "agent": {"kind": "hermes", "name": name, "version": VERSION, "capabilities": CAPABILITIES}})

    def device_code(self, device_name: Optional[str] = None) -> dict:
        """Mint a one-time phone enrolment code (POST /api/v1/agents/me/device-codes; needs the `enroll_device` scope)."""
        return self.request("POST", "/api/v1/agents/me/device-codes", body={"device_name": device_name} if device_name else {}) or {}

    def next_delegation(self, wait: int = 25) -> Optional[dict]:
        r = self.request("GET", "/api/v1/agents/me/delegations/next", query={"wait": wait}, timeout=wait + 12)
        return (r or {}).get("delegation") if isinstance(r, dict) else None

    def heartbeat(self, delegation_id: str) -> dict:
        return self.request("POST", f"/api/v1/delegations/{_seg(delegation_id)}/heartbeat", body={}) or {}

    def post_event(self, delegation_id: str, seq: int, type_: str, text: Optional[str] = None, data: Any = None) -> None:
        body: dict = {"seq": seq, "type": type_}
        if text is not None:
            body["text"] = text
        if data is not None:
            body["data"] = data
        self.request("POST", f"/api/v1/delegations/{_seg(delegation_id)}/events", body=body)

    def revoke_self(self) -> dict:
        """The agent revokes its own token (DELETE /api/v1/agents/me): what unpair does so the server stops trusting it."""
        return self.request("DELETE", "/api/v1/agents/me") or {}

    # ---- bridge (allowlisted) ----
    def bridge(self, method: str, path: str, query: Optional[dict] = None, body: Any = None) -> Any:
        if not bridge_allowed(method, path):
            raise TasukiError("that bridge call is not allowed from the Hermes plugin", 403, "not_allowed")
        return self.request(method, "/bridge" + path, query=query, body=body, timeout=45)


def _seg(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


_ID = r"[^/?#\s]+"
ALLOWED_BRIDGE = (
    ("GET", re.compile(r"^/mail/(conversations|folders|accounts)$")),
    ("GET", re.compile(rf"^/mail/threads/{_ID}$")),
    ("GET", re.compile(rf"^/mail/messages/{_ID}$")),
    ("POST", re.compile(rf"^/mail/messages/{_ID}/draft-reply$")),      # DRAFT only, the single write
    ("GET", re.compile(r"^/calendar/(events|free|summary)$")),
    ("GET", re.compile(rf"^/calendar/events/{_ID}/prep$")),
    ("GET", re.compile(r"^/food/(day|week|recent)$")),
    ("GET", re.compile(r"^/health/days$")),                             # the shared daily health summary, read only
)


def bridge_allowed(method: str, path: str) -> bool:
    p = path.split("?", 1)[0]
    return ".." not in p and any(method == m and rx.match(p) for m, rx in ALLOWED_BRIDGE)
