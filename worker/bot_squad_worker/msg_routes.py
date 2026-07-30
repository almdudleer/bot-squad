"""Per-message-TYPE outbound destination map (T-0799).

Stakeholder, 2026-07-30, verbatim: «Не хардкодь что это именно ЛС, топики под
логи и т.п., пускай будет конфигурируемые chat_id все типы сообщений, но по
дефолту всё ЛС» — every automated message type gets a configurable ``chat_id``,
and the DEFAULT reproduces today's behaviour (everything to his bot DM) so the
migration is a no-op by construction. He is putting a siren on the DM, so the
split he is buying is by URGENCY — "does this need him NOW" — not by which
subsystem emitted the message.

WHAT THIS MODULE IS
-------------------
A lookup that turns ``(slug, msg_type)`` into the ``(chat_id, topic_id)`` an
automated send should use, **defaulting to whatever the caller already
computed**. With no configured route, :func:`route` hands back the caller's own
destination unchanged — that is the no-op default, and it is why shipping this
changes nothing until he moves a type.

It is deliberately NOT a second copy of the destination logic. Each emitter
still computes today's destination (``project.tg_chat`` +
``tg_topics.resolve(...)``) and passes it in as the default; this only gets to
REPLACE that pair, never to invent one.

WHAT IT IS NOT
--------------
* **Not a quiet-hours decision.** ``urgent=`` is the quiet-hours bypass flag and
  nothing else (``tg.TgClient.send``: ``if not urgent and _in_quiet_hours(...)``
  → drop). Destination and quiet-hours are two independent axes, and nothing
  here reads or writes ``urgent``. That is what keeps T-0188 intact through the
  reclassification below: ``deploy_status`` is LOG-class, and deploy notices
  keep ``urgent=True``, so a 03:00 deploy still FIRES — into whichever
  destination is configured. T-0188 was about the gate silently DROPPING deploy
  alerts; classifying one as routine does not re-introduce a drop.
* **Not an override of an explicitly-addressed send.** ``bsq tg ping --chat X``,
  ``bsq topic say``, the T-0569 relay and every reply-in-place (voice-note ACK,
  inbound command replies) spell their destination out and pass NO ``msg_type``.
  A map that redirected those would break the reply rather than tidy it.
* **Not the topic-creation machinery.** Creating ``[WR] Logs`` / ``[BS] Logs``
  is ``bsq topic create`` (T-0660) and binding them is ``bsq topic bind``
  (T-0639). This map only points a type at a topic that already exists.

THE URGENCY AXIS
----------------
:data:`URGENT` = worth waking him for; work is stopped, or stops soon, unless a
human acts. :data:`LOG` = a record he reads when he chooses to. The class is a
DEFAULT grouping, not a constraint: ``class:log`` / ``class:urgent`` entries let
him move a whole class in one command, and a per-type entry still wins over its
class.

STORE
-----
Worker-owned, single-writer JSON per project — mirrors ``tg_topics.py``'s
per-project class→thread-id map, for the same reason: the API owns the static
supergroup id in ``projects.toml``, the worker owns the runtime routing map.
Per-project rather than global because the ask itself is per-project (``[WR]
Logs`` AND ``[BS] Logs``).

Shape on disk (``data/<slug>/_worker/msg_routes.json``)::

    { "<type key or class:urgent|class:log>": {"chat_id": "<id>|"",
                                               "topic_id": <int>|null} }

**Both fields are always present in a stored record** (see :func:`set_route`),
so reading never has to distinguish "absent" from "null" — a distinction that is
invisible on disk and has bitten this repo before. Their meanings:

* ``chat_id: ""`` — keep the caller's default chat (i.e. "a topic inside the
  project's own chat"). A real, useful destination, not a missing value.
* ``topic_id: null`` — no forum thread; the chat's own feed.

A stored record REPLACES THE WHOLE PAIR. It cannot contribute a chat while
inheriting the default's topic, because a thread id is only meaningful inside
the chat that owns it — pairing a routed chat with an inherited thread id would
address a thread in the wrong chat. (``tg_notify``'s rung-4 comment names the
same hazard for topic classes.)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Urgency classes. See "THE URGENCY AXIS" above — these say WHERE a message
#: belongs by default, never whether the quiet-hours gate may drop it.
URGENT = "urgent"
LOG = "log"
URGENCIES = (URGENT, LOG)

#: Prefix for a whole-class route entry, e.g. ``class:log``.
CLASS_PREFIX = "class:"


@dataclass(frozen=True)
class MsgType:
    """One automated message type: its stable key, urgency class, and what it
    says. ``summary`` is user-facing — it is what ``bsq msg-route list`` prints,
    so it has to read as "does this need him now" rather than naming a module.
    """

    key: str
    urgency: str
    summary: str


#: THE ENUMERATION (T-0799 DoD item 3), measured at `131f40a` — every automated
#: message type that reaches his DM today. It was bounded by the two existing
#: enumeration guards rather than by grepping for symptoms: every personal page
#: routes through ``actions._send_stakeholder_dm`` (proved by
#: ``test_notify_ssot_guard.py``) and every other sender holds a
#: ``channels.get_channel`` handle, so enumerating those two seams' callers is
#: exhaustive by construction.
#:
#: Two things the ticket expected to find here are genuinely absent, which is
#: what the enumeration was for: ``uc_redrive`` alerts go to an operator session
#: over the peer bus, and ``drift`` alarms go into the drifting session's own
#: tmux composer. Neither sends TG at all.
#:
#: Adding an automated pager? Add its type here and pass ``msg_type=`` at the
#: call site — ``test_msg_routes.py`` fails a registered type nobody sends and a
#: pager that names no type.
TYPES: dict[str, MsgType] = {
    # --- URGENT: work is stopped, or stops soon, unless a human acts ---------
    "needs_input": MsgType(
        "needs_input", URGENT,
        "a session is blocked and waiting on YOU (carries the tmux-attach line)",
    ),
    # ONE type for every way a deploy goes wrong — rc≠0, a watchdog kill, a
    # reaped orphan, a failed worker-restart, a green recipe whose worker is
    # still on old code. They are five emitters and one decision ("a deploy
    # needs me"), and splitting them would be splitting by subsystem: nobody
    # would ever route a killed build to the log while keeping rc≠0 on the
    # siren.
    "deploy_failed": MsgType(
        "deploy_failed", URGENT,
        "a deploy failed, was killed, or shipped without the worker picking it up",
    ),
    "oauth_expired": MsgType(
        "oauth_expired", URGENT,
        "the OAuth refresh failed — once creds expire, every session breaks",
    ),
    "autoupdate_failed": MsgType(
        "autoupdate_failed", URGENT,
        "an autoupdate APPLY failed — the install is left half-updated",
    ),
    "quota_alert": MsgType(
        "quota_alert", URGENT,
        "quota projected to run out before EOD, or a 429 is throttling now",
    ),
    "monitor_breach": MsgType(
        "monitor_breach", URGENT,
        "a monitor you declared breached its threshold, or went blind",
    ),
    "outbound_decayed": MsgType(
        "outbound_decayed", URGENT,
        "the outbound recording path itself decayed — messages may be lost",
    ),
    # --- LOG: a record to read when he chooses to ----------------------------
    "deploy_status": MsgType(
        "deploy_status", LOG,
        "a deploy started, or finished successfully",
    ),
    "deploy_queue": MsgType(
        "deploy_queue", LOG,
        "the deploy queue was paused or resumed",
    ),
    "outbound_recovered": MsgType(
        "outbound_recovered", LOG,
        "the outbound recording path recovered",
    ),
    "task_lifecycle": MsgType(
        "task_lifecycle", LOG,
        "a task changed status (e.g. T-0123 -> totest)",
    ),
    "autopilot_notice": MsgType(
        "autopilot_notice", LOG,
        "an autopilot run stopped, exited early, or completed",
    ),
}


def urgency(msg_type: str) -> str:
    """The urgency class of ``msg_type``. Unknown type → :data:`LOG`.

    An UNREGISTERED type defaults to LOG deliberately: the failure mode worth
    designing against is a new pager that quietly joins the siren channel, not
    one that quietly joins the log. (:func:`validate_key` is what refuses an
    unknown type at the configuration surface; this is the read path, which must
    not raise mid-send.)
    """
    t = TYPES.get(msg_type)
    return t.urgency if t is not None else LOG


def keys() -> list[str]:
    """Every configurable route key: each registered type, plus the two
    whole-class keys. This is the ONE list the config surface validates
    against, so "what can I route" cannot drift from what exists."""
    return sorted(TYPES) + [f"{CLASS_PREFIX}{u}" for u in URGENCIES]


def validate_key(key: str) -> str:
    """Return ``key`` if it is routable, else raise ``ValueError`` naming the
    valid set. A typo'd type would otherwise be stored and silently never
    match anything — a configuration that looks applied and is not."""
    if key in TYPES or key in {f"{CLASS_PREFIX}{u}" for u in URGENCIES}:
        return key
    raise ValueError(
        f"unknown message type {key!r}; routable keys: {', '.join(keys())}"
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

#: The fields of a stored route record, in report order. ONE definition, shared
#: by the writer, the loss guard and the change report (the T-0771 shape).
RECORD_FIELDS = ("chat_id", "topic_id")


class LossyRouteError(ValueError):
    """A ``set_route`` call would have implicitly dropped a field the stored
    record carries (the T-0771 rule, applied to this store).

    Raised INSTEAD of writing. A route record is only two fields and there is
    no backup, so an implicit drop is both easy and untraceable: re-pointing a
    type's chat while forgetting its topic would silently move it to the new
    chat's General feed, which looks exactly like a successful re-point. The
    caller has to say which operation it meant — restate the field, or ask for
    its explicit empty form. ``fields`` maps each at-risk field to the value
    that would have been lost so a caller can put real values in front of a
    human.
    """

    def __init__(self, slug: str, key: str, fields: dict[str, Any]) -> None:
        self.slug = slug
        self.key = key
        self.fields = dict(fields)
        named = ", ".join(f"{k}={v!r}" for k, v in self.fields.items())
        super().__init__(
            f"refusing a lossy route write for {slug}/{key}: it currently "
            f"carries {named}, which this call does not name and would drop"
        )


def routes_path(cfg: Any, slug: str) -> Path:
    """Where a project's type→destination map is persisted."""
    return Path(cfg.data_dir) / slug / "_worker" / "msg_routes.json"


