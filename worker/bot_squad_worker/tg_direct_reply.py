"""A human wrote into a session-bound topic — and is owed an answer THERE (T-0770).

The failure this fixes
----------------------
A forum topic whose binding carries a ``session_id`` routes the stakeholder's
message STRAIGHT into that session's composer (``tg_listener._handle_topic_bound``,
the T-0660 Phase-2 direct-mode branch). Until this module the whole payload was
``{"sid": sid, "text": text}`` — his words and nothing else. The receiving
session had no way to know (a) a human wrote them, (b) which chat/topic they
arrived in, or (c) that an answer was owed BACK to that topic rather than to its
own transcript. So it did the natural thing with text in its composer: it
answered in its own turn, the answer never left the process, and he saw silence.

On 2026-07-28 he said so himself, at 22:33:23Z: «Топик про бот вижу, но там
опять игнор меня». ОПЯТЬ — the second time, and the second time was in topic
517, a topic created twenty-five minutes earlier *specifically to route around*
the first (220). The receiving session (p70) corroborated it unprompted: his
four messages "landed as unattributed text in my session".

Why an envelope alone is not the fix
------------------------------------
The operator's gate on this ticket is behavioural, not structural: *"a session
receiving such an injection must ANSWER INTO THE TOPIC without being told to by
a human. If the fix is only a prefix that a session may or may not notice, it
has not fixed this."* So this module has two halves and they are not separable:

1. :func:`compose_envelope` — what the session receives. Provenance, the exact
   destination, his verbatim words, and the command that answers.
2. The ANSWER-OWED LEDGER (:func:`record_owed` / :func:`tick`) — after a grace
   period it asks the OUTBOUND SPOOL whether anything a session sent actually
   landed in that exact ``(chat, topic)``, and re-drives the session when
   nothing did. Silence is measured, not hoped away.

Two measurements from the manual walkthrough shaped both halves, and neither is
guesswork — see the ticket for the transcripts:

* ``inject_input``'s transport (``input_mux.deliver_direct``) sends one Enter
  **per line**, so his own 3-line message already arrives as 3 separate
  composer submissions. A multi-line envelope on that transport would be
  strictly worse than the bare text it replaces, which is why delivery goes
  through the paste primitive (``inject_prompt``) instead.
* The destination MUST be spelled out. ``bsq tg ping`` resolves it by lookup
  (``actions._own_topic_binding`` → ``tg_bindings.find_by_session``), which
  returns the FIRST binding naming the session. p70 held both 220 and 517, so a
  session that obeyed a destination-less instruction would have answered into
  220 — the topic he had already been ignored in — while he was writing in 517.

The sibling paths, provenance only (T-0773)
-------------------------------------------
Two other surfaces reach a session's composer the same way: an explicit
``[<sid>]`` reply to a bot message, and ``/say <sid> <text>``. They get
:func:`compose_light_envelope` — the provenance half WITHOUT the ledger, by an
operator ruling that is recorded in that function's docstring rather than here
so it travels with the thing it governs. Both switched to ``inject_prompt`` at
the same time, which is the part that was a plain defect: the one-Enter-per-line
transport was splitting his multi-line messages into N submissions on those
paths too.

What this module must NOT do (T-0667, inherited)
------------------------------------------------
A task-topic message must never redirect the project's attendant-reply relay
into the task topic. So nothing here writes the conversation LOCUS and nothing
here pins a route for his next ORDINARY message: the envelope carries identity
(who wrote, where it arrived) and the ledger reads a log. Both are inert with
respect to routing — the same separation ``sender_sid`` exists for (T-0758/
T-0762).

Shape on disk
-------------
``data/_worker/tg_answer_owed.json``, worker-owned single-writer JSON, atomic
write — the ``tg_bindings.py`` pattern::

    { "<chat_id>:<thread_id>:<sid>": {
        sid, chat_id, thread_id, slug, gid, ticket_id, text, at,
        attempts, last_attempt_at, escalated } }

Keyed by ``(chat, topic, sid)`` rather than by sid, because one session can
hold more than one direct-mode topic (p70 held two) and a debt in one is not a
debt in the other.

Kill switch: ``BOT_SQUAD_TG_ANSWER_OWED=0`` (the ledger and the tick; the
envelope itself is not gated — it is the message, not a background sweep).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# How much of his text the reminder quotes back. The envelope carries the whole
# thing; the reminder only has to make it recognisable.
_QUOTE_CAP = 240


# ---------------------------------------------------------------------------
# Knobs (read at call time so tests can monkeypatch the environment)
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return os.environ.get("BOT_SQUAD_TG_ANSWER_OWED", "1").strip() != "0"


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def grace_sec() -> int:
    """How long the session gets to answer on its own before the first nudge.

    Long enough for a real turn (read the envelope, do the thing, answer),
    short enough that he is not sitting in front of an unanswered topic. The
    incident's own clock is the reference: four messages and ~26 minutes
    elapsed before he wrote «игнор меня»."""
    return _int_env("BOT_SQUAD_TG_ANSWER_OWED_GRACE_SEC", 180)


def cooldown_sec() -> int:
    """Minimum gap between re-drives of the same debt."""
    return _int_env("BOT_SQUAD_TG_ANSWER_OWED_COOLDOWN_SEC", 180)


def max_attempts() -> int:
    """How many reminders before the debt is escalated off the session."""
    return _int_env("BOT_SQUAD_TG_ANSWER_OWED_MAX_ATTEMPTS", 2)


# ---------------------------------------------------------------------------
# The envelope — what the session actually receives
# ---------------------------------------------------------------------------

def reply_command(chat_id: Any, thread_id: Any) -> str:
    """The one command that posts back into THIS topic, destination spelled out.

    Explicit ``--chat``/``--topic`` rather than ``--ticket`` or a bare
    ``bsq tg ping``: a direct-mode topic frequently carries no ticket_id at all
    (``/pin-session`` sets only the session), and the lookup forms resolve to
    whichever binding names the session FIRST — the wrong-topic answer measured
    on p70's two bindings."""
    return f'bsq topic say --chat {chat_id} --topic {thread_id} "<your answer>"'


