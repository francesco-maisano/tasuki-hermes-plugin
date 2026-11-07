"""The inbox: a folder where a dropped markdown file becomes a tasuki task.

    <mirror dir>/inbox/            *.md dropped here are read, validated and POSTed to tasuki (POST /api/v1/tasks)
    <mirror dir>/inbox/processed/  the file after it became a task (``task_id`` appended to its front matter)
    <mirror dir>/inbox/rejected/   the file after a validation or scope failure, with ``<name>.reason.txt`` beside it

Rules (each has a test):
* The folder is opened once with O_DIRECTORY|O_NOFOLLOW and every later operation is relative to that descriptor
  (``dir_fd``): a name can never point outside the folder, and a symlink is never followed (it is moved aside, as a link, and
  rejected). Only regular files are read (O_NOFOLLOW|O_NONBLOCK, then fstat).
* At most 64 KB per file (bigger is rejected without reading it), UTF-8 text only, 30 files a minute (the rest wait).
* The task is created through the REST API with the agent token, ``source`` = ``agent:<agent id>``, and the idempotency key is the
  hash of the file bytes, so a re-drop of the same file is the same task (the server replays within 24 h; a local table of
  hashes covers longer).
* A temporary-looking name (leading dot, ``~``, ``.tmp``, ``.part``, ``.swp``) and README files are ignored; a file younger than the
  settle time is left for the next scan (so a file still being written is never read half-way).
* Server trouble (429, 5xx, unreachable) leaves the file where it is for the next scan; only a verdict (created, 4xx) moves it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat as statmod
import time
from typing import Any, Optional

from .client import TasukiError

log = logging.getLogger("tasuki.inbox")

MAX_BYTES = 64 * 1024
PER_MINUTE = 30
ALLOWED_KEYS = ("title", "due", "project", "client", "labels", "priority", "effort", "next")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SKIP = re.compile(r"(^\.|~$|\.(tmp|part|swp|swx|crdownload)$|^readme(\.md)?$)", re.I)


class Reject(Exception):
    """A verdict: the file cannot become a task. args[0] is a short code, args[1] a sentence for the .reason.txt."""


def parse_inbox(raw: bytes, filename: str) -> dict:
    """Front matter (optional) + body -> the fields of a new task. Raises Reject."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Reject("not_utf8", "The file is not UTF-8 text.") from None
    if "\x00" in text:
        raise Reject("not_text", "The file contains NUL bytes: it is not a text file.")
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    fm: dict = {}
    body = text
    if text.startswith("---\n") or text == "---":
        end = text.find("\n---", 3)
        if end < 0:
            raise Reject("bad_front_matter", "The front matter starts with --- but is never closed.")
        head = text[4:end]
        after = text[end + 4:]
        if after and not after.startswith("\n"):
            raise Reject("bad_front_matter", "The closing --- of the front matter must be on its own line.")
        body = after.lstrip("\n")
        fm = _front_matter(head)
    title = str(fm.pop("title", "") or "").strip()
    notes = body
    if not title:
        m = re.match(r"\s*#[ \t]+([^\n]+?)[ \t]*(?:\n|$)", body)
        if m:
            title = m.group(1).strip()
            notes = body[m.end():]
    if not title:
        stem = re.sub(r"\.md$", "", os.path.basename(filename), flags=re.I)
        title = re.sub(r"[-_]+", " ", stem).strip()
    if not title:
        raise Reject("no_title", "No title: put a '# heading', a title: line, or name the file.")
    if len(title) > 500:
        raise Reject("title_too_long", "The title is longer than 500 characters.")
    notes = notes.strip("\n")
    if len(notes) > 20000:
        raise Reject("notes_too_long", "The notes are longer than 20000 characters.")
    out: dict = {"title": title}
    if notes:
        out["notes"] = notes
    due = str(fm.get("due") or "").strip()
    if due:
        if not _DATE.match(due):
            raise Reject("bad_due", "due must be a date like 2026-11-20.")
        out["due_date"] = due
    for k in ("project", "next", "effort"):
        v = str(fm.get(k) or "").strip()
        if v:
            if len(v) > 500:
                raise Reject(f"bad_{k}", f"{k} is too long (max 500).")
            out[k] = v
    pr = str(fm.get("priority") or "").strip()
    if pr:
        if pr not in ("1", "2", "3", "4"):
            raise Reject("bad_priority", "priority must be 1, 2, 3 or 4.")
        out["priority"] = int(pr)
    client = str(fm.get("client") or "").strip()
    if client:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", client):
            raise Reject("bad_client", "client must be a short slug like acme-pty-ltd.")
        out["refs"] = {"client": client}
    labels = fm.get("labels")
    if labels:
        ls = [str(x).strip() for x in labels] if isinstance(labels, list) else [x.strip() for x in str(labels).split(",")]
        ls = [x for x in ls if x]
        if len(ls) > 20 or any(len(x) > 60 or re.search(r"[\x00-\x1f,]", x) for x in ls):
            raise Reject("bad_labels", "labels: at most 20, each up to 60 characters without commas.")
        if ls:
            out["labels"] = ls
    return out