def load(cfg: Any, slug: str) -> dict[str, dict]:
    """The persisted key→``{chat_id, topic_id}`` map, or ``{}`` when none.

    Unreadable file → ``{}`` with a warning (never raises: this sits on the
    send path, and a corrupt routing file must degrade to today's default
    destination rather than swallow the message). Records are normalised to
    carry BOTH fields, so callers never branch on key presence.
    """
    p = routes_path(cfg, slug)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("msg_routes: unreadable map at %s — treating as empty", p)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        chat_id = str(v.get("chat_id") or "")
        topic_raw = v.get("topic_id")
        try:
            topic_id = None if topic_raw in (None, "") else int(topic_raw)
        except (TypeError, ValueError):
            log.warning(
                "msg_routes: %s/%s has a non-integer topic_id %r — ignoring "
                "the entry rather than sending to an unaddressable thread",
                slug, k, topic_raw)
            continue
        # An all-empty record is KEPT, and means "the caller's default". That is
        # not a no-op: with a `class:log` entry in place it is the only way to
        # say "this one type stays where it is" — see :func:`route`.
        out[str(k)] = {"chat_id": chat_id, "topic_id": topic_id}
    return out


def _save(cfg: Any, slug: str, mapping: dict[str, dict]) -> None:
    """Atomically persist a project's route map."""
    p = routes_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2))
    os.replace(tmp, p)