def compose_envelope(
    *, text: str, chat_id: Any, thread_id: Any, sid: str, slug: str = "",
    ticket_id: str = "", sender: str = "",
) -> str:
    """The injected message: provenance + his words + the reply contract.

    Deliberately ONE block delivered as ONE composer submission (see the module
    docstring). His words are fenced rather than prefixed — the T-0746 lesson
    is that system prose mixed into a human's line makes the two
    indistinguishable, and here the session has to quote him back accurately."""
    who = sender.strip() or "the stakeholder"
    where = f"chat {chat_id}, forum topic {thread_id}"
    tags = " · ".join(x for x in (slug, ticket_id) if x)
    body = str(text or "")
    return "\n".join([
        f"📨 TELEGRAM — {who.upper()} WROTE TO YOU. AN ANSWER IS OWED IN THAT TOPIC.",
        "",
        f"From    : {who}, a human, via Telegram",
        f"Where   : {where}" + (f"  [{tags}]" if tags else ""),
        "Why you : that topic is bound DIRECTLY to your session, so his message",
        "          came straight here instead of through the project's",
        "          user-conversation attendant.",
        f"Session : {sid}",
        "",
        "His message, verbatim:",
        "--- 8< ---",
        body,
        "--- >8 ---",
        "",
        "⚠️ HE CANNOT SEE THIS SESSION. Answering in your own turn is invisible to",
        "him — that is exactly the failure this envelope exists to prevent",
        "(T-0770: he wrote «опять игнор меня» after four unanswered messages).",
        "An answer only counts when it is SENT:",
        "",
        f"    {reply_command(chat_id, thread_id)}",
        "",
        "Send it now, before continuing what you were doing. Use those exact",
        "--chat/--topic values: `bsq tg ping` and `--ticket` resolve the",
        "destination by lookup and post into the wrong topic when your session",
        "holds more than one. If nothing lands in that topic, the system will",
        "remind you, and then answer for you — noisily.",
    ])


def answer_route(chat_id: Any, thread_id: Any) -> str:
    """The command that reaches him WHERE HE WROTE, forum topic or plain DM.

    :func:`reply_command` covers the forum case only — its ``--chat``/``--topic``
    flags "go together" (``bsq topic say`` refuses one without the other,
    because a thread id is only meaningful inside its own chat). A DM has no
    thread, so naming ``--topic None`` there would hand the session a command
    that dies on arrival; ``bsq tg ping`` is the DM route and always has been.
    """
    if thread_id is None:
        return 'bsq tg ping "<your answer>"'
    return reply_command(chat_id, thread_id)