def _front_matter(head: str) -> dict:
    """The tiny YAML subset the inbox accepts: `key: value`, `key: [a, b]`, and `key:` followed by `- item` lines."""
    fm: dict = {}
    key: Optional[str] = None
    for raw in head.split("\n"):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^- +(.*)$", line.strip())
        if m and key and isinstance(fm.get(key), list):
            fm[key].append(_scalar(m.group(1)))
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not m or line[0] in " \t":
            raise Reject("bad_front_matter", "Front matter lines must look like 'key: value'.")
        k, v = m.group(1).lower(), m.group(2).strip()
        if k not in ALLOWED_KEYS:
            raise Reject("unknown_key", f"Unknown front matter key '{k}'. Allowed: {', '.join(ALLOWED_KEYS)}.")
        if k in fm:
            raise Reject("bad_front_matter", f"The key '{k}' appears twice.")
        key = k
        if v == "":
            fm[k] = [] if k == "labels" else ""
        elif v.startswith("[") and v.endswith("]"):
            fm[k] = [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
        else:
            fm[k] = _scalar(v)
    return fm


def _scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _unique(dir_fd: int, name: str) -> str:
    base, ext = os.path.splitext(name)
    cand, n = name, 0
    while True:
        try:
            os.stat(cand, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return cand
        n += 1
        cand = f"{base}-{int(time.time())}-{n}{ext}"


class InboxWatcher:
    def __init__(self, mirror: Any) -> None:
        self.m = mirror

    # ---- directory handles (opened per scan: cheap, and immune to the folder being replaced) ----
    def _open_dirs(self) -> Optional[tuple]:
        root = self.m.dir()
        try:
            if statmod.S_ISLNK(os.lstat(root / "inbox").st_mode):
                return None                                    # an inbox that is a symlink is not followed
            inbox = os.open(root / "inbox", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            for sub in ("processed", "rejected"):
                try:
                    os.mkdir(sub, 0o700, dir_fd=inbox)
                except FileExistsError:
                    pass
            fds = []
            for sub in ("processed", "rejected"):
                fds.append(os.open(sub, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=inbox))
        except OSError:
            os.close(inbox)
            return None
        return inbox, fds[0], fds[1]

    def _allowed_now(self) -> bool:
        now = time.monotonic()
        b = self.m.inbox_bucket
        while b and b[0] <= now - 60:
            b.pop(0)
        return len(b) < PER_MINUTE

    def scan(self, client: Any, scopes: list, agent_id: str) -> dict:
        """One pass over the inbox. Returns counts {processed, rejected, pending}."""
        res = {"processed": 0, "rejected": 0, "pending": 0}
        dirs = self._open_dirs()
        if dirs is None:
            return res
        inbox, processed, rejected = dirs
        try:
            names = []
            with os.scandir(inbox) as it:
                for e in it:
                    if e.name in ("processed", "rejected") or _SKIP.search(e.name):
                        continue
                    names.append(e.name)
            names.sort()
            for name in names:
                if not self._allowed_now():
                    res["pending"] += 1
                    continue
                self.m.inbox_bucket.append(time.monotonic())               # every verdict counts towards the 30 a minute
                v = self._one(client, scopes, agent_id, inbox, processed, rejected, name)
                if v in ("wait", "stop"):
                    self.m.inbox_bucket.pop()                              # no verdict: not spent
                if v == "wait":
                    res["pending"] += 1
                elif v == "stop":
                    res["pending"] += 1
                    break
                else:
                    res[v] += 1
        finally:
            for fd in dirs:
                os.close(fd)
        self.m.inbox_pending = res["pending"]
        return res

    def _reject(self, inbox: int, rejected: int, name: str, code: str, why: str) -> str:
        dest = _unique(rejected, name)
        try:
            os.rename(name, dest, src_dir_fd=inbox, dst_dir_fd=rejected)       # rename moves a symlink itself, never its target
        except OSError:
            return "wait"
        try:
            fd = os.open(dest + ".reason.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=rejected)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{code}\n{why}\n")
        except OSError:
            pass
        with self.m.lock:
            self.m.state["counters"]["inbox_rejected"] += 1
            self.m.save()
        log.info("inbox rejected a file: %s", code)
        return "rejected"

    def _one(self, client: Any, scopes: list, agent_id: str, inbox: int, processed: int, rejected: int, name: str) -> str:
        try:
            st = os.stat(name, dir_fd=inbox, follow_symlinks=False)
        except OSError:
            return "wait"
        if statmod.S_ISLNK(st.st_mode):
            return self._reject(inbox, rejected, name, "symlink", "Symbolic links are never followed; copy the file itself into the inbox.")
        if not statmod.S_ISREG(st.st_mode):
            return self._reject(inbox, rejected, name, "not_a_file", "Only regular files are read.")
        if not name.lower().endswith(".md"):
            return self._reject(inbox, rejected, name, "not_markdown", "Only .md files are turned into tasks.")
        if st.st_size > MAX_BYTES:
            return self._reject(inbox, rejected, name, "too_large", f"The file is {st.st_size} bytes; the limit is {MAX_BYTES}.")
        if time.time() - st.st_mtime < self.m.inbox_settle_s:
            return "wait"                                                      # maybe still being written
        if not ({"write", "add"} & set(scopes)):
            return self._reject(inbox, rejected, name, "missing_scope", "This agent token lacks the write scope, so it cannot create tasks.")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=inbox)
        except OSError:
            return self._reject(inbox, rejected, name, "unreadable", "The file could not be opened (a symlink or no permission).")
        try:
            fst = os.fstat(fd)
            if not statmod.S_ISREG(fst.st_mode) or fst.st_size > MAX_BYTES:
                return self._reject(inbox, rejected, name, "too_large" if fst.st_size > MAX_BYTES else "not_a_file", "Not a small regular file.")
            with os.fdopen(fd, "rb", closefd=False) as f:
                raw = f.read(MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_BYTES:
            return self._reject(inbox, rejected, name, "too_large", f"The file is larger than {MAX_BYTES} bytes.")
        key = hashlib.sha256(raw).hexdigest()
        known = self.m.state["inbox_hashes"].get(key)
        try:
            fields = parse_inbox(raw, name)
        except Reject as r:
            return self._reject(inbox, rejected, name, str(r.args[0]), str(r.args[1]))
        task_id = known
        if not task_id:
            body = {**fields, "source": {"label": f"agent:{agent_id}"}}
            try:
                out = client.request("POST", "/api/v1/tasks", body=body, headers={"Idempotency-Key": f"inbox-{key[:40]}"})
            except TasukiError as e:
                if e.status == 401:
                    self.m.error = "unauthorized"
                    return "stop"
                if e.status in (400, 409, 413, 422):
                    return self._reject(inbox, rejected, name, e.code or "rejected_by_server", _safe(str(e)))
                if e.status == 403:
                    return self._reject(inbox, rejected, name, "missing_scope", _safe(str(e)) or "tasuki refused: the token lacks a scope.")
                return "stop" if e.status in (0, 429) or e.status >= 500 else "wait"   # try again next scan
            task_id = (out or {}).get("id") if isinstance(out, dict) else None
            if not task_id:
                dup = (out or {}).get("duplicate_of") if isinstance(out, dict) else None
                task_id = dup or (out or {}).get("task_id")
            if not task_id:
                return "wait"
        with self.m.lock:
            h = self.m.state["inbox_hashes"]
            h[key] = task_id
            while len(h) > 2000:
                h.pop(next(iter(h)))
        text = raw.decode("utf-8", "replace")
        stamp = f"task_id: {task_id}\nprocessed_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        if text.startswith("---\n"):
            end = text.find("\n---", 3)
            text = text[:end + 1] + stamp + text[end + 1:]
        else:
            text = "---\n" + stamp + "---\n" + text
        dest = _unique(processed, name)
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=processed)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.unlink(name, dir_fd=inbox)
        except OSError:
            return "wait"
        with self.m.lock:
            self.m.state["counters"]["inbox_processed"] += 1
            self.m.save()
        log.info("inbox created a task")
        return "processed"


def _safe(msg: str) -> str:
    """A server message for a .reason.txt: one line, no control characters, bounded."""
    return re.sub(r"[\x00-\x1f]+", " ", msg)[:300]