class _NotGiven:
    """Sentinel for "this call did not name the field".

    A plain default of ``None``/``""`` cannot express it: BOTH of those are real
    destinations here (no thread / the default chat), so "unnamed" needs a third
    value. Without it, ``set_route(chat_id="-100")`` would be indistinguishable
    from an explicit request to clear the topic — the silent-loss shape.
    """

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "NOT_GIVEN"


NOT_GIVEN = _NotGiven()


def set_route(
    cfg: Any,
    slug: str,
    key: str,
    *,
    chat_id: Any = NOT_GIVEN,
    topic_id: Any = NOT_GIVEN,
) -> dict:
    """Point ``key`` (a type, or ``class:urgent``/``class:log``) at a chat and
    thread for ``slug``. Idempotent — re-setting the same key rewrites it.

    ``chat_id=""`` means "the caller's default chat" and ``topic_id=None`` means
    "no thread"; both are real destinations, so a caller has to distinguish
    "leave this field alone" from "set it empty". :data:`NOT_GIVEN` (the default
    for both) is how a call says it did not name the field.

    A call that leaves a field unnamed while the stored record carries a value
    for it raises :class:`LossyRouteError` and writes NOTHING. Restate the value
    to keep it, or pass its empty form to clear it on purpose.

    A write naming NEITHER field is refused (``ValueError``) — it expresses no
    intent. Naming both as EMPTY is different and is allowed: it PINS the type
    to the caller's default destination, which is the only way to exempt one
    type from a ``class:*`` entry that governs it. (Use :func:`clear_route` to
    remove a route entirely and let the class govern it again.)

    Returns the stored record.
    """
    validate_key(key)
    given = {
        "chat_id": chat_id is not NOT_GIVEN,
        "topic_id": topic_id is not NOT_GIVEN,
    }
    if not any(given.values()):
        raise ValueError(
            f"set_route({slug}/{key}): name a chat_id and/or a topic_id — a "
            f"record with neither is not a destination (to remove a route, "
            f"clear it)"
        )

    normalised_chat = "" if chat_id is NOT_GIVEN else str(chat_id or "")
    if topic_id is NOT_GIVEN or topic_id in (None, ""):
        normalised_topic: Optional[int] = None
    else:
        try:
            normalised_topic = int(topic_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"set_route({slug}/{key}): topic_id must be an integer or "
                f"empty, got {topic_id!r}"
            ) from None

    mapping = load(cfg, slug)
    prev = mapping.get(key) or {}
    at_risk = {
        f: prev.get(f)
        for f in RECORD_FIELDS
        if prev.get(f) not in (None, "") and not given[f]
    }
    if at_risk:
        raise LossyRouteError(slug, key, at_risk)

    rec = {"chat_id": normalised_chat, "topic_id": normalised_topic}
    mapping[key] = rec
    _save(cfg, slug, mapping)
    return rec


