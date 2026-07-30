"""The drive STOPPING-CONDITION axis (T-0800, design D-0069) — axis B entire.

> надо сделать еще в настройках драйва по проекту режим stall alert / alert me
> when done, чтобы как только всё сделано, он писал в какой-то оперативный
> канал, что ВСЁ СДЕЛАНО, ПРОВЕРЯЙ, МЫ ПРОСТАИВАЕМ
> — the stakeholder, 2026-07-30T09:45:35Z (T-0800's verbatim request).

and, from the four-mode list that opened this work (T-0783, 2026-07-29T12:20:46Z):

> • Потратить квоту

**What it inverts.** Today a project that runs out of work either invents work or
sits quietly, and he finds out by asking. This makes the system say it has run
dry, so the idle time becomes his decision instead of a silent gap.

WHERE THE SETTING LIVES — nowhere new
-------------------------------------
«в настройках драйва по проекту» = the per-project pace config
(:mod:`pace`, store ``data/<slug>/_worker/pace/pace.json``, surface
``bsq pace show``), which T-0828 already gave the ``drive`` block. This module
adds **no store, no CLI noun and no settings surface** — it READS
``pace.read_drive`` and reads :func:`pickup.pickup_queue`, and owns exactly one
piece of state of its own: the edge-trigger latch below, which is not a setting.

Two axis-B values, one attribute (all defined and validated in :mod:`pace`):

* ``stop_when: scope_exhausted`` — default, today's implicit rule.
* ``stop_when: spend_quota`` — «Потратить квоту». See "the unknown spend" below.
* ``on_stop: nothing`` — default. ``on_stop: alert`` — this ticket.

NO-OP BY CONSTRUCTION
---------------------
:func:`tick` reads ``on_stop`` first and returns before touching the board when
it is not ``alert``. Since ``alert`` is opt-in per project and nothing sets it
today, shipping this scans nothing and sends nothing — the same property T-0799
and T-0828 shipped with.

THE PREDICATE (T-0800 DoD item 2) — handed down, not re-derived
---------------------------------------------------------------
"All done" is the condition that either never fires or fires constantly, so it
is stated once, here, and it is L2's (T-0829) — this module is a CONSUMER of a
predicate that lane built and pinned::

    q = pickup.pickup_queue(cfg, slug)          # scope read from the pace config
    takeable = q["pickup"]
    residue  = q["drive_scope"][pickup.TRIAGE_IN_SCOPE_KEY]

``not takeable`` **alone is not "done"**, and that is not an opinion: T-0829
wrote ``test_an_empty_pickup_band_alone_would_call_a_scope_done_that_is_not``,
which drives the naive predicate over a scope holding two open tickets that need
a human and asserts it answers True. The obvious implementation of this ticket
is pinned as a LIE in the lane below it. Hence three distinct stopped states:

============================  =======================================
``reason``                    what is true
============================  =======================================
:data:`STOP_DONE`             nothing takeable, nothing awaiting a human
:data:`STOP_TRIAGE_BLOCKED`   nothing takeable, but N in-scope tickets need HIM
:data:`STOP_QUOTA_SPENT`      the budget is consumed (``spend_quota`` only)
============================  =======================================

**The residue state is neither silent nor the done-alert** (the judgement the TL
handed L4, decided here). It gets its own message whose FIRST LINE denies «ВСЁ
СДЕЛАНО». Silence there would reproduce the very failure this ticket closes: on
a board whose every remaining in-scope ticket needs a decision, the done-alert
never fires and he learns of the idleness by asking. Both states are idleness;
only the cause differs. Merging them would post «ВСЁ СДЕЛАНО» over an unfinished
board, which is how a channel he asked for so he could TRUST it becomes one he
mutes.

THE BOARD IT CANNOT SEE — the unknown that fires 1440 times a day
------------------------------------------------------------------
The same explicit-UNKNOWN discipline the spend signal gets, applied to the
board, because a 60s tick is the worst place to get it wrong. An empty queue and
an unreadable one are the same shape to a consumer that only calls ``len()``,
and confusing them posts «ВСЁ СДЕЛАНО» *because the tick could not see the
board* — a confident false statement into the one channel he asked for so he
could trust it. :func:`_read_board` therefore CHECKS the queue rather than
merely reading it (a raise, a non-dict, ``ok`` not True, a missing band, a
missing residue count), and any problem suppresses every stop AND leaves the
latch untouched: a blind tick is not evidence that work resumed either.

THE UNKNOWN SPEND — the most dangerous line in D-0069, implemented
------------------------------------------------------------------
There is no ``_quota.json`` anchor today, so ``spend_pct`` is unresolvable
(measured on T-0783 by p455; T-0695). An unknown spend must NOT read as "the
quota is not spent yet, keep going" — that absent value is indistinguishable
from a real negative and here it means *drive forever*.

So :func:`resolve_stop_when` resolves an unreadable spend to an explicit
UNKNOWN, **refuses to let ``spend_quota`` be the active stopping condition**
(``refused=True``, ``unknown_reason`` naming why), and falls back to
``scope_exhausted``. Setting the mode is allowed and recorded; ACTING on it
without an anchor is not. The fallback direction is the safe one: a condition
that stops when the work runs out can never run away.

**The threshold is 100% of the budget anchor, never
``weekly_quota_target_pct``.** D-0069 Q4: the target is a RATE control (it
adjusts concurrency) and this is a TERMINUS (it decides when to stop). Keying
the terminus off the rate control is exactly the conflation he corrected on
2026-07-29T12:35Z — «у нас уже есть quota target, не спутайте».

**One departure from D-0069, stated because the TL asked to be contradicted with
the reasoning.** The doc says ``spend_quota`` keeps driving "regardless of the
scope emptying". Here it ADDS a terminus and never REMOVES the scope-exhausted
one: with an empty scope there is nothing left to spend the budget on, so
reporting "not stopped" would leave the system idle and silent with an alert
configured — the same silent-idle shape the doc forbids on the unknown-spend
path, reached by a different door. ``stopped`` is therefore (budget consumed) OR
(scope stop reached), and ``reason`` names which.

SUPPRESSION (T-0800 DoD item 5) — edge-triggered, and stated
------------------------------------------------------------
A level-triggered read of "nothing to drive" fires every 60s for ever, which is
the named failure and the one that would make him mute the channel. The latch
(``data/<slug>/_worker/drive_stop/state.json``) holds the reasons already
announced in the CURRENT idle episode:

* fire only when ``reason`` is not in the latch;
* the latch is CLEARED — re-armed — the moment the drive is not stopped, i.e.
  when the scope becomes non-empty again;
* so an idle episode costs at most one message per distinct reason (two, and
  only if a residue episode is later triaged down to a genuinely empty board —
  which is real news, not a repeat), never one per tick.

Three further suppressions, each a deliberate choice rather than an omission:

* **A user-paused operator is silent.** He caused that idleness and already
  knows. The latch is deliberately NOT cleared while paused, so resuming into
  the same idle state does not re-announce it.
* **A send that did not land does not consume the edge.** The alert rides
  ``urgent=False``, so :func:`tg.TgClient.send` DROPS it during his sleep window
  and returns False. Consuming the latch there would lose the alert for the
  whole episode; instead the next tick retries and it arrives when quiet hours
  end. That is the pair worth having — no 03:00 page saying "we are idle", and
  no alert silently eaten either. (Routing class and quiet hours are
  independent axes; see :mod:`msg_routes`.)
* **No configured chat → no attempt.** Reported as an action, not retried into
  a log every minute.

DESTINATION (T-0800 DoD item 3) — never a hardcoded DM
-------------------------------------------------------
:data:`MSG_TYPE` is a registered :mod:`msg_routes` type, urgency **URGENT** by
that module's own definition — "work is stopped unless a human acts" is
literally what this message says. The caller's default destination is the
project's own chat + ``team_queries`` topic, exactly like every sibling
per-project page; the map may replace it once he points the type somewhere.

Kill switch: ``BOT_SQUAD_DRIVE_STOP_ALERT=0`` disables the tick entirely.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: The registered :mod:`msg_routes` type this alert is sent as. URGENT class.
MSG_TYPE = "drive_stopped"

#: The ``on_stop`` value that turns this module on (``pace.DRIVE_ON_STOP``).
ON_STOP_ALERT = "alert"

#: The ``stop_when`` values (``pace.DRIVE_STOP_WHEN``), named so this module's
#: branches read as the axis rather than as string literals.
STOP_WHEN_SCOPE = "scope_exhausted"
STOP_WHEN_QUOTA = "spend_quota"

#: Why the drive is stopped. Three DISTINCT facts — see the module docstring;
#: conflating the first two is the failure this whole bundle exists to prevent.
STOP_DONE = "done"
STOP_TRIAGE_BLOCKED = "blocked_on_triage"
STOP_QUOTA_SPENT = "quota_spent"

#: «Потратить квоту» = the budget is CONSUMED. 100% of the operator-set anchor,
#: deliberately NOT ``weekly_quota_target_pct`` — that is the rate control, and
#: keying a terminus off it is the conflation he corrected (D-0069 Q4).
SPEND_SPENT_PCT = 100.0

#: Named reasons a spend figure is UNKNOWN. Stored beside the value rather than
#: collapsing to a bare None, so a surface can say WHY it refused instead of
#: showing a blank (the explicit-UNKNOWN rule).
UNKNOWN_NO_ANCHOR = "no-quota-anchor"
UNKNOWN_UNREADABLE = "quota-signal-unreadable"

#: Named reasons the BOARD could not be read — the second unknown, and the one
#: that fires 1440 times a day. An empty answer and an unanswerable question look
#: identical here, and getting them confused posts «ВСЁ СДЕЛАНО» because the tick
#: could not see the board. Both suppress every stop; see :func:`_read_board`.
BOARD_UNREADABLE = "board-unreadable"
BOARD_MALFORMED = "board-malformed"


def _enabled() -> bool:
    """False iff the kill switch (``BOT_SQUAD_DRIVE_STOP_ALERT``) disables it."""
    raw = os.environ.get("BOT_SQUAD_DRIVE_STOP_ALERT")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# The edge-trigger latch — this module's ONLY state, and it is not a setting
# ---------------------------------------------------------------------------

def state_path(cfg: Any, slug: str) -> Path:
    """Where the per-project latch lives (``_worker/<feature>/`` layout)."""
    return Path(cfg.data_dir) / slug / "_worker" / "drive_stop" / "state.json"


def _load_state(cfg: Any, slug: str) -> dict:
    p = state_path(cfg, slug)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt latch must not wedge the alert. Treating it as empty
        # re-arms, i.e. costs at most ONE duplicate message — the cheap
        # direction, versus a permanently silenced channel.
        log.warning("drive_stop: unreadable latch at %s — treating as re-armed", p)
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(cfg: Any, slug: str, state: dict) -> None:
    p = state_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def fired_reasons(cfg: Any, slug: str) -> list[str]:
    """Which stop reasons have already been announced in the current idle
    episode. Empty = armed."""
    st = _load_state(cfg, slug)
    got = st.get("fired")
    return [str(x) for x in got] if isinstance(got, list) else []


def rearm(cfg: Any, slug: str) -> bool:
    """Clear the latch — the next stop announces itself again. True iff one was
    actually set (so a caller can report a re-arm without a spurious write)."""
    p = state_path(cfg, slug)
    if not p.exists():
        return False
    try:
        p.unlink()
    except OSError:  # pragma: no cover — a doomed unlink costs one duplicate
        log.exception("drive_stop: could not clear the latch at %s", p)
        return False
    return True


# ---------------------------------------------------------------------------
# AXIS B — resolving the stopping condition
# ---------------------------------------------------------------------------

def resolve_stop_when(cfg: Any, slug: str, drive: Optional[dict] = None) -> dict:
    """Which stopping condition is ACTUALLY in force, and whether it is reached.

    ``drive`` is a :func:`pace.read_drive` block; read here when not supplied.

    Returns::

        {"configured": str,          # what he set (or the default)
         "effective": str,           # what will actually be acted on
         "refused": bool,            # configured != effective, and why below
         "known": bool,              # is the spend signal resolvable at all
         "unknown_reason": str|None, # UNKNOWN_* — never a silent None
         "spend_pct": float|None,
         "threshold_pct": float|None,
         "spent": bool}              # budget consumed (False whenever unknown)

    ``spend_quota`` with no readable spend does not become "not spent yet, keep
    going" — it is REFUSED as the active condition and ``effective`` falls back
    to ``scope_exhausted``. See the module docstring; that fallback direction is
    what makes an unresolvable signal cost a narrower stop rather than an
    unbounded drive. Never raises: this sits on a scheduler tick.
    """
    if drive is None:
        from bot_squad_worker import pace as _pace
        drive = _pace.read_drive(cfg, slug)
    configured = str(drive.get("stop_when") or STOP_WHEN_SCOPE)

    base = {
        "configured": configured,
        "effective": configured,
        "refused": False,
        "known": True,
        "unknown_reason": None,
        "spend_pct": None,
        "threshold_pct": None,
        "spent": False,
    }
    if configured != STOP_WHEN_QUOTA:
        return base

    # The ONE spend signal (D-0069 Q2): the same figure `bsq pace show` prints.
    # Read through the PUBLIC seam so no second spend accounting exists — and
    # so this module never reaches into operator_redrive's privates.
    spend_pct: Optional[float] = None
    unknown: Optional[str] = None
    try:
        from bot_squad_worker import operator_redrive as _ord
        spend_pct = _ord.pacing_status(cfg, slug).get("spend_pct")
    except Exception:  # noqa: BLE001 — a pacing read must never break the tick
        log.exception("drive_stop: pacing read failed for %s", slug)
        unknown = UNKNOWN_UNREADABLE

    if spend_pct is None:
        # TODAY'S REAL CASE, not an edge (T-0695): no `[quota]` anchor exists,
        # so there is no total to measure spend against.
        base.update({
            "effective": STOP_WHEN_SCOPE,
            "refused": True,
            "known": False,
            "unknown_reason": unknown or UNKNOWN_NO_ANCHOR,
        })
        return base

    base.update({
        "spend_pct": float(spend_pct),
        "threshold_pct": SPEND_SPENT_PCT,
        "spent": float(spend_pct) >= SPEND_SPENT_PCT,
    })
    return base


def _read_board(cfg: Any, slug: str, now_epoch: Optional[float]) -> tuple[
        Optional[dict], Optional[str]]:
    """``(queue, problem)`` — the board, or a NAMED reason it is not knowable.

    **The second explicit-UNKNOWN in this module, and the one that fires 1440
    times a day.** An empty board and an unreadable one are the same shape to a
    consumer that only calls ``len()``: both look like "nothing to drive". Here
    that mistake posts «ВСЁ СДЕЛАНО» *because the tick could not see the board*,
    which is worse than any missed alert — it is a confident false statement
    into the one channel he asked for so he could trust it.

    So the queue is not merely read, it is CHECKED: a raise, a non-dict, an
    ``ok`` that is not True, a missing pickup list or a missing residue count
    each resolve to a problem, and a problem suppresses every stop rather than
    defaulting to "done". Silence plus a log is the cheap direction to be wrong
    in; a false all-clear is not.
    """
    from bot_squad_worker import pickup as _pickup

    try:
        q = _pickup.pickup_queue(cfg, slug, now_epoch=now_epoch)
    except Exception:  # noqa: BLE001 — an unreadable board is a state, not a crash
        log.exception("drive_stop: pickup queue read failed for %s", slug)
        return None, BOARD_UNREADABLE
    if not isinstance(q, dict) or q.get("ok") is not True:
        log.error("drive_stop: pickup queue for %s did not report ok — not "
                  "treating an unanswerable board as an empty one", slug)
        return None, BOARD_MALFORMED
    ds = q.get("drive_scope")
    if not isinstance(q.get("pickup"), list) or not isinstance(ds, dict):
        log.error("drive_stop: pickup queue for %s is missing pickup/drive_scope",
                  slug)
        return None, BOARD_MALFORMED
    if not isinstance(ds.get(_pickup.TRIAGE_IN_SCOPE_KEY), int):
        # Without the residue count the ONLY predicate left is the naive one
        # T-0829 pinned as a lie. Refuse rather than fall back to it.
        log.error("drive_stop: pickup queue for %s carries no %s — the residue "
                  "is what separates 'done' from 'nothing takeable'",
                  slug, _pickup.TRIAGE_IN_SCOPE_KEY)
        return None, BOARD_MALFORMED
    return q, None


def evaluate(cfg: Any, slug: str, *, now_epoch: Optional[float] = None,
             drive: Optional[dict] = None) -> dict:
    """Is the drive stopped, and why — a PURE read (no state, no send).

    Returns the record :func:`alert_text` renders and :func:`tick` decides on::

        {ok, slug, stopped, reason, board_problem, takeable, triage_in_scope,
         out_of_scope, scope, scope_problem, scope_configured, on_stop,
         stop_when, set_by, set_at, source_text, invalid}

    ``reason`` is None exactly when ``stopped`` is False. A non-None
    ``board_problem`` forces both — see :func:`_read_board`. The scope is NOT
    passed to ``pickup_queue``: it reads the standing per-project setting
    itself, which is why this alert honours «какой режим драйва щас стоит»
    without owning a copy of the scope logic.
    """
    from bot_squad_worker import pace as _pace
    from bot_squad_worker import pickup as _pickup

    if drive is None:
        drive = _pace.read_drive(cfg, slug)
    q, board_problem = _read_board(cfg, slug, now_epoch)
    ds = (q.get("drive_scope") if q else None) or {}
    takeable = len(q.get("pickup")) if q else 0
    residue = int(ds.get(_pickup.TRIAGE_IN_SCOPE_KEY) or 0)
    stop = resolve_stop_when(cfg, slug, drive)

    # The scope-side stop, per the predicate handed down by T-0829. The residue
    # is what separates the two: an empty pickup band ALONE is not "done".
    scope_reason: Optional[str] = None
    if takeable == 0:
        scope_reason = STOP_DONE if residue == 0 else STOP_TRIAGE_BLOCKED

    # spend_quota ADDS a terminus; it never removes the scope one (the stated
    # departure from D-0069 — see the module docstring). Budget-consumed wins
    # the naming when both hold, because it is the condition he chose.
    if stop["effective"] == STOP_WHEN_QUOTA and stop["spent"]:
        reason: Optional[str] = STOP_QUOTA_SPENT
    else:
        reason = scope_reason

    if board_problem is not None:
        # EVERY stop is suppressed, the quota terminus included: its message
        # quotes board counts we do not have, and "the budget is gone" is not
        # worth announcing beside numbers we cannot stand behind.
        reason = None

    return {
        "ok": board_problem is None,
        "slug": slug,
        "stopped": reason is not None,
        "reason": reason,
        "board_problem": board_problem,
        "takeable": takeable,
        "triage_in_scope": residue,
        "out_of_scope": int(ds.get("out_of_scope") or 0),
        "scope": ds.get("scope"),
        "scope_problem": ds.get("problem"),
        # Reported, never branched on: an absent block and an explicit
        # `scope: all` must behave identically (T-0828's trap #1).
        "scope_configured": bool(ds.get("configured")),
        "on_stop": str(drive.get("on_stop") or "nothing"),
        "stop_when": stop,
        "set_by": drive.get("set_by"),
        "set_at": drive.get("set_at"),
        "source_text": drive.get("source_text"),
        # The RAW rejected values, never the fallback dressed up as the setting
        # (T-0828's trap #2).
        "invalid": dict(drive.get("invalid") or {}),
    }


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------
# HIS WORDS ARE THE HEADLINE, verbatim and in his capitals. The body below it is
# the context that makes the headline checkable — which scope, set by whom, from
# which of his own sentences. `test_drive_stop.py` pins all three headlines and
# the phrase «МЫ ПРОСТАИВАЕМ» (T-0800 DoD item 4, precedent T-0795).

#: His own words, exactly as he wrote them on 2026-07-30T09:45:35Z. HUMAN-ONLY —
#: this string is the ask, not a label; do not "improve" it.
HEADLINE_DONE = "ВСЁ СДЕЛАНО, ПРОВЕРЯЙ, МЫ ПРОСТАИВАЕМ"

#: The residue case. It DENIES the done headline in its own first line rather
#: than softening it — the two facts must not be confusable at a glance in a
#: notification list, which is where he will actually read them.
HEADLINE_TRIAGE_BLOCKED = "МЫ ПРОСТАИВАЕМ, НО ЭТО НЕ «ВСЁ СДЕЛАНО»"

#: «Потратить квоту» reached its terminus.
HEADLINE_QUOTA_SPENT = "КВОТА ПОТРАЧЕНА, МЫ ПРОСТАИВАЕМ"

HEADLINES = {
    STOP_DONE: HEADLINE_DONE,
    STOP_TRIAGE_BLOCKED: HEADLINE_TRIAGE_BLOCKED,
    STOP_QUOTA_SPENT: HEADLINE_QUOTA_SPENT,
}


def _plural_task(n: int) -> str:
    """Russian task-count agreement — «1 задача / 2 задачи / 5 задач»."""
    tail_100 = n % 100
    tail_10 = n % 10
    if 11 <= tail_100 <= 14:
        return f"{n} задач"
    if tail_10 == 1:
        return f"{n} задача"
    if 2 <= tail_10 <= 4:
        return f"{n} задачи"
    return f"{n} задач"


def _agree(n: int) -> tuple[str, str]:
    """``(verb, pronoun)`` agreeing with a count of «задача». One ticket needing
    a decision is the commonest case there is, and «1 задача ждут» reads as a
    machine talking — this message is asking him to trust it."""
    if n % 10 == 1 and n % 100 != 11:
        return "ждёт", "её"
    return "ждут", "их"


def alert_text(rec: dict) -> str:
    """Render the message for a STOPPED record. Raises ``ValueError`` on a
    record that is not stopped — a text with no fact under it is the thing this
    ticket is trying to stop being sent."""
    reason = rec.get("reason")
    if not rec.get("stopped") or reason not in HEADLINES:
        raise ValueError(
            f"drive_stop.alert_text: nothing to announce for {rec.get('slug')!r} "
            f"(stopped={rec.get('stopped')!r}, reason={reason!r})"
        )
    slug = rec.get("slug") or "?"
    stop = rec.get("stop_when") or {}
    lines = [HEADLINES[reason], ""]

    if reason == STOP_DONE:
        lines.append(
            f"{slug}: в драйве не осталось задач — взять нечего, "
            f"и ничего не ждёт твоего решения."
        )
    elif reason == STOP_TRIAGE_BLOCKED:
        verb, pron = _agree(rec["triage_in_scope"])
        lines.append(
            f"{slug}: взять нечего, но {_plural_task(rec['triage_in_scope'])} "
            f"в scope {verb} твоего решения — драйв {pron} сам не возьмёт."
        )
    else:
        pct = stop.get("spend_pct")
        pct_s = f"{float(pct):.0f}%" if pct is not None else "?"
        lines.append(
            f"{slug}: драйв остановлен по «потратить квоту» — "
            f"потрачено {pct_s} бюджета. "
            f"В очереди: {_plural_task(rec['takeable'])}, "
            f"ждут решения: {rec['triage_in_scope']}."
        )

    lines.append(
        f"Режим драйва: scope={rec.get('scope') or '?'}, "
        f"stop_when={stop.get('configured')}, on_stop={rec.get('on_stop')}"
    )
    # The refusal is stated IN the message, not only in a log: he set
    # spend_quota and something else decided the stop, and a surface that hides
    # that is the silent-fallback defect wearing a different hat.
    if stop.get("refused"):
        lines.append(
            f"⚠ stop_when={stop.get('configured')} не применён "
            f"({stop.get('unknown_reason')}): расход квоты сейчас неизвестен, "
            f"поэтому остановка считается по {stop.get('effective')}."
        )
    for field, raw in sorted((rec.get("invalid") or {}).items()):
        # The RAW rejected value, so he sees his own typo (T-0828's trap #2).
        lines.append(f"⚠ drive.{field}={raw!r} — не из допустимого набора, взят дефолт.")
    if rec.get("scope_problem"):
        lines.append(f"⚠ scope: {rec['scope_problem']} — драйв идёт по самому широкому.")
    if rec.get("source_text"):
        who = rec.get("set_by") or "?"
        when = rec.get("set_at") or "?"
        lines.append(f"Задан {when} ({who}): «{rec['source_text']}»")
    if rec.get("out_of_scope"):
        _, pron = _agree(rec["out_of_scope"])
        lines.append(
            f"Вне scope: {_plural_task(rec['out_of_scope'])} — "
            f"этот режим драйва {pron} не трогает."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _send(cfg: Any, slug: str, text: str) -> bool:
    """Deliver via the paging SSOT. True iff it actually went out.

    ``urgent=False`` on purpose: this is real news but it is not worth waking
    him at 03:00 to be told the queue is empty. The quiet-hours gate DROPS it
    and returns False, and :func:`tick` does not consume the latch on a False —
    so it lands when the window ends instead of being lost. Destination and
    quiet hours are independent axes (:mod:`msg_routes`), so the URGENT routing
    class is unaffected by this.
    """
    from bot_squad_worker.actions import _send_stakeholder_dm
    from bot_squad_worker import tg_topics as _tg_topics

    project = (getattr(cfg, "projects", {}) or {}).get(slug)
    chat_id = getattr(project, "tg_chat", "") if project else ""
    try:
        res = _send_stakeholder_dm(
            cfg,
            message=text,
            sid="drive-stop",
            # Renders `[<slug> drive-stop]`: this alert is ABOUT a project and
            # he may have several (the T-0758 lesson).
            slug=slug,
            urgent=False,
            tg_chat_id=chat_id,
            tg_topic_id=_tg_topics.resolve(cfg, slug, "team_queries"),
            # T-0800 DoD item 3 / T-0799: the destination is the per-type map,
            # never a hardcoded chat. The pair above is only the DEFAULT it
            # falls back to.
            #
            # The LITERAL, not :data:`MSG_TYPE`, and deliberately so:
            # `test_msg_routes.py`'s enumeration guard is an AST scan for
            # `msg_type=` string literals, so a constant here would make this
            # emitter invisible to it and let a registered type exist that
            # nothing sends. The duplication is pinned instead — the delivered
            # kwarg is asserted equal to MSG_TYPE in `test_drive_stop.py`.
            msg_type="drive_stopped",
            # The text is self-contained; nothing to point at.
            do_slim=False,
        )
    except Exception:  # noqa: BLE001 — a failed page must not kill the sweep
        log.exception("drive_stop: alert send failed for %s", slug)
        return False
    return bool(res.get("sent"))


def tick(cfg: Any, slug: str, *, now_epoch: Optional[float] = None) -> dict:
    """One stopping-condition pass for one project. Returns ``{action, ...}``.

    actions: ``disabled`` | ``off`` | ``paused`` | ``board-unknown`` |
    ``running`` | ``rearmed`` | ``suppressed`` | ``no-destination`` |
    ``send-failed`` | ``sent``.

    The ``on_stop`` read comes FIRST and returns before any board scan, so a
    project that has not opted in costs one small JSON read per minute.

    ``board-unknown`` is deliberately NOT ``running`` and NOT ``rearmed``: an
    unreadable board is not evidence that work resumed, so it must neither
    announce a stop nor silently re-arm the latch into announcing the next one
    twice.
    """
    if not _enabled():
        return {"action": "disabled"}

    from bot_squad_worker import pace as _pace

    try:
        drive = _pace.read_drive(cfg, slug)
    except Exception:  # noqa: BLE001 — read_drive is documented not to raise
        log.exception("drive_stop: drive config unreadable for %s", slug)
        return {"action": "off", "reason": "config-unreadable"}
    if str(drive.get("on_stop") or "") != ON_STOP_ALERT:
        return {"action": "off"}

    # A user-paused operator is idle BY HIS OWN HAND. The latch is deliberately
    # left alone so resuming into the same idle state does not re-announce it.
    try:
        if _pace.read_config(cfg, slug).get("paused"):
            return {"action": "paused"}
    except Exception:  # noqa: BLE001
        log.exception("drive_stop: pause read failed for %s", slug)

    rec = evaluate(cfg, slug, now_epoch=now_epoch, drive=drive)
    if rec["board_problem"] is not None:
        # Say nothing AND touch nothing. See the docstring above.
        return {"action": "board-unknown", "problem": rec["board_problem"]}
    if not rec["stopped"]:
        # RE-ARM. This is the only place the latch is cleared, which is what
        # makes the trigger an EDGE: the next stop is announced, and the ticks
        # in between announce nothing.
        return {"action": "rearmed" if rearm(cfg, slug) else "running",
                "takeable": rec["takeable"]}

    reason = rec["reason"]
    already = fired_reasons(cfg, slug)
    if reason in already:
        return {"action": "suppressed", "reason": reason}

    project = (getattr(cfg, "projects", {}) or {}).get(slug)
    if not (getattr(project, "tg_chat", "") if project else ""):
        # Nothing to send TO. Reported rather than attempted, so an unconfigured
        # project does not log a failed page every 60s for ever.
        log.debug("drive_stop: %s has no tg_chat — no destination for the alert", slug)
        return {"action": "no-destination", "reason": reason}

    if not _send(cfg, slug, alert_text(rec)):
        # Quiet hours, or a transport failure. The latch is NOT consumed — the
        # next tick retries, so the alert arrives late rather than never.
        return {"action": "send-failed", "reason": reason}

    st = _load_state(cfg, slug)
    fired = list(already)
    fired.append(reason)
    st["fired"] = fired
    st["last_reason"] = reason
    st["last_sent_at"] = time.time()
    _save_state(cfg, slug, st)
    log.info("drive_stop[%s]: alerted — %s (takeable=%d, triage_in_scope=%d)",
             slug, reason, rec["takeable"], rec["triage_in_scope"])
    return {"action": "sent", "reason": reason}


def drive_stop_tick(cfg: Any) -> None:
    """Scheduler entry point: one pass across every project.

    Per-project errors are caught and logged so one bad project never kills the
    sweep — the same contract as every sibling lifecycle tick.

    **A blind tick says so out loud.** Every other action here is correctly
    silent, and a suppressed stop that logged nothing would be indistinguishable
    from a tick that correctly found work in flight — the two are different
    facts, and telling them apart is the entire point of the gate. Nobody audits
    1440 quiet no-ops a day, so the refusal is raised to WARNING here rather
    than left in :func:`tick`'s return value, which nothing surfaces.
    """
    for slug in getattr(cfg, "projects", {}) or {}:
        try:
            res = tick(cfg, slug)
        except Exception:  # noqa: BLE001
            log.exception("drive_stop_tick: unhandled error for project %s", slug)
            continue
        if res.get("action") == "board-unknown":
            log.warning(
                "drive_stop[%s]: REFUSING to judge the drive — the board is "
                "not readable (%s). No alert either way: 'nothing to drive' "
                "and 'cannot tell what there is to drive' are different facts.",
                slug, res.get("problem"))
