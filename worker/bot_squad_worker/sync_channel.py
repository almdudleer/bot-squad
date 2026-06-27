"""Synchronous inter-session channel (T-0498, M6/F6.2).

voice-08 asked for a way for two sessions to *quickly start talking directly
without interruptions*: one session **requests** a sync channel, the other
**acks** when ready, **both enter**, and they then exchange messages live
(delivered to the terminal with session IDs). The channel **closes on exit or
timeout**.

This is a thin coordination layer ON TOP of the verified M6 async substrate
(D-0037). The handshake notices and the in-channel messages are all delivered
through ``intersession.send`` — the same durable per-SID inbox that powers
``bsq inbox check`` / the ``check mail`` nudge. This module adds only:

  1. a small per-channel state file (the handshake state machine), and
  2. close-on-exit / idle-timeout.

It NEVER modifies ``intersession.py`` delivery primitives — it calls
``intersession.send`` as a black box (REUSE, per the T-0498 design).

Lifecycle / state machine::

    request ──> requested ──ack──> acked ──enter×2──> open ──exit/timeout──> closed
                   │                  │                  │
                   └────── timeout (any non-closed state auto-closes) ───────┘

Each handshake/send refreshes the channel's idle deadline, so an *active*
channel stays open and only an *idle* one ages out (matching "without
interruptions").

File layout under ``data/<slug>/_chat/sync/``::

    _counter            single int, the SC-NNN allocator (flock-guarded)
    SC-<n>.json         one channel record

Channel record (JSON)::

    {
      "channel_id": "SC-1",
      "requester": "<sid>",
      "responder": "<sid>",
      "status": "requested|acked|open|closed",
      "entered": ["<sid>", ...],
      "reason": "<free text>",
      "ttl": <seconds>,
      "created_at": <epoch>, "updated_at": <epoch>, "expires_at": <epoch>,
      "closed_reason": "exit|timeout" | null
    }
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any

from bot_squad_worker import intersession as _is

log = logging.getLogger(__name__)

# Default idle TTL for a channel (1h). Each handshake step / send refreshes it,
# so this bounds *inactivity*, not total channel lifetime. Mirrors the
# inbox_wait cap's "human-perceptible turn" latency budget: a channel nobody
# touches for an hour is stale and should free itself.
DEFAULT_TTL = 3600.0

_STATUSES = ("requested", "acked", "open", "closed")


class SyncError(Exception):
    """A sync-channel operation was invalid (unknown channel, wrong role, closed)."""


def _now() -> float:
    return time.time()


def _sync_dir(cfg: Any, slug: str) -> Path:
    """Return ``data/<slug>/_chat/sync``, creating it 770 if missing."""
    d = Path(cfg.data_dir) / slug / "_chat" / "sync"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(stat.S_IRWXU | stat.S_IRWXG)  # 770
        except OSError:
            pass
    return d


def _channel_path(cfg: Any, slug: str, channel_id: str) -> Path:
    return _sync_dir(cfg, slug) / f"{channel_id}.json"


def _alloc_cid(cfg: Any, slug: str) -> str:
    """Allocate the next ``SC-<n>`` id under an flock on the counter file."""
    counter = _sync_dir(cfg, slug) / "_counter"
    fd = os.open(counter, os.O_RDWR | os.O_CREAT, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode("utf-8", errors="replace").strip()
        try:
            n = int(raw)
        except ValueError:
            n = 0
        n += 1
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(n).encode("utf-8"))
        return f"SC-{n}"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _store(path: Path, ch: dict) -> None:
    """Atomically write a channel record (tmp + os.replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ch, separators=(",", ":")))
    os.replace(tmp, path)


def _load(cfg: Any, slug: str, channel_id: str) -> dict:
    path = _channel_path(cfg, slug, channel_id)
    if not path.exists():
        raise SyncError(f"unknown channel: {channel_id}")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SyncError(f"corrupt channel record {channel_id}: {e}") from e


def _apply_timeout(ch: dict, now: float) -> dict:
    """Close an idle channel whose deadline has passed (lazy, on every touch)."""
    if ch.get("status") != "closed" and now > float(ch.get("expires_at", 0.0)):
        ch["status"] = "closed"
        ch["closed_reason"] = "timeout"
        ch["updated_at"] = now
    return ch


def _members(ch: dict) -> set[str]:
    return {ch["requester"], ch["responder"]}


def _peer(ch: dict, sid: str) -> str:
    return ch["responder"] if sid == ch["requester"] else ch["requester"]