def change_summary(before: Optional[dict], after: Optional[dict]) -> dict[str, dict]:
    """Which fields a write actually moved: ``{field: {"from": x, "to": y}}``.

    Reported alongside the record because a record alone cannot say what it used
    to be, and a write that changed nothing is a true and useful thing to be
    told (the T-0771 report shape)."""
    before = before or {}
    after = after or {}
    return {
        f: {"from": before.get(f), "to": after.get(f)}
        for f in RECORD_FIELDS
        if before.get(f) != after.get(f)
    }


def clear_route(cfg: Any, slug: str, key: str) -> bool:
    """Remove ``key``'s route for ``slug``, restoring today's default
    destination for it. Returns True if one existed (idempotent)."""
    mapping = load(cfg, slug)
    if key not in mapping:
        return False
    del mapping[key]
    _save(cfg, slug, mapping)
    return True


# ---------------------------------------------------------------------------
# Resolution — the send path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Routed:
    """Where an automated send of one type should go.

    ``applied`` distinguishes "the configured route happens to equal the
    default" from "no route is configured", which callers need: a route that
    names a forum thread is a TG-only destination (MAX has no threads), and
    ``source`` names which rung supplied it so an operator can see WHY a message
    landed where it did.
    """

    chat_id: str
    topic_id: Optional[int]
    applied: bool
    source: str


