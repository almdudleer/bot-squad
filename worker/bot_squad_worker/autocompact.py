"""T-0333 / T-0334 / T-0467: self-healing context compaction — a CLOSED LOOP.

Doctrine (docs/design/closed-loops-principle.md): every process must have an
explicit downstream owner that closes it. Context growth used to leak two ways —
(1) telemetry pinged the *human* to "plan a /compact" (a system action dumped on
a person), and (2) the operator session had NO compaction mechanism at all and
grew until the harness surprise-compacted it mid-orchestration.

This module is the owner of that close: when a session's context crosses the
ceiling, the SYSTEM recycles it. The human is NOT in this loop. The operator
session is a first-class member (T-0334) — no session is exempt from its own
close.

Two compaction strategies, both gated on an **idle** pane whose composer is ready
(the ``❯`` rune, no "esc to interrupt" mid-generation marker) — we never interrupt
active work, a Ctrl-C'd (paused) pane, or a confirmation dialog:

* **handoff (T-0467, default)** — OUR custom compact and the process-paradigm
  lifecycle (clarification-01: "the session is asked to write down everything an
  in-system artifact + clear the context"). A 2-phase, cross-tick state machine:

    ARM      over-ceiling + idle + composer-ready & a handoff destination
             resolves → inject the HANDOFF prompt (write your full forward-state
             THERE) + stamp ``rec['compact'] = {phase: writing}``.
    FINALIZE phase==writing & the forward-state was written & the pane is idle →
             ``suspend`` the old pane (CLEAR the context) + relaunch a FRESH
             incarnation that boots from what was written, re-bound to the SAME
             assignment (binding continuity). No session relies on retained
             in-context memory across the compact.

  **WHERE the forward-state goes is decided by whether the session owns a TASK**
  (T-0863, stakeholder 2026-08-11): *«он должен просто обновлять контекст по
  задаче, и всё, никаких файлов, никаких notes, на одну задачу один артефакт —
  контекст, он же на тикете в UI виден»*.

    task-bound  → the ticket's own ``## Context`` (T-0767's ``task_context_set``
                  writer, driven by the session as ``bsq ticket context <id>``).
                  ONE artifact per task, already rendered on the board. NO
                  ``artifacts/<task_id>.md``, NO ``bsq compact-save``, and NO
                  auto ``## Progress`` note.
    task-less   → the role artifact, unchanged: an operator's state-doc
                  (``artifacts/operator-state.md``, T-0473) or a per-assignment
                  ``artifacts/role-<role>-<window>.md``. These sessions have no
                  task to write a Context onto, so the artifact seam is what
                  still gives them a home (see :func:`_resolve_compact_target`).

  The reload paths (``boot_prompt_from_ticket`` / ``boot_prompt_from_artifact``,
  ``_relaunch_from_ticket`` / ``_relaunch_from_artifact``) mirror that split;
  the artifact half is factored for reuse by T-0471 (crash recovery).

* **claude (legacy, fallback)** — Claude's in-context ``/compact``. Used when no
  handoff destination resolves at all, when an armed handoff produced NO
  forward-state at all within :func:`handoff_timeout_sec` (never wedge), or when
  ``BOT_SQUAD_COMPACT_MODE=claude`` forces it. A per-session cooldown stops a
  re-/compact while a compaction is still in flight (it rotates the transcript,
  so context only resets a tick or two later).

  T-0905 narrowed the timeout half of that list, and the narrowing is the whole
  ticket: a handoff whose forward-state IS written and is only waiting for a
  quiet pane must NOT fall back. ``/compact`` is gated on the same idle,
  composer-ready pane the finalize is, so the fallback can never take context
  down earlier than the clean relaunch would have — it can only burn a full
  summarization at the moment the clean path became available. See
  :func:`_maybe_finalize`.

Neither strategy above ever runs against the human's own exempt sessions
(:func:`recycle_gate.user_session_exempt`) — T-0649 (2026-07-18) gives them a
third path instead: :func:`_maybe_compact_stay_ceiling` reuses
:mod:`idle_timeout`'s T-0617 compact-in-place machine (``compact_stay_*`` md
fields), so the ceiling trigger built here and the idle-window trigger built
there share ONE anti-loop guard (``compact_stay_last_at``) and can never
double-compact the same session. Worker sessions (dev/TL/operator) are
unaffected — the stakeholder's "autocompact loop is worse" complaint never
targeted them, and the handoff/artifact model above IS their designed
continuity mechanism.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from bot_squad_worker import assignment, recycle_gate

log = logging.getLogger(__name__)

# Don't re-/compact a session within this window — a compaction rotates the
# transcript (new claude_uuid) and context only reads back down a tick later.
DEFAULT_COMPACT_COOLDOWN_SEC = 600  # 10 min

# How long to wait for a session to write its forward-state after the handoff
# prompt before falling back to Claude's /compact (never wedge over-ceiling).
# T-0905 narrowed what this deadline decides: it now only bounds the wait for a
# session that has written NOTHING. 900s is generous for that question — over
# the 6 days of worker journal that ticket measured, all 74 completed handoffs
# had their forward-state on disk within 719s of ARM.
DEFAULT_HANDOFF_TIMEOUT_SEC = 900  # 15 min

# T-0905: the SECOND deadline, for the other half of the split — a session whose
# forward-state IS written but whose pane has not gone idle+composer-ready yet.
# Waiting there is free (the state is safe on disk) and is strictly better than
# /compact, so the wait is long; this is the "never wedge" cap on it, after
# which the finalize is forced even against a busy pane. Sized off the measured
# worst case: S-almdudleer-operator-p355 needed 1194s of ARM-to-composer-ready
# on 2026-08-18, so 2700s is ~2.3x the longest busy stretch ever observed.
DEFAULT_HANDOFF_HARD_TIMEOUT_SEC = 2700  # 45 min


def autocompact_enabled() -> bool:
    """Master switch (default ON). ``BOT_SQUAD_AUTOCOMPACT=0`` disables the loop
    (the leak returns, but it's a deliberate operator kill-switch for incidents)."""
    return os.environ.get("BOT_SQUAD_AUTOCOMPACT", "1") != "0"


def compact_mode() -> str:
    """Which compaction strategy to use: ``handoff`` (T-0467, default) or
    ``claude`` (legacy in-context /compact). ``BOT_SQUAD_COMPACT_MODE`` overrides;
    anything unrecognised falls back to ``handoff`` so a typo can't silently
    disable the paradigm lifecycle."""
    v = (os.environ.get("BOT_SQUAD_COMPACT_MODE") or "handoff").strip().lower()
    return v if v in ("handoff", "claude") else "handoff"


def handoff_timeout_sec() -> int:
    """Deadline for a session to write its forward-state after being asked.
    Overridable via ``BOT_SQUAD_HANDOFF_TIMEOUT_SEC``; non-positive/garbage →
    default."""
    raw = os.environ.get("BOT_SQUAD_HANDOFF_TIMEOUT_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_HANDOFF_TIMEOUT_SEC


def handoff_hard_timeout_sec() -> int:
    """T-0905: cap on the "state written, pane still busy" wait — after this the
    handoff is finalized against a busy pane rather than held open forever.

    Never below :func:`handoff_timeout_sec`: a hard cap under the soft one would
    invert the two halves of the split (a session would be force-finalized
    before the "wrote nothing" branch it belongs to could even be evaluated).
    Overridable via ``BOT_SQUAD_HANDOFF_HARD_TIMEOUT_SEC``; non-positive/garbage
    → default.
    """
    raw = os.environ.get("BOT_SQUAD_HANDOFF_HARD_TIMEOUT_SEC")
    v = DEFAULT_HANDOFF_HARD_TIMEOUT_SEC
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                v = parsed
        except (TypeError, ValueError):
            pass
    return max(v, handoff_timeout_sec())


def compact_cooldown_sec() -> int:
    """Per-session /compact cooldown. Overridable via
    ``BOT_SQUAD_COMPACT_COOLDOWN_SEC``; non-positive/garbage → default."""
    raw = os.environ.get("BOT_SQUAD_COMPACT_COOLDOWN_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_COMPACT_COOLDOWN_SEC


# --- pure decision helpers --------------------------------------------------

def compact_safe(activity: str) -> bool:
    """True only for an ``idle`` pane — Claude finished its turn and is awaiting
    input. ``running`` (mid-turn), ``paused`` (Ctrl-C'd) and ``suspended`` (no
    live pane) are all unsafe to /compact."""
    return activity == "idle"


def compact_due(level: str, fired_at: dict, now: float, cooldown: int | None = None) -> bool:
    """True when context is at the compaction threshold (``urgent`` = at/above the
    ceiling) AND we haven't compacted within the cooldown window."""
    if level != "urgent":
        return False
    if cooldown is None:
        cooldown = compact_cooldown_sec()
    last = (fired_at or {}).get("compact")
    if last is None:
        return True
    try:
        return (now - float(last)) >= cooldown
    except (TypeError, ValueError):
        return True


def composer_ready(buf: str, *, sid: str | None = None,
                   now: float | None = None) -> bool:
    """True when the captured pane buffer shows the composer ready for input.

    The ``❯`` rune is rendered by Claude Code's input box only when it accepts
    keystrokes (T-0126). A mid-generation pane shows an "esc to interrupt"
    marker — never /compact then, even if a stale ``❯`` lingers in scrollback.

    T-0864: pass ``sid`` (and the tick's ``now``) from a RECYCLE path to get one
    debounced INFO line naming which of the two reasons deferred this tick — a
    permission dialog or a stale render can hold this gate closed for hours, and
    it used to be as silent as ``recycle_gate.is_attached``. Callers that are
    not a recycle gate — ``input_mux``, which reuses this predicate per keystroke
    batch to decide whether a pane can be typed into — pass no ``sid`` and log
    nothing; a "not ready" there is the normal case, not a deferred recycle.
    The return value is unchanged either way: this stays a pure predicate over
    ``buf``.
    """
    if not buf or "❯" not in buf:
        _log_not_composer_ready(sid, "no ❯ composer rune in the captured pane "
                                "(empty capture, or a dialog/alt-screen over it)",
                                now)
        return False
    if "esc to interrupt" in buf.lower():
        _log_not_composer_ready(sid, "pane is mid-generation "
                                "('esc to interrupt' on screen)", now)
        return False
    return True


def composer_free(buf: str, *, sid: str | None = None,
                  now: float | None = None) -> bool:
    """:func:`composer_ready` AND no human-typed text sitting in the composer.

    T-0930 (stakeholder, 2026-08-30, verbatim: «только не надо ее компактить,
    когда у меня текст во вводе»): every compact/handoff send must use THIS
    gate, not bare ``composer_ready`` — a composer showing ``❯`` with his
    half-typed message in it is "ready" by the T-0126 rune check, and
    ``deliver_direct``'s 3-second bounded typing wait then FORCES the send
    anyway (correct for a wake nudge that must never be dropped, exactly wrong
    for a compact that can simply wait a tick). Deferring here means the
    bounded-force path is never reached with his text at risk.
    """
    if not composer_ready(buf, sid=sid, now=now):
        return False
    from bot_squad_worker import input_mux
    if input_mux.user_is_typing(buf):
        _log_not_composer_ready(sid, "human-typed text is sitting in the "
                                "composer (T-0930: never compact over it)", now)
        return False
    return True


def ceiling_stay_enabled() -> bool:
    """T-0930: the ceiling trigger compacts IN PLACE (handoff checkpoint, then
    native /compact, session continues) instead of relaunching a fresh
    incarnation. Default ON — the stakeholder's 2026-08-30 ruling makes this
    the natural lifecycle («handoff + compact без exit было бы правильным
    поведением, если есть шанс что эта сессия будет продолжаться»).
    ``BOT_SQUAD_CEILING_COMPACT_STAY=0`` restores the relaunch behaviour."""
    return os.environ.get("BOT_SQUAD_CEILING_COMPACT_STAY", "1") != "0"


def _log_not_composer_ready(sid: str | None, reason: str,
                            now: float | None) -> None:
    """One debounced INFO line per session per window, matching
    ``recycle_gate.project_allowed``'s pattern (T-0864 DoD 3)."""
    if not sid:
        return
    if recycle_gate.should_log_skip(f"composer:{sid}", now):
        log.info("recycle: %s not composer-ready — %s; gate deferred this tick",
                 sid, reason)


# --- prompts (the "ask the session to write everything down" + the reload) --

def handoff_prompt(artifact_path: str, role: str | None = None, *,
                   relaunch: bool = True, stay: bool = False,
                   resume: bool = False) -> str:
    """The COMPACT HANDOFF — ask a TASK-LESS session to dump its full
    forward-state into its role artifact, then signal done.

    (A task-bound session takes :func:`context_handoff_prompt` instead — T-0863
    routes its forward-state to the ticket's ``## Context``.)

    ``role`` grafts role-specific shape guidance onto the generic handoff (T-0473:
    an operator must write the future-focused state-doc schema, not a free dump).
    The graft is a PURE ADDITION — for any role with no
    :func:`assignment.role_compact_guidance` (dev/teamlead/None) the returned
    prompt is byte-identical to the role-agnostic T-0467 handoff.

    ``relaunch=False`` is the ~1h idle-window trigger, which STOPS the session
    rather than booting a successor from what it wrote. Only the closing
    sentence differs, and it has to: telling a session it is about to be
    relaunched when it is about to be ended is the kind of small lie that
    changes what it bothers to write down.

    ``stay=True`` (T-0930, overrides ``relaunch``) is the compact-in-place
    ceiling trigger: after the write the SAME session continues with a
    compacted context — no exit, no fresh incarnation. Truthfulness matters
    here for the same reason as above, in the opposite direction: a session
    told it is dying writes a will; a session told it continues writes a
    checkpoint, and a checkpoint is what the trace is for (the stakeholder's
    2026-08-29 ruling: «наш компакт лучше встроенного, он оставляет след в
    системе» — the trace is audit + crash-net, not a successor's only memory).

    ``resume=True`` (T-0945, only meaningful with ``relaunch=False``) is the
    ``compact_exit`` plan: this session is ended AND resumed later with a
    compacted transcript. Same truthfulness rule a third time — the
    ``relaunch=False`` text says "NOTHING from this conversation survives",
    which would be a lie here and would make the session over-write a will it
    does not need. It is also where the stakeholder's *«система должна ей
    ставить дедлайн и предоставлять четкие критерии»* is delivered: the prompt
    states the bounded deadline and what happens on either side of it.
    """
    if stay:
        head = (
            "⏳ CONTEXT FULL — CHECKPOINT + COMPACT. Your context is over the "
            "ceiling and will be COMPACTED IN PLACE: this SAME session "
            "continues afterwards — no relaunch, no fresh incarnation. Details "
            "not in the compact summary survive ONLY in what you write to your "
            "role artifact now (your checkpoint, and the recovery net if this "
            "session ever dies unexpectedly). "
        )
        tail = ("The system then compacts your context in place and you "
                "continue working in THIS session.")
    else:
        head = (
            "⏳ CONTEXT FULL — COMPACT HANDOFF. Per the process-paradigm lifecycle you "
            "are about to be relaunched as a FRESH incarnation with an EMPTY context. "
            if relaunch else
            "⏳ CACHE WINDOW EXPIRING — COMPACT HANDOFF. Per the process-paradigm "
            "lifecycle this session is about to be ENDED. "
        )
        tail = ("The system then relaunches you fresh." if relaunch else
                "The system then ends this session; a later incarnation boots from "
                "what you wrote.")
        if resume and not relaunch:
            head = (
                "⏳ CACHE WINDOW EXPIRING — HANDOFF, COMPACT, EXIT, RESUME. Your "
                "~1h prompt cache is about to go cold while idle. The system will "
                "compact your context, end this session, and RESUME it "
                "(`claude --resume`) the next time there is something for you — "
                "so this conversation is not lost, it comes back summarized. "
            )
            tail = (
                f"The system then compacts you, ends the session, and resumes it "
                f"on the next message. You have about "
                f"{handoff_timeout_sec() // 60} minutes; after that the exit "
                f"happens with or without your write."
            )
    if stay:
        base = (
            f"{head}\n\n"
            "Write your CURRENT forward-state as a checkpoint: the "
            "assignment/goal, what's DONE, what's IN PROGRESS, the EXACT next "
            "steps, key file paths + decisions + gotchas — everything you'd "
            "need if the compact summary loses a detail.\n\n"
            "Run exactly:\n"
            '  bsq compact-save "<your full forward-state markdown>"\n\n'
            f"(It full-replaces your role artifact at {artifact_path}.) After it "
            f"returns ok, reply: HANDOFF WRITTEN. {tail}"
        )
    else:
        # T-0945: a resumed session's conversation is NOT lost, so the
        # "nothing survives" sentence would be a lie for it — and one that
        # changes what it writes. Its artifact still matters (a compact summary
        # is lossy, and anyone OTHER than the resumed session reads the file).
        stake = (
            "The compact summary is LOSSY, and anyone other than the resumed you "
            "reads your role artifact — so write it as if it were the only "
            "record.\n\n" if resume else
            "NOTHING from this conversation survives EXCEPT what you write to your "
            "role artifact now.\n\n"
        )
        base = (
            f"{head}"
            f"{stake}"
            "Write your COMPLETE forward-state so your successor continues seamlessly: "
            "the assignment/goal, what's DONE, what's IN PROGRESS, the EXACT next "
            "steps, key file paths + decisions + gotchas, and anything you'd need if "
            "you woke up fresh. Be exhaustive — this is your only memory.\n\n"
            "Run exactly:\n"
            '  bsq compact-save "<your full forward-state markdown>"\n\n'
            f"(It full-replaces your role artifact at {artifact_path}.) After it "
            f"returns ok, reply: HANDOFF WRITTEN. {tail}"
        )
    guidance = assignment.role_compact_guidance(role)
    return base + ("\n\n" + guidance if guidance else "")


def context_handoff_prompt(task_id: str, *, relaunch: bool,
                           stay: bool = False, resume: bool = False) -> str:
    """The FINALIZE handoff for a TASK-BOUND session — write the forward-state
    into the ticket's own ``## Context`` (T-0863).

    Replaces the ``bsq compact-save`` → ``artifacts/<task_id>.md`` indirection
    for every session that owns a task, on BOTH triggers (context ceiling and
    the ~1h idle window). The stakeholder's correction, verbatim: *«bsq
    compact-save, ждёт файл — кажется, тут лишний шаг, устаревший, он должен
    просто обновлять контекст по задаче, и всё, никаких файлов, никаких notes,
    на одну задачу один артефакт — контекст»*.

    ``relaunch`` is the only difference between the two triggers, and the
    session is told which one it is because the two mean different things to
    the successor: the ceiling path clears this pane and boots a fresh
    incarnation immediately, the idle path just stops (a later re-drive picks
    the ticket up). Either way the ticket is the whole inheritance.

    The prompt names ``## Stakeholder notes`` as off-limits on purpose — this
    is a write against the same md that holds his words, and the one failure
    mode worth a sentence of prompt is a degraded, context-full session
    rewriting them (T-0729 shipped exactly that, on 414 tickets).

    T-0863 also asks for ``## Executive summary``. Finalize is the RIGHT moment
    for it and close to the only reliable one: it is the one point in a
    session's life where it is required to state where the work stands, so
    hanging the status write here costs no new discipline. THE WAIT STILL
    WATCHES ONLY THE CONTEXT DIGEST, deliberately — a summary is one cheap
    paragraph, so gating on either-one-changed would let a session satisfy
    FINALIZE with the status line and lose the forward state, which is the
    whole failure this path exists to prevent. A session that writes only the
    summary runs out the bounded wait and is recorded ``wrote_state=False``.

    ``stay=True`` (T-0930, overrides ``relaunch``): compact-in-place — the
    session is told the truth that it CONTINUES after the compact, so it
    writes a checkpoint rather than a will (see :func:`handoff_prompt`).

    ``resume=True`` (T-0945, with ``relaunch=False``): the ``compact_exit``
    plan — ended AND resumed with a compacted transcript. Carries the DEADLINE
    and the criteria the stakeholder asked the system to supply, rather than
    leaving the session to guess: *«система должна ей ставить дедлайн и
    предоставлять четкие критерии, как решить, нужно ли делать compact для
    последующего resume или только handoff + exit … Это должна решать сама
    сессия, закончила она работу или нет»*. The session's half of that decision
    is the TICKET STATUS, which is exactly what the recycler reads next tick, so
    the non-resume branch names it.
    """
    if stay:
        after = (
            "The system then compacts your context in place and you continue "
            "working on the ticket in THIS session."
        )
        opening = (
            f"⏳ CHECKPOINT — WRITE YOUR FORWARD-STATE ONTO {task_id}. Your "
            "context is over the ceiling and will be COMPACTED IN PLACE: this "
            "SAME session continues afterwards — no relaunch. Details not in "
            "the compact summary survive ONLY in what you write onto the "
            "ticket now.\n\n"
        )
    else:
        after = (
            "The system then clears this pane and relaunches you FRESH on the same "
            "ticket — the Context you just wrote is what you will wake up holding."
            if relaunch else
            "The system then ENDS this session. A later session re-drives "
            f"{task_id} starting from the Context you just wrote."
        )
        opening = (
            f"⏳ FINALIZE — WRITE YOUR FORWARD-STATE ONTO {task_id}. Per the "
            "process-paradigm lifecycle this incarnation is ending now. NOTHING "
            "from this conversation survives EXCEPT what you write onto the "
            "ticket.\n\n"
        )
        deadline = (
            f"You have about {handoff_timeout_sec() // 60} minutes; after that "
            "the exit happens with or without your write.\n\n"
        )
        if resume:
            opening = (
                f"⏳ FINALIZE — WRITE YOUR FORWARD-STATE ONTO {task_id}. Your "
                "~1h prompt cache is about to go cold while idle, so the system "
                "will compact your context, end this session, and RESUME it "
                "(`claude --resume`) when there is next something for you. This "
                "conversation comes back summarized — but a summary is lossy, "
                "and anyone OTHER than the resumed you reads the ticket.\n\n"
                + deadline
            )
            after = (
                "The system then compacts your context, ends this session, and "
                "resumes it on the next message."
            )
        else:
            opening += (
                deadline +
                "YOU decide whether the work is finished; the system only sets "
                "the deadline. Say it in the TICKET STATUS, which is what "
                f"decides what happens next: `bsq ticket update {task_id} "
                "totest` if it is delivered, `blocked_on_user` if you are "
                "waiting on the stakeholder (the system then waits for him "
                "rather than restarting work on a blocked ticket), otherwise "
                "leave it as it is and a later session picks it up from the "
                "Context you are about to write.\n\n"
            )
    return (
        f"{opening}"
        "Run exactly:\n"
        f"  bsq ticket context {task_id} --file <file holding the new Context>\n\n"
        "That REPLACES the ticket's `## Context` — the shared working area, "
        "which is also what the board renders. So write the CURRENT state, not "
        "a diary: the goal, what is DONE, what is IN PROGRESS, the EXACT next "
        "steps, key file paths, decisions and gotchas. Carry over everything in "
        "the existing Context that is still true and drop what is not. Be "
        + ("exhaustive — this is your checkpoint.\n\n" if stay else
           "exhaustive — this is your successor's only memory.\n\n") +
        "One ticket, one artifact: do NOT write a handoff file, and do NOT file "
        "a progress note about this — the Context IS the handoff. Do NOT touch "
        "`## Stakeholder notes`; those are his words and are human-only.\n\n"
        "Then run:\n"
        f"  bsq ticket summary {task_id} \"<one paragraph>\"\n\n"
        "That REPLACES `## Executive summary` — the STAKEHOLDER reads this one, "
        "not your successor. Strictly ONE paragraph, and only where the work "
        "STANDS: what progress has been made and what remains. Do NOT restate "
        "what the ticket is about — he sees that in `## Stakeholder notes` and "
        "opens the Context himself if he wants the detail. It refuses anything "
        "with a blank line, a bullet or a heading in it.\n\n"
        f"After both return ok, reply: CONTEXT WRITTEN. {after}"
    )


def boot_prompt_from_ticket(*, role: str | None, task_id: str,
                            task_md_path: str | None = None) -> str:
    """The reload prompt for a fresh incarnation of a TASK-BOUND session.

    Counterpart to :func:`boot_prompt_from_artifact` for the ticket-Context
    handoff: the predecessor wrote its forward-state into ``## Context``, so
    that is where the successor is pointed — not at an artifact file.
    """
    who = f"the {role} role" if role else "your role"
    where = f"\n  {task_md_path}" if task_md_path else ""
    return (
        f"You are a FRESH incarnation continuing {who} on {task_id}. Your "
        "predecessor's context was cleared — NOTHING from it survives. Your "
        f"ONLY memory is what it wrote into {task_id}'s `## Context`:{where}\n"
        f"READ {task_id} FIRST, in full, before doing anything else — "
        "`## Stakeholder notes` is the ask in his own words (the anchor) and "
        "`## Context` is the complete forward-state (what is done, what is in "
        "progress, the exact next steps, key paths/decisions/gotchas). Then "
        "continue from there — do not restart already-finished work."
    )


def boot_prompt_from_artifact(*, role: str | None, assignment_id: str | None,
                              artifact_path: str) -> str:
    """The reload prompt for a fresh incarnation booting from a role artifact.

    Factored + reusable: T-0471 (crash recovery) boots an ungracefully-crashed
    session through this SAME recover-from-artifact path.
    """
    who = f"the {role} role" if role else "your role"
    asg = f" for assignment {assignment_id}" if assignment_id else ""
    return (
        f"You are a FRESH incarnation continuing {who}{asg}. Your predecessor's "
        "context was full and has been cleared — NOTHING from it survives. Your "
        "ONLY memory is the role artifact your predecessor wrote at:\n"
        f"  {artifact_path}\n"
        "READ THAT FILE FIRST, in full, before doing anything else. It holds the "
        "complete forward-state (goal, what's done, what's in progress, the exact "
        "next steps, key paths/decisions/gotchas). Then continue the work from "
        "there — do not restart already-finished work."
    )


# --- side-effecting seams (monkeypatched in tests) --------------------------

def _pane_for(sid: str) -> str | None:
    from bot_squad_worker.sessions import live_pane_map
    try:
        return live_pane_map().get(sid)
    except Exception:
        log.exception("autocompact: live_pane_map failed for %s", sid)
        return None


def _capture_pane(pane_id: str) -> str:
    from bot_squad_worker.sessions import _run
    try:
        cap = _run(["tmux", "capture-pane", "-t", pane_id, "-p"])
        return cap.stdout if cap.returncode == 0 else ""
    except Exception:
        log.exception("autocompact: capture-pane failed for %s", pane_id)
        return ""


def _send_compact(sid: str) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid, "text": "/compact"})


def _inject_handoff(sid: str, artifact_path: str, role: str | None = None, *,
                    relaunch: bool = True, stay: bool = False,
                    resume: bool = False) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid,
                          "text": handoff_prompt(artifact_path, role,
                                                 relaunch=relaunch, stay=stay,
                                                 resume=resume)})


