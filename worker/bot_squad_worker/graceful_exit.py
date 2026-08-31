"""T-0465 / M1-F1.2 — work-done → graceful EXIT (the uniform lifecycle's
missing terminal path).

Stakeholder (SOURCE-VERBATIM Part A + clarification-01): *"If the work the
session was created to do is done, the session should exit"* … *"ALL the sessions
from operator to dev to user session have same lifecycle."*

The session lifecycle (docs/design/session-lifecycle-state-machine.md) is ONE
role-agnostic state machine. Its RECYCLE/RECOVER transitions (autocompact /
idle_timeout / recovery) already existed: when work is NOT done they record the
forward-state, clear the context, and relaunch a FRESH incarnation to continue.
This module owns the one path that was missing — **work DONE → EXIT**: a session
whose assignment is complete suspends itself with NO relaunch, because the
deliverable already exists.

Uniform across roles; the done-signal differs ONLY by role + trigger:
  * a **task-bound** role (dev / team-lead) is done when its bound task is
    terminal — ``totest``/``closed`` (it has committed + reported READY);
  * an **operator** is done when its backlog is empty (no non-closed task left to
    clear — the operator's standing assignment, T-0474).
A session with no done-signal (a user-conversation / role-only session) is never
force-exited here — but it is NOT exempt from RECYCLE-on-timeout (idle_timeout),
so it still never blocks indefinitely.

Why EXIT is record-FREE (the deliberate asymmetry vs idle_timeout/autocompact):
the recycle paths must record-then-relaunch because work is unfinished; EXIT does
not re-ask the session to record because (1) the deliverable that proves "done"
already exists (committed code + ``totest`` for a dev; a cleared backlog for an
operator), (2) a done dev frequently has NO role artifact at all, so the
write-then-relaunch handoff would never even fire for it, and (3) there is no
successor. So EXIT = a clean :func:`sessions.suspend` (visible-close stamp,
T-0444). Operator continuity is owned by ``operator_redrive`` (it re-drives the
operator from its artifact the moment a task lands) — so suspending the idle
empty-backlog operator here merely stops the ~1h ``idle_timeout`` churn it would
otherwise suffer; the two compose, both keyed on the same empty-backlog signal.

A GRACE window (default ~3min, the session's jsonl idle age) protects a session
that JUST finished from being cut mid-finish-sequence — a dev sets ``totest`` then
commits then pings READY; we only exit it once it has actually gone quiet. The
pane must also be idle + composer-ready (never cut mid-turn). Every gate fails
closed.

T-0945 (2026-08-31) adds two things to this path, both from the same
stakeholder ruling:

* **A HANDOFF BEFORE THE EXIT.** *«когда to test для меня, не имеет смысла
  держать сессию уже, если что, можно будет начать новую из тикета, компакты
  делать не надо»* — starting a new session FROM THE TICKET presumes the ticket
  is current, so a task-bound session is asked to write its ``## Context``
  before it goes (:func:`_maybe_arm_exit_handoff`, bounded, never wedges).
  Still **no compact, ever**, on this path: nothing resumes a done session, so
  a squeeze would be paid for and discarded — the exact waste T-0863 removed
  from ``idle_timeout``. That asymmetry ("compact only when a resume is
  expected") is the whole of the T-0945 policy, and this is its else-branch.
  ``BOT_SQUAD_EXIT_HANDOFF=0`` restores the record-free exit.
* **PINNED / ATTACHED PANES ARE NEVER EXITED.** This path had no
  :mod:`recycle_gate` check at all, so a done session the human had attached to
  (or pinned) was suspended out from under him — *«исчезновение сессии у меня
  из под носа»*, which he reads as a symptom of a badly configured process
  rather than a correct decision. A pin or a live client now defers the exit
  for as long as it lasts.

Kill switch: ``BOT_SQUAD_GRACEFUL_EXIT=0`` disables the path entirely.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from bot_squad_worker import autocompact, recycle_gate, sessions

log = logging.getLogger(__name__)

# A task-bound role is "done" when its bound task reaches a terminal status. Same
# set as recovery.DONE_STATUSES / drift._TERMINAL_TICKET_STATUSES — a dev that set
# ``totest`` has reported READY and handed off to its TL (kill-not-resume).
DONE_STATUSES = frozenset({"totest", "closed"})

# Default grace: a session must have been quiet (jsonl idle) for at least this
# long after going "done" before we exit it — covers the dev's own
# totest→commit→READY finish sequence so we never cut it off mid-finish.
DEFAULT_EXIT_GRACE_SEC = 180


def graceful_exit_enabled() -> bool:
    """Master switch (default ON, mirroring the sibling recyclers).

    ``BOT_SQUAD_GRACEFUL_EXIT=0`` disables the work-done→exit path (a deliberate
    operator kill-switch). ON-by-default is safe: a graceful exit only ever
    suspends an idle session whose deliverable already exists, and operator
    continuity is re-driven independently.
    """
    return os.environ.get("BOT_SQUAD_GRACEFUL_EXIT", "1") != "0"


def exit_grace_sec() -> int:
    """The post-done idle grace before exit (seconds). Overridable via
    ``BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC``; non-positive/garbage falls back to the
    default so a bad env can never collapse the grace to zero (which would risk
    cutting a session mid-finish)."""
    raw = os.environ.get("BOT_SQUAD_GRACEFUL_EXIT_GRACE_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_EXIT_GRACE_SEC


# --- pure decision helpers (unit-testable, no I/O) --------------------------

def work_done(role: str, task_id: Any, task_status: str, pending_backlog: int,
              *, initiative_pending: int | None = None) -> bool:
    """True when the session's assignment is complete — role-agnostic policy, the
    done-signal differing ONLY by role.

    * operator: backlog empty (``pending_backlog == 0``).
    * task-bound role (has a real ``task_id``): bound task is terminal.
    * team-lead bound only to an initiative (no primary task_id): the
      initiative's own backlog is empty (``initiative_pending == 0`` — T-0930,
      "TL with nothing open"). ``initiative_pending=None`` means "not
      computed" (no initiative to scope by), not "zero" — stays False.
    * otherwise (user-conversation / role-only): no auto-done signal → False.
    """
    if role == "operator":
        return pending_backlog == 0
    if task_id and task_id != "~":
        return task_status in DONE_STATUSES
    if role == "teamlead" and initiative_pending is not None:
        return initiative_pending == 0
    return False


def count_pending_initiative_tasks(cfg: Any, slug: str, initiative: Any) -> int:
    """T-0930: the task-less TL's "nothing open" done-signal — the
    initiative-scoped analogue of ``operator_redrive.count_pending_backlog``'s
    project-wide count. Same terminal-status criterion (``closed`` only — a
    ``totest`` task is still the TL's to review/close, same reasoning
    ``operator_redrive`` already uses for the operator's own empty-backlog
    signal)."""
    from bot_squad_worker import frontmatter as _fm
    from bot_squad_worker.actions import normalize_id

    target = normalize_id(str(initiative or "").strip())
    if not target or target == "~":
        return 0
    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return 0
    n = 0
    for md in sorted(backlog.glob("*.md")):
        try:
            parsed = _fm.parse_or_none(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed:
            continue
        meta, _body = parsed
        meta = meta or {}
        task_init = normalize_id(str(meta.get("initiative") or "").strip())
        if task_init != target:
            continue
        if str(meta.get("archived", "")).strip().lower() in ("true", "yes", "1", "on"):
            continue
        if str(meta.get("status", "")).strip().lower() == "closed":
            continue
        n += 1
    return n


def exit_due(idle_age: float | None, grace: int) -> bool:
    """True when the session has been quiet for at least ``grace`` seconds.

    ``idle_age`` is ``now - <jsonl mtime>``. ``None`` (no activity signal — can't
    establish the age) is conservative: NOT due, so a session whose idle age we
    cannot measure is never cut off mid-finish.
    """
    if idle_age is None:
        return False
    return idle_age >= grace


# --- T-0468 / M1-F1.5: exit-resume hint -------------------------------------
#
# SOURCE-VERBATIM Part A: *"The sessions can search the exited sessions history,
# but full conversation reread (e.g. via --resume) is discouraged in many cases.
# In fact on exit the sessions should state whether and when it will be better to
# resume this session rather than starting a new one from documented progress."*
#
# A graceful exit fires ONLY when work is DONE (terminal task / empty backlog),
# so BY CONSTRUCTION the deliverable already exists and is documented (committed
# code + progress notes for a dev/TL; the backlog artifact for an operator) and
# the exited session's history stays searchable. The uniform lifecycle keeps
# continuity in those ARTIFACTS, not in a kept-alive conversation — a fresh
# session (or the operator's artifact-driven respawn, :mod:`operator_redrive`,
# which RESPAWNS from the backlog, never resumes) plans from the deliverable. So
# at a clean work-done exit ``claude --resume`` (a full conversation reread) buys
# little and costs a whole context reload: ``resume_recommended`` is False, and
# the hint states the narrow ``when`` under which a human should still prefer it.
#
# The hint is general (a future non-graceful exit path could pass a non-terminal
# status — work INTERRUPTED, the deliverable does NOT yet capture it — and then
# resume DOES beat fresh): ``compute_resume_hint`` returns True for that branch,
# so the policy is genuinely two-valued, not a constant.


def _last_work_summary(role: str, task_id: Any, task_status: str) -> str:
    """A short, factual record of what the session last did — the one
    continuity breadcrumb EXIT can honestly stamp (the record-free exit can't
    re-ask the session for a narrative, so this is derived, not authored)."""
    if role == "operator":
        return "operator — backlog cleared (no actionable task left)"
    if task_id and task_id != "~":
        return f"{role} on {task_id} — reached {task_status or 'done'}"
    return f"{role} — work done"


def compute_resume_hint(role: str, task_id: Any, task_status: str,
                        last_work_summary: str) -> dict:
    """The exit-resume hint: whether/when resuming THIS session beats a fresh
    start from documented progress (SOURCE-VERBATIM Part A).

    Policy — resume is DISCOURAGED after a documented-done exit (the deliverable
    is the source of truth and the history stays searchable); it is only
    RECOMMENDED when the session exits with work still in flight that the
    deliverable does not yet capture. Returns the four DoD fields.
    """
    documented_done = role == "operator" or (task_status in DONE_STATUSES)
    if role == "operator":
        reason = (
            "operator exited on an empty backlog; it is re-driven FRESH from the "
            "backlog artifact when work lands (operator_redrive respawns, it does "
            "not resume), so resuming this conversation adds nothing."
        )
        when = (
            "not needed for continuity — the next operator respawns from the "
            "backlog; resume only to audit THIS run's orchestration reasoning."
        )
    elif documented_done:
        reason = (
            f"work committed and reported (status {task_status or 'done'}); the "
            "deliverable + progress notes are the source of truth and the session "
            "history stays searchable, so a fresh session plans from the "
            "deliverable — a full --resume reread is discouraged."
        )
        when = (
            f"resume only if {task_id} is REOPENED and you need in-context "
            "reasoning the commit + progress notes don't capture; otherwise start "
            "fresh from the deliverable and search the exited history."
        )
    else:
        # Work interrupted (a non-terminal status reaching an exit path): the
        # deliverable does NOT yet capture the in-context state, so resume wins.
        reason = (
            f"session exited with work still in progress (status "
            f"{task_status or 'unknown'}); resuming preserves in-context state "
            "not yet in the deliverable."
        )
        when = "resume now — before starting any fresh session on this work."
    return {
        "resume_recommended": not documented_done,
        "reason": reason,
        "when": when,
        "last_work_summary": last_work_summary,
    }


# --- side-effecting seam (monkeypatched in tests) ---------------------------

def _suspend(cfg: Any, slug: str, sid: str) -> None:
    """Graceful EXIT = clear the pane, NO relaunch (the deliverable exists). The
    ``source``/``reason`` make the auto-close visible on the Processes badge
    (T-0444)."""
    sessions.suspend(cfg, slug, sid, source="graceful-exit",
                     reason="work done — graceful exit (T-0465)")


def _stamp_resume_hint(cfg: Any, slug: str, sid: str, role: str,
                       task_id: Any, task_status: str) -> dict:
    """Stamp the T-0468 exit-resume hint onto the just-suspended session md.

    Stored as FLAT frontmatter scalars (mirroring suspend_source/reason, T-0444)
    — the line-oriented frontmatter round-trips bool + colon-strings safely, a
    nested dict does not. Stamped AFTER ``_suspend`` because ``sessions.suspend``
    rebuilds the md (a fresh dict) on the live-pane path and would drop a
    stamp-before. ``decide_dispatch`` reads these keys back. Best-effort: a stamp
    failure must never undo the exit (the session is already suspended)."""
    hint = compute_resume_hint(
        role, task_id, task_status, _last_work_summary(role, task_id, task_status))
    meta_file = sessions._session_file(cfg.data_dir, slug, sid)
    existing = sessions._read_session_metadata(meta_file) or {}
    existing["resume_recommended"] = hint["resume_recommended"]
    existing["resume_hint_reason"] = hint["reason"]
    existing["resume_hint_when"] = hint["when"]
    existing["last_work_summary"] = hint["last_work_summary"]
    sessions._write_session_metadata(meta_file, existing, atomic=True)
    return hint


def _idle_age(row: dict, user_home: str, now: float) -> float | None:
    """Seconds since the session's last Claude turn (jsonl mtime), or None — the
    same idle clock idle_timeout uses (THE IDLE CLOCK, jsonl-only)."""
    cwd = str(row.get("cwd") or "")
    claude_uuid = row.get("claude_uuid")
    at = sessions._pane_activity_at(cwd, claude_uuid, user_home)
    if at is None:
        return None
    return max(0.0, now - at)


# --- T-0945: handoff before the exit ----------------------------------------

# In-flight state for the pre-exit handoff. Flat scalars on the session md, the
# same storage and the same hook contract as idle_timeout's ``idle_recycle_*``
# (they are in ``session_start.sh``'s ``_INFLIGHT_RECYCLE`` set, so a hook fire
# on a NEW life clears them rather than handing this tick a stale finalize).
_EXIT_HANDOFF_FIELDS = ("exit_handoff_phase", "exit_handoff_armed_at",
                        "exit_handoff_mark")


def exit_handoff_enabled() -> bool:
    """T-0945: ask a task-bound session to write its ``## Context`` before the
    work-done exit. ``BOT_SQUAD_EXIT_HANDOFF=0`` restores the pre-T-0945
    record-free exit."""
    return os.environ.get("BOT_SQUAD_EXIT_HANDOFF", "1") != "0"


def _clear_exit_handoff(meta: dict) -> None:
    for k in _EXIT_HANDOFF_FIELDS:
        meta.pop(k, None)


def _maybe_arm_exit_handoff(cfg: Any, slug: str, sid: str, meta: dict, md_path,
                            task_id: Any, now: float, pane: str) -> bool | None:
    """Drive the pre-exit handoff. Returns:

    * ``True``  — an action was taken this tick (the handoff was just armed);
      the caller must NOT exit yet.
    * ``False`` — still waiting for the write; the caller must not exit either.
    * ``None``  — nothing to wait for (written, timed out, disabled, or no
      destination); the caller proceeds to the exit.

    The wait is bounded by ``autocompact.handoff_timeout_sec`` and NEVER
    wedges: a session that ignores the ask is exited anyway. NO ``/compact`` is
    ever sent from here — see the module docstring.
    """
    if not exit_handoff_enabled():
        return None
    task_md = autocompact.task_md_path(cfg, slug, str(task_id))
    if not task_md:
        return None  # no ticket to write onto — nothing to hand off to

    phase = meta.get("exit_handoff_phase")
    if phase == "writing":
        armed_at = sessions._parse_ts_epoch(meta.get("exit_handoff_armed_at")) or now
        mark = autocompact.context_digest(task_md)
        wrote = bool(mark) and mark != meta.get("exit_handoff_mark")
        if not wrote and (now - armed_at) <= autocompact.handoff_timeout_sec():
            return False  # still writing — retry next tick
        if not wrote:
            log.warning("graceful_exit: %s never updated %s's ## Context within "
                        "the handoff window — exiting anyway (never wedge)",
                        sid, task_id)
        _clear_exit_handoff(meta)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return None

    if not autocompact.composer_free(autocompact._capture_pane(pane), sid=sid,
                                     now=now):
        return False  # half-typed draft — never inject over it
    try:
        autocompact._inject_context_handoff(sid, str(task_id), relaunch=False)
    except Exception:
        log.exception("graceful_exit: exit handoff inject failed for %s "
                      "(will retry)", sid)
        return False
    meta["exit_handoff_phase"] = "writing"
    meta["exit_handoff_armed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(now))
    meta["exit_handoff_mark"] = autocompact.context_digest(task_md)
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("graceful_exit: asked %s to bring %s's ## Context up to date "
             "before exiting (T-0945: handoff, no compact)", sid, task_id)
    return True


# --- per-session executor ---------------------------------------------------

def maybe_exit(cfg: Any, slug: str, row: dict, now: float, user_home: str) -> bool:
    """Suspend ``row``'s session if its work is DONE AND it has gone quiet AND its
    pane is idle/composer-ready. Returns True iff it was suspended this tick.
    Every gate fails closed (defer to the next tick rather than cut live work)."""
    if not graceful_exit_enabled():
        return False
    sid = row.get("sid")
    if not sid or row.get("status") != "active":
        return False

    role = row.get("role") or sessions._derive_role(
        row.get("window"), row.get("task_id"), row.get("initiative"))
    task_id = row.get("task_id")

    # Done-signal — role selects which trigger; compute only the one we need.
    pending = 0
    task_status = ""
    initiative_pending = None
    if role == "operator":
        from bot_squad_worker import operator_redrive
        pending = operator_redrive.count_pending_backlog(cfg, slug)
    elif task_id and task_id != "~":
        from bot_squad_worker import recovery
        task_status = recovery.read_task_status(cfg, slug, task_id)
    elif role == "teamlead":
        initiative = row.get("initiative")
        if initiative and initiative != "~":
            initiative_pending = count_pending_initiative_tasks(cfg, slug, initiative)
    if not work_done(role, task_id, task_status, pending,
                     initiative_pending=initiative_pending):
        return False

    # GRACE: only exit a session that has actually gone quiet (finished its own
    # done-sequence). A fresh/unknowable idle age defers to the next tick.
    if not exit_due(_idle_age(row, user_home, now), exit_grace_sec()):
        return False

    # The pane must still exist and be idle/composer-ready to clear safely. A
    # vanished pane means the session already exited — nothing to do.
    pane = autocompact._pane_for(sid)
    if not pane:
        return False
    if not autocompact.composer_ready(autocompact._capture_pane(pane),
                                      sid=sid, now=now):
        return False

    # T-0945: this path had NO recycle_gate check — a done session the human had
    # attached to (or explicitly pinned) was suspended out from under him. Both
    # signals now defer the exit for as long as they last; nothing is lost by
    # waiting, since the work is already done and the deliverable already
    # exists.
    md_path = sessions._session_file(cfg.data_dir, slug, sid)
    meta = sessions._read_session_metadata(md_path) or {}
    if recycle_gate.session_pinned(meta):
        if recycle_gate.should_log_skip(f"exit-pinned:{sid}", now):
            log.info("graceful_exit: %s is done but PINNED — holding the exit "
                     "(T-0945)", sid)
        return False
    if recycle_gate.is_attached(pane, sid=sid, now=now):
        if recycle_gate.should_log_skip(f"exit-attached:{sid}", now):
            log.info("graceful_exit: %s is done but a human client is attached "
                     "— holding the exit (T-0945)", sid)
        return False

    # T-0945: «можно будет начать новую из тикета» — so bring the ticket up to
    # date first. Bounded, never wedges, and never spends a /compact.
    if task_id and task_id != "~":
        handoff = _maybe_arm_exit_handoff(cfg, slug, sid, meta, md_path,
                                          task_id, now, pane)
        if handoff is not None:
            return handoff

    try:
        _suspend(cfg, slug, sid)
    except Exception:
        log.exception("graceful_exit: suspend failed for %s (will retry)", sid)
        return False
    # T-0468: stamp the exit-resume hint onto the now-suspended md so the
    # operator's reuse-vs-spawn decision can state resume-vs-fresh. Best-effort —
    # the session is already exited; a stamp failure must not flip the result.
    try:
        _stamp_resume_hint(cfg, slug, sid, role, task_id, task_status)
    except Exception:
        log.exception("graceful_exit: resume-hint stamp failed for %s", sid)
    log.info("graceful_exit: %s (%s) work done — suspended (no relaunch)", sid, role)
    return True


# --- scheduler entry --------------------------------------------------------

def tick(cfg: Any) -> None:
    """Per-project sweep: suspend sessions whose assignment is done.

    Sibling of the idle_timeout / telemetry 60s ticks. Per-session and
    per-project errors are swallowed so one bad session/project never kills the
    sweep.
    """
    if not graceful_exit_enabled():
        return
    user_home = sessions._get_user_home()
    cur_user = sessions._get_current_user()
    now = time.time()
    for slug in cfg.projects:
        try:
            rows = sessions.list_sessions(cfg, slug)
        except Exception:
            log.exception("graceful_exit.tick: list_sessions failed for %s", slug)
            continue
        for row in rows:
            if row.get("status") != "active":
                continue
            # Per-user worker reads only its own ~/.claude (the idle clock lives
            # under a home we can't stat for other users).
            if (row.get("linux_user") or cur_user) != cur_user:
                continue
            try:
                maybe_exit(cfg, slug, row, now, user_home)
            except Exception:
                log.exception("graceful_exit.tick: %s failed", row.get("sid"))