def compose_light_envelope(
    *, text: str, chat_id: Any, thread_id: Any, sid: str, sender: str = "",
    origin: str = "reply",
) -> str:
    """Provenance WITHOUT a debt — the two sibling paths of T-0773.

    ``origin="reply"``  an explicit ``[<sid>]`` reply to a bot message
                        (``tg_listener.handle_update``, the stall-escalation
                        answer path).
    ``origin="say"``    ``/say <sid> <text>`` — a human operator writing
                        straight into a session.

    WHY THIS IS NOT :func:`compose_envelope`, and it is a product ruling rather
    than a size difference (operator p374 on T-0773, 2026-07-29). That envelope
    tells a session an answer is OWED back to Telegram and
    :func:`record_owed` then measures whether one landed. Both are justified by
    ONE harm: he wrote into a topic and sat looking at silence. Neither of these
    paths reproduces it —

    * on a ``[<sid>]`` reply he is ANSWERING a question the session asked him
      (``bsq tg ping "should I do X?"`` → «да»); the correct response to «да» is
      to go do X, and posting «да, понял» back into Telegram is the ticket's own
      harm inverted — noise pushed into the thread he is reading;
    * ``/say`` is one-way by construction: he spoke last, deliberately.

    "A backstop applied where its harm does not occur stops being a backstop and
    becomes noise, and noise is what gets the real alarm muted." So nothing here
    writes the ledger, and the text is careful never to IMPLY a debt: a session
    that reads "an answer is owed" will post an acknowledgement, which is the
    outcome the ruling refuses.

    What it does keep is the half the ruling said belongs on all three paths:
    the session is told a HUMAN wrote this and over what channel. "The value is
    not politeness — it is that a session which knows a human is on the other
    end behaves differently from one handed an anonymous string."

    Delivered by ``inject_prompt`` (one composer submission), never
    ``inject_input`` — see :func:`compose_envelope`'s note on the one-Enter-per-
    line transport. That measurement is the reason this ticket exists at all:
    both paths were already splitting his multi-line messages line by line,
    envelope or no envelope.
    """
    who = sender.strip() or "the stakeholder"
    where = (f"chat {chat_id}, forum topic {thread_id}" if thread_id is not None
             else f"chat {chat_id} (direct message)")
    body = str(text or "")
    if origin == "say":
        head = f"📨 TELEGRAM — {who.upper()} SENT THIS STRAIGHT INTO YOUR SESSION."
        why = [
            "Why you : a human addressed it to your SID explicitly (`/say`). That",
            "          channel is one-way by construction — nothing is owed back",
            "          to Telegram. Treat it as instruction/context and continue.",
        ]
        tail: list[str] = []
    else:
        head = f"📨 TELEGRAM — {who.upper()} WROTE THIS. A HUMAN, NOT A SYSTEM MESSAGE."
        why = [
            "Why you : they replied to a message YOUR session sent, so this is",
            "          their answer to you. Acting on it IS the response — no",
            "          acknowledgement is owed back to Telegram.",
        ]
        tail = [
            "",
            "If something still has to reach THEM (a question, a result they asked",
            "for), it only counts when SENT — your own turn is invisible to them:",
            "",
            f"    {answer_route(chat_id, thread_id)}",
        ]
    return "\n".join([
        head,
        "",
        f"From    : {who}, a human, via Telegram",
        f"Where   : {where}",
        *why,
        f"Session : {sid}",
        "",
        "Their message, verbatim:",
        "--- 8< ---",
        body,
        "--- >8 ---",
        *tail,
    ])


def compose_reminder(entry: dict, *, attempt: int, attempts_left: int) -> str:
    """The re-drive: shorter, sharper, and it says what happens next."""
    quote = str(entry.get("text") or "")
    if len(quote) > _QUOTE_CAP:
        quote = quote[:_QUOTE_CAP] + "…"
    tail = (
        f"({attempts_left} reminder(s) left — after that the topic is told the "
        "answer is late and the project attendant is woken to answer for you.)"
        if attempts_left > 0 else
        "(This was the last reminder — the topic is being told and the project "
        "attendant is being woken to answer for you.)"
    )
    return "\n".join([
        f"⚠️ TELEGRAM ANSWER STILL OWED (T-0770, reminder {attempt}). The "
        "stakeholder wrote to you in",
        f"chat {entry.get('chat_id')}, topic {entry.get('thread_id')} and NOTHING "
        "has been posted back into",
        "that topic since. He is looking at that topic and reads this as being ignored.",
        "",
        f"He wrote: «{quote}»",
        "",
        "Answer it now:",
        "",
        f"    {reply_command(entry.get('chat_id'), entry.get('thread_id'))}",
        "",
        tail,
    ])


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def owed_path(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "_worker" / "tg_answer_owed.json"


def key(chat_id: Any, thread_id: Any, sid: str) -> str:
    return f"{chat_id}:{'' if thread_id is None else thread_id}:{sid}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> float:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def load(cfg: Any) -> dict[str, dict]:
    """The persisted debt map, or ``{}`` when missing/unreadable."""
    p = owed_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("tg_direct_reply: unreadable ledger at %s — treating as empty", p)
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)} \
        if isinstance(raw, dict) else {}