def _inject_context_handoff(sid: str, task_id: str, *, relaunch: bool,
                            stay: bool = False, resume: bool = False) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid,
                          "text": context_handoff_prompt(task_id, relaunch=relaunch,
                                                         stay=stay, resume=resume)})


def _artifact_mtime(path: str | None) -> float:
    if not path:
        return 0.0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# The frontmatter split every ticket reader must use. NOT ``split("---", 2)``:
# a title containing ` --- ` (T-0286 has one) makes that read the status as
# empty and the body as a fragment.
_TICKET_FM_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


def task_md_path(cfg: Any, slug: str, task_id: str | None) -> str | None:
    """Absolute path of the ticket md a task-bound session finalizes INTO.

    ``None`` for an unbound session (``task_id`` absent or the ``~`` placeholder
    a session md carries when nothing is bound) and for an id that resolves to
    no file — both mean "no Context destination", and the caller falls back to
    the role artifact.
    """
    tid = (task_id or "").strip()
    if not tid or tid == "~":
        return None
    try:
        from bot_squad_worker import frontmatter as _fm
        p = _fm.resolve_id_file(Path(cfg.data_dir) / slug / "backlog", tid,
                                strict=True)
    except Exception:
        log.exception("autocompact: could not resolve ticket md for %s", tid)
        return None
    return str(p) if p is not None else None


