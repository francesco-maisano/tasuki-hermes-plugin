"""Dashboard API for the tasuki plugin: mounted by Hermes at /api/plugins/tasuki/ once the plugin is installed AND enabled.

Routes (all need the dashboard credential: the session token on loopback, the login cookie/bearer when the dashboard is
gated): POST /pair, GET /status, POST /unpair, POST /config (the mirror directory), POST /mirror/resync. The delegation worker starts here (this module is imported inside the
dashboard process, which already serves the in-process Hermes gateway at /api/ws).
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("tasuki.api")
_ROOT = Path(__file__).resolve().parent.parent
_CORE = "_tasuki_hermes_core"


def load_core():
    """The single shared copy of the core package (also used by register())."""
    mod = sys.modules.get(_CORE)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(_CORE, _ROOT / "core" / "__init__.py",
                                                  submodule_search_locations=[str(_ROOT / "core")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CORE] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(_CORE, None)
        raise
    return mod


load_core()
from _tasuki_hermes_core.client import TasukiError  # noqa: E402
from _tasuki_hermes_core.service import get_service  # noqa: E402

router = APIRouter()


def _guard(request: Request) -> None:
    """Defence in depth: Hermes' middleware already gates /api/plugins/*, and so do we (fail closed)."""
    try:
        from hermes_cli.web_server import _require_token
    except ImportError:
        if os.environ.get("TASUKI_PLUGIN_TEST") == "1":
            return
        raise HTTPException(status_code=503, detail={"code": "no_auth_guard", "message": "dashboard auth is unavailable"})
    _require_token(request)


def gateway_url() -> str:
    """ws URL of THIS dashboard's own /api/ws plus the credential it accepts from in-process callers.
    Gated dashboard: the process-lifetime internal credential. Loopback dashboard: the session token."""
    from hermes_cli.web_server import _SESSION_TOKEN, app
    port = getattr(app.state, "bound_port", None)
    if not port:
        raise RuntimeError("dashboard port unknown")
    bound = str(getattr(app.state, "bound_host", "") or "")
    # Dial the interface the dashboard really bound (its Host check accepts exactly that); wildcard binds take loopback.
    host = "127.0.0.1" if bound in ("", "0.0.0.0", "::", "localhost") else (f"[{bound}]" if ":" in bound else bound)
    if getattr(app.state, "auth_required", False):
        from hermes_cli.dashboard_auth.ws_tickets import internal_ws_credential
        return f"ws://{host}:{port}/api/ws?internal={internal_ws_credential()}"
    return f"ws://{host}:{port}/api/ws?token={_SESSION_TOKEN}"


def _service():
    s = get_service()
    if s.gateway_url is None:
        s.gateway_url = gateway_url
    return s


def _http(e: TasukiError) -> HTTPException:
    status = e.status if 400 <= e.status < 500 else 502
    if e.status == 0:
        status = 502
    return HTTPException(status_code=status, detail={"code": e.code or "error", "message": str(e)})


class PairBody(BaseModel):
    server_url: str = Field(max_length=300)
    code: str = Field(max_length=64)


@router.post("/pair")
async def pair(request: Request, body: PairBody):
    _guard(request)
    import asyncio
    try:
        return await asyncio.to_thread(_service().pair, body.server_url, body.code)
    except TasukiError as e:
        raise _http(e) from None
    except Exception as e:
        log.error("pair failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail={"code": "internal", "message": f"pairing failed ({type(e).__name__})"}) from None


@router.get("/status")
async def status(request: Request):
    _guard(request)
    return _service().status()


class ConfigBody(BaseModel):
    mirror_dir: str | None = Field(default=None, max_length=500)


@router.post("/config")
async def config(request: Request, body: ConfigBody):
    """Change the task-mirror directory (null or "" resets to the default `<HERMES_HOME>/tasuki/`). Answers the new mirror status."""
    _guard(request)
    import asyncio
    try:
        m = await asyncio.to_thread(_service().set_mirror_dir, body.mirror_dir)
    except TasukiError as e:
        raise _http(e) from None
    return {"mirror": m, "config": _service().status()["config"]}


@router.post("/mirror/resync")
async def mirror_resync(request: Request):
    """Queue a full re-sync of the task mirror (it happens within one poll, at most ~10 s)."""
    _guard(request)
    s = _service()
    if not s.pairing():
        raise HTTPException(status_code=409, detail={"code": "not_paired", "message": "pair tasuki first: the mirror runs only while paired"})
    s.mirror.resync()
    return {"queued": True, "mirror": s.mirror_status()}


@router.post("/unpair")
async def unpair(request: Request):
    _guard(request)
    import asyncio
    return await asyncio.to_thread(_service().unpair)


def _autostart() -> None:
    """Dashboard (re)start with a pairing already stored: resume the worker."""
    try:
        s = _service()
        if s.pairing():
            s.start_worker_if_paired()
    except Exception as e:
        log.error("tasuki worker autostart failed: %s", type(e).__name__)


_autostart()