def _save(cfg: Any, mapping: dict[str, dict]) -> None:
    p = owed_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def record_owed(
    cfg: Any, *, sid: str, chat_id: Any, thread_id: Any, text: str,
    slug: str = "", gid: str = "", ticket_id: str = "", at: str = "",
) -> dict | None:
    """An envelope was DELIVERED — start the clock on the answer it owes.

    Only ever called after a successful delivery: a message the session never
    received is not a debt it owes (the T-0746 fallback owns that case, and it
    already wakes a project attendant who WILL answer).

    A new message REPLACES any debt for the same ``(chat, topic, sid)`` and
    resets its attempts — he is owed an answer to what he just said, and
    re-driving on a stale quote would be answering the wrong message. Never
    raises: a ledger failure must not break the delivery it follows."""
    if not enabled() or thread_id is None:
        return None
    try:
        mapping = load(cfg)
        entry = {
            "sid": sid,
            "chat_id": str(chat_id),
            "thread_id": thread_id,
            "slug": slug,
            "gid": gid,
            "ticket_id": ticket_id or "",
            "text": str(text or ""),
            "at": at or _now_iso(),
            "attempts": 0,
            "last_attempt_at": "",
            "escalated": False,
        }
        mapping[key(chat_id, thread_id, sid)] = entry
        _save(cfg, mapping)
        return entry
    except Exception:  # noqa: BLE001
        log.exception("tg_direct_reply.record_owed failed (sid=%s chat=%s thread=%s)",
                      sid, chat_id, thread_id)
        return None


def clear_owed(cfg: Any, *, sid: str, chat_id: Any, thread_id: Any) -> bool:
    """Forget one debt. Returns True when something was removed."""
    mapping = load(cfg)
    if mapping.pop(key(chat_id, thread_id, sid), None) is None:
        return False
    _save(cfg, mapping)
    return True


# ---------------------------------------------------------------------------
# Did anyone actually answer? — the measurement
# ---------------------------------------------------------------------------

#: A session posted into that topic after his message. The debt is paid.
ANSWERED = "answered"
#: The log is readable and live, and holds no such send. The debt stands.
UNANSWERED = "unanswered"
#: The log cannot answer the question. NOT the same as "he was ignored".
BLIND = "blind"


def answered_since(cfg: Any, *, chat_id: Any, thread_id: Any, since: str) -> str:
    """:data:`ANSWERED` / :data:`UNANSWERED` / :data:`BLIND` for one topic.

    ★ BLIND IS A STATE, NOT A ZERO (T-0740/T-0759). An empty outbound spool
    reads exactly like "nobody answered him", and it reads that way *most
    convincingly* when the recording has broken — which is when a monitor built
    on the raw count would start accusing every session on the install of
    ignoring him and re-driving them all. So the liveness verdict is consulted
    FIRST and its own positive control (``scan``) is what licenses believing a
    zero here. ``DECAYED`` is treated the same way as ``BLIND``: a log that is
    missing sends cannot be used to prove one is absent.

    The criterion is ``author`` starting with ``session:`` — a SESSION spoke
    into that topic. Deliberately not "any record": ``system:`` lines (a
    lifecycle ``📋`` notice, a deploy post, and this module's own escalation
    notice) land in topics for their own reasons and would silently clear a debt
    nobody paid — the exact regression to today's behaviour, with a green test.
    Deliberately not "*that* session": if a peer answered him in that topic he
    is not sitting in silence, and nagging on is noise."""
    from bot_squad_worker import outbound_liveness, outbound_log

    try:
        verdict = outbound_liveness.check(cfg)
        state = verdict.get("state")
    except Exception:  # noqa: BLE001 — an unreadable install cannot accuse anyone
        log.exception("tg_direct_reply: liveness check failed")
        return BLIND
    if state not in (outbound_liveness.OK, outbound_liveness.IDLE):
        # SAY SO. A backstop that switches itself off silently is the shape of
        # failure this whole ticket is about, one level up — and one path here
        # is easy to miss: ``outbound_log.DROPS`` is process-global, so a SINGLE
        # dropped spool record makes the verdict DECAYED for the rest of the
        # worker's life, and every debt after it goes unmeasured until a
        # restart. T-0759's own tick pages a human about the log itself; this
        # line is what connects that page to "and the answer-owed sweep is
        # currently blind".
        log.warning(
            "tg_direct_reply: outbound log is %s — making NO claim about "
            "chat=%s topic=%s (%s)", state, chat_id, thread_id,
            verdict.get("reason"))
        return BLIND
    try:
        records = outbound_log.read_spool(
            cfg.data_dir, since=since, chat_id=str(chat_id), limit=500)
    except Exception:  # noqa: BLE001
        log.exception("tg_direct_reply: spool read failed")
        return BLIND
    for rec in records:
        if str(rec.get("author") or "").startswith("session:") and \
                _same_thread(rec.get("thread_id"), thread_id):
            return ANSWERED
    return UNANSWERED


