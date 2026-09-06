"""T-0930 — automatic resume for WAITING sessions.

The stakeholder's ask (2026-08-30, live tmux, on T-0930): a session that
decides it is waiting on something (the stakeholder's answer to a blocking
question) should ``handoff + compact + exit`` and "hang itself a state that
it is not finished, waiting, that will be continued when the condition for
it is ready" — resumed automatically, not left for a human to notice and
type ``bsq spawn``/``sessions.resume`` by hand. That automatic trigger is
what this module is.

The wait-state itself is stamped by
:func:`idle_timeout._terminate_and_remember` at the moment a dev whose bound
task is ``blocked_on_user`` (T-0931) gets recycled: ``wait_reason:
"blocked_on_user"`` + ``wait_task_id: <task_id>`` alongside the existing
``resumable``/``resume_hint`` fields. This module's :func:`tick` watches
every SUSPENDED session carrying that stamp and, the moment the named task
LEAVES ``blocked_on_user`` (T-0931's transition graph only allows
``blocked_on_user -> {in_progress, closed}`` — either the stakeholder
answered and the block was manually or auto-lifted, or the task was closed
out from under it), calls :func:`sessions.resume` with no human in the loop.

Deliberately its own module rather than folded into :mod:`idle_timeout`
(which only ever RECYCLES, never resumes) or :mod:`graceful_exit` (which
only ever suspends DONE work, never resumes either) — this is the one place
in the worker that autonomously RESURRECTS a session, and keeping that
single responsibility in one small, kill-switched file makes it easy to
reason about and to disable independently of its siblings.

Kill switch: ``BOT_SQUAD_WAIT_RESUME=0`` disables the tick entirely (a
suspended WAITING session then simply sits until a human resumes it by
hand — the pre-T-0930 posture).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from bot_squad_worker import recovery, sessions

log = logging.getLogger(__name__)

#: The only wait reason today (T-0931's blocked_on_user). A string, not a
#: bool, on purpose — a future WAITING sub-kind (e.g. "waiting on a tracked
#: build across a recycle") gets its own value here rather than a second
#: field, so the tick's dispatch stays a single membership check.
BLOCKED_ON_USER = "blocked_on_user"


def wait_resume_enabled() -> bool:
    """Master switch (default ON, mirroring the sibling recyclers).
    ``BOT_SQUAD_WAIT_RESUME=0`` disables auto-resume — a WAITING session then
    waits for a human to notice and resume it by hand."""
    return os.environ.get("BOT_SQUAD_WAIT_RESUME", "1") != "0"


def condition_cleared(task_status: str) -> bool:
    """True the moment a blocked_on_user wait's condition has cleared — the
    task LEFT that status. Per T-0931's transition graph the only edges out
    of ``blocked_on_user`` are ``in_progress`` (the normal unblock) and
    ``closed`` (the stakeholder decided to drop it while it sat blocked) —
    both mean "stop waiting", so this is a plain not-still-blocked check
    rather than naming ``in_progress`` alone. An unreadable/missing status
    (deleted ticket) is conservatively NOT cleared — nothing to resume INTO."""
    return bool(task_status) and task_status != BLOCKED_ON_USER


def resume_prompt(task_id: str, task_status: str) -> str:
    return (
        f"Welcome back — {task_id} is no longer blocked_on_user (now "
        f"{task_status}). You were auto-resumed because the condition you "
        f"were waiting on cleared. Re-read the ticket's ## Context and the "
        f"stakeholder's latest reply/reasoning before continuing."
    )


def maybe_resume(cfg: Any, slug: str, sid: str, meta: dict) -> bool:
    """Resume ``sid`` if it is a suspended blocked_on_user WAITING session
    whose task has left that status. Every gate fails closed (defer to the
    next tick, never resume into a still-unresolved wait)."""
    if not wait_resume_enabled():
        return False
    if meta.get("status") != "suspended":
        return False
    if meta.get("wait_reason") != BLOCKED_ON_USER:
        return False
    task_id = meta.get("wait_task_id")
    if not task_id:
        return False
    status = recovery.read_task_status(cfg, slug, task_id)
    if not condition_cleared(status):
        return False
    try:
        sessions.resume(cfg, slug, sid, initial_prompt=resume_prompt(task_id, status))
    except Exception:
        log.exception("wait_resume: resume failed for %s (will retry next tick)", sid)
        return False
    log.info("wait_resume: auto-resumed %s — %s left blocked_on_user (now %s)",
             sid, task_id, status)
    return True


def tick(cfg: Any) -> None:
    """Per-project sweep: auto-resume every suspended blocked_on_user WAITING
    session whose task has been unblocked. Per-session/per-project errors are
    swallowed so one bad session never kills the sweep — same posture as
    idle_timeout/graceful_exit."""
    if not wait_resume_enabled():
        return
    from bot_squad_worker import automation as _automation
    cur_user = sessions._get_current_user()
    for slug in cfg.projects:
        # T-0929 — THE automation gate. Waking a suspended session is automatic
        # activity; while the switch is off it stays suspended, and the first
        # tick after he turns the drive back on resumes it.
        if not _automation.gate(cfg, slug, "wait_resume"):
            continue
        try:
            sessions_dir = cfg.data_dir / slug / "sessions"
            if not sessions_dir.exists():
                continue
            for md in sorted(sessions_dir.glob("*.md")):
                meta = sessions._read_session_metadata(md)
                if meta is None:
                    continue
                if meta.get("wait_reason") != BLOCKED_ON_USER:
                    continue
                # Per-user worker: only resume sessions this worker process
                # can actually see a live pane for (same filter idle_timeout/
                # graceful_exit apply via their `row["linux_user"]`).
                if (meta.get("linux_user") or cur_user) != cur_user:
                    continue
                sid = meta.get("sid") or md.stem
                try:
                    maybe_resume(cfg, slug, sid, meta)
                except Exception:
                    log.exception("wait_resume.tick: %s failed", sid)
        except Exception:
            log.exception("wait_resume.tick: project %s failed", slug)
