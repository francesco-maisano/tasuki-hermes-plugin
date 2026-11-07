"""Where the pairing lives.

* The agent TOKEN is a secret: it goes into Hermes' own secret store (``$HERMES_HOME/.env``, mode 0600, written by
  ``hermes_cli.config.save_env_value``, the same place provider keys live) under ``TASUKI_AGENT_TOKEN``. It is never
  written to the plugin directory, the repo, config.yaml or the state file below.
* Non-secret facts (server origin, agent id, scopes, paired time) go to ``$HERMES_HOME/state/tasuki/state.json``
  (0600). They are not credentials, but they live with the profile, not the plugin.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

TOKEN_KEY = "TASUKI_AGENT_TOKEN"
CLAIM_KEY = "TASUKI_CLAIM_SECRET"      # optional: the owner claim secret, kept only when the user asked (remember:true) or set it by hand


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # not running inside Hermes (unit tests, tooling)
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


class SecretStore:
    def get(self, name: str) -> Optional[str]:
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError


class MemoryStore(SecretStore):
    """Test double."""

    def __init__(self) -> None:
        self.data: dict = {}

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


class HermesEnvStore(SecretStore):
    """Hermes' secret store: ``$HERMES_HOME/.env`` through Hermes' own writer, so locking, quoting and 0600 are Hermes'."""

    def get(self, name):
        try:
            from hermes_cli.config import get_env_value
            return get_env_value(name) or None
        except Exception:
            return None

    def set(self, name, value):
        from hermes_cli.config import save_env_value
        save_env_value(name, value)
        if self.get(name) != value:  # save_env_value silently refuses some writes (blocked profile); never pretend
            raise RuntimeError("Hermes did not store the secret")

    def delete(self, name):
        try:
            from hermes_cli.config import remove_env_value
            remove_env_value(name)
        except Exception:
            pass


@dataclass
class Pairing:
    server: str
    token: str = field(repr=False)
    agent_id: str = ""
    scopes: list = field(default_factory=list)
    paired_at: float = 0.0
    claimed: bool = False          # true when this connection was made with the owner claim secret (not a legacy pairing code)


class PairingStore:
    def __init__(self, secrets: Optional[SecretStore] = None, state_dir: Optional[Path] = None) -> None:
        self.secrets = secrets or HermesEnvStore()
        self._dir = state_dir
        self._lock = threading.Lock()

    @property
    def dir(self) -> Path:
        return self._dir or hermes_home() / "state" / "tasuki"

    @property
    def path(self) -> Path:
        return self.dir / "state.json"

    def load(self) -> Optional[Pairing]:
        with self._lock:
            token = self.secrets.get(TOKEN_KEY)
            try:
                meta = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                return None
        if not token or not isinstance(meta, dict) or not meta.get("server"):
            return None
        return Pairing(server=str(meta["server"]), token=token, agent_id=str(meta.get("agent_id") or ""),
                       scopes=list(meta.get("scopes") or []), paired_at=float(meta.get("paired_at") or 0), claimed=bool(meta.get("claimed")))

    def save(self, p: Pairing) -> None:
        with self._lock:
            self.secrets.set(TOKEN_KEY, p.token)
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            body = json.dumps({"server": p.server, "agent_id": p.agent_id, "scopes": p.scopes,
                               "paired_at": p.paired_at or time.time(), "claimed": bool(p.claimed)})
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(body)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def clear(self) -> None:
        with self._lock:
            self.secrets.delete(TOKEN_KEY)
            try:
                self.path.unlink()
            except OSError:
                pass
