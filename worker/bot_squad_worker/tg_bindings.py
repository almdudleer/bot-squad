"""Runtime (chat_id, message_thread_id) -> project/session routing bindings
(T-0639, gateway topic-supergroup routing — D-0055 §2-3).

Today chat->project is static and 1:1 (``project.tg_chat`` in projects.toml,
resolved by ``tg_listener._slug_for_chat``). The stakeholder's ask is a forum
supergroup with MANY topics, each topic bound to a project — and that binding
must be a runtime, configurable map, NOT baked into code (D-0055 §3). A project
may have MULTIPLE bound ``(chat_id, message_thread_id)`` entries.

Worker-owned, single-writer JSON in the data dir, atomic write — mirrors
``tg_topics.py``'s per-project class->thread-id map (docstring/style/pattern),
but this store is GLOBAL rather than per-project (a binding maps INTO a
project, so — like ``pins_store``'s ``current_project.json`` at the mothership
level, for the same reason — it can't live under a single project's own dir).

Shape on disk (``data/_worker/tg_bindings.json``)::

    { "<chat_id>:<thread_id>": {"slug": "...", "ticket_id": null, "session_id": null} }

T-0660 (per-task topics, already scoped) generalizes what a binding ROUTES TO:
a project topic binds only ``slug`` (this MVP); a task topic additionally
carries ``ticket_id``/``session_id`` to route to one specific originating
session rather than the project's user-conversation attendant. The record
shape and ``resolve()`` return type already carry both fields so that layer
bolts on with no rewrite — this slice only ever writes/reads ``slug``.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def bindings_path(cfg: Any) -> Path:
    """Where the global (chat_id,thread_id)->target map is persisted."""
    return Path(cfg.data_dir) / "_worker" / "tg_bindings.json"


def _key(chat_id: Any, thread_id: Any) -> str:
    """Composite key for the (chat_id, thread_id) pair. ``thread_id=None``
    (a chat's General feed / a non-topic message) keys to an empty segment,
    distinct from any real thread id."""
    return f"{chat_id}:{'' if thread_id is None else thread_id}"


def load(cfg: Any) -> dict[str, dict]:
    """Return the persisted key->{slug, ticket_id, session_id} map, or {} when
    none/unreadable. Entries missing a ``slug`` are dropped as corrupt."""
    p = bindings_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("tg_bindings: unreadable map at %s — treating as empty", p)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not isinstance(v, dict) or not v.get("slug"):
            continue
        out[str(k)] = {
            "slug": v["slug"],
            "ticket_id": v.get("ticket_id"),
            "session_id": v.get("session_id"),
        }
    return out


def _save(cfg: Any, mapping: dict[str, dict]) -> None:
    """Atomically persist the binding map."""
    p = bindings_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2))
    os.replace(tmp, p)


def set_binding(
    cfg: Any,
    chat_id: Any,
    thread_id: Any,
    slug: str,
    *,
    ticket_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Bind ``(chat_id, thread_id)`` -> ``slug`` (+ optional ``ticket_id``/
    ``session_id`` for the T-0660 per-task-topic layer). Idempotent —
    rebinding the same key REPLACES that one entry; a project may have
    multiple bound keys simultaneously (stakeholder requirement, D-0055 §3).

    Returns the stored record.
    """
    mapping = load(cfg)
    rec = {"slug": slug, "ticket_id": ticket_id, "session_id": session_id}
    mapping[_key(chat_id, thread_id)] = rec
    _save(cfg, mapping)
    return rec


def clear_binding(cfg: Any, chat_id: Any, thread_id: Any) -> bool:
    """Remove the ``(chat_id, thread_id)`` binding. Returns True if one
    existed (idempotent)."""
    mapping = load(cfg)
    key = _key(chat_id, thread_id)
    if key not in mapping:
        return False
    del mapping[key]
    _save(cfg, mapping)
    return True


def resolve(cfg: Any, chat_id: Any, thread_id: Any) -> Optional[dict]:
    """The routing target bound to ``(chat_id, thread_id)``, or ``None`` when
    unbound.

    Returns ``{slug, ticket_id, session_id}``. For this MVP (T-0639) only
    ``slug`` is ever populated — callers that just need the project (Slice 1)
    read ``["slug"]``. ``ticket_id``/``session_id`` stay ``None`` until the
    T-0660 per-task-topic layer starts writing them; its resolver reads those
    to target one specific session instead of the project's user-conversation
    attendant.
    """
    return load(cfg).get(_key(chat_id, thread_id))


def bound_chat_ids(cfg: Any) -> set[str]:
    """Every chat_id with at least one binding — folded into the listener's
    allowlist (T-0639) so a bound supergroup is admitted even when it isn't
    any project's static ``tg_chat``."""
    return {key.split(":", 1)[0] for key in load(cfg)}


def find_by_ticket(cfg: Any, ticket_id: str) -> Optional[dict]:
    """Reverse lookup: the topic bound to ``ticket_id`` (T-0660 direct-write,
    ``bsq topic say``/``tg_notify``'s ``ticket_id`` param) — a dev/TL/
    orchestrator session posting into its task's topic knows the ticket id,
    not the raw ``(chat_id, thread_id)``.

    Returns ``{chat_id, thread_id, slug, session_id}`` for the first binding
    whose ``ticket_id`` matches, or ``None`` when no topic is bound to it
    (e.g. the task is being discussed in the project's General instead —
    T-0660 Addendum 2: a dedicated topic is opt-in, not automatic)."""
    for key, rec in load(cfg).items():
        if rec.get("ticket_id") == ticket_id:
            chat_id, _, thread_part = key.partition(":")
            thread_id = int(thread_part) if thread_part else None
            return {
                "chat_id": chat_id, "thread_id": thread_id,
                "slug": rec["slug"], "session_id": rec.get("session_id"),
            }
    return None