def route(
    cfg: Any,
    msg_type: str,
    *,
    slug: str = "",
    chat_id: str = "",
    topic_id: Optional[int] = None,
) -> Routed:
    """Resolve where a ``msg_type`` send for ``slug`` goes.

    ``chat_id``/``topic_id`` are the destination the CALLER already computed —
    today's behaviour. They are returned unchanged when no route is configured,
    which is the whole no-op-by-default property: with an empty store this
    function is the identity, so shipping it moves nothing.

    Ladder, first match wins:

      1. an entry for the exact ``msg_type``
      2. an entry for its urgency class (``class:urgent`` / ``class:log``) —
         how "send every routine message to [BS] Logs" is one command instead
         of one per type
      3. the caller's default (no route configured)

    A matched entry replaces the WHOLE pair (see the module docstring): its
    ``chat_id`` falls back to the caller's chat when empty, but its ``topic_id``
    is used as-is and never inherits the default's thread — a thread id only
    means anything inside the chat that owns it.

    So an ALL-EMPTY entry resolves to the caller's default while still counting
    as a match — which is how one type is exempted from a ``class:*`` entry that
    would otherwise govern it ("everything routine to [BS] Logs, except keep the
    task notices in the DM"). ``applied`` is True and ``source`` names the entry,
    so the exemption is visible rather than looking like an absent route.

    Never raises. A missing slug, unreadable store or unknown type all resolve
    to the caller's default, because the alternative on a send path is losing
    the message.
    """
    if not slug:
        return Routed(chat_id, topic_id, False, "default")
    try:
        mapping = load(cfg, slug)
    except Exception:  # noqa: BLE001 — a routing lookup must never lose a send
        log.exception("msg_routes: route lookup failed for %s/%s", slug, msg_type)
        return Routed(chat_id, topic_id, False, "default")

    for source in (msg_type, f"{CLASS_PREFIX}{urgency(msg_type)}"):
        rec = mapping.get(source)
        if rec is None:
            continue
        return Routed(
            chat_id=rec["chat_id"] or chat_id,
            topic_id=rec["topic_id"],
            applied=True,
            source=source,
        )
    return Routed(chat_id, topic_id, False, "default")


def describe(cfg: Any, slug: str) -> list[dict[str, Any]]:
    """One row per registered type for ``slug``: its class, its summary, and the
    route key that decides its destination today (``"default"`` when none).

    What ``bsq msg-route list`` prints. It reports the RESOLVED key per type
    rather than dumping the store, because the store does not answer the
    question an operator actually has — a ``class:log`` entry silently governs
    five types, and reading the file cannot tell you which.
    """
    mapping = load(cfg, slug)
    rows: list[dict[str, Any]] = []
    for key in sorted(TYPES):
        t = TYPES[key]
        r = route(cfg, key, slug=slug, chat_id="", topic_id=None)
        rows.append({
            "msg_type": key,
            "urgency": t.urgency,
            "summary": t.summary,
            "source": r.source,
            "chat_id": r.chat_id,
            "topic_id": r.topic_id,
        })
    # Class entries are reported too — they are configurable keys, and one that
    # every type overrides individually would otherwise be invisible.
    for u in URGENCIES:
        ckey = f"{CLASS_PREFIX}{u}"
        if ckey in mapping:
            rows.append({
                "msg_type": ckey,
                "urgency": u,
                "summary": f"default destination for every {u}-class type",
                "source": ckey,
                "chat_id": mapping[ckey]["chat_id"],
                "topic_id": mapping[ckey]["topic_id"],
            })
    return rows
