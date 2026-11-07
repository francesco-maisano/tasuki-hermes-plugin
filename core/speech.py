"""Make an agent reply fit speech and label tool calls. (Copied in spirit from tasuki voice; no import from voice/.)"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

PERSONA_VERSION = "v2"
PERSONA_DIR = Path(__file__).with_name("persona")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF\u200d\ufe0f\u20e3\u2B50\u2B06\u2B07\u2B1B\u2B1C\u2300-\u23FF]+")
_SENT_END = re.compile(r"(?<=[.!?])\s+")
SPEAKABLE_MAX = 200
FINAL_MAX = 1500
CHUNK_MAX = 1200
PROPOSAL_RE = re.compile(r"^[ \t]*PROPOSAL:\s*(\{.*)$", re.M)


def sanitize(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"```[\s\S]*?(```|$)", " ", s)
    s = re.sub(r"^\s*\|.*\|\s*$", " ", s, flags=re.M)
    s = re.sub(r"^\s*[-=*_]{3,}\s*$", " ", s, flags=re.M)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"<https?://[^>]+>|(?:https?://|www\.)[^\s]*[^\s.,;:!?)\]]", " ", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*(?:[-+*•]|\d+[.)])\s+", "", s, flags=re.M)
    s = re.sub(r"(\*\*|__|\*|~~)", "", s)
    s = re.sub(r"(?<!\w)_|_(?!\w)", "", s)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)
    s = _EMOJI.sub(" ", s)
    s = re.sub(r'[“”"«»]', "", s)
    s = s.replace("’", "'").replace("—", ", ").replace("–", "-").replace("\u00a0", " ")
    s = re.sub(r":\s*$", ".", s, flags=re.M)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.!?;:])", r"\1", s)
    return s.strip()


def cap(text: str, limit: int) -> str:
    t = text.strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    m = list(re.finditer(r"[.!?](?=\s|$)", cut))
    if m and m[-1].end() > limit * 0.4:
        return cut[: m[-1].end()].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "."


def chunks(text: str, limit: int = CHUNK_MAX) -> list:
    out: list = []
    cur = ""
    for sent in [p for p in _SENT_END.split(text.strip()) if p]:
        while len(sent) > limit:
            head = sent[:limit].rsplit(" ", 1)[0] or sent[:limit]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
            sent = sent[len(head):].strip()
        nxt = f"{cur} {sent}".strip()
        if len(nxt) <= limit:
            cur = nxt
        else:
            out.append(cur)
            cur = sent
    if cur:
        out.append(cur)
    return out


_TOOL_LABELS = (
    (("calendar", "schedule", "event", "meeting"), "Checking your calendar."),
    (("mail", "email", "outlook", "inbox"), "Checking your email."),
    (("food", "nutrition", "meal"), "Checking your food log."),
    (("tasuki", "task", "todo", "todoist", "project"), "Checking your tasks."),
    (("search", "web", "browse", "fetch", "extract"), "Looking that up."),
    (("terminal", "shell", "bash", "exec", "run"), "Running that now."),
    (("file", "read", "write", "patch", "vault", "note"), "Looking at that now."),
)


def tool_line(name: str, args_text: str = "") -> str:
    """One short 'checking' line; deliberately coarse, never the raw tool name or arguments. The tool NAME is matched
    first (so tasuki_calendar_events says calendar, not tasks)."""
    for hay in (str(name).lower(), f"{name} {args_text}".lower()):
        for keys, line in _TOOL_LABELS:
            if any(k in hay for k in keys):
                return line
    return "One moment, working on that."


def parse_proposal(text: str) -> Optional[dict]:
    m = None
    for m in PROPOSAL_RE.finditer(text or ""):
        pass
    if not m:
        return None
    raw = m.group(1)
    try:
        j, _ = json.JSONDecoder().raw_decode(raw)
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    items = [cap(sanitize(str(i)), 120) for i in (j.get("items") or []) if str(i).strip()][:8]
    summary = cap(sanitize(str(j.get("summary") or "")), 160)
    if not summary and not items:
        return None
    return {"summary": summary or "Make those changes", "items": [i for i in items if i],
            "action": str(j.get("action") or "")[:30]}


def strip_proposal(text: str) -> str:
    return PROPOSAL_RE.sub("", text or "").strip()


def question(summary: str, items: list) -> str:
    s = summary.rstrip(".?! ")
    names = [i.rstrip(".") for i in items]
    if not names:
        return f"{s}. Should I go ahead?"
    listed = names[0] if len(names) == 1 else (", ".join(names[:-1]) + " and " + names[-1]) if len(names) <= 3 \
        else ", ".join(names[:3]) + f" and {len(names) - 3} more"
    return cap(f"{s}: {listed}. Should I go ahead?", FINAL_MAX)


def _read(name: str) -> str:
    return (PERSONA_DIR / name).read_text(encoding="utf-8").strip()


def turn_extra() -> str:
    return _read(f"turn_extra.{PERSONA_VERSION}.txt")


def confirmed_extra(summary: str, items: list, request: str) -> str:
    return (_read(f"turn_confirmed.{PERSONA_VERSION}.txt").replace("{summary}", summary)
            .replace("{items}", "; ".join(items) or "(as listed in your proposal)")
            .replace("{request}", request.strip()[:600]))
