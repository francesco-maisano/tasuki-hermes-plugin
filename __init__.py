"""Tasuki Agent Gateway plugin for Hermes (agent side).

register(ctx) adds the read-only data tools (mail, calendar, food; mail is draft-only). It starts nothing: the
delegation worker and the pair/status/unpair routes live in dashboard/plugin_api.py, which Hermes mounts inside the
dashboard process once the plugin is installed and enabled. No hooks: nothing here changes a non-tasuki session.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parent
_CORE = "_tasuki_hermes_core"


def _load_core():
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


def register(ctx) -> None:
    _load_core()
    from _tasuki_hermes_core.service import get_service
    from _tasuki_hermes_core.tools import specs

    svc = get_service()
    check = svc.tools.paired
    try:  # Hermes caches check_fn verdicts for 30 s; pairing is a cheap local read, so ask every time
        from tools.registry import no_cache_check_fn
        no_cache_check_fn(check)
    except Exception:
        pass
    for name, description, schema, handler in specs(svc.tools):
        try:
            ctx.register_tool(
                name=name, toolset="tasuki",
                schema={"name": name, "description": description, "parameters": schema},
                handler=handler, check_fn=check, description=description, emoji="🗓️")
        except Exception:
            logger.warning("tasuki: could not register %s", name, exc_info=True)