def _same_thread(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return str(a) == str(b)


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def tick(cfg: Any, *, now: float | None = None) -> dict:
    """One sweep of the answer-owed ledger. Returns a summary dict.

    Wired next to ``uc_redrive`` (T-0622) and shaped like it on purpose: this is
    the same class of failure — a message that reached us and was never answered
    — on the one path uc_redrive structurally cannot see. Its sweep looks for a
    conversation thread whose newest record is ``author: "user"``; a direct-mode
    topic message is recorded as an ``fyi`` from ``system:direct-reply`` (T-0746),
    which is not that shape and must not become it (the FYI marker is what stops
    the attendant answering a message that went to a session).

    Never raises — a sweep that can crash the scheduler tick it shares is a
    worse outcome than a late reminder."""
    import time as _time

    if not enabled():
        return {"ok": True, "disabled": True, "checked": 0}
    now = _time.time() if now is None else float(now)
    try:
        mapping = load(cfg)
    except Exception:  # noqa: BLE001
        log.exception("tg_direct_reply.tick: ledger unreadable")
        return {"ok": False, "checked": 0}
    if not mapping:
        return {"ok": True, "checked": 0, "cleared": [], "reminded": [],
                "deferred": [], "escalated": [], "blind": []}

    cleared: list[str] = []
    reminded: list[str] = []
    deferred: list[str] = []
    escalated: list[str] = []
    blind: list[str] = []
    dirty = False

    for k, entry in list(mapping.items()):
        try:
            verdict = answered_since(
                cfg, chat_id=entry.get("chat_id"),
                thread_id=entry.get("thread_id"), since=str(entry.get("at") or ""))
            if verdict == ANSWERED:
                mapping.pop(k, None)
                cleared.append(k)
                dirty = True
                continue
            if verdict == BLIND:
                # No claim, no nudge, no attempt burned. The debt stays on the
                # books so it is still checked once the log can answer again.
                blind.append(k)
                continue
            age = now - _epoch(str(entry.get("at") or ""))
            if age < grace_sec():
                continue
            last = _epoch(str(entry.get("last_attempt_at") or ""))
            if last and (now - last) < cooldown_sec():
                continue
            attempts = int(entry.get("attempts") or 0)
            if attempts >= max_attempts():
                if _escalate(cfg, entry):
                    mapping.pop(k, None)
                    escalated.append(k)
                    dirty = True
                continue
            outcome = _remind(cfg, entry, attempt=attempts + 1)
            entry["last_attempt_at"] = _now_iso()
            dirty = True
            if outcome == "delivered":
                entry["attempts"] = attempts + 1
                reminded.append(k)
            elif outcome == "no_pane":
                # The session is gone. Reminding a dead pane forever is the
                # silence again with extra steps — hand it over now.
                if _escalate(cfg, entry, reason="session_gone"):
                    mapping.pop(k, None)
                    escalated.append(k)
            else:
                # Composer busy: the reminder is QUEUED and will land on a later
                # flush; nothing is burned and the cooldown paces the next look.
                # Reported so a quiet tick summary is never read as "nothing
                # happened" when a reminder is in fact in flight.
                deferred.append(k)
        except Exception:  # noqa: BLE001 — one bad entry must not stop the sweep
            log.exception("tg_direct_reply.tick: entry %s failed", k)

    if dirty:
        try:
            _save(cfg, mapping)
        except Exception:  # noqa: BLE001
            log.exception("tg_direct_reply.tick: could not persist the ledger")
    out = {"ok": True, "checked": len(mapping) + len(cleared) + len(escalated),
           "cleared": cleared, "reminded": reminded, "deferred": deferred,
           "escalated": escalated, "blind": blind}
    if reminded or escalated:
        log.info("tg_direct_reply.tick: %s", out)
    return out


def _remind(cfg: Any, entry: dict, *, attempt: int) -> str:
    """Queue one reminder into the session. Returns delivered/deferred/no_pane.

    The QUEUED lane (``input_mux.enqueue`` + ``flush``), not the direct one the
    envelope itself uses: a reminder must never clobber a composer the session
    is mid-thought in, and a deferred reminder still lands. The envelope is the
    message and is synchronous; this is a nudge about it."""
    from bot_squad_worker import input_mux

    text = compose_reminder(
        entry, attempt=attempt,
        attempts_left=max(0, max_attempts() - attempt))
    sid = str(entry.get("sid") or "")
    input_mux.enqueue(cfg.data_dir, sid, text, "tg-answer-owed")
    res = input_mux.flush(cfg.data_dir, sid, pane_lookup=_pane_lookup)
    if res.get("delivered"):
        return "delivered"
    return "no_pane" if res.get("reason") == "no_pane" else "deferred"


def _pane_lookup(sid: str) -> str | None:
    from bot_squad_worker.actions import _send_input_pane_lookup
    return _send_input_pane_lookup(sid)


def _escalate(cfg: Any, entry: dict, *, reason: str = "no_answer") -> bool:
    """The session did not answer. He must not be the one who finds that out.

    Two things, in this order, both bounded to ONCE per debt:

    1. A line INTO THE TOPIC he is looking at. Not an apology and not a
       diagnosis — it tells him an answer is coming and from whom, so the
       silence stops being silence.
    2. His text handed to the project's user-conversation attendant, with the
       situation stated beside it, and the attendant woken. That is the same
       hand-over ``tg_listener._fallback_undelivered`` performs for a message
       that could not be delivered at all — the difference here is only WHY the
       session did not answer, so the destination is the same and the attendant
       gets a session that will actually reply.

    Returns True when the debt was handed over (so it can be dropped). False
    keeps it on the books for the next tick rather than losing it quietly."""
    from bot_squad_worker import actions as A, tg_listener as TL

    sid = str(entry.get("sid") or "")
    slug = str(entry.get("slug") or "")
    gid = str(entry.get("gid") or "")
    chat_id = entry.get("chat_id")
    thread_id = entry.get("thread_id")
    told = False
    try:
        A.dispatch("tg_notify", {
            "chat_id": str(chat_id), "topic_id": thread_id,
            "slug": slug,
            "message": (
                f"⚠️ Твоё сообщение получено, но сессия {sid} так и не ответила "
                f"сюда. Подключаю user-conversation проекта «{slug}» — ответ "
                f"придёт оттуда."
            ),
            "urgent": True, "debounce": False,
        })
        told = True
    except Exception:  # noqa: BLE001 — a failed notice must not block the handover
        log.exception("tg_direct_reply: could not post the late-answer notice "
                      "into chat=%s topic=%s", chat_id, thread_id)

    if not slug or not gid:
        # Nothing to hand it to — the notice above is all there is. Drop the
        # debt only if he was at least told; otherwise leave it for a retry.
        return told
    try:
        TL._post_conversation(cfg, slug, gid, {
            "author": "system:answer-owed",
            "text": (
                f"Сообщение ушло напрямую в сессию {sid} (топик {thread_id}), "
                f"но она не ответила туда ({reason}). Оригинальный текст — "
                f"следующим сообщением; ответь на него сам, в тот же топик "
                f"({reply_command(chat_id, thread_id)})."
            ),
            "direction": "in",
        })
        message_ref = _now_iso()
        TL._post_conversation(cfg, slug, gid, {
            "author": "user", "text": str(entry.get("text") or ""),
            "timestamp": message_ref,
        })
        TL._ensure_user_conversation(cfg, slug, gid, message_ref)
        return True
    except Exception:  # noqa: BLE001
        log.exception("tg_direct_reply: handover to the %s attendant failed", slug)
        return told