def _touch(ch: dict, now: float) -> None:
    """Mark activity: refresh the idle deadline + updated_at."""
    ch["updated_at"] = now
    ch["expires_at"] = now + float(ch.get("ttl", DEFAULT_TTL))


def _notify(cfg: Any, slug: str, from_sid: str, to_sid: str, text: str) -> dict:
    """Durably deliver a line to ``to_sid`` via the async substrate; return the
    notify handle the CLI uses to also nudge the live pane."""
    _is.send(cfg, slug, from_sid, to_sid, text)
    return {"sid": to_sid, "text": text}


def _public(ch: dict) -> dict:
    """The subset of a channel record returned to callers."""
    return {
        "channel_id": ch["channel_id"],
        "status": ch["status"],
        "requester": ch["requester"],
        "responder": ch["responder"],
        "entered": list(ch.get("entered", [])),
        "closed_reason": ch.get("closed_reason"),
        "expires_at": ch.get("expires_at"),
    }


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------
def request(
    cfg: Any,
    slug: str,
    from_sid: str,
    to_sid: str,
    reason: str = "",
    ttl: float = DEFAULT_TTL,
    now: float | None = None,
) -> dict:
    """``from_sid`` requests a sync channel with ``to_sid``.

    Creates a ``requested`` channel and durably notifies the responder to ack.
    Returns ``{ok, channel_id, status, requester, responder, notify}``.
    """
    if from_sid == to_sid:
        raise SyncError("cannot open a sync channel with yourself")
    now = _now() if now is None else now
    cid = _alloc_cid(cfg, slug)
    ch = {
        "channel_id": cid,
        "requester": from_sid,
        "responder": to_sid,
        "status": "requested",
        "entered": [],
        "reason": reason or "",
        "ttl": float(ttl),
        "created_at": now,
        "updated_at": now,
        "expires_at": now + float(ttl),
        "closed_reason": None,
    }
    _store(_channel_path(cfg, slug, cid), ch)
    text = (
        f"[sync {cid}] {from_sid} requests a synchronous channel"
        + (f" — {reason}" if reason else "")
        + f". Run: bsq sync ack {cid}"
    )
    notify = _notify(cfg, slug, from_sid, to_sid, text)
    out = _public(ch)
    out["ok"] = True
    out["notify"] = notify
    return out


def ack(cfg: Any, slug: str, sid: str, channel_id: str, now: float | None = None) -> dict:
    """The responder acks a request when ready. Moves ``requested -> acked`` and
    notifies the requester that both should now enter."""
    now = _now() if now is None else now
    ch = _apply_timeout(_load(cfg, slug, channel_id), now)
    if ch["status"] == "closed":
        _store(_channel_path(cfg, slug, channel_id), ch)
        raise SyncError(f"channel {channel_id} is closed ({ch.get('closed_reason')})")
    if sid != ch["responder"]:
        raise SyncError(f"only the responder ({ch['responder']}) can ack {channel_id}")
    if ch["status"] != "requested":
        raise SyncError(f"channel {channel_id} already {ch['status']}, cannot ack")
    ch["status"] = "acked"
    _touch(ch, now)
    _store(_channel_path(cfg, slug, channel_id), ch)
    text = (
        f"[sync {channel_id}] {sid} acked — both sessions enter now. "
        f"Run: bsq sync enter {channel_id}"
    )
    notify = _notify(cfg, slug, sid, ch["requester"], text)
    out = _public(ch)
    out["ok"] = True
    out["notify"] = notify
    return out


def enter(cfg: Any, slug: str, sid: str, channel_id: str, now: float | None = None) -> dict:
    """A member enters an acked channel. When both members have entered the
    channel opens (``status == 'open'``)."""
    now = _now() if now is None else now
    ch = _apply_timeout(_load(cfg, slug, channel_id), now)
    if ch["status"] == "closed":
        _store(_channel_path(cfg, slug, channel_id), ch)
        raise SyncError(f"channel {channel_id} is closed ({ch.get('closed_reason')})")
    if sid not in _members(ch):
        raise SyncError(f"{sid} is not a member of {channel_id}")
    if ch["status"] not in ("acked", "open"):
        raise SyncError(f"channel {channel_id} not acked yet (status {ch['status']})")
    entered = set(ch.get("entered", []))
    entered.add(sid)
    ch["entered"] = [s for s in (ch["requester"], ch["responder"]) if s in entered]
    both_in = _members(ch) <= entered
    if both_in:
        ch["status"] = "open"
    _touch(ch, now)
    _store(_channel_path(cfg, slug, channel_id), ch)
    peer = _peer(ch, sid)
    notify = None
    if both_in:
        # Tell the peer the channel is now live.
        text = f"[sync {channel_id}] channel open with {sid} — send: bsq sync send {channel_id} <text>"
        notify = _notify(cfg, slug, sid, peer, text)
    out = _public(ch)
    out["ok"] = True
    out["both_in"] = both_in
    out["peer"] = peer
    out["notify"] = notify
    return out