def context_digest(task_md_path_: str | None) -> str:
    """Digest of a ticket's ``## Context`` — the ARM-time snapshot FINALIZE
    compares against to know the session actually wrote its forward-state.

    Deliberately the SECTION's content, not the file's mtime (which is what the
    artifact handoff watches). The ticket md is a shared file: a peer session
    filing a progress note, a stakeholder quote, or a status change all bump its
    mtime, and any of those would have read as "the handoff is written" and
    terminated the session with nothing recorded — the exact loss this path
    exists to prevent. Comparing the Context itself asserts the claim.

    Unreadable / missing file → ``""``. An arm-time ``""`` that stays ``""``
    simply never satisfies FINALIZE, so the bounded timeout takes over rather
    than a missing ticket looking like a completed write.
    """
    if not task_md_path_:
        return ""
    try:
        text = Path(task_md_path_).read_text()
    except OSError:
        return ""
    m = _TICKET_FM_RE.match(text)
    body = m.group(2) if m else text
    from bot_squad_worker.task_body import parse_body
    ctx = parse_body(body).get("context", "")
    return hashlib.sha256(ctx.encode("utf-8")).hexdigest()


def _resolve_compact_target(cfg: Any, slug: str, rec: dict) -> dict:
    """Where this session's forward-state goes at finalize (T-0863).

    ``kind`` is the whole decision, and it turns on ONE fact — does the session
    own a task:

      ``context``  a task-bound session → that ticket's ``## Context``. One
                   task, one artifact; it is already on the board.
      ``artifact`` a task-LESS session (operator state-doc, or any other role's
                   per-assignment file) → the role artifact, unchanged. The
                   stakeholder's "никаких файлов" is scoped to *«контекст по
                   задаче»* — a session with no task has no Context to write, so
                   removing the artifact here would leave it with no home at all.
      ``none``     neither resolves → caller falls back to Claude's ``/compact``.
    """
    role = rec.get("role") or ""
    task_id = (rec.get("task_id") or "").strip()
    task_bound = bool(task_id) and task_id != "~"

    if task_bound:
        tmd = task_md_path(cfg, slug, task_id)
        if tmd:
            return {"kind": "context", "task_id": task_id, "task_md": tmd,
                    "role": role, "assignment_id": task_id}
        # Bound to an id whose md is gone/ambiguous. Falling through to the
        # role artifact would resolve it to `artifacts/<task_id>.md` — the
        # exact file this ticket removed — so take the /compact fallback
        # instead of resurrecting the retired destination.
        log.warning("autocompact: %s is bound to %s but its md did not resolve "
                    "— no Context destination, falling back to /compact",
                    rec.get("sid"), task_id)
        return {"kind": "none", "role": role, "assignment_id": task_id}

    artifact_path, role, assignment_id = _resolve_role_artifact(cfg, slug, rec)
    if artifact_path:
        return {"kind": "artifact", "artifact_path": artifact_path,
                "role": role, "assignment_id": assignment_id}
    return {"kind": "none", "role": role, "assignment_id": None}


