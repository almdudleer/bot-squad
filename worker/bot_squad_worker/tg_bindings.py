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

    { "<chat_id>:<thread_id>": {"slug": "...", "ticket_id": null,
                                "session_id": null, "pinned_message_id": null} }

T-0660 (per-task topics) generalizes what a binding ROUTES TO: a project topic
binds only ``slug``; a task topic additionally carries ``ticket_id``/
``session_id`` to route to one specific originating session rather than the
project's user-conversation attendant.

T-0771: ``set_binding`` is the ONE writer of a whole record, and every field of
that record is reachable through it — for a while ``ticket_id``/``session_id``
were writable only by ``tg_topic_create``, so any rebind of a per-task topic
silently stripped it back to a bare project binding with no supported way to
put the association back. It is now a read-modify-write that REFUSES an
implicit drop; see :func:`set_binding`.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: The fields of a stored binding record, in report order. ONE definition, so
#: "what a binding is made of" can't drift between the writer, the loss guard
#: and the change report (T-0771).
RECORD_FIELDS = ("slug", "ticket_id", "session_id", "pinned_message_id")

#: The two fields :func:`set_binding` refuses to drop implicitly (T-0771).
#: Both are ROUTING targets — ``ticket_id`` is what ``find_by_ticket`` (hence
#: ``bsq topic say`` and ``_own_topic_binding`` rung 2) resolves on, and
#: ``session_id`` is what ``find_by_session``/the listener's direct-mode branch
#: resolve on. ``pinned_message_id`` is deliberately NOT here: it is a
#: bookkeeping handle for a pin that already exists in Telegram, not a route,
#: and it is PRESERVED rather than guarded (see :func:`set_binding`).
GUARDED_FIELDS = ("ticket_id", "session_id")


class LossyRebindError(ValueError):
    """A rebind would have implicitly dropped a field the stored record
    currently carries (T-0771).

    Raised INSTEAD of writing — the caller has to say which operation it meant:
    name the field to keep it, or ask for it to be cleared. ``fields`` maps
    each at-risk field name to the value that would have been lost, so a caller
    can put the real values in front of a human (there is no second copy: the
    store is a single atomic tmp+``os.replace`` file with no backup and no
    rotation, so a dropped field leaves NO trace to recover from afterwards —
    which is why this refuses rather than warns).
    """

    def __init__(self, key: str, fields: dict[str, Any]) -> None:
        self.key = key
        self.fields = dict(fields)
        named = ", ".join(f"{k}={v!r}" for k, v in self.fields.items())
        super().__init__(
            f"refusing a lossy rebind of {key}: it currently carries {named}, "
            f"which this call does not name and would drop"
        )


def bindings_path(cfg: Any) -> Path:
    """Where the global (chat_id,thread_id)->target map is persisted."""
    return Path(cfg.data_dir) / "_worker" / "tg_bindings.json"


def _key(chat_id: Any, thread_id: Any) -> str:
    """Composite key for the (chat_id, thread_id) pair. ``thread_id=None``
    (a chat's General feed / a non-topic message) keys to an empty segment,
    distinct from any real thread id."""
    return f"{chat_id}:{'' if thread_id is None else thread_id}"