def send(
    cfg: Any,
    slug: str,
    sid: str,
    channel_id: str,
    text: str,
    now: float | None = None,
) -> dict:
    """Deliver a live message to the other member of an open channel.

    Reuses ``intersession.send`` (durable inbox, prefixed ``[from <sid>]``) so
    the message rides the normal bus; the CLI additionally injects it into the
    peer's terminal for the "talking directly" experience. Returns
    ``{ok, peer, line, notify}``.
    """
    now = _now() if now is None else now
    ch = _apply_timeout(_load(cfg, slug, channel_id), now)
    if ch["status"] == "closed":
        _store(_channel_path(cfg, slug, channel_id), ch)
        reason = ch.get("closed_reason") or "closed"
        if reason == "timeout":
            raise SyncError(f"channel {channel_id} timed out")
        raise SyncError(f"channel {channel_id} is closed ({reason})")
    if sid not in _members(ch):
        raise SyncError(f"{sid} is not a member of {channel_id}")
    if ch["status"] != "open":
        raise SyncError(f"channel {channel_id} not open (status {ch['status']})")
    peer = _peer(ch, sid)
    body = f"[sync {channel_id}] {text}"
    _is.send(cfg, slug, sid, peer, body)
    _touch(ch, now)
    _store(_channel_path(cfg, slug, channel_id), ch)
    # notify.text for the live pane carries the sender SID (the inbox copy
    # already has the [from <sid>] prefix; a raw pane injection does not).
    notify = {"sid": peer, "text": f"[sync {channel_id} from {sid}] {text}"}
    out = _public(ch)
    out["ok"] = True
    out["peer"] = peer
    out["line"] = body
    out["notify"] = notify
    return out


def exit_channel(
    cfg: Any,
    slug: str,
    sid: str,
    channel_id: str,
    reason: str = "exit",
    now: float | None = None,
) -> dict:
    """A member leaves: the channel closes and the peer is notified.

    Idempotent — exiting an already-closed channel is a no-op that returns the
    closed record. ``reason`` defaults to ``exit`` (vs the lazy ``timeout``).
    """
    now = _now() if now is None else now
    ch = _apply_timeout(_load(cfg, slug, channel_id), now)
    if sid not in _members(ch):
        raise SyncError(f"{sid} is not a member of {channel_id}")
    notify = None
    if ch["status"] != "closed":
        ch["status"] = "closed"
        ch["closed_reason"] = reason or "exit"
        ch["updated_at"] = now
        _store(_channel_path(cfg, slug, channel_id), ch)
        peer = _peer(ch, sid)
        text = f"[sync {channel_id}] {sid} left — channel closed ({ch['closed_reason']})."
        notify = _notify(cfg, slug, sid, peer, text)
    out = _public(ch)
    out["ok"] = True
    out["notify"] = notify
    return out


def status(
    cfg: Any,
    slug: str,
    channel_id: str | None = None,
    sid: str | None = None,
    now: float | None = None,
) -> dict:
    """Report one channel (``channel_id``) or every channel a ``sid`` is in.

    Applies the lazy idle-timeout before reporting, so a stale channel reads
    ``closed``/``timeout`` even if no other op has touched it. Persists any
    timeout transition so the state is durable.
    """
    now = _now() if now is None else now
    if channel_id:
        ch = _apply_timeout(_load(cfg, slug, channel_id), now)
        _store(_channel_path(cfg, slug, channel_id), ch)
        out = _public(ch)
        out["ok"] = True
        return out
    # List mode.
    rows: list[dict] = []
    for path in sorted(_sync_dir(cfg, slug).glob("SC-*.json")):
        try:
            ch = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        ch = _apply_timeout(ch, now)
        _store(path, ch)
        if sid and sid not in _members(ch):
            continue
        rows.append(_public(ch))
    return {"ok": True, "channels": rows}