def _resolve_role_artifact(cfg: Any, slug: str, rec: dict):
    """Resolve ``(artifact_path|None, role, assignment_id|None)`` for a session's
    role artifact — the compact destination. ``None`` path ⇒ no destination ⇒ the
    caller falls back to Claude's /compact."""
    role = rec.get("role") or ""
    task_id = rec.get("task_id")
    sid = rec.get("sid")
    art = assignment.role_artifact(cfg.data_dir, slug, role=role, sid=sid,
                                   task_id=task_id)
    if art is None:
        return (None, role, None)
    task_bound = bool(task_id) and task_id != "~"
    assignment_id = task_id if task_bound else (role or None)
    return (str(art.path), role, assignment_id)


def _suspend_session(cfg: Any, slug: str, sid: str) -> None:
    """CLEAR the context — close the over-ceiling pane. (T-0467 step 2.)"""
    from bot_squad_worker import sessions
    sessions.suspend(cfg, slug, sid, source="autocompact",
                     reason="context-full compact handoff")


def _alert_orphaned_handoff(cfg: Any, slug: str, sid: str, reason: str) -> None:
    """Best-effort operator alert when a handoff cleared the old pane but the
    relaunch failed — telemetry won't re-sample a paneless session, so without
    this the assignment would orphan silently (mirrors recovery.py park-notify)."""
    try:
        from bot_squad_worker import intersession
        # T-0827: send_notice — this is the alert for a handoff that ALREADY
        # failed; losing it silently is how the assignment orphans unseen,
        # which is the exact thing this alert exists to prevent.
        intersession.send_notice(
            cfg, slug, to="operator", from_sid="S-autocompact",
            text=(f"⚠️ compact handoff for {sid} cleared the pane but the relaunch "
                  f"failed ({reason}). The assignment needs a manual respawn — "
                  f"the forward-state artifact is intact."))
    except Exception:
        log.exception("autocompact: orphaned-handoff alert failed for %s", sid)


