"""Crash recovery for ungraceful exits — dead/orphaned session reconcile.

WS-4 S4 (T-0251) shipped the first slice: a dev session whose tmux pane had
DIED while its bound task still needed work was respawned/parked. T-0471
(Process Paradigm M1/F1.8) generalizes it so the system does NOT rely only on
the graceful autocompact exit:

  * EVERY role (operator / team-lead / dev), not just devs, is recovered;
  * a BOOT-TIME reconcile pass runs once on worker startup so a session that
    died on a server restart (and never got to gracefully compact) is brought
    back — ``boot_reconcile`` is wired as a one-shot job in ``__main__``;
  * a crashed role is re-driven from its ROLE ARTIFACT (T-0467 / T-0473) + task
    state via the SAME ``autocompact._relaunch_from_artifact`` path the graceful
    compact uses — NOT from the lost in-context work; if no artifact has been
    written yet, we fall back to a generic re-read-the-ticket respawn from task
    state.

Crash signature (the heartbeat/liveness reconcile): a session whose md still
says ``status: active`` but whose pane is DEAD. A gracefully-suspended session
carries ``status: suspended`` and is left alone — that is intent, not a crash.
A dead pane carries NO risk of killing live work, so this never SIGTERMs a live
pane (the idle-wedge slice is separate).

After a successful re-drive the dead predecessor md is ARCHIVED so the next
pass / boot does not recover it again (binding_gc only dedups task-bound
sessions; a role-only operator/TL would otherwise re-drive every pass).

Switches:
  * ``BOT_SQUAD_RECOVERY`` (default OFF) gates the periodic ``recovery_tick`` —
    opt-in so it can't surprise a live run.
  * ``BOT_SQUAD_BOOT_RECONCILE`` (default ON) gates the boot-time pass — crash
    recovery on restart is a first-class guarantee (clarification-04), but
    still has a kill-switch.
Respawn goes through the normal spawn admission, so the backoff governor + cap
still gate it. ``state.json`` keys per-SID respawn counters; the tick never
raises (mirrors the other scheduler ticks).

T-0563/T-0564 (recycle-v2): both entry points (``recovery_tick`` AND
``boot_reconcile``) now consult :mod:`bot_squad_worker.recycle_gate` per
gathered row — a per-project allowlist (T-0563) plus a user-conversation-role
exemption (T-0564). The human-attached half of T-0564 is a structural no-op
here: ``_gather`` only ever yields rows with a DEAD pane (``classify`` never
respawns/parks a live one), and a dead pane can have no attached tmux client —
but the SAME shared gate is still called (with ``tmux_target=None``) so the
policy lives in exactly one place for all three recycle paths.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from bot_squad_worker import recycle_gate

log = logging.getLogger(__name__)

# Canonical task statuses (see reference_task_status_schema).
ACTIVE_STATUSES = {"open", "in_progress", "reopened"}  # still needs work → recover
DONE_STATUSES = {"totest", "closed"}                   # deliverable exists → leave it


def recovery_enabled() -> bool:
    """Periodic ``recovery_tick`` switch — default OFF (opt-in for a live run)."""
    return os.environ.get("BOT_SQUAD_RECOVERY", "0").strip() == "1"


def boot_reconcile_enabled() -> bool:
    """Boot-time reconcile switch — default ON (crash recovery on restart is a
    first-class guarantee per clarification-04), with a kill-switch."""
    return os.environ.get("BOT_SQUAD_BOOT_RECONCILE", "1").strip() != "0"


def respawn_bound() -> int:
    try:
        v = int(os.environ.get("BOT_SQUAD_RESPAWN_MAX", 2))
    except (TypeError, ValueError):
        return 2
    return v if v > 0 else 2


def read_task_status(cfg: Any, slug: str, task_id: str) -> str:
    """Re-read a backlog task's frontmatter to get its current status.

    Inlined from the (removed) autonomous orchestrator in T-0403 — recovery is
    the sole remaining consumer. Matches by filename prefix first, then by the
    ``id`` frontmatter field; returns ``""`` when the task can't be read.
    """
    from bot_squad_worker import frontmatter as _frontmatter

    backlog_dir = cfg.data_dir / slug / "backlog"
    if not backlog_dir.exists():
        return ""

    def _status(md_file: Path) -> str:
        try:
            parsed = _frontmatter.parse_or_none(md_file.read_text())
        except OSError:
            return ""
        if parsed is None:
            return ""
        return str(parsed[0].get("status", ""))

    for md_file in backlog_dir.glob("*.md"):
        if md_file.name.startswith(task_id):
            return _status(md_file)
    for md_file in backlog_dir.glob("*.md"):
        try:
            parsed = _frontmatter.parse_or_none(md_file.read_text())
        except OSError:
            continue
        if parsed is not None and parsed[0].get("id") == task_id:
            return str(parsed[0].get("status", ""))
    return ""


# --- pure decision ---------------------------------------------------------

def classify(*, pane_live: bool, task_status: str, has_artifact: bool,
             respawn_count: int, bound: int) -> str:
    """Pure recovery decision → one of none|respawn|park (role-agnostic).

    Acts on a DEAD-pane session that has recoverable forward-state — either its
    task still needs work, or it wrote a role artifact (the graceful-compact
    sink) before dying. A live pane is never touched (live work is sacred); a
    DONE task is left alone (the deliverable exists); a session with neither an
    active task nor an artifact has nothing to recover from.
    """
    if pane_live:
        return "none"  # live work is never touched here
    if task_status in DONE_STATUSES:
        return "none"  # deliverable exists → leave to stale-archive
    recoverable = (task_status in ACTIVE_STATUSES) or has_artifact
    if not recoverable:
        return "none"
    return "respawn" if respawn_count < bound else "park"


# --- state -----------------------------------------------------------------

def _state_path(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "recovery" / "state.json"


def _load_state(cfg: Any) -> dict:
    try:
        d = json.loads(_state_path(cfg).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(cfg: Any, state: dict) -> None:
    p = _state_path(cfg)
    # T-0373: unique-tmp atomic write (state file is single-writer — apscheduler
    # max_instances=1 — so no lock needed, but a unique tmp matches the task-md
    # writers and is clobber-proof if a tick ever overlaps).
    from bot_squad_worker.mdlock import atomic_write
    atomic_write(p, json.dumps(state, indent=1))


# --- signal gathering (real) -----------------------------------------------

def _gather(cfg: Any, now: float | None = None) -> list[dict]:
    """One row per CRASHED session (md ``status: active`` but DEAD pane), any
    role. Each row carries: {sid, slug, role, pane_live, task_id, task_status,
    window, initiative, artifact_path, has_artifact}.

    Skips archived mds (already history) and non-``active`` mds — a
    ``suspended``/``paused`` session is intentional, not a crash. T-0563/T-0564:
    also skips a session gated by :func:`recycle_gate.recycle_allowed`
    (non-allowlisted project or a user-conversation role) before it ever
    reaches ``classify``.
    """
    from bot_squad_worker import assignment as _assignment
    from bot_squad_worker.sessions import (
        _read_session_metadata, _derive_role, live_pane_map)
    now = now if now is not None else time.time()
    pane_map = live_pane_map()  # SID -> live pane, the truth (md pane_id is empty)
    rows: list[dict] = []
    for slug in getattr(cfg, "projects", {}) or {}:
        sess_dir = cfg.data_dir / slug / "sessions"
        if not sess_dir.exists():
            continue
        for md in sess_dir.glob("*.md"):
            meta = _read_session_metadata(md)
            if not meta or str(meta.get("archived", "")).lower() == "true":
                continue
            # Only a session the system still believes is RUNNING can have
            # crashed. A suspended/paused md is deliberate (graceful exit /
            # operator pause) — not an ungraceful death.
            if str(meta.get("status", "")).lower() != "active":
                continue
            role = meta.get("role") or _derive_role(
                meta.get("window"), meta.get("task_id"), meta.get("initiative"))
            # T-0563/T-0564/T-0616: never recycle a non-allowlisted project or
            # any of the human's own sessions — user-conversation role,
            # hand-launched user-session window, recycle_exempt md marker (a
            # dead pane here can never be human-attached, so tmux_target=None
            # is structurally correct).
            if not recycle_gate.recycle_allowed(cfg, slug=slug, role=role,
                                                tmux_target=None, now=now,
                                                window=meta.get("window"),
                                                meta=meta):
                continue
            sid = meta.get("sid")
            task_id = meta.get("task_id")
            task_id = task_id if (task_id and task_id != "~") else None
            pane_live = sid in pane_map
            art = _assignment.role_artifact(
                cfg.data_dir, slug, role=role, sid=sid, task_id=task_id)
            has_artifact = bool(art) and art.exists() and _nonempty(art.path)
            rows.append({
                "sid": sid, "slug": slug, "role": role,
                "pane_live": pane_live, "task_id": task_id,
                "task_status": read_task_status(cfg, slug, task_id) if task_id else "",
                "window": meta.get("window") or meta.get("window_name") or "",
                "initiative": meta.get("initiative"),
                "artifact_path": str(art.path) if art else None,
                "has_artifact": has_artifact,
            })
    return rows


def _nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


# --- apply (real, conservative) --------------------------------------------

def _retire_dead(cfg: Any, slug: str, sid: str) -> None:
    """Archive the dead predecessor md so the next pass / boot does not recover
    it again. Best-effort — a failed archive must not block the re-drive (the
    respawn bound caps any resulting churn). Reaps only the chat/telemetry
    sidecars, NOT the role artifact (which the fresh incarnation still reads)."""
    from bot_squad_worker import sessions as _sessions
    try:
        _sessions.archive_session(cfg, slug, sid)
    except Exception:
        log.exception("recovery: could not retire dead predecessor %s", sid)


def _do_respawn(cfg: Any, row: dict) -> None:
    """Re-drive a crashed session. Artifact-driven when the role artifact exists
    (boot a fresh incarnation from it via the graceful-compact reload path),
    else a generic respawn from task state. Retires the dead predecessor after."""
    slug = row["slug"]
    sid = row.get("sid")
    artifact_path = row.get("artifact_path")
    if row.get("has_artifact") and artifact_path:
        from bot_squad_worker import autocompact as _autocompact
        log.warning("recovery: re-driving crashed %s (%s) from artifact %s",
                    sid, row.get("role"), artifact_path)
        # row carries sid/task_id/role/window — the rec shape _relaunch expects.
        _autocompact._relaunch_from_artifact(cfg, slug, row, artifact_path)
    else:
        from bot_squad_worker import sessions as _sessions
        task_id = row.get("task_id")
        prompt = (f"You are an auto-respawn for ticket {task_id} — your prior "
                  f"session exited ungracefully (no forward-state artifact was "
                  f"written). Re-read the ticket and continue from its last "
                  f"progress note. Do not restart finished work.")
        log.warning("recovery: respawning crashed %s for %s (no artifact yet)",
                    sid, task_id)
        _sessions.spawn(
            cfg, slug, row.get("window") or f"recover-{task_id}",
            prompt, task_id=task_id, initiative=row.get("initiative"),
        )
    if sid:
        # T-0470: a crashed session was re-driven → record the recycle on the
        # unified lifecycle surface (operator measurement), before retiring the
        # dead predecessor. Best-effort; never raises.
        from bot_squad_worker import lifecycle_events as _lc
        _lc.emit(cfg, slug, sid, _lc.SESSION_RECYCLED, cause="recovery",
                 role=row.get("role"), task_id=row.get("task_id"))
        _retire_dead(cfg, slug, sid)


def _do_park(cfg: Any, row: dict, reason: str) -> None:
    from bot_squad_worker import sessions as _sessions
    log.warning("recovery: parking %s (%s)", row.get("sid"), reason)
    try:
        _sessions.suspend(cfg, row["slug"], row["sid"])
    except Exception:
        log.exception("recovery: suspend failed for %s", row.get("sid"))
    try:
        from bot_squad_worker import intersession as _inter
        _inter.send(cfg, row["slug"], to="operator",
                    text=(f"⚠️ recovery PARKED {row.get('sid')} (task {row['task_id']}): "
                          f"{reason}. Needs a human look — auto-respawn gave up."),
                    from_sid="S-recovery")
    except Exception:
        log.exception("recovery: park notify failed for %s", row.get("sid"))


# --- the passes ------------------------------------------------------------

def recovery_tick(cfg: Any, now_epoch: Optional[float] = None) -> dict:
    """Periodic stall-recovery pass (gated by ``BOT_SQUAD_RECOVERY``, default OFF)."""
    if not recovery_enabled():
        return {"enabled": False, "acted": []}
    try:
        return _run(cfg, source="tick", now=now_epoch)
    except Exception:
        log.exception("recovery_tick error")
        return {"enabled": True, "acted": [], "error": True}


def boot_reconcile(cfg: Any) -> dict:
    """One-shot boot-time crash reconcile (T-0471). Detects sessions whose md is
    still ``active`` but whose pane is dead — the ungraceful-exit / server-
    restart signature the graceful autocompact never got to handle — and
    re-drives each from its role artifact + task state. Runs regardless of the
    periodic ``BOT_SQUAD_RECOVERY`` switch; gated by ``BOT_SQUAD_BOOT_RECONCILE``
    (default ON). Never raises (wired as a scheduler job)."""
    if not boot_reconcile_enabled():
        log.info("boot_reconcile: disabled (BOT_SQUAD_BOOT_RECONCILE=0)")
        return {"boot": True, "enabled": False, "acted": []}
    try:
        out = _run(cfg, source="boot")
        out["boot"] = True
        log.info("boot_reconcile: reconciled %d crashed session(s): %s",
                 len(out.get("acted", [])), out.get("acted"))
        return out
    except Exception:
        log.exception("boot_reconcile error")
        return {"boot": True, "enabled": True, "acted": [], "error": True}


def _run(cfg: Any, source: str = "tick", now: Optional[float] = None) -> dict:
    bound = respawn_bound()
    state = _load_state(cfg)
    acted: list = []
    for row in _gather(cfg, now):
        sid = row.get("sid")
        if not sid:
            continue
        count = int(state.get(sid, 0))
        action = classify(pane_live=row["pane_live"], task_status=row["task_status"],
                          has_artifact=row.get("has_artifact", False),
                          respawn_count=count, bound=bound)
        if action == "respawn":
            try:
                _do_respawn(cfg, row)
                acted.append(("respawn", sid))
            except Exception:
                log.exception("recovery: respawn failed for %s", sid)
                acted.append(("respawn-failed", sid))
            state[sid] = count + 1  # count the attempt toward the bound either way
        elif action == "park":
            _do_park(cfg, row, reason=f"respawn bound {bound} reached")
            acted.append(("park", sid))
    _save_state(cfg, state)
    return {"enabled": True, "source": source, "acted": acted}
