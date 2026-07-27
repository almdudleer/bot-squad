"""Our own words coming back at us on the inbound channel (T-0746 item c).

The failure this prevents
------------------------
On 2026-07-27T04:57:58Z the conversation store recorded, with
``author="user"``::

    ❌ session S-almdudleer-rv-pair-trading-signals-poc-review-real--p266 not active — message dropped

That is machine-generated text attributed to the STAKEHOLDER in the permanent
record — worse than the drop it describes, because it corrupts the only account
we have of what he actually said, and it cost an operator real time that night.

How it got there, measured rather than assumed. All three records at that
timestamp carry ``general_feed: true``, which only ``_handle_topic_bound``
sets, so all three arrived as ordinary unquoted messages in bot-squad's General
feed. Three messages in the same second is not typing, and the body of one of
them is byte-identical to a message recorded in watchrobot's thread 94 seconds
earlier — that is a batch FORWARD. Nothing in ``tg_listener`` looked at forward
metadata, so ``msg["from"]`` was the human (he did send it) and the writer
recorded human authorship in good faith.

Why the fix has to live here and not in the store
-------------------------------------------------
T-0755 closed the author vocabulary (``user`` | ``session:<sid>`` |
``system:<kind>``) and validates it in ``conversation_store.append``, the one
write path. But that gate is SHAPE-only: no validator can tell "a human typed
this" from "our own error text arrived on the human's channel" — the payload is
identical. Only the delivery half knows what we said. So this module answers
one question at the point of ENTRY:

    did the sender COMPOSE this text, or is it something we handed him?

Two rungs, both needed
----------------------
1. **Forward provenance** (:func:`forward_provenance`). Telegram marks every
   forward — ``forward_origin`` on Bot API 7.0+, the legacy ``forward_from`` /
   ``forward_from_chat`` / ``forward_sender_name`` fields before it. An origin
   naming our OWN bot id is proof, not inference.
2. **Delivery match** (:func:`recent_send_match`). Rung 1 goes blind exactly
   when it matters most: a forward sent with "hide sender name" reports only
   ``hidden_user``, and a COPY-PASTE carries no metadata at all. So we also ask
   the outbound log T-0755 built — did we send this exact text to this chat
   recently? — which is class-independent: it catches any of our text coming
   back, not just the one notice shape that caused the incident. Matching a
   shape (``"❌ session … not active"``) would have fixed precisely the message
   class we already know about, which is the opposite of a guard.

What it does NOT do
-------------------
* It does not silence anything. An echo still routes and still wakes the
  attendant (both TG handlers call ``_ensure_user_conversation`` themselves,
  independently of the append's author) — the stakeholder forwarded it FOR a
  reason. Only the attribution changes.
* It does not add a fourth author class. Our own text becomes
  ``system:bot-echo``; a forward of somebody ELSE's message stays
  ``author="user"`` (a human did write it) and is merely annotated with
  ``forwarded_from`` so a reader is not told the sender composed it.
* It never raises. A guard that can break inbound routing is a worse bug than
  a mislabelled line, so every entry point falls back to "the sender composed
  it" — the pre-T-0746 behaviour — on any failure.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: The author for text WE produced that re-entered on the inbound channel.
#: ``system:<kind>`` per the T-0755 vocabulary — no new class.
ECHO_AUTHOR = "system:bot-echo"

#: ``forwarded_from`` value for our own bot, on either rung.
ECHO_ORIGIN = "bot"

#: How far back the delivery match looks. The spool keeps 14 days
#: (``outbound_log.SPOOL_RETENTION_DAYS``); 7 covers any plausible "look at
#: this thing the bot said" while keeping the window — and so the
#: false-positive surface — smaller than the retention it reads from.
LOOKBACK_DAYS = 7

#: Below this, a match is not evidence. We send short strings ("Принял",
#: "ok") that a human plausibly types verbatim, and mislabelling HIS "ok" as
#: our echo is the same class of lie in the other direction.
MIN_MATCH_LEN = 24

#: Cap on spool records scanned per inbound message. Live volume is ~10
#: outbound/day, so this is a runaway guard, not routine trimming.
MATCH_SCAN_LIMIT = 2000


def _own_bot_id(cfg: Any) -> str:
    """Our bot's numeric TG id, derived from the configured token.

    Mirrors ``tg_listener._own_bot_id`` deliberately rather than importing it:
    this module is imported BY tg_listener, and the derivation is two lines of
    string splitting. Kept identical (and pinned by a test) so the two can
    never disagree about which bot is "ours".
    """
    token = getattr(cfg, "tg_bot_token", "") or ""
    return token.split(":", 1)[0] if ":" in token else ""


def forward_provenance(msg: dict) -> str:
    """Where a forwarded message's CONTENT came from, or ``""`` when the sender
    composed it themselves.

    Handles both encodings, because a bot can be talking to either:

    * Bot API 7.0+ ``forward_origin``: ``user`` (``sender_user``),
      ``hidden_user`` (``sender_user_name`` only — the "hide sender" case that
      rung 1 cannot resolve), ``chat`` (``sender_chat``), ``channel``
      (``chat``).
    * Pre-7.0 ``forward_from`` / ``forward_from_chat`` / ``forward_sender_name``.

    Returns a short, stable descriptor (``"user:<id>"``, ``"chat:<id>"``,
    ``"hidden"``, or a bare name) — it is recorded verbatim as the record's
    ``forwarded_from``, so it must stay terse and must not carry a body.
    """
    origin = msg.get("forward_origin")
    if isinstance(origin, dict):
        kind = str(origin.get("type") or "")
        if kind == "user":
            uid = (origin.get("sender_user") or {}).get("id")
            return f"user:{uid}" if uid is not None else "user"
        if kind == "hidden_user":
            return str(origin.get("sender_user_name") or "hidden")
        if kind == "chat":
            cid = (origin.get("sender_chat") or {}).get("id")
            return f"chat:{cid}" if cid is not None else "chat"
        if kind == "channel":
            cid = (origin.get("chat") or {}).get("id")
            return f"chat:{cid}" if cid is not None else "channel"
        return kind or "forward"
    # Legacy fields. Checked AFTER forward_origin (a 7.0+ payload carries both
    # for back-compat and the structured one is richer), and independently of
    # each other because only one of them is ever present.
    frm = msg.get("forward_from")
    if isinstance(frm, dict) and frm.get("id") is not None:
        return f"user:{frm['id']}"
    chat = msg.get("forward_from_chat")
    if isinstance(chat, dict) and chat.get("id") is not None:
        return f"chat:{chat['id']}"
    name = msg.get("forward_sender_name")
    if name:
        return str(name)
    # forward_date with no origin field at all: still definitely a forward.
    if msg.get("forward_date"):
        return "hidden"
    return ""


def is_own_bot_forward(msg: dict, cfg: Any) -> bool:
    """RUNG 1 — this message was forwarded from our own bot, provably.

    ``user:<our bot id>`` is the only shape that proves it: a bot is a TG user,
    so forwarding its message yields a ``user`` origin whose id is the bot id.
    ``hidden`` deliberately does NOT count — "someone chose not to say" is not
    evidence it was us, and guessing there is how a guard starts eating the
    stakeholder's own words. That gap is what rung 2 is for.
    """
    own = _own_bot_id(cfg)
    if not own:
        return False
    return forward_provenance(msg) == f"user:{own}"


def _normalize(text: str) -> str:
    """Comparison form for the delivery match: whitespace-collapsed, stripped.

    ``str.split()`` treats U+00A0 as whitespace, so a non-breaking space picked
    up by a copy-paste collapses like any other. Deliberately NOT case-folded
    and NOT punctuation-stripped — the match must stay an equality test on the
    body we sent, not a similarity score.
    """
    return " ".join(str(text or "").split())


def recent_send_match(cfg: Any, *, chat_id: Any, text: str) -> dict | None:
    """RUNG 2 — the spool record of an identical message WE sent to ``chat_id``
    within :data:`LOOKBACK_DAYS`, or ``None``.

    This is the rung that makes the guard class-independent. It knows nothing
    about the "session not active" notice, or about any other message class we
    might invent later: it asks only whether these exact bytes left through our
    own transport, which is a fact the delivery half owns and the payload
    cannot fake.

    Scoped to the SAME chat on purpose. A body we sent to a different chat
    reaching this one is a human choosing to relay it — that is his act, and
    ``forwarded_from`` (rung 1) is the honest record of it, not an echo.

    Never raises: an unreadable/absent spool means "no evidence", which lands
    on the pre-T-0746 behaviour.
    """
    body = _normalize(text)
    if len(body) < MIN_MATCH_LEN or not str(chat_id or ""):
        return None
    try:
        from datetime import datetime, timedelta, timezone

        from bot_squad_worker import outbound_log

        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        records = outbound_log.read_spool(
            getattr(cfg, "data_dir", None), since=since,
            chat_id=str(chat_id), limit=MATCH_SCAN_LIMIT,
        )
    except Exception:  # noqa: BLE001 — a lookup failure must not break intake
        log.exception("echo_guard: outbound spool lookup failed for chat %s", chat_id)
        return None
    for rec in reversed(records):  # newest first: the likeliest match
        if _normalize(rec.get("text")) == body:
            return rec
    return None


def classify_inbound(cfg: Any, msg: dict, *, chat_id: Any = None) -> dict:
    """Whether the SENDER composed this message, and how to attribute it.

    Returns ``{"author": <str>, "forwarded_from": <str>, "reason": <str>}``.
    ``author`` is ``"user"`` (the sender composed it — the pre-T-0746 answer
    for every genuine message) or :data:`ECHO_AUTHOR`. ``forwarded_from`` is
    ``""`` when the content originated with the sender.

    Order matters: rung 1 is checked first because it is proof and costs
    nothing, and it also supplies the provenance string for the
    somebody-else's-message case, which rung 2 has no opinion about.
    """
    out = {"author": "user", "forwarded_from": "", "reason": ""}
    try:
        if is_own_bot_forward(msg, cfg):
            out.update(author=ECHO_AUTHOR, forwarded_from=ECHO_ORIGIN,
                       reason="forward-origin")
            return out
        provenance = forward_provenance(msg)
        match = recent_send_match(
            cfg,
            chat_id=chat_id if chat_id is not None else (msg.get("chat") or {}).get("id"),
            text=msg.get("text") or "",
        )
        if match is not None:
            out.update(author=ECHO_AUTHOR, forwarded_from=ECHO_ORIGIN,
                       reason="outbound-match")
            return out
        if provenance:
            # Somebody ELSE's message, relayed by the sender. A human did write
            # it, so `user` stays correct — but the record must not imply the
            # sender composed it.
            out["forwarded_from"] = provenance
            out["reason"] = "forwarded"
        return out
    except Exception:  # noqa: BLE001 — never break inbound routing over a label
        log.exception("echo_guard: classify_inbound failed; treating as user-authored")
        return {"author": "user", "forwarded_from": "", "reason": ""}