#: T-0909: what a relaunch carries forward from the incarnation it replaces.
#:
#: THE HOLE THIS CLOSES, measured on the live journal over 9 days: of 123
#: role-default (`config:dev` -> opus) dev spawns, **87 (71%) were relaunches
#: from this module**, not dispatches anybody made. A relaunch already carries
#: task_id / initiative / parent_sid / owner so the reconcilers see continuity —
#: but it dropped the MODEL, so a dispatch deliberately sent to Sonnet came back
#: as Opus at the first cache-window recycle, roughly an hour later. The gate
#: T-0909 put on `bsq spawn` would therefore have decayed to nothing on exactly
#: the long-lived sessions that cost the most.
#:
#: This is not a new policy and it changes no default. It is the same
#: carry-forward `operator_redrive._respawn_operator` has done since T-0678, for
#: the same stated reason ("a full respawn mints a BRAND-NEW SID ... without
#: this the override would silently revert to the fleet/role default"), applied
#: to the path that produces 71% of the role-defaulted dev traffic.
def _inherited_model_choice(meta: dict) -> tuple:
    """``(model, effort, model_reason)`` to carry into a relaunch.

    Each is inherited ONLY when the predecessor's md shows it was CHOSEN:

    * ``model`` is stamped only for an explicit choice (T-0678), so its mere
      presence is the signal — a role-defaulted predecessor carries nothing and
      the successor re-reads the role default, exactly as today.
    * ``effort`` is stamped UNCONDITIONALLY (T-0871), so presence proves
      nothing; it rides on ``effort_source == "explicit"``. Inheriting a role
      DEFAULT would freeze today's config onto every future incarnation — the
      staleness T-0678 deliberately avoided — so an unstamped/defaulted effort
      is left to re-resolve.
    * ``model_reason`` follows the model: a carried-over premium choice keeps
      the justification that bought it, instead of reappearing in the
      compliance number as an unexplained Opus.
    """
    def _clean(v):
        return v if (v and v != "~") else None

    model = _clean(meta.get("model"))
    effort = None
    if str(meta.get("effort_source") or "").strip() == "explicit":
        effort = _clean(meta.get("effort"))
    reason = _clean(meta.get("model_reason")) if model else None
    return model, effort, reason