def load(cfg: Any) -> dict[str, dict]:
    """Return the persisted key->{slug, ticket_id, session_id, pinned_message_id}
    map, or {} when none/unreadable. Entries missing a ``slug`` are dropped as
    corrupt.

    T-0677: ``pinned_message_id`` is the message_id of the direct-mode
    confirmation pinned in the topic (see :func:`set_direct_session`) — kept in
    the record so the marker can be UNPINNED when direct mode is switched off
    or re-pointed, instead of leaving a pin that lies about where the topic
    routes. ``None`` for every binding that has never pinned one."""
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
            "pinned_message_id": v.get("pinned_message_id"),
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
    clear_ticket_id: bool = False,
    clear_session_id: bool = False,
) -> dict:
    """Bind ``(chat_id, thread_id)`` -> ``slug`` (+ optional ``ticket_id``/
    ``session_id`` for the T-0660 per-task-topic layer). Idempotent —
    rebinding the same key rewrites that one entry; a project may have
    multiple bound keys simultaneously (stakeholder requirement, D-0055 §3).

    T-0771 — this is a READ-MODIFY-WRITE, not a whole-record replace, and it
    follows :func:`set_direct_session` next door (whose docstring named this
    function's replace-everything behaviour as the hazard, and whose comment
    saying so prevented nothing). What it does with each field:

    * ``slug`` — always written; it is the point of the verb.
    * ``ticket_id``/``session_id`` — written when NAMED; explicitly emptied
      when the matching ``clear_*`` flag is set. When neither is given and the
      stored record CARRIES one, this raises :class:`LossyRebindError` and
      writes NOTHING. It does not merge: a silent merge would leave a caller
      that MEANT to clear ``session_id`` believing it had, while the direct-mode
      routing branch stayed live — invisible from the caller's side, where the
      drop at least used to be visible by reading the record back. Refusing is
      the only outcome that forces the caller to say which one it meant.
    * ``pinned_message_id`` — always PRESERVED (never guarded, never settable
      here). It is the id of the ``/pin-session`` confirmation pinned in the
      topic (T-0677), kept so ``_unpin_previous`` can still take that pin down;
      dropping it strands a pin in Telegram that claims a routing which no
      longer exists, and no caller of this verb has any reason to name it.

    Passing a field AND its ``clear_*`` flag is a contradiction, not a
    precedence question — raises ``ValueError`` rather than picking one.

    Returns the stored record.
    """
    for name, value, clear in (
        ("ticket_id", ticket_id, clear_ticket_id),
        ("session_id", session_id, clear_session_id),
    ):
        if value and clear:
            raise ValueError(
                f"set_binding: {name}={value!r} and clear_{name}=True contradict "
                f"each other — pass one or the other"
            )

    mapping = load(cfg)
    key = _key(chat_id, thread_id)
    prev = mapping.get(key) or {}

    at_risk = {
        name: prev.get(name)
        for name, value, clear in (
            ("ticket_id", ticket_id, clear_ticket_id),
            ("session_id", session_id, clear_session_id),
        )
        if prev.get(name) and not value and not clear
    }
    if at_risk:
        raise LossyRebindError(key, at_risk)

    rec = {
        "slug": slug,
        "ticket_id": ticket_id if ticket_id else (None if clear_ticket_id else prev.get("ticket_id")),
        "session_id": session_id if session_id else (None if clear_session_id else prev.get("session_id")),
        "pinned_message_id": prev.get("pinned_message_id"),
    }
    mapping[key] = rec
    _save(cfg, mapping)
    return rec


def change_summary(before: Optional[dict], after: dict) -> dict[str, dict]:
    """Which fields this write actually moved: ``{field: {"from": x, "to": y}}``
    for every :data:`RECORD_FIELDS` entry whose value differs (T-0771).

    The whole failure this ticket records is that a lossy write looked exactly
    like a faithful one, so the caller reports THIS rather than the record —
    a record alone can't say what it used to be. An empty dict means the write
    changed nothing, which is a true and useful thing to be told.
    """
    before = before or {}
    return {
        f: {"from": before.get(f), "to": after.get(f)}
        for f in RECORD_FIELDS
        if before.get(f) != after.get(f)
    }


def set_direct_session(
    cfg: Any,
    chat_id: Any,
    thread_id: Any,
    session_id: Optional[str],
    *,
    pinned_message_id: Optional[int] = None,
) -> Optional[dict]:
    """T-0677: point an EXISTING binding's direct-mode target at ``session_id``
    (or back at the project's user-conversation attendant when ``None``).

    This is the whole of "direct mode" — the routing it toggles already exists
    (``tg_listener._handle_topic_bound``: a binding carrying a ``session_id``
    injects straight into that session; ``None`` goes through the attendant,
    which stays the DEFAULT). Nothing here writes a new route; it flips the one
    field that branch already reads.

    Deliberately does NOT create a binding: an unbound topic has no project, so
    there is no candidate-session set to pin from and no slug to invent — the
    caller reports that rather than guessing (the T-0693 lesson). Returns the
    updated record, or ``None`` when ``(chat_id, thread_id)`` is unbound.

    ``slug``/``ticket_id`` are preserved (unlike :func:`set_binding`, which
    replaces the whole record). ``pinned_message_id`` records the confirmation
    message pinned in the topic so it can later be unpinned; pass ``None`` to
    forget it.
    """
    mapping = load(cfg)
    key = _key(chat_id, thread_id)
    rec = mapping.get(key)
    if rec is None:
        return None
    rec["session_id"] = session_id
    rec["pinned_message_id"] = pinned_message_id
    mapping[key] = rec
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

    Returns the stored record (``{slug, ticket_id, session_id,
    pinned_message_id}``). Callers that just need the project (T-0639 Slice 1)
    read ``["slug"]``, which is the only field a plain project topic carries.
    A T-0660 per-task topic also carries ``ticket_id``/``session_id``, which
    the listener reads to target one specific session instead of the project's
    user-conversation attendant.
    """
    return load(cfg).get(_key(chat_id, thread_id))


def bound_chat_ids(cfg: Any) -> set[str]:
    """Every chat_id with at least one binding — folded into the listener's
    allowlist (T-0639) so a bound supergroup is admitted even when it isn't
    any project's static ``tg_chat``."""
    return {key.split(":", 1)[0] for key in load(cfg)}


