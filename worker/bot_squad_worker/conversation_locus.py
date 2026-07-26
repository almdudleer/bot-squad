"""Last-seen (chat_id, message_thread_id) an inbound TG message from a
(project, user) pair arrived on — the conversation LOCUS (T-0667).

T-0639 fixed INCOMING routing (a message in a bound forum topic routes to the
right project). OUTGOING was still resolving the reply target from the
project's STATIC tg_chat/tg_topic_id (or the user's DM), so a stakeholder
writing in a freshly-bound topic group got replies back in his old DM — a
split conversation, breaking the "one coherent dialogue" bar (D-0055
Addendum 3). This store closes that gap.

Recorded here, worker-side, in-process, the instant an inbound message is
routed to a project's user-session (``tg_listener._handle_topic_bound`` /
``_handle_unquoted``) — no round trip needed, both run in this same process.
Read by:
  - the worker itself (``_action_tg_notify``'s slug-based resolution, e.g.
    ``bsq tg ping`` / stall escalations) — direct, in-process;
  - the API (``routes_conversations._resolve_relay_target``, the session-
    writeback relay) — a lightweight direct read of this SAME on-disk file
    (worker and API share the data dir; mirrors the pattern
    ``routes_autoupdate.py`` already uses for the worker's autoupdate state).

Worker-owned, single-writer JSON in the data dir, atomic write — mirrors
``tg_bindings.py``'s pattern.

Shape on disk (``data/_worker/conversation_locus.json``)::

    { "<slug>:<global_user_id>": {"chat_id": "...", "thread_id": 7|null, "at": "..."} }

T-0693 Finding B: ``thread_id: null`` is overloaded — it means "a genuine DM
or non-topic message" (the overwhelming case) OR "an explicit General-feed
``tg_bindings`` entry" (``thread_id=None`` bound ON PURPOSE, D-0055 §3), and
both key to the SAME ``slug:gid`` entry. The optional ``general_feed`` flag
(set only by ``tg_listener._handle_topic_bound``, which is only ever reached
via a RESOLVED binding) makes the two cases distinguishable on read instead of
silently indistinguishable; see ``tg_listener._warn_if_general_feed_collides``
for the loud-not-silent guard on the specific risk this was written for: a
General-feed binding and one or more per-topic bindings coexisting on the
SAME project slug (not exercised in production today).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def locus_path(cfg: Any) -> Path:
    """Where the global (slug,gid)->locus map is persisted."""
    return Path(cfg.data_dir) / "_worker" / "conversation_locus.json"


def _key(slug: str, global_user_id: str, thread_id: Any = None) -> str:
    """T-0676 items 3/6: a bare ``(slug, global_user_id)`` key collapses EVERY
    bound topic of a project onto one locus entry, so the last-written topic
    always wins for outbound relay (the misrouted-reply half of item 3) and a
    per-topic attendant can't tell which topic it's actually resolving for
    (item 6). ``thread_id`` is optional and additive: ``None`` (every DM /
    unbound-group message, and every locus recorded before this change)
    resolves to the EXACT pre-existing key, so nothing already on disk is
    affected. A given ``thread_id`` gets its own isolated key."""
    if thread_id is None or thread_id == "":
        return f"{slug}:{global_user_id}"
    return f"{slug}:{global_user_id}:{thread_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(cfg: Any) -> dict[str, dict]:
    """Return the persisted key->{chat_id, thread_id, at} map, or {} when
    none/unreadable. Entries missing a ``chat_id`` are dropped as corrupt."""
    p = locus_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("conversation_locus: unreadable map at %s — treating as empty", p)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not isinstance(v, dict) or not v.get("chat_id"):
            continue
        entry = {
            "chat_id": v["chat_id"],
            "thread_id": v.get("thread_id"),
            "at": v.get("at"),
        }
        if v.get("general_feed"):
            entry["general_feed"] = True
        out[str(k)] = entry
    return out


def _save(cfg: Any, mapping: dict[str, dict]) -> None:
    """Atomically persist the locus map."""
    p = locus_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2))
    os.replace(tmp, p)


def set_locus(
    cfg: Any, slug: str, global_user_id: str, chat_id: Any, thread_id: Any,
    *, general_feed: bool = False,
) -> dict:
    """Record ``(chat_id, thread_id)`` as the last-seen locus for
    ``(slug, global_user_id[, thread_id])``. Idempotent — always overwrites
    with the latest inbound message's origin for that key. Returns the stored
    record.

    T-0676 items 3/6: keyed (also) on ``thread_id`` when present, so a bound
    topic's locus is isolated from every other topic of the same project —
    see :func:`_key`. The stored record still names its own ``thread_id`` (as
    before), so a caller resolving without a thread (e.g. ``latest_for_slug``)
    can still see which topic a match came from.

    ``general_feed`` (T-0693 Finding B): marks a record written via an
    EXPLICIT ``tg_bindings`` General-feed binding (``thread_id=None`` bound
    on purpose, D-0055) as distinct from an ordinary DM/no-thread record
    (``thread_id`` is ``None`` in BOTH cases — the key alone can't tell them
    apart, see module docstring). Additive: omitted from the stored record
    when ``False`` (every pre-T-0693 caller), so an existing record's shape
    is unaffected."""
    mapping = load(cfg)
    rec = {"chat_id": str(chat_id), "thread_id": thread_id, "at": _now_iso()}
    if general_feed:
        rec["general_feed"] = True
    mapping[_key(slug, global_user_id, thread_id)] = rec
    _save(cfg, mapping)
    return rec


def get_locus(
    cfg: Any, slug: str, global_user_id: str, thread_id: Any = None,
) -> Optional[dict]:
    """The last-seen ``{chat_id, thread_id, at}`` for
    ``(slug, global_user_id[, thread_id])``, or ``None`` when this exact key
    has never been recorded."""
    return load(cfg).get(_key(slug, global_user_id, thread_id))


def latest_for_slug(cfg: Any, slug: str) -> Optional[dict]:
    """Most-recently-recorded locus for ANY user under ``slug`` — used by
    ``tg_notify``'s slug-only resolution path (e.g. ``bsq tg ping``, stall
    escalations), which has no ``global_user_id`` to key on. bot-squad's
    single-operator-per-project model makes "most recent" a reasonable proxy
    for "the" locus even without an explicit gid.

    The returned record also carries ``gid`` (extracted from the key) — used
    by T-0660's FYI-append mechanic, which needs to know WHICH (slug, gid)
    attendant thread to record into, not just the chat/topic to send to.

    T-0676 items 3/6: a key may now be ``slug:gid`` OR ``slug:gid:thread_id``
    (see :func:`_key`) — ``gid`` is always the FIRST segment after the
    ``slug:`` prefix (a ``global_user_id`` never itself contains ``:``), so
    splitting once on ``:`` extracts it correctly either way.
    """
    prefix = f"{slug}:"
    candidates = [
        {**v, "gid": k[len(prefix):].split(":", 1)[0]}
        for k, v in load(cfg).items() if k.startswith(prefix)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("at") or "")
