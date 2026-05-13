"""Cross-session message bus.

Ports the cctv-backend ``ops/intersession.sh`` pattern into a worker action
surface. Three primitives:

- ``send`` appends a message line to the recipient's inbox log; recipients
  can be a literal SID or a role keyword (``teamlead`` / ``dev`` / ``all``).
- ``inbox_read`` drains lines since the high-water mark.
- ``inbox_wait`` long-polls until the inbox file grows past the mark or a
  timeout elapses. Designed to be invoked from Claude Code with
  ``run_in_background: true`` so the harness's task-notification fires when
  this returns — that's the no-polling push primitive.

File layout under ``data/<slug>/_chat/``:
    inbox-<sid>.log    append-only, one message per line
    seen-<sid>         single int (byte offset), read high-water mark
    heartbeat-<sid>    mtime touched while wait/read is running
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

_MAX_TEXT_LEN = 4000
_MAX_WAIT_TIMEOUT = 1800
_HEARTBEAT_INTERVAL = 10.0


def _chat_dir(cfg: Any, slug: str) -> Path:
    """Return data/<slug>/_chat, creating it 770 if missing."""
    d = Path(cfg.data_dir) / slug / "_chat"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(stat.S_IRWXU | stat.S_IRWXG)  # 770
        except OSError:
            pass
    return d


def _sanitize(text: str) -> str:
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN]
    return text


def _list_session_sids(cfg: Any, slug: str) -> list[tuple[str, dict]]:
    """Parse session md frontmatter for every session in data/<slug>/sessions/.

    Returns (sid, meta) tuples. Meta values are raw strings (no type coercion).
    """
    sess_dir = Path(cfg.data_dir) / slug / "sessions"
    if not sess_dir.exists():
        return []
    out: list[tuple[str, dict]] = []
    for md in sorted(sess_dir.glob("*.md")):
        text = md.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        meta: dict[str, str] = {}
        for line in parts[1].strip().splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        sid = meta.get("sid", md.stem)
        out.append((sid, meta))
    return out


def _resolve_recipients(cfg: Any, slug: str, to: str) -> list[str]:
    """Map a recipient spec to a list of SIDs.

    Role keywords:
      - ``teamlead``: every session with no task_id (or task_id == "~")
      - ``dev``:      every session with a real task_id
      - ``all``:      every session listed in data/<slug>/sessions/

    Anything else is treated as a literal SID (returned as-is — see ``send``
    docstring: an unknown SID still gets a per-sid inbox so the recipient
    will pick it up on their next read).
    """
    if to in {"teamlead", "dev", "all"}:
        sids: list[str] = []
        for sid, meta in _list_session_sids(cfg, slug):
            tid = meta.get("task_id", "") or ""
            is_dev = bool(tid) and tid != "~"
            if to == "all":
                sids.append(sid)
            elif to == "teamlead" and not is_dev:
                sids.append(sid)
            elif to == "dev" and is_dev:
                sids.append(sid)
        return sids
    return [to]


def _inbox_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"inbox-{sid}.log"


def _seen_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"seen-{sid}"


def _heartbeat_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"heartbeat-{sid}"


def _touch(p: Path) -> None:
    try:
        p.touch(exist_ok=True)
    except OSError:
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def send(cfg: Any, slug: str, from_sid: str, to: str, text: str) -> dict:
    """Append a message line to recipient inboxes.

    Returns ``{"ok": True, "delivered_to": [sid, ...]}``.
    """
    sanitized = _sanitize(text)
    recipients = _resolve_recipients(cfg, slug, to)
    line = f"{_now_iso()}\t[from {from_sid}]\t{sanitized}\n"
    delivered: list[str] = []
    for sid in recipients:
        inbox = _inbox_path(cfg, slug, sid)
        with inbox.open("ab") as f:
            f.write(line.encode("utf-8"))
        delivered.append(sid)
    return {"ok": True, "delivered_to": delivered}


def inbox_read(cfg: Any, slug: str, sid: str) -> dict:
    """Drain inbox lines since the seen-<sid> byte offset."""
    _touch(_heartbeat_path(cfg, slug, sid))
    inbox = _inbox_path(cfg, slug, sid)
    seen = _seen_path(cfg, slug, sid)
    if not inbox.exists():
        # Touch a zero-byte inbox so subsequent waits have something to watch.
        inbox.touch()
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0
    size = inbox.stat().st_size
    messages: list[str] = []
    if size > offset:
        with inbox.open("rb") as f:
            f.seek(offset)
            buf = f.read(size - offset)
        text = buf.decode("utf-8", errors="replace")
        # Split on real newlines, drop the trailing empty from the final \n.
        messages = [m for m in text.split("\n") if m]
        seen.write_text(str(size))
    return {"ok": True, "messages": messages, "count": len(messages)}


def inbox_wait(cfg: Any, slug: str, sid: str, timeout: float) -> dict:
    """Long-poll for inbox growth.

    Returns ``{"ok": True, "ready": bool, "elapsed_sec": float}``. ``ready``
    True means "you have new mail, call ``inbox_read``"; False means the
    timeout expired without new mail.
    """
    timeout = max(0.0, min(float(timeout), float(_MAX_WAIT_TIMEOUT)))
    inbox = _inbox_path(cfg, slug, sid)
    seen = _seen_path(cfg, slug, sid)
    hb = _heartbeat_path(cfg, slug, sid)
    if not inbox.exists():
        inbox.touch()
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0

    start = time.monotonic()
    deadline = start + timeout
    last_hb = 0.0
    # Plain stat() poll (1s) — keeps the implementation portable. Linux
    # inotify would only buy us sub-second wake latency, and the message
    # bus's latency budget is "human-perceptible turn", not sub-second.
    poll_interval = 1.0
    while True:
        now = time.monotonic()
        if now - last_hb >= _HEARTBEAT_INTERVAL:
            _touch(hb)
            last_hb = now
        try:
            size = inbox.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > offset:
            return {"ok": True, "ready": True, "elapsed_sec": now - start}
        if now >= deadline:
            return {"ok": True, "ready": False, "elapsed_sec": now - start}
        sleep_for = min(poll_interval, deadline - now)
        if sleep_for > 0:
            time.sleep(sleep_for)
