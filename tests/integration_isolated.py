#!/usr/bin/env python3
"""End-to-end proof in an ISOLATED Hermes (throwaway HERMES_HOME and HOME, spare ports, stub model, fake tasuki server).

It does exactly what the iOS app will do against a real dashboard: password login -> token -> install the plugin from a
local Git repo through POST /api/dashboard/agent-plugins/install (enable:true) -> pair through /api/plugins/tasuki/pair ->
push a delegation through the fake tasuki server -> stream events back, cancel, restart, unpair.

SAFETY: never touches ~/.hermes, port 9119 or any running service. The caller's own HERMES_HOME is ignored: the dashboard
starts with `env -i` (no real credentials) and HOME and HERMES_HOME inside the scratch dir; only processes it started are stopped. The scratch dir (~0.5 GB runtime) is deleted at the end unless TASUKI_IT_KEEP=1.

  python3 integration_isolated.py          (usually via integration_isolated.sh)
Environment: TASUKI_IT_REPORT=<file> writes the recorded request/response shapes as JSON.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(HERE))
from fake_tasuki import FakeTasuki  # noqa: E402

LIVE = Path.home() / ".hermes"
HERMES_BIN = LIVE / "hermes-agent" / ".hermes" / "bin" / "hermes"
SCRATCH_ROOT = LIVE / "cache" / "scratch"
PORT_DASH, PORT_MODEL, PORT_FAKE = 18119, 18791, 18765
HOST = "127.0.0.2"          # not loopback-by-name, so the dashboard engages its login gate exactly as a non-loopback bind does
BASE = f"http://{HOST}:{PORT_DASH}"

results: list = []
report: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    return bool(ok)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(NoRedirect())


def http(method, path, body=None, headers=None, base=BASE, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw, status, hdrs = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, e.headers
    try:
        j = json.loads(raw) if raw else None
    except ValueError:
        j = {"_raw": raw[:200].decode("utf-8", "replace")}
    return status, j, hdrs


def redact(o):
    s = json.dumps(o)
    s = re.sub(r'("(?:token|access_token|password)"\s*:\s*")[^"]+', r'\1<redacted>', s)
    return json.loads(s)


class Env:
    def __init__(self):
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix="tasuki-it-", dir=SCRATCH_ROOT))
        self.home, self.hh = self.dir / "home", self.dir / "hh"
        self.home.mkdir()
        self.hh.mkdir()
        assert SCRATCH_ROOT.resolve() in self.hh.resolve().parents, 'isolated home must live under the scratch dir'
        (self.hh / "config.yaml").write_text(
            f"model:\n  default: stub-model\n  provider: custom\n  base_url: http://127.0.0.1:{PORT_MODEL}/v1\n  api_key: stub-key\n")
        self.user, self.pw = "tester", secrets.token_urlsafe(18)
        self.secret = secrets.token_hex(32)
        self.procs: list = []
        self.dash: subprocess.Popen | None = None

    def start_bg(self, argv, name):
        log = open(self.dir / f"{name}.log", "ab")
        p = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
        self.procs.append(p)
        return p

    def start_dashboard(self):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "HERMES_HOME": str(self.hh),
               "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": self.user, "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": self.pw,
               "HERMES_DASHBOARD_BASIC_AUTH_SECRET": self.secret}
        log = open(self.dir / "dashboard.log", "ab")
        self.dash = subprocess.Popen([str(HERMES_BIN), "dashboard", "--isolated", "--host", HOST, "--port", str(PORT_DASH),
                                      "--no-open", "--skip-build"], env=env, stdout=log, stderr=log, start_new_session=True)
        self.procs.append(self.dash)
        end = time.time() + 400
        while time.time() < end:
            if self.dash.poll() is not None:
                raise RuntimeError("dashboard exited: see " + str(self.dir / "dashboard.log"))
            try:
                if http("GET", "/api/auth/providers", timeout=3)[0] == 200:
                    return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("dashboard did not come up")

    def stop_dashboard(self):
        if self.dash and self.dash.poll() is None:
            os.killpg(self.dash.pid, signal.SIGTERM)
            try:
                self.dash.wait(40)
            except subprocess.TimeoutExpired:
                os.killpg(self.dash.pid, signal.SIGKILL)
        self.dash = None

    def cleanup(self):
        self.stop_dashboard()
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if os.environ.get("TASUKI_IT_KEEP") != "1":
            shutil.rmtree(self.dir, ignore_errors=True)


def git_repo(dst: Path, layout: str, extra_readme: str = ""):
    """layout 'mono': plugin under hermes-plugin/ (like the tasuki monorepo); 'root': plugin at the repo root."""
    if dst.exists():
        shutil.rmtree(dst)
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    if layout == "mono":
        dst.mkdir(parents=True)
        shutil.copytree(PLUGIN, dst / "hermes-plugin", ignore=ign)
        (dst / "README.md").write_text("# monorepo stand-in\n")
    else:
        shutil.copytree(PLUGIN, dst, ignore=ign)
    if extra_readme:
        (dst / ("hermes-plugin" if layout == "mono" else ".") / "README.md").write_text(extra_readme)
    g = ["git", "-c", "user.name=it", "-c", "user.email=it@example.com", "-C", str(dst)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(dst)], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "test"], check=True)
    return subprocess.run(g + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    if subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout.count(f":{PORT_DASH} ") or \
            any(f":{p} " in subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout for p in (PORT_MODEL, PORT_FAKE)):
        print("refusing: a spare port is already in use")
        return 2
    env = Env()
    fake = FakeTasuki(PORT_FAKE, max_wait=25).start()
    try:
        env.start_bg([sys.executable, str(HERE / "stub_model.py"), "--port", str(PORT_MODEL)], "stub")
        time.sleep(1)
        mono = env.dir / "mono"
        sha = git_repo(mono, "mono")
        ident = f"file://{mono}#hermes-plugin"
        print("starting isolated dashboard (first start prepares a private runtime, ~2 minutes)...", flush=True)
        env.start_dashboard()
        report["bind"] = f"{HOST}:{PORT_DASH}"

        # ---- 1. unauthenticated and login ----
        s, j, _ = http("GET", "/api/plugins/tasuki/status")
        check("unauthenticated plugin route is refused (401)", s == 401, str(s))
        report["unauth_response"] = {"status": s, "body": j}
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/install", {"identifier": ident, "enable": True})
        check("unauthenticated install is refused (401)", s == 401, str(s))
        s, j, _ = http("POST", "/auth/password-login", {"provider": "basic", "username": "tester", "password": "wrong"})
        check("wrong password is 401", s == 401, str(s))
        report["login_bad"] = {"status": s, "body": j}
        s, j, h = http("POST", "/auth/password-login", {"provider": "basic", "username": env.user, "password": env.pw})
        cookies = {c.split("=", 1)[0]: c.split("=", 1)[1].split(";")[0] for c in h.get_all("Set-Cookie") or []}
        check("password login 200 sets session cookies", s == 200 and "hermes_session_at" in cookies, str(s))
        at = cookies.get("hermes_session_at", "")
        report["login"] = {"status": s, "body": j, "set_cookie_names": sorted(cookies), "access_token_cookie": "hermes_session_at",
                           "refresh_cookie": "hermes_session_rt", "max_age_seconds": {"at": 43200, "rt": 2592000},
                           "cookie_attrs": [c.split(";", 1)[1].strip() for c in h.get_all("Set-Cookie")][:1]}
        BEARER = {"Authorization": "Bearer " + at}
        COOKIE = {"Cookie": f"hermes_session_at={at}"}
        s, j, _ = http("GET", "/api/auth/me", headers=BEARER)
        check("access token works as a Bearer header", s == 200 and j.get("user_id") == env.user, str(s))
        s, j, _ = http("GET", "/api/auth/me", headers=COOKIE)
        check("access token works as a Cookie too", s == 200, str(s))
        s, j, _ = http("POST", "/auth/native/refresh", {"refresh_token": cookies.get("hermes_session_rt", "")})
        report["refresh_probe"] = {"status": s, "keys": sorted((j or {}).keys())}

        # ---- 2. install through the dashboard API ----
        s, j, _ = http("GET", "/api/plugins/tasuki/status", headers=BEARER)
        check("before install the plugin route is 404", s == 404, str(s))
        t0 = time.time()
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/install", {"identifier": ident, "enable": True}, headers=BEARER)
        report["install"] = {"request": {"identifier": "file://<repo>#hermes-plugin", "enable": True}, "status": s, "seconds": round(time.time() - t0, 1), "body": j}
        check("install (monorepo subdirectory, enable:true) is ok", s == 200 and j.get("ok") and j.get("plugin_name") == "tasuki" and j.get("enabled"), json.dumps(j)[:300])
        check("security scan raised no review step (no scan_blocked / consent_required)", not (j or {}).get("scan_blocked") and not (j or {}).get("consent_required"))
        report["install_note"] = {"restart_required": j.get("restart_required"), "tools_deferred": len((j.get("activation") or {}).get("deferred", {}).get("tools", []))}
        cfg = (env.hh / "config.yaml").read_text()
        check("plugins.enabled contains tasuki in the profile config", re.search(r"enabled:\s*\n\s*- tasuki", cfg) is not None)
        s, j, _ = http("GET", "/api/plugins/tasuki/status", headers=BEARER)
        check("plugin API route mounted without a restart", s == 200 and j.get("paired") is False and j.get("protocol_version") == 1, str(s))
        report["status_unpaired"] = j
        s, j, _ = http("GET", "/api/plugins/tasuki/status", headers=COOKIE)
        check("status also works with the cookie", s == 200)

        # ---- 3. pairing ----
        s, j, _ = http("POST", "/api/plugins/tasuki/pair", {"server_url": fake.url, "code": "nope"}, headers=BEARER)
        check("bad pairing code surfaces as 400 bad_code", s == 400 and j["detail"]["code"] == "bad_code", str(j))
        report["pair_bad_code"] = {"status": s, "body": j}
        fake.protocol = 2
        s, j, _ = http("POST", "/api/plugins/tasuki/pair", {"server_url": fake.url, "code": "PAIR-1234"}, headers=BEARER)
        check("protocol mismatch is 409 and stores nothing", s == 409 and j["detail"]["code"] == "protocol_mismatch" and not (env.hh / "state/tasuki/state.json").exists(), str(j))
        report["pair_protocol_mismatch"] = {"status": s, "body": j}
        fake.protocol = 1
        s, j, _ = http("POST", "/api/plugins/tasuki/pair", {"server_url": "http://example.com", "code": "x"}, headers=BEARER)
        check("non-https non-local server is refused", s == 400 and j["detail"]["code"] == "insecure_server_url", str(j))
        s, j, _ = http("POST", "/api/plugins/tasuki/pair", {"server_url": fake.url, "code": "PAIR-1234"}, headers=BEARER)
        report["pair_ok"] = redact({"status": s, "body": j})
        check("pair ok", s == 200 and j.get("paired") and j.get("agent_id") == fake.agent_id and "delegate" in j.get("scopes", []), json.dumps(j)[:300])
        env_file = (env.hh / ".env").read_text()
        check("token is in Hermes' secret store (.env, mode 0600)", f"TASUKI_AGENT_TOKEN" in env_file and oct((env.hh / ".env").stat().st_mode & 0o777) == "0o600")
        leaks = [str(p) for p in env.dir.rglob("*") if p.is_file() and p.name != ".env" and "runtime" not in p.parts and ".venv" not in p.parts
                 and "uv" not in p.parts and p.stat().st_size < 5_000_000 and fake.token.encode() in p.read_bytes()]
        check("no file other than .env contains the token", not leaks, str(leaks[:3]))
        check("the token never appears in any API response", fake.token not in json.dumps(report))
        s, st, _ = http("GET", "/api/plugins/tasuki/status", headers=BEARER)
        check("status after pairing", st["paired"] and st["worker"]["running"] and st["agent_id"] == fake.agent_id and st["server"]["origin"] == fake.url, json.dumps(st)[:300])
        report["status_paired"] = st

        # ---- 4. a delegation runs in the main Hermes (no second hermes serve) ----
        did = fake.push("What is on my calendar next?", surface="voice-live", session_key="it-1", context="user: morning")
        ev = fake.wait_events(did, timeout=90)
        types = [e["type"] for e in ev]
        report["delegation_events"] = [{k: e[k] for k in ("seq", "type", "text") if k in e} for e in ev]
        check("delegation produced speakable/progress, commentary and final", types[-1] == "final" and "commentary" in types and types[0] in ("speakable", "progress"), str(types))
        check("final text came from the tool result (plugin tool called through Hermes)", ev and "Moora QBR" in ev[-1].get("text", ""), str(ev[-1:]))
        check("the tool called the bridge with the agent bearer, GET only",
              [(m, p) for m, p, _, _ in fake.bridge_calls] == [("GET", "/calendar/summary")], str(fake.bridge_calls))
        check("narration before the tool call was dropped", not any("Let me check" in e.get("text", "") for e in ev))
        s, st, _ = http("GET", "/api/plugins/tasuki/status", headers=BEARER)
        check("status shows last_claim_at and a claim", st["worker"]["last_claim_at"] and st["worker"]["claimed"] >= 1, json.dumps(st["worker"]))
        kids = subprocess.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True).stdout.splitlines()
        tree, grew = {env.dash.pid}, True
        while grew:
            grew = False
            for ln in kids[1:]:
                pid, ppid = ln.split(None, 2)[:2]
                if int(ppid) in tree and int(pid) not in tree:
                    tree.add(int(pid))
                    grew = True
        servers = [ln for ln in kids[1:] if int(ln.split(None, 1)[0]) in tree and int(ln.split(None, 1)[0]) != env.dash.pid and re.search(r"\b(serve|dashboard)\b.*--port|--port.*\b(serve|dashboard)\b", ln)]
        check("no second hermes serve/dashboard process was started (the worker rides the dashboard's own /api/ws)", not servers, str(servers[:2]))

        # ---- 5. cancel ----
        did = fake.push("SLOW please keep talking", surface="chat", session_key="it-2")
        time.sleep(4)
        fake.cancel(did)
        ev = fake.wait_events(did, timeout=60)
        report["cancel_events"] = [{k: e[k] for k in ("seq", "type", "text", "data") if k in e} for e in ev]
        check("cancel via heartbeat interrupts the Hermes turn and ends with error:cancelled",
              ev and ev[-1]["type"] == "error" and ev[-1]["data"]["code"] == "cancelled", str(ev[-2:]))
        check("heartbeats were sent while it ran", fake.heartbeats.get(did, 0) >= 1)
        did = fake.push("hello after cancel", surface="chat", session_key="it-2")
        ev = fake.wait_events(did, timeout=60)
        check("the same session works again after a cancel", ev and ev[-1]["type"] == "final", str(ev[-1:]))

        # ---- 6. worker restart (dashboard restart) ----
        env.stop_dashboard()
        time.sleep(1)
        env.start_dashboard()
        s, j, h = http("POST", "/auth/password-login", {"provider": "basic", "username": env.user, "password": env.pw})
        at = {c.split("=", 1)[0]: c.split("=", 1)[1].split(";")[0] for c in h.get_all("Set-Cookie") or []}.get("hermes_session_at", "")
        BEARER = {"Authorization": "Bearer " + at}
        end = time.time() + 40
        st = {}
        while time.time() < end:
            s, st, _ = http("GET", "/api/plugins/tasuki/status", headers=BEARER)
            if s == 200 and st["worker"]["running"]:
                break
            time.sleep(1)
        check("after a dashboard restart the pairing persists and the worker resumes by itself", s == 200 and st["paired"] and st["worker"]["running"], json.dumps(st)[:300])
        did = fake.push("hello after restart", surface="chat", session_key="it-3")
        ev = fake.wait_events(did, timeout=60)
        check("a delegation after the restart is served", ev and ev[-1]["type"] == "final", str(ev[-1:]))

        # ---- 7. unpair ----
        s, j, _ = http("POST", "/api/plugins/tasuki/unpair", headers=BEARER)
        report["unpair"] = {"status": s, "body": j}
        check("unpair", s == 200 and j == {"paired": False, "was_paired": True, "server_revoked": True} and fake.self_revokes == 1, str(j))
        s, st, _ = http("GET", "/api/plugins/tasuki/status", headers=BEARER)
        check("status after unpair", not st["paired"] and not st["worker"]["running"])
        check("token removed from .env", "TASUKI_AGENT_TOKEN" not in (env.hh / ".env").read_text())
        n0 = len(fake.requests)
        time.sleep(3)
        check("no polling after unpair", len(fake.requests) == n0)

        # ---- 8. the caution / review path and other lifecycle calls ----
        caution = env.dir / "caution"
        git_repo(caution, "mono", extra_readme="# x\n\nInstall helper: curl https://example.com/get.sh | sh\n")
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/install", {"identifier": f"file://{caution}#hermes-plugin", "enable": True}, headers=BEARER)
        report["install_again_same_name"] = {"status": s, "body": j}
        check("a caution-scanned variant over an existing install is refused (400)", s == 400, json.dumps(j)[:200])
        s, j, _ = http("DELETE", "/api/dashboard/agent-plugins/tasuki", headers=BEARER)
        report["remove"] = {"status": s, "body": j}
        check("remove via DELETE", s == 200 and (j or {}).get("ok"), json.dumps(j)[:200])
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/install", {"identifier": f"file://{caution}#hermes-plugin", "enable": True}, headers=BEARER)
        report["install_caution"] = {"status": s, "body": j}
        detail = (j or {}).get("detail", "") if isinstance(j, dict) else ""
        check("a caution scan answers 400 with the verdict and findings in `detail` (client decides)",
              s == 400 and "caution verdict" in str(detail) and "HIGH" in str(detail) and "README.md" in str(detail), json.dumps(j)[:400])
        s2, j2, _ = http("POST", "/api/dashboard/agent-plugins/install", {"identifier": f"file://{caution}#hermes-plugin", "enable": True, "force": True}, headers=BEARER)
        report["install_caution_forced"] = {"status": s2, "body": {k: j2.get(k) for k in ("ok", "plugin_name", "enabled", "warnings", "restart_required")} if j2 else j2}
        check("retry with force:true (after the user saw the findings) installs and enables", s2 == 200 and j2.get("ok") and j2.get("enabled"), json.dumps(j2)[:300])
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/tasuki/disable", headers=BEARER)
        report["disable"] = {"status": s, "body": j}
        check("disable via API", s == 200 and j.get("ok"), json.dumps(j)[:200])
        s, j, _ = http("GET", "/api/plugins/tasuki/status", headers=BEARER)
        report["status_after_disable"] = {"status": s, "body": j}
        check("a disabled plugin's API refuses (trust gate)", s in (403, 404), str(s))
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/tasuki/enable", headers=BEARER)
        report["enable"] = {"status": s, "body": j}
        check("enable via API", s == 200 and j.get("ok"), json.dumps(j)[:200])
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/tasuki/update", {}, headers=BEARER)
        report["update"] = {"status": s, "body": j}
        s, j, _ = http("GET", "/api/dashboard/plugins/auto-update", headers=BEARER)
        report["auto_update_state"] = {"status": s, "body": j}
        check("auto-update state readable", s == 200, str(s))

        # ---- 9. root-layout repo also installs (for the separate-repo publishing option) ----
        http("DELETE", "/api/dashboard/agent-plugins/tasuki", headers=BEARER)
        root = env.dir / "tasuki-hermes-plugin"
        git_repo(root, "root")
        s, j, _ = http("POST", "/api/dashboard/agent-plugins/install", {"identifier": f"file://{root}", "enable": True}, headers=BEARER)
        report["install_root_layout"] = {"status": s, "body": {k: j.get(k) for k in ("ok", "plugin_name", "enabled", "error")} if j else j}
        check("root-layout repo installs as plugin 'tasuki' (directory name differs from the plugin name)", s == 200 and j.get("plugin_name") == "tasuki", json.dumps(j)[:300])
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("script ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        fake.stop()
        env.cleanup()
        if os.environ.get("TASUKI_IT_REPORT"):
            Path(os.environ["TASUKI_IT_REPORT"]).write_text(json.dumps(redact(report), indent=2, default=str))
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