def bound_topic_slugs(cfg: Any, chat_id: Any) -> set[str]:
    """Distinct slugs bound to REAL forum topics (``thread_id is not None``)
    of ``chat_id`` — deliberately excludes any ``(chat_id, None)`` General-feed
    entry, which isn't a topic.

    T-0700: lets the listener tell a genuinely multi-project shared forum
    (bound topics pointing at MORE THAN ONE distinct slug — the live
    incident's topology) apart from a single-project chat that merely also
    has one or more topics bound to its own project. Used to gate the
    General-feed (``thread_id=None``) hold-and-warn onto only the former, so a
    single-project chat's General tab keeps working with zero added friction.
    """
    chat_id = str(chat_id)
    slugs: set[str] = set()
    for key, rec in load(cfg).items():
        bound_chat, _, thread_part = key.partition(":")
        if bound_chat == chat_id and thread_part:
            slugs.add(rec["slug"])
    return slugs


def find_by_ticket(cfg: Any, ticket_id: str) -> Optional[dict]:
    """Reverse lookup: the topic bound to ``ticket_id`` (T-0660 direct-write,
    ``bsq topic say``/``tg_notify``'s ``ticket_id`` param) — a dev/TL/
    orchestrator session posting into its task's topic knows the ticket id,
    not the raw ``(chat_id, thread_id)``.

    Returns ``{chat_id, thread_id, slug, ticket_id, session_id}`` for the first
    binding whose ``ticket_id`` matches, or ``None`` when no topic is bound to
    it (e.g. the task is being discussed in the project's General instead —
    T-0660 Addendum 2: a dedicated topic is opt-in, not automatic)."""
    return _find(cfg, lambda rec: rec.get("ticket_id") == ticket_id)


def find_by_session(cfg: Any, session_id: str) -> Optional[dict]:
    """Reverse lookup: the topic whose binding NAMES ``session_id`` (T-0723) —
    the mirror of :func:`find_by_ticket` for a sender that knows its own sid
    but not the ticket the topic was bound to.

    A binding carries a ``session_id`` in exactly two cases, and both mean
    "this topic belongs to that session": a T-0677 direct-mode/pinned topic
    (the stakeholder pointed the topic at one session) and a T-0660 Phase-2
    per-task topic created with its originating session. That makes this the
    lookup ``tg_notify`` needs to route a session's slug-only send into its OWN
    topic instead of the project-wide conversation locus (T-0723 — the locus is
    "wherever the human last wrote", which is not an identity).

    A REAL forum topic wins over a ``(chat_id, None)`` General-feed binding
    pointed at the same session: General is not a topic of one's own, so a
    sender that only has that keeps the locus behaviour (see the caller,
    ``actions._own_topic_binding``). Returns the same record shape as
    :func:`find_by_ticket`, or ``None`` when no binding names the session
    (``session_id`` empty/None never matches — an unnamed binding is not
    everyone's).
    """
    if not session_id:
        return None
    return _find(cfg, lambda rec: rec.get("session_id") == session_id,
                 prefer_topic=True)


def _find(cfg: Any, pred: Any, *, prefer_topic: bool = False) -> Optional[dict]:
    """Shared reverse-lookup body for the ``find_by_*`` helpers: the first
    binding satisfying ``pred``, decoded into
    ``{chat_id, thread_id, slug, ticket_id, session_id}``.

    ``prefer_topic`` keeps scanning past a General-feed (``thread_id is None``)
    match for a real forum topic, and only falls back to the General one when
    no topic matched.
    """
    fallback: Optional[dict] = None
    for key, rec in load(cfg).items():
        if not pred(rec):
            continue
        chat_id, _, thread_part = key.partition(":")
        thread_id = int(thread_part) if thread_part else None
        out = {
            "chat_id": chat_id, "thread_id": thread_id, "slug": rec["slug"],
            "ticket_id": rec.get("ticket_id"), "session_id": rec.get("session_id"),
        }
        if not prefer_topic or thread_id is not None:
            return out
        if fallback is None:
            fallback = out
    return fallback