def _relaunch(cfg: Any, slug: str, rec: dict, make_prompt,
              dispatched_by: str = "autocompact-relaunch") -> None:
    """Relaunch a FRESH incarnation re-bound to the SAME assignment (binding
    continuity — task_id/initiative/parent_sid/owner carried from the
    predecessor's session md so reconcilers don't see an orphan).

    ``make_prompt(role, task_id, assignment_id)`` supplies the boot prompt, so
    the ticket-Context and role-artifact relaunches share every line of the
    binding-continuity logic and differ only in what the successor is told to
    read.
    """
    from bot_squad_worker import sessions

    sid = rec.get("sid")
    meta = sessions._read_session_metadata(
        sessions._session_file(cfg.data_dir, slug, sid)) or {}

    def _clean(v):
        return v if (v and v != "~") else None

    task_id = _clean(rec.get("task_id") or meta.get("task_id"))
    role = rec.get("role") or meta.get("role") or ""
    assignment_id = task_id if task_id else (role or None)
    window = (_clean(meta.get("window")) or _clean(rec.get("window"))
              or f"recover-{assignment_id or role or 'session'}")
    model, effort, model_reason = _inherited_model_choice(meta)
    sessions.spawn(
        cfg, slug, window, make_prompt(role, task_id, assignment_id),
        task_id=task_id,
        initiative=_clean(meta.get("initiative")),
        parent_sid=_clean(meta.get("parent_sid")),
        owner=_clean(meta.get("owner")),
        owner_user=_clean(meta.get("owner_user")),
        model=model,
        effort=effort,
        model_reason=model_reason,
        dispatched_by=dispatched_by,
    )


def _relaunch_from_ticket(cfg: Any, slug: str, rec: dict, task_md: str | None) -> None:
    """T-0863: relaunch a task-bound session pointed at its TICKET — the
    predecessor's forward-state is that ticket's ``## Context``, not a file."""
    _relaunch(cfg, slug, rec, lambda role, task_id, assignment_id:
              boot_prompt_from_ticket(
                  role=role, task_id=task_id or assignment_id or "",
                  task_md_path=task_md))


def _relaunch_from_artifact(cfg: Any, slug: str, rec: dict, artifact_path: str,
                            dispatched_by: str = "autocompact-relaunch") -> None:
    """Relaunch a FRESH incarnation booting from the role artifact — the
    task-LESS half (operator state-doc / per-assignment role file), and the path
    T-0471 crash recovery reuses for any session that still has an artifact.

    T-0909: ``dispatched_by`` is a parameter precisely BECAUSE recovery reuses
    this function. Hardcoding "autocompact-relaunch" here would file every
    crash recovery under the graceful-compact producer, and the ledger's whole
    job is to say who actually produced a dispatch — a label that names the
    mechanism it borrowed rather than the caller that ran it is the failure the
    breakdown exists to prevent.
    """
    _relaunch(cfg, slug, rec, lambda role, task_id, assignment_id:
              boot_prompt_from_artifact(role=role, assignment_id=assignment_id,
                                        artifact_path=artifact_path),
              dispatched_by=dispatched_by)


# --- executor ---------------------------------------------------------------

def _do_claude_compact(cfg: Any, slug: str, rec: dict, now: float) -> bool:
    """Legacy fallback — send Claude's in-context ``/compact`` to an idle,
    composer-ready pane and stamp the per-session cooldown."""
    sid = rec.get("sid")
    if not sid:
        return False
    pane = _pane_for(sid)
    if not pane:
        return False
    if not composer_free(_capture_pane(pane), sid=sid, now=now):
        return False
    try:
        _send_compact(sid)
    except Exception:
        log.exception("autocompact: /compact send failed for %s (will retry)", sid)
        return False
    fired = rec.get("alert_fired_at") or {}
    fired["compact"] = now
    rec["alert_fired_at"] = fired
    log.info("autocompact: sent /compact to idle over-ceiling session %s", sid)
    return True


def handoff_mark(target: dict) -> str:
    """A destination's current state folded into ONE string.

    The idle-window recycler keeps its in-flight state as FLAT scalar session-md
    fields (the line-based ``session_start.sh`` reader cannot hold a nested
    mapping), so it snapshots the destination as this single value at ARM and
    re-derives it at FINALIZE: a changed mark means the session wrote. The
    ceiling recycler keeps a nested ``rec['compact']`` dict instead and reads
    the same two signals directly — see :func:`handoff_written`.
    """
    if target.get("kind") == "context":
        return "ctx:" + context_digest(target.get("task_md"))
    if target.get("kind") == "artifact":
        return "mtime:%r" % _artifact_mtime(target.get("artifact_path"))
    return ""


def handoff_written(compact: dict) -> bool:
    """True once the in-flight handoff's forward-state has actually landed.

    The two destinations are checked by the signal that is honest for each:
    a ticket ``## Context`` by its own content (a shared md's mtime moves for
    reasons that are not this write — see :func:`context_digest`), a role
    artifact by mtime (a private file only its own session writes).
    """
    if compact.get("kind") == "context":
        armed = compact.get("arm_digest", "")
        now_digest = context_digest(compact.get("task_md"))
        return bool(now_digest) and now_digest != armed
    return _artifact_mtime(compact.get("artifact_path")) > float(
        compact.get("arm_mtime", 0.0))


