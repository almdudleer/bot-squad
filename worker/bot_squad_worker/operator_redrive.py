"""Operator re-drive cadence (T-0474) — Process Paradigm M2 / F2.1.

What keeps the operator *continuously scheduled WITHOUT a persistent session*.
clarification-01: the operator "is kept being re-driven while work is on (and is
paused when the user pauses / stops when it stalls out of time) — NOT a
never-recycled process"; clarification-03: "when on it always has a task to clear
the backlog". The operator's state lives in its artifact (operator-state.md,
T-0473) and continuity is achieved by *re-driving* — this tick — not by keeping a
conversation alive.

A 60s scheduler tick (:func:`operator_tick`, wired in ``scheduler.py``) walks
every project and, for each:

* **re-drives** (respawns the operator with its standing task) when the backlog
  has pending work AND the user has not paused AND no operator is currently live;
* **continues** (no-op) when an operator is already live — exactly one operator
  drives a project at a time (T-0472), and a within-time-box live operator is
  left alone for the universal lifecycle (idle_timeout / autopilot) to end;
* **stops** re-driving when the user has paused (a per-project pause flag) or
  when the operator stalls out of its time/quota budget — the latter is honored
  for free by routing the respawn through the normal spawn-admission path, which
  defers under capacity / quota backpressure (T-0250 / backoff_tick). Deferring
  into a wall instead of respawning is precisely "NOT a never-recycled process".

Design notes
------------
* Continue-vs-respawn rides :func:`dispatch.live_operator_sids` — the SAME seam
  the T-0472 one-operator spawn guard uses, so the tick can never double-drive.
* The respawn brief is :func:`dispatch.operator_standing_task` — the SSOT for
  the "clear the backlog" directive (shared with the spawn-time brief).
* Purely scheduler-internal: no new worker socket action (TL directive — keep
  actions.py single-owner). The pause flag is a flag *file* with in-module
  helpers; wiring a ``bsq operator pause/resume`` verb is a follow-up.

Kill switch: ``BOT_SQUAD_OPERATOR_REDRIVE=0`` disables the tick entirely.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Per-project minimum gap between operator respawns, even when work is pending.
# Belt-and-braces (with the live_operator_sids gate) against an overlapping tick
# or a fast-exiting operator stampeding the spawn path. Override via env in tests.
_SPAWN_COOLDOWN_SEC = int(os.environ.get("BOT_SQUAD_OPERATOR_REDRIVE_COOLDOWN", "90"))

# A board task is "pending" (keeps the operator on) unless it is terminal. closed
# is the only terminal status (task status schema: planned/open/in_progress/
# totest/reopened/closed); an archived task is off-board intent. Everything else —
# including totest awaiting close — is still backlog the operator must clear, so
# "empty backlog (nothing actionable left) is the only idle state" (clarification-03).
_TERMINAL_STATUSES = frozenset({"closed"})


def _enabled() -> bool:
    """False iff the kill switch (``BOT_SQUAD_OPERATOR_REDRIVE``) disables re-drive."""
    raw = os.environ.get("BOT_SQUAD_OPERATOR_REDRIVE")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# State + user-pause flag (per project, under _worker/operator_redrive/)
# ---------------------------------------------------------------------------

def _state_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "operator_redrive"


def _pause_flag_path(cfg: Any, slug: str) -> Path:
    """Path of the per-project user-pause flag. Presence = paused (mirrors the
    autoupdate pause-flag pattern, T-0089: a flag file, not a config edit, so the
    pause is a cheap atomic toggle)."""
    return _state_dir(cfg, slug) / "operator_paused.flag"


def is_paused(cfg: Any, slug: str) -> bool:
    """True iff the user has paused the operator re-drive for ``slug``."""
    return _pause_flag_path(cfg, slug).exists()


def pause(cfg: Any, slug: str, *, by: str = "user", reason: str = "") -> dict:
    """Set the user-pause flag — re-drive stops until :func:`resume`. Returns the
    metadata written. Idempotent (re-pausing just rewrites the marker)."""
    d = _state_dir(cfg, slug)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"paused_by": by, "reason": reason, "paused_at": time.time()}
    p = _pause_flag_path(cfg, slug)
    tmp = p.with_suffix(".flag.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, p)
    log.info("operator_redrive: %s paused by %s — %s", slug, by, reason or "(no reason)")
    return meta


def resume(cfg: Any, slug: str) -> bool:
    """Clear the user-pause flag. Returns True iff it was actually set."""
    p = _pause_flag_path(cfg, slug)
    if p.exists():
        p.unlink()
        log.info("operator_redrive: %s resumed", slug)
        return True
    return False


def _state_file(cfg: Any, slug: str) -> Path:
    return _state_dir(cfg, slug) / "state.json"


def _load_state(cfg: Any, slug: str) -> dict:
    p = _state_file(cfg, slug)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(cfg: Any, slug: str, state: dict) -> None:
    d = _state_dir(cfg, slug)
    d.mkdir(parents=True, exist_ok=True)
    p = _state_file(cfg, slug)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Pending-backlog signal
# ---------------------------------------------------------------------------

def count_pending_backlog(cfg: Any, slug: str) -> int:
    """Count board tasks that still need clearing — top-level ``backlog/*.md``
    whose status is not terminal (``closed``) and which are not archived. The
    ``_gc/`` archive subdir is a child dir, so a top-level glob never re-counts
    already-archived tasks. An empty result is the operator's only idle state."""
    from bot_squad_worker import frontmatter as _fm

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
        if str(meta.get("archived", "")).strip().lower() in ("true", "yes", "1", "on"):
            continue
        status = str(meta.get("status", "")).strip().lower()
        if status in _TERMINAL_STATUSES:
            continue
        n += 1
    return n


# ---------------------------------------------------------------------------
# Re-drive (spawn) — purely scheduler-internal
# ---------------------------------------------------------------------------

def _respawn_operator(cfg: Any, slug: str) -> Optional[str]:
    """Spawn a fresh operator driving the standing 'clear the backlog' task.

    Returns the new SID, or None when the spawn is *deferred* (capacity / quota
    backpressure — "stalls out of time") or fails. The continue-vs-respawn gate
    (live_operator_sids) is applied by the caller, so this never double-drives.
    """
    from bot_squad_worker import sessions as S
    from bot_squad_worker import dispatch as _dispatch
    from bot_squad_worker.actions import ActionError

    try:
        res = S.spawn(
            cfg, slug, "operator",
            initial_prompt=_dispatch.operator_standing_task(),
            owner="operator-redrive",
        )
        return res.get("sid")
    except ActionError as e:
        # The parallel-session cap / quota backpressure is normal — the operator
        # has, in effect, stalled out of its time budget. Defer quietly (don't
        # ERROR-spam every 60s) so re-drive backs off instead of respawning into
        # a wall. This is what makes it "NOT a never-recycled process".
        if "capacity reached" in str(e):
            log.debug("operator_redrive: respawn deferred for %s (%s)", slug, e)
        else:
            log.exception("operator_redrive: respawn failed for %s", slug)
        return None
    except Exception:  # noqa: BLE001
        log.exception("operator_redrive: respawn failed for %s", slug)
        return None


def tick(cfg: Any, slug: str) -> dict:
    """One re-drive pass for a single project. Returns a small record dict
    ``{action, ...}`` describing what the pass decided (for tests + the journal).

    actions: ``disabled`` | ``paused`` | ``idle-empty-backlog`` | ``continue`` |
    ``cooldown`` | ``deferred`` | ``respawned``.
    """
    if not _enabled():
        return {"action": "disabled"}

    # STOP re-driving when the user has paused (clarification-01).
    if is_paused(cfg, slug):
        return {"action": "paused"}

    # "When on, it always has a task to clear the backlog"; an empty backlog
    # (nothing actionable left) is the only idle state (clarification-03).
    pending = count_pending_backlog(cfg, slug)
    if pending <= 0:
        return {"action": "idle-empty-backlog", "pending": 0}

    # Continue-vs-respawn rides the T-0472 seam: a live operator means one is
    # already driving (continue — no-op, exactly one per project); none means the
    # prior incarnation exited (or never started), so re-drive spawns a fresh one.
    from bot_squad_worker import dispatch as _dispatch
    live = _dispatch.live_operator_sids(cfg, slug)
    if live:
        return {"action": "continue", "operator": live[0], "pending": pending}

    # Cooldown gate (belt-and-braces against overlapping ticks / a fast-exiting
    # operator stampeding the spawn path).
    state = _load_state(cfg, slug)
    last_spawn = float(state.get("last_spawn_at", 0) or 0)
    if time.time() - last_spawn < _SPAWN_COOLDOWN_SEC:
        return {"action": "cooldown", "pending": pending}

    sid = _respawn_operator(cfg, slug)
    if not sid:
        # Spawn deferred under backpressure ("stalls out of time") — re-drive
        # backs off; a later tick retries when a slot / quota frees.
        return {"action": "deferred", "pending": pending}

    state["last_spawn_at"] = time.time()
    _save_state(cfg, slug, state)
    log.info("operator_redrive[%s]: re-drove operator %s (%d pending)", slug, sid, pending)
    return {"action": "respawned", "operator": sid, "pending": pending}


def operator_tick(cfg: Any) -> None:
    """Scheduler entry point (T-0474): one re-drive pass across every project.

    Per-project errors are caught and logged so one bad project never kills the
    sweep — same contract as the sibling lifecycle ticks. Registered on the 60s
    cadence in ``scheduler.py``.
    """
    for slug in cfg.projects:
        try:
            tick(cfg, slug)
        except Exception:  # noqa: BLE001
            log.exception("operator_tick: unhandled error for project %s", slug)
