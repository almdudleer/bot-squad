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

Kill switch: ``BOT_SQUAD_GRACEFUL_EXIT=0`` disables the path entirely.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from bot_squad_worker import autocompact, sessions

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

def work_done(role: str, task_id: Any, task_status: str, pending_backlog: int) -> bool:
    """True when the session's assignment is complete — role-agnostic policy, the
    done-signal differing ONLY by role.

    * operator: backlog empty (``pending_backlog == 0``).
    * task-bound role (has a real ``task_id``): bound task is terminal.
    * otherwise (user-conversation / role-only): no auto-done signal → False.
    """
    if role == "operator":
        return pending_backlog == 0
    if task_id and task_id != "~":
        return task_status in DONE_STATUSES
    return False


def exit_due(idle_age: float | None, grace: int) -> bool:
    """True when the session has been quiet for at least ``grace`` seconds.

    ``idle_age`` is ``now - <jsonl mtime>``. ``None`` (no activity signal — can't
    establish the age) is conservative: NOT due, so a session whose idle age we
    cannot measure is never cut off mid-finish.
    """
    if idle_age is None:
        return False
    return idle_age >= grace


# --- side-effecting seam (monkeypatched in tests) ---------------------------

def _suspend(cfg: Any, slug: str, sid: str) -> None:
    """Graceful EXIT = clear the pane, NO relaunch (the deliverable exists). The
    ``source``/``reason`` make the auto-close visible on the Processes badge
    (T-0444)."""
    sessions.suspend(cfg, slug, sid, source="graceful-exit",
                     reason="work done — graceful exit (T-0465)")


def _idle_age(row: dict, user_home: str, now: float) -> float | None:
    """Seconds since the session's last Claude turn (jsonl mtime), or None — the
    same idle clock idle_timeout uses (THE IDLE CLOCK, jsonl-only)."""
    cwd = str(row.get("cwd") or "")
    claude_uuid = row.get("claude_uuid")
    at = sessions._pane_activity_at(cwd, claude_uuid, user_home)
    if at is None:
        return None
    return max(0.0, now - at)


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
    if role == "operator":
        from bot_squad_worker import operator_redrive
        pending = operator_redrive.count_pending_backlog(cfg, slug)
    elif task_id and task_id != "~":
        from bot_squad_worker import recovery
        task_status = recovery.read_task_status(cfg, slug, task_id)
    if not work_done(role, task_id, task_status, pending):
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
    if not autocompact.composer_ready(autocompact._capture_pane(pane)):
        return False

    try:
        _suspend(cfg, slug, sid)
    except Exception:
        log.exception("graceful_exit: suspend failed for %s (will retry)", sid)
        return False
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
