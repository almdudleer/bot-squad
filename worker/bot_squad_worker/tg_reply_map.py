"""T-0719: sent-TG-message-id -> originating SID, so reply routing never
depends on how a message is DISPLAYED.

Why this exists. Every session that pages the stakeholder went out through
``tg.TgClient.send(sid=<display label>)``, and the inbound side recovered the
target by re-parsing that label out of the quoted text with
``tg_listener.SID_RE``. Routing was therefore coupled to presentation: T-0676
item 5 made the label compact (``"bot-squad operator"`` — no raw SID in it at
all), and every reply silently stopped reaching its session and fell through to
the user-conversation attendant instead. Nothing errored; the capability just
went quiet.

Telegram hands us ``result.message_id`` on every send and echoes
``reply_to_message.message_id`` on every reply, so the (chat, message) pair is
a presentation-independent join key. Record it on the way out, look it up on
the way in, and no future label change can sever routing again. The SID_RE path
stays as a FALLBACK — messages sent before this map existed (and the bracket
form ``[<slug>] S-...-pNNN``, still used by non-compact callers) must keep
resolving.

Shape on disk (``data/_worker/tg_reply_map.json``)::

    { "<chat_id>:<message_id>": {"sid": "<S-...-pNNN>", "ts": <epoch_seconds>} }

GLOBAL, not per-project — a ``(chat_id, message_id)`` pair is unique across
Telegram and a raw SID isn't project-scoped either (see
``session_aliases`` for the same reasoning). Worker-owned, locked
read-modify-write + atomic replace (``mdlock``), mirroring ``tg_bindings`` /
``session_aliases``.

Eviction: entries older than :data:`TTL_SECONDS` are dropped on every write,
and the map is capped at :data:`MAX_ENTRIES` (oldest-first). The TTL is
generous on purpose — the stakeholder scrolling back a couple of weeks to
answer an old page is exactly the case this must not lose.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MAP_PATH_RELPATH = ("_worker", "tg_reply_map.json")

# 30 days. A reply to a page older than this falls back to SID_RE (bracket-form
# senders) or, failing that, to the normal unquoted/attendant path.
TTL_SECONDS = 30 * 24 * 3600
MAX_ENTRIES = 5000

# A REAL routing SID, i.e. something ``inject_input`` can target. Deliberately
# stricter than a "sender name": synthetic senders like ``deploy_monitor``
# (jobs.deploy_monitor) are not sessions and must NOT be recorded — a reply to
# a deploy notification belongs on the attendant path, exactly as before.
_ROUTING_SID_RE = re.compile(r"^S-[A-Za-z0-9_-]+?-p\d+$")


def map_path(data_dir: Any) -> Path:
    """Where the (chat, message) -> sid map is persisted."""
    return Path(data_dir) / Path(*_MAP_PATH_RELPATH)


def is_routing_sid(sid: str) -> bool:
    """True when ``sid`` is a real ``compute_sid`` routing key (and so worth
    recording). Display labels and synthetic sender names are not."""
    return bool(_ROUTING_SID_RE.match(str(sid or "").strip()))


def _key(chat_id: Any, message_id: Any) -> str:
    return f"{str(chat_id).strip()}:{str(message_id).strip()}"


def load(data_dir: Any) -> dict[str, dict]:
    """Return the persisted map, or ``{}`` when missing/corrupt.

    Degrades to empty rather than raising — a corrupt map must cost us the
    message-id path (falling back to SID_RE), never the whole inbound route."""
    try:
        raw = json.loads(map_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if isinstance(v, dict) and v.get("sid"):
            out[str(k)] = {"sid": str(v["sid"]), "ts": float(v.get("ts") or 0)}
    return out


def _prune(mapping: dict[str, dict], *, now: float) -> dict[str, dict]:
    """Drop expired entries, then cap to :data:`MAX_ENTRIES` oldest-first."""
    fresh = {k: v for k, v in mapping.items() if now - v.get("ts", 0) < TTL_SECONDS}
    if len(fresh) <= MAX_ENTRIES:
        return fresh
    keep = sorted(fresh.items(), key=lambda kv: kv[1].get("ts", 0))[-MAX_ENTRIES:]
    return dict(keep)


def record(data_dir: Any, *, chat_id: Any, message_id: Any, sid: str) -> bool:
    """Remember that the TG message ``(chat_id, message_id)`` came FROM ``sid``.

    No-op (returns False) when ``sid`` isn't a real routing SID or the ids are
    missing — a non-session sender has nothing to route a reply back to.
    Best-effort: a store failure is logged and swallowed, because losing the
    reply-map entry must never fail the send itself."""
    if not is_routing_sid(sid) or chat_id in (None, "") or message_id in (None, ""):
        return False
    from bot_squad_worker import mdlock

    path = map_path(data_dir)
    try:
        with mdlock.task_lock(path):
            mapping = load(data_dir)
            mapping[_key(chat_id, message_id)] = {
                "sid": str(sid).strip(), "ts": time.time(),
            }
            mapping = _prune(mapping, now=time.time())
            mdlock.atomic_write(path, json.dumps(mapping, indent=2))
        return True
    except OSError:
        log.exception("tg_reply_map.record failed (non-fatal) for sid=%s", sid)
        return False


def lookup(data_dir: Any, *, chat_id: Any, message_id: Any) -> str | None:
    """``(chat_id, message_id)`` -> the SID that sent it, or ``None``.

    ``None`` covers unknown, expired, and corrupt-store alike; every caller
    must have a fallback path for it."""
    if chat_id in (None, "") or message_id in (None, ""):
        return None
    entry = load(data_dir).get(_key(chat_id, message_id))
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) >= TTL_SECONDS:
        return None
    return entry.get("sid") or None
