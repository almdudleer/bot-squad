"""WS-4 S4 (T-0251) — auto stall-recovery: dead/exited respawn-or-park.

Removes the operator's manual nursing for the most unambiguous failure: a dev
session whose tmux pane has DIED while its bound task still needs work. We
respawn a fresh dev for that task (bounded), and once the respawn bound is hit
we PARK it + notify the operator instead of looping. A dead pane carries NO risk
of killing live work — the aggressive idle-wedge SIGTERM of *live* sessions is a
separate, day-1-tuned slice and is NOT in this module.

Conservative recs (G1-G5, operator-approved 2026-06-20): dev-only (coordinators
self-manage), respawn bound 2 then park, global. Kill-switch
``BOT_SQUAD_RECOVERY`` defaults **OFF** so it cannot surprise a live run — the
operator opts in (``BOT_SQUAD_RECOVERY=1``) once the backoff governor has proven
stable. Respawn goes through the normal spawn admission, so the backoff governor
+ cap still gate it (a respawn won't fire while at effective capacity).

State (respawn counters): ``data/_worker/recovery/state.json`` ``{sid: count}``.
The tick never raises (mirrors the other scheduler ticks).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Canonical task statuses (see reference_task_status_schema).
ACTIVE_STATUSES = {"open", "in_progress", "reopened"}  # still needs work → recover
DONE_STATUSES = {"totest", "closed"}                   # deliverable exists → leave to T-0233


def recovery_enabled() -> bool:
    return os.environ.get("BOT_SQUAD_RECOVERY", "0").strip() == "1"


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

def classify(*, role: str, pane_live: bool, task_status: str,
             respawn_count: int, bound: int) -> str:
    """Pure recovery decision → one of none|respawn|park.

    Only acts on a DEAD-pane dev whose task still needs work; a live pane and a
    done/planned task are both left alone in this slice.
    """
    if role != "dev":
        return "none"
    if pane_live:
        return "none"  # live work is never touched here
    if task_status in ACTIVE_STATUSES:
        return "respawn" if respawn_count < bound else "park"
    return "none"


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

def _gather(cfg: Any) -> list[dict]:
    """One row per non-archived dev session: {sid, slug, role, pane_live,
    task_id, task_status}."""
    from bot_squad_worker.sessions import (
        _read_session_metadata, _derive_role, live_pane_map)
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
            role = meta.get("role") or _derive_role(
                meta.get("window"), meta.get("task_id"), meta.get("initiative"))
            if role != "dev":
                continue
            task_id = meta.get("task_id")
            if not task_id or task_id == "~":
                continue
            pane_live = meta.get("sid") in pane_map
            rows.append({
                "sid": meta.get("sid"), "slug": slug, "role": role,
                "pane_live": pane_live, "task_id": task_id,
                "task_status": read_task_status(cfg, slug, task_id),
                "window": meta.get("window") or meta.get("window_name") or "",
                "initiative": meta.get("initiative"),
            })
    return rows


# --- apply (real, conservative) --------------------------------------------

def _do_respawn(cfg: Any, row: dict) -> None:
    from bot_squad_worker import sessions as _sessions
    prompt = (f"You are an auto-respawn for ticket {row['task_id']} — your prior "
              f"session exited. Re-read the ticket and continue from its last "
              f"progress note / your last summary. Do not restart finished work.")
    log.warning("recovery: respawning dead dev %s for %s", row.get("sid"), row["task_id"])
    _sessions.spawn(
        cfg, row["slug"], row.get("window") or f"recover-{row['task_id']}",
        prompt, task_id=row["task_id"], initiative=row.get("initiative"),
    )


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


# --- the tick --------------------------------------------------------------

def recovery_tick(cfg: Any, now_epoch: Optional[float] = None) -> dict:
    if not recovery_enabled():
        return {"enabled": False, "acted": []}
    try:
        return _run(cfg)
    except Exception:
        log.exception("recovery_tick error")
        return {"enabled": True, "acted": [], "error": True}


def _run(cfg: Any) -> dict:
    bound = respawn_bound()
    state = _load_state(cfg)
    acted: list = []
    for row in _gather(cfg):
        sid = row.get("sid")
        if not sid:
            continue
        count = int(state.get(sid, 0))
        action = classify(role=row["role"], pane_live=row["pane_live"],
                          task_status=row["task_status"], respawn_count=count,
                          bound=bound)
        if action == "respawn":
            _do_respawn(cfg, row)
            state[sid] = count + 1
            acted.append(("respawn", sid))
        elif action == "park":
            _do_park(cfg, row, reason=f"respawn bound {bound} reached")
            acted.append(("park", sid))
    _save_state(cfg, state)
    return {"enabled": True, "acted": acted}