def _maybe_finalize(cfg: Any, slug: str, rec: dict, compact: dict, now: float) -> bool:
    """A handoff is in flight (``phase == writing``). Finalize once the session
    has written its forward-state and the pane is idle.

    T-0905 SPLIT THE ONE DEADLINE IN TWO, because "the handoff did not finish"
    hides two failures with opposite remedies:

      nothing written   the session ignored the handoff. Nothing exists for a
                        successor to boot from, so after
                        :func:`handoff_timeout_sec` the only non-destructive
                        move is Claude's ``/compact`` — keep the session, take
                        its context down. UNCHANGED behaviour.
      written, pane busy the state IS on disk and only an idle pane is missing.
                        ``/compact`` needs that same idle, composer-ready pane,
                        so falling back cannot compact one second earlier than
                        the clean relaunch — it only spends a full
                        summarization instead of doing the relaunch. So wait,
                        up to :func:`handoff_hard_timeout_sec`, then finalize
                        against the busy pane anyway.

    The un-split version took the ``/compact`` branch for BOTH, which is how a
    continuously-driving operator (whose role artifact is rewritten every few
    minutes by its own drive cycle, and whose pane is rarely quiet for a whole
    15-minute window) ended up paying ~390k tokens of summarization, twice in
    two hours, for a handoff that was already written and would have relaunched
    cleanly seconds later.

    T-0863: ``compact['kind']`` selects the destination — ``context`` (the
    ticket's ``## Context``, for a task-bound session) or ``artifact`` (the role
    artifact, for a task-less one). A record with no ``kind`` is an in-flight
    handoff armed by the previous worker build and is finalized as ``artifact``,
    the only thing it can be.
    """
    sid = rec.get("sid")
    armed_at = float(compact.get("armed_at", now))
    kind = compact.get("kind") or "artifact"
    age = now - armed_at
    wrote = handoff_written(compact)

    # --- half 1: NOTHING was written -----------------------------------------
    # Never wedge over-ceiling with no forward-state anywhere: the session
    # ignored the handoff → drop it and let Claude's /compact take the context
    # back down. Relaunching is NOT an option here and never becomes one — there
    # is nothing on disk for a successor to boot from, so /compact (which keeps
    # the session, summarized) is the only move that does not destroy state.
    if age > handoff_timeout_sec() and not wrote:
        if not _do_claude_compact(cfg, slug, rec, now):
            # Pane busy — keep the handoff ARMED and retry next tick. The old
            # code cleared ``rec['compact']`` here before knowing whether the
            # send landed; on the False path telemetry never persisted that
            # clear (it only writes the record when an action was taken), so
            # the phase came back next tick and re-logged the same warning
            # every 60s for as long as the pane stayed busy — 6 identical
            # WARNINGs for one timeout on p355, 2026-08-18T07:53-07:58.
            # Leaving it armed is also what lets a LATE write still finalize
            # cleanly instead of being locked out of its own handoff.
            return False
        rec["compact"] = {}
        log.warning("autocompact: handoff timed out for %s with no forward-state "
                    "written — fell back to /compact", sid)
        return True

    # Wait until the session has actually written its forward-state.
    if not wrote:
        return False

    # --- half 2: the forward-state IS on disk ---------------------------------
    # Only the pane is missing. Do NOT fall back to /compact here, however long
    # this takes (T-0905). ``_do_claude_compact`` needs a composer-ready pane —
    # a PRECONDITION this finalize needs too — so the fallback can never take
    # the context down any sooner than the clean relaunch would have. All it can
    # do is spend a full summarization at the exact tick the clean path became
    # possible. Measured on S-almdudleer-operator-p355 (2026-08-18): two
    # fallbacks, 389,980 and 396,913 pre-compact tokens, fired 294s and 54s
    # after the deadline and within seconds of the pane first going ready, with
    # operator-state.md already rewritten four times since ARM. Both were a
    # clean handoff+relaunch converted into ~390k tokens of summarization.
    idle = compact_safe(rec.get("activity", ""))
    if idle:
        pane = _pane_for(sid)
        if pane and not composer_ready(_capture_pane(pane), sid=sid, now=now):
            idle = False
    elif age > handoff_timeout_sec() and recycle_gate.should_log_skip(
            f"handoff-busy:{sid}", now):
        # Only past the soft deadline: before it this is the ordinary wait every
        # handoff goes through (p50 300s), and logging it per tick would bury
        # the case worth seeing. ``composer_ready`` logs its own deferral; the
        # activity gate was silent, which is the T-0864 attribution gap for
        # exactly this session shape.
        log.info("autocompact: %s has written its forward-state but its pane is "
                 "%s — holding the handoff open (%ds since arm, hard cap %ds)",
                 sid, rec.get("activity") or "unknown", int(age),
                 handoff_hard_timeout_sec())

    if not idle:
        if age <= handoff_hard_timeout_sec():
            return False
        # Never wedge, the other way round: a continuously-busy pane cannot be
        # waited on forever. The forward-state is on disk, so clearing this pane
        # loses only the in-flight turn — cheaper than leaving the session
        # running over the ceiling indefinitely.
        log.warning("autocompact: %s never went idle in %ds and its "
                    "forward-state is already written — finalizing against a "
                    "busy pane (hard cap %ds)", sid, int(age),
                    handoff_hard_timeout_sec())

    # --- T-0930: the checkpoint promise — COMPACT IN PLACE, session lives ----
    # ARM stamped ``stay: true`` and told the session it would continue; honour
    # that here. The trace is on disk (audit + crash-net), so all that is left
    # is taking the context down without killing the incarnation: send native
    # /compact and stamp the cooldown. Requires an idle, typing-free pane —
    # when the pane stayed busy past the hard cap, fall THROUGH to the old
    # clear+relaunch below: it is the one move that cannot wedge (measured
    # T-0905 shape), and the checkpoint the session just wrote is exactly what
    # a successor boots from, so nothing is lost beyond the in-flight turn.
    if compact.get("stay"):
        if idle:
            pane = _pane_for(sid)
            if not pane or not composer_free(_capture_pane(pane), sid=sid,
                                             now=now):
                return False  # typing/mid-turn — retry next tick, never force
            try:
                _send_compact(sid)
            except Exception:
                log.exception("autocompact: compact-in-place send failed for "
                              "%s (will retry)", sid)
                return False
            fired = rec.get("alert_fired_at") or {}
            fired["compact"] = now
            rec["alert_fired_at"] = fired
            rec["compact"] = {}
            wrote_to = (compact.get("task_id") if kind == "context"
                        else compact.get("artifact_path"))
            log.info("autocompact: checkpoint complete for %s — forward-state "
                     "on %s, compacted in place, session continues", sid,
                     wrote_to)
            return True
        log.warning("autocompact: %s checkpoint written but the pane never "
                    "went idle in %ds — falling back to clear+relaunch (the "
                    "checkpoint is the successor's boot state)", sid, int(age))

    # T-0930: NEVER clear+relaunch a pane a human is attached to — «я не смогу
    # ее найти когда вернусь». maybe_compact now lets attached sessions through
    # (for the compact-in-place path above), so this is the barrier that keeps
    # the relaunch half off-limits for them: hold the handoff open and retry —
    # the moment he detaches, the normal flow resumes.
    if recycle_gate.is_attached(_pane_for(sid), sid=sid, now=now):
        if recycle_gate.should_log_skip(f"finalize-attached:{sid}", now):
            log.info("autocompact: %s has its forward-state written but a human "
                     "client is attached — holding the relaunch (T-0930)", sid)
        return False

    # CLEAR + RELAUNCH: close the old pane, boot a fresh incarnation from what
    # the predecessor wrote, re-bound to the same assignment.
    try:
        _suspend_session(cfg, slug, sid)
    except Exception:
        log.exception("autocompact: finalize suspend failed for %s — retry next "
                      "tick", sid)
        return False
    wrote_to = (compact.get("task_id") if kind == "context"
                else compact.get("artifact_path"))
    try:
        if kind == "context":
            _relaunch_from_ticket(cfg, slug, rec, compact.get("task_md"))
        else:
            _relaunch_from_artifact(cfg, slug, rec, compact.get("artifact_path"))
    except Exception as e:
        # The old pane is already gone and won't be re-sampled — alert the
        # operator instead of orphaning the assignment silently.
        log.exception("autocompact: relaunch failed for %s after clear", sid)
        _alert_orphaned_handoff(cfg, slug, sid, repr(e))
        return False
    rec["compact"] = {}
    log.info("autocompact: compact handoff complete for %s — relaunched fresh "
             "from %s", sid, wrote_to)
    return True


