"""Shared fake of the core's owner-claim and device-code routes (used by fake_tasuki.py over HTTP and fake_core.py in process).

  POST /api/v1/owner/claim {claim_secret, agent:{...}}      no auth -> {agent_id, token, scopes[], owner:{id}, server:{origin, protocol_version}}
       401 invalid_claim_secret (the real core's answer); after `lock_after` wrong secrets in a row 429 locked_out (even for the right secret) until `unlock()`;
       429 rate_limited when `claim_rate_limited` is set
  POST /api/v1/agents/me/device-codes {device_name?}        Bearer with `enroll_device` -> {code:'ABCD-EFGH', expires_at (5 min)}
       429 rate_limited after 3 in 10 minutes; 403 missing_scope without the scope. Every code is new.
Errors use the gateway shape {error, message, code}.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

ASSISTANT_SCOPES = ["read", "suggest", "write", "comment", "complete", "delete", "undo", "delegate", "mail:read", "mail:draft",
                    "calendar:read", "food:read"]
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def err(status: int, code: str, message: str):
    return status, {"error": code, "message": message, "code": code}


class ClaimLogic:
    def __init__(self, claim_secret: str = "CLAIM-SECRET-FAKE", now=time.time) -> None:
        self.claim_secret, self.now = claim_secret, now
        self.lock_after = 5
        self.wrong_in_a_row = 0
        self.locked = False
        self.claim_rate_limited = False
        self.claim_attempts: list = []           # bodies that reached the route (the secret included: tests assert it never leaks elsewhere)
        self.claims = 0
        self.device_code_calls: list = []        # (timestamp, body)
        self.device_codes: list = []             # issued: {code, expires_at, device_name}
        self.device_limit, self.device_window = 3, 600
        self.owner_id = "owner_" + uuid.uuid4().hex[:8]

    def unlock(self) -> None:
        self.locked, self.wrong_in_a_row = False, 0

    def claim(self, body: dict, new_agent):
        """new_agent() -> (agent_id, token, origin, protocol_version) and registers the token on the host fake."""
        self.claim_attempts.append(body)
        if self.locked:
            return err(429, "locked_out", "Too many wrong claim secrets; claiming is locked.")
        if self.claim_rate_limited:
            return err(429, "rate_limited", "Slow down.")
        if not isinstance(body.get("claim_secret"), str) or body["claim_secret"] != self.claim_secret:
            self.wrong_in_a_row += 1
            if self.wrong_in_a_row >= self.lock_after:
                self.locked = True
            return err(401, "invalid_claim_secret", "that claim secret is not valid")
        self.wrong_in_a_row = 0
        self.claims += 1
        agent_id, token, origin, proto = new_agent()
        return 200, {"agent_id": agent_id, "token": token, "scopes": ASSISTANT_SCOPES + ["enroll_device"], "owner": {"id": self.owner_id},
                     "server": {"origin": origin, "protocol_version": proto, "claimed": True}}

    def device_code(self, body: dict, scopes: list):
        if "enroll_device" not in scopes:
            return err(403, "missing_scope", "enroll_device scope required")
        t = self.now()
        recent = [x for x in self.device_code_calls if t - x[0] < self.device_window]
        self.device_code_calls = recent
        if len(recent) >= self.device_limit:
            return err(429, "rate_limited", "3 device codes per 10 minutes.")
        self.device_code_calls.append((t, dict(body)))
        while True:
            c = "".join(ALPHABET[uuid.uuid4().int >> (5 * i) & 31] for i in range(8))
            c = c[:4] + "-" + c[4:]
            if all(c != d["code"] for d in self.device_codes):
                break
        exp = (datetime.fromtimestamp(t, timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.device_codes.append({"code": c, "expires_at": exp, "device_name": body.get("device_name")})
        return 200, {"code": c, "expires_at": exp}
