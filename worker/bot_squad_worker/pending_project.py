"""Messages parked waiting for the user to say WHICH project they were about
(T-0640, gateway routing Slice 2 — D-0055 §2 step 3's ask-when-ambiguous
fallback).

Why this store has to exist at all. Before Slice 2, "which project?" was a
question about STATE: the bot asked, the user tapped a `/project <slug>`
button, that PINNED the project, and every *subsequent* message rode the pin.
The message that triggered the question was never routed anywhere — it stayed
in the incidental chat's thread and the user re-typed it, or lost it.

Slice 2 retires the pin (stakeholder, D-0055 Addendum 3: "эту механику с
закреплением проекта, давай мы ее уберем"), so the question stops being about
state and becomes a question about THIS message. That only works if the
message survives until the answer arrives — otherwise the fallback the DoD
says to KEEP is a prompt that leads nowhere, which is strictly worse than the
pin it replaced. So: park the message here, replay it under the project the
user names, then drop it.

Worker-owned, single-writer JSON in the data dir, atomic write — same shape
and conventions as ``tg_bindings.py``. Global (mothership-level) rather than
per-project for the same reason the binding map is: the whole point is that
the project is not yet known.

Shape on disk (``data/_worker/pending_project.json``)::

    { "<global_user_id>": [ {"chat_id": "...", "thread_id": null,
                             "asked_at": "<iso8601>", "msg": {...}}, ... ] }

``msg`` is the raw Telegram message dict as it reached the listener, because
the replay re-drives the SAME durable path an ordinary routed message takes
(``append_conversation`` + locus + ``_ensure_user_conversation``) and those
read the raw message, not a projection of it.

Entries are a LIST, not a single slot: a user who dumps three ambiguous
messages before answering asked ONE question about all three, and answering it
must route all three. Bounded by :data:`MAX_PENDING` and :data:`TTL_SECONDS`
so an unanswered question can never grow the file without limit.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Most parked messages kept per user. Oldest are dropped first. They are NOT
#: lost by being dropped — every parked message was already written durably to
#: the incidental chat's conversation thread before it was parked (see
#: ``tg_listener._handle_unquoted``); the park only decides whether it also
#: gets REPLAYED into the answered project.
MAX_PENDING = 20

#: How long a parked message stays replayable. A day-old dump replayed into a
#: project because the user happened to type that project's name today would
#: be worse than not replaying it — the answer has to plausibly be an answer to
#: THIS question.
TTL_SECONDS = 24 * 3600


def pending_path(cfg: Any) -> Path:
    """Where the parked-messages map is persisted."""
    return Path(cfg.data_dir) / "_worker" / "pending_project.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _fresh(entry: dict, now: Optional[datetime] = None) -> bool:
    """An entry inside its TTL. An UNPARSEABLE timestamp counts as stale — a
    replay is a write into a project's thread, so an entry we cannot date is
    one we must not act on."""
    ts = _parse_ts(entry.get("asked_at"))
    if ts is None:
        return False
    return (now or _now()) - ts <= timedelta(seconds=TTL_SECONDS)


def load(cfg: Any) -> dict[str, list[dict]]:
    """Return the persisted global_user_id->[entry] map, or {} when
    none/unreadable. Malformed entries are dropped as corrupt (same
    treat-as-empty posture as ``tg_bindings.load``: an unreadable park store
    must never break inbound routing)."""
    p = pending_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("pending_project: unreadable store at %s — treating as empty", p)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for gid, entries in raw.items():
        if not isinstance(entries, list):
            continue
        kept = [
            {
                "chat_id": str(e.get("chat_id") or ""),
                "thread_id": e.get("thread_id"),
                "asked_at": e.get("asked_at"),
                "msg": e["msg"],
            }
            for e in entries
            if isinstance(e, dict) and isinstance(e.get("msg"), dict)
        ]
        if kept:
            out[str(gid)] = kept
    return out


def _save(cfg: Any, mapping: dict[str, list[dict]]) -> None:
    """Atomically persist the park map."""
    p = pending_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def park(cfg: Any, global_user_id: str, chat_id: Any, thread_id: Any, msg: dict) -> int:
    """Park ``msg`` awaiting a project answer. Returns how many are now parked
    for this user."""
    gid = str(global_user_id or "").strip()
    if not gid or not isinstance(msg, dict):
        return 0
    mapping = load(cfg)
    entries = mapping.get(gid, [])
    entries.append({
        "chat_id": str(chat_id),
        "thread_id": thread_id,
        "asked_at": _now().isoformat().replace("+00:00", "Z"),
        "msg": msg,
    })
    mapping[gid] = entries[-MAX_PENDING:]
    _save(cfg, mapping)
    return len(mapping[gid])


def take(cfg: Any, global_user_id: str) -> list[dict]:
    """Remove and return this user's parked entries, oldest first, dropping any
    past :data:`TTL_SECONDS`.

    Take-and-clear in one call deliberately: the caller is about to replay
    these into a project, and a crash mid-replay must not leave them parked to
    be replayed a SECOND time on the next answer. Losing a replay costs the
    user a re-send of a message that is already durably recorded; a duplicate
    replay writes the same dump into a project's thread twice and wakes its
    attendant twice, with no way for either to tell."""
    gid = str(global_user_id or "").strip()
    if not gid:
        return []
    mapping = load(cfg)
    entries = mapping.pop(gid, [])
    if entries:
        _save(cfg, mapping)
    now = _now()
    return [e for e in entries if _fresh(e, now)]


def has_pending(cfg: Any, global_user_id: str) -> bool:
    """Whether a fresh parked message exists — a read that does NOT consume."""
    gid = str(global_user_id or "").strip()
    if not gid:
        return False
    now = _now()
    return any(_fresh(e, now) for e in load(cfg).get(gid, []))