def _maybe_compact_stay_ceiling(sid: str, meta: dict, md_path, level: str, now: float,
                                pane: str | None) -> bool:
    """T-0649: the ceiling-trigger counterpart to :mod:`idle_timeout`'s T-0617
    compact-and-stay. Drives the SAME ``compact_stay_phase`` /
    ``compact_stay_armed_at`` / ``compact_stay_last_at`` session-md fields as
    the idle-window trigger — sharing that state (not forking it) is what
    makes ``compact_stay_last_at`` an effective anti-loop guard across BOTH
    triggers: whichever one compacts first blocks the other for the rest of
    the cache window. Only the ARM condition differs from idle_timeout's own
    :func:`idle_timeout._maybe_compact_and_stay`: this arms on the context
    ceiling (``level == 'urgent'``), not an idle window elapsing — a
    ceiling-triggered compact needs to fire immediately, same as it always has
    for worker sessions, not wait out an idle clock. NEVER calls
    ``sessions.suspend`` — the pane/tmux/process stay exactly where the human
    left them, same guarantee as T-0617.
    """
    from bot_squad_worker import idle_timeout, sessions as _sessions

    if meta.get("compact_stay_phase") == "compacting":
        return idle_timeout._finalize_compact_stay(sid, meta, md_path, now, pane)

    if level != "urgent":
        return False
    if not idle_timeout.compact_stay_due(meta.get("compact_stay_last_at"), now,
                                         idle_timeout.idle_timeout_sec()):
        return False  # already compacted-and-stayed this cache window
    if not pane or not composer_free(_capture_pane(pane), sid=sid, now=now):
        return False

    try:
        _send_compact(sid)
    except Exception:
        log.exception("autocompact: ceiling compact-and-stay /compact send "
                      "failed for %s (will retry)", sid)
        return False
    meta["compact_stay_phase"] = "compacting"
    meta["compact_stay_armed_at"] = idle_timeout._now_iso()
    _sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("autocompact: ceiling compact-and-stay sent /compact to %s — "
             "session stays, no relaunch", sid)
    return True


def maybe_compact(cfg: Any, slug: str, rec: dict, level: str, now: float) -> bool:
    """Recycle ``rec``'s session if it's over-threshold AND safe.

    Two strategies for WORKER (dev/TL/operator) sessions (see module
    docstring): the T-0467 write-to-artifact handoff (default) and the legacy
    Claude ``/compact`` fallback. The human's own exempt sessions (T-0616)
    never ride either — T-0649 routes their ceiling trigger to T-0617's
    compact-in-place mechanism instead (see :func:`_maybe_compact_stay_ceiling`).
    Returns True iff an action was taken this tick (handoff armed, handoff
    finalized, compact-and-stay armed/finalized, or /compact sent). Every gate
    fails closed — an unresolved pane / not-ready composer simply defers to the
    next tick rather than risk interrupting work.
    """
    if not autocompact_enabled():
        return False
    sid = rec.get("sid")
    # T-0563/T-0564/T-0616 (recycle-v2): never recycle a non-allowlisted
    # project, or touch a pane a human is currently attached to — even the
    # operator (T-0334) is subject to this gate. The telemetry rec doesn't
    # carry the recycle_exempt md marker, so read the session md best-effort.
    from bot_squad_worker import sessions as _sessions
    meta: dict = {}
    md_path = None
    if sid:
        try:
            md_path = _sessions._session_file(cfg.data_dir, slug, sid)
            meta = _sessions._read_session_metadata(md_path) or {}
        except Exception:
            meta = {}
            md_path = None
    if not recycle_gate.project_allowed(cfg, slug, now):
        return False
    # T-0926 follow-up: an explicit human pin beats every other signal here —
    # including the compact-and-stay ceiling path a few lines down.
    if recycle_gate.session_pinned(meta):
        return False
    pane = _pane_for(sid) if sid else None
    # T-0930 (stakeholder, 2026-08-30): an ATTACHED pane no longer blocks the
    # ceiling response outright — «это разумная компакт логика даже когда я
    # работаю с сессией». Attended sessions get the compact-in-place flow only
    # (stay is forced further down; the relaunch/exit paths stay barred — see
    # _maybe_finalize's attached guard), and the composer_free typing gate is
    # what protects his half-typed draft. With the stay mode killed
    # (BOT_SQUAD_CEILING_COMPACT_STAY=0) the old hands-off behaviour returns,
    # since the only available response would again be the relaunch he must
    # never get while attached.
    attached = recycle_gate.is_attached(pane, sid=sid, now=now)
    if attached and not ceiling_stay_enabled():
        return False

    role = rec.get("role") or meta.get("role")
    window = rec.get("window") or meta.get("window")

    # T-0649: the human's own sessions (user-conversation role, hand-launched
    # user-session window, recycle_exempt md marker) never ride the
    # handoff/terminate machinery below — their ceiling trigger reuses
    # idle_timeout's T-0617 compact-in-place state instead, sharing its
    # compact_stay_last_at anti-loop guard so the ceiling and idle-window
    # triggers can never double-compact the same session. Worker sessions
    # (dev/TL/operator) are not exempt and fall through to the unchanged flow
    # below.
    if recycle_gate.user_session_exempt(role=role, window=window, meta=meta):
        if md_path is None:
            return False
        return _maybe_compact_stay_ceiling(sid, meta, md_path, level, now, pane)

    fired = rec.get("alert_fired_at") or {}
    rec["alert_fired_at"] = fired

    # A handoff already in flight → drive its finalize half (independent of the
    # cooldown; the phase dict is its own guard).
    compact = rec.get("compact") or {}
    if compact.get("phase") == "writing":
        return _maybe_finalize(cfg, slug, rec, compact, now)

    # Otherwise decide whether to START a compact this tick.
    if not (compact_due(level, fired, now) and compact_safe(rec.get("activity", ""))):
        return False
    sid = rec.get("sid")
    if not sid:
        return False
    pane = _pane_for(sid)
    if not pane:
        return False
    if not composer_free(_capture_pane(pane), sid=sid, now=now):
        return False

    # Handoff mode + a resolvable destination → ARM the write-it-down flow.
    # T-0930: ``stay`` decides what the session is PROMISED (checkpoint +
    # compact-in-place vs relaunch) — stamped into the phase dict so FINALIZE
    # honours the promise made at ARM even across a worker restart or an env
    # flip mid-flight.
    if compact_mode() == "handoff":
        stay = ceiling_stay_enabled()
        target = _resolve_compact_target(cfg, slug, rec)
        if target["kind"] == "context":
            # T-0863: a task-bound session hands off through its TICKET.
            task_id, task_md = target["task_id"], target["task_md"]
            try:
                _inject_context_handoff(sid, task_id, relaunch=True, stay=stay)
            except Exception:
                log.exception("autocompact: context handoff inject failed for "
                              "%s (will retry)", sid)
                return False
            rec["compact"] = {
                "phase": "writing",
                "kind": "context",
                "armed_at": now,
                "task_id": task_id,
                "task_md": task_md,
                "arm_digest": context_digest(task_md),
                "role": target["role"],
                "assignment_id": task_id,
                "stay": stay,
            }
            log.info("autocompact: armed context %s for %s → %s ## Context",
                     "checkpoint (compact-in-place)" if stay else "handoff",
                     sid, task_id)
            return True

        if target["kind"] == "artifact":
            artifact_path, role = target["artifact_path"], target["role"]
            try:
                _inject_handoff(sid, artifact_path, role, stay=stay)
            except Exception:
                log.exception("autocompact: handoff inject failed for %s (will "
                              "retry)", sid)
                return False
            rec["compact"] = {
                "phase": "writing",
                "kind": "artifact",
                "armed_at": now,
                "arm_mtime": _artifact_mtime(artifact_path),
                "artifact_path": artifact_path,
                "role": role,
                "assignment_id": target["assignment_id"],
                "stay": stay,
            }
            log.info("autocompact: armed %s for %s → %s",
                     "checkpoint (compact-in-place)" if stay else
                     "compact handoff", sid, artifact_path)
            return True

    # No destination (or claude mode) → legacy /compact fallback.
    return _do_claude_compact(cfg, slug, rec, now)
