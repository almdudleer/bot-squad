"""T-0466 / M1-F1.3 — ~1h cache-window idle/waiting-session recycle + postpone.

Stakeholder (SOURCE-VERBATIM Part A): *"it should get recycled on timeout. Good
time for that is cache invalidation timeout, e.g. for claude subscription it's 1
hour. On timeout, stale waiting sessions should be asked to record their results
and exit. They should be able to postpone this until next timeout, allowed to
repeat indefinitely on each timeout. Generally they should postpone if they are
actively waiting for some long ongoing process to finish (e.g. long build),
which is expected to finish within known time boundaries."*

SIBLING of :mod:`autocompact` (T-0467), not a rival. autocompact recycles a
session when its CONTEXT crosses the ceiling; idle_timeout recycles a session
that has been *waiting* — no Claude turn means no API call, so the subscription
cache window goes cold — past the ~1h cache-invalidation window. Both end in the
SAME "record results and exit": the universal-compact handoff (write the full
forward-state to the role artifact → clear the pane → relaunch a fresh
incarnation that boots from the artifact, re-bound to the same assignment).

idle_timeout REUSES autocompact's handoff helpers for every concrete operation
(resolve-artifact / inject-prompt / artifact-mtime / suspend / relaunch /
orphan-alert) — it does NOT fork the exit. It only adds (1) a time-based TRIGGER
and (2) the postpone protocol. autocompact.py is left untouched; the in-flight
handoff + postpone STATE lives on the session md (a sessions-lifecycle concern),
so the two recyclers never share storage and can't race on each other's state.

THE IDLE CLOCK is the Claude transcript jsonl mtime (the last assistant turn ≈
the last API call ≈ the cache anchor), NOT the peer-bus heartbeat. A session
long-polling its inbox has a fresh heartbeat but a cold cache — and that IS the
"stale waiting session" the stakeholder means. Using ``_pane_activity_at``
(jsonl-only) instead of the row's heartbeat-folded ``activity_at`` is deliberate.

POSTPONE (per-window, unbounded): a session runs ``bsq postpone`` → the worker
stamps ``idle_postpone_until = now + window`` on its session md, so the tick
skips the next window. It can repeat every window, forever. ``bsq postpone --for
<seconds>`` stamps a longer deadline — the way a session declares it is waiting
on a bounded job with a known ETA (the stakeholder's "known time boundaries").

AUTO-POSTPONE: while the session is waiting on a *tracked* long bounded job the
recycle auto-defers with NO session action — killing it mid-build would throw
away the very wait it is parked on. The concrete bot-squad "build" is a deploy:
an in-flight ``_jobs/deploy/{queue,processing}/*.json`` with ``requested_by ==
sid`` auto-postpones the requester with zero registration.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from bot_squad_worker import autocompact, sessions

log = logging.getLogger(__name__)

# Default recycle window = the Claude subscription cache-invalidation window (~1h).
DEFAULT_IDLE_TIMEOUT_SEC = 3600


def idle_timeout_enabled() -> bool:
    """Master switch (default ON, mirroring :func:`autocompact.autocompact_enabled`).

    ``BOT_SQUAD_IDLE_TIMEOUT=0`` disables the time-based recycle (a deliberate
    operator kill-switch). The recycle is fully reversible — it writes the
    session's forward-state and relaunches it fresh — so ON-by-default is safe
    and matches the stakeholder's "it should get recycled on timeout".
    """
    return os.environ.get("BOT_SQUAD_IDLE_TIMEOUT", "1") != "0"


def idle_timeout_sec() -> int:
    """The idle/waiting recycle window in seconds (~1h cache window by default).

    Overridable via ``BOT_SQUAD_IDLE_TIMEOUT_SEC``; non-positive/garbage falls
    back to the default so a bad env can never collapse the window to zero (which
    would recycle every session on every tick).
    """
    raw = os.environ.get("BOT_SQUAD_IDLE_TIMEOUT_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_IDLE_TIMEOUT_SEC


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- pure decision helpers (unit-testable, no I/O) --------------------------

def idle_due(idle_age: float | None, window: int) -> bool:
    """True when the session's jsonl has been quiet for at least ``window``.

    ``idle_age`` is ``now - <jsonl mtime>`` (seconds). ``None`` (no activity
    signal — can't establish the age) is conservative: NOT due, so a session
    whose cache age we cannot measure is never recycled.
    """
    if idle_age is None:
        return False
    return idle_age >= window


def postpone_active(postpone_until: Any, now: float) -> bool:
    """True when a postpone deadline is set and still in the future."""
    ts = sessions._parse_ts_epoch(postpone_until)
    if ts is None:
        return False
    return now < ts


# --- tracked-long-job detection (auto-postpone) -----------------------------

def _inflight_deploy_for(cfg: Any, slug: str, sid: str) -> bool:
    """True when ``sid`` has a deploy queued or processing — the bot-squad
    analogue of the stakeholder's "long build". Reads the deploy job dir
    directly (queue/ + processing/) and matches ``requested_by``. Never raises:
    any read error reads as "no tracked job" so a transient fs hiccup can't wedge
    the recycle decision.
    """
    if not sid:
        return False
    base = cfg.data_dir / slug / "_jobs" / "deploy"
    for phase in ("processing", "queue"):
        d = base / phase
        if not d.exists():
            continue
        try:
            entries = sorted(d.glob("*.json"))
        except OSError:
            continue
        for f in entries:
            try:
                payload = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("requested_by") == sid:
                return True
    return False


def tracking_long_job(cfg: Any, slug: str, sid: str) -> bool:
    """True when the session is actively waiting on a tracked, time-bounded job.

    Today that is an in-flight deploy/build it requested. A session-declared
    bounded wait (``bsq postpone --for <seconds>``) rides the ``idle_postpone_until``
    stamp instead (handled by :func:`postpone_active`), so it is not re-checked
    here.
    """
    return _inflight_deploy_for(cfg, slug, sid)


# --- per-session executor ---------------------------------------------------

# In-flight handoff + postpone state lives as FLAT scalar md fields (never a
# nested mapping) so the line-based session_start hook reader stays happy.
_RECYCLE_FIELDS = (
    "idle_recycle_phase",
    "idle_recycle_armed_at",
    "idle_recycle_arm_mtime",
    "idle_recycle_artifact",
)


def _clear_recycle_state(meta: dict) -> None:
    for k in _RECYCLE_FIELDS:
        meta.pop(k, None)


def maybe_recycle(cfg: Any, slug: str, row: dict, now: float, user_home: str) -> bool:
    """Recycle ``row``'s session if it has been idle/waiting past the window AND
    safe AND not postponed. Returns True iff an action was taken this tick
    (handoff armed or finalized). Every gate fails closed.

    Drives a tiny 2-phase machine on the session md — ARM (inject the handoff
    prompt) then FINALIZE (suspend + relaunch) — reusing autocompact's helpers
    for each concrete step.
    """
    if not idle_timeout_enabled():
        return False
    sid = row.get("sid")
    if not sid or row.get("status") != "active":
        return False

    sessions_dir = cfg.data_dir / slug / "sessions"
    md_path = sessions._find_session_md(sessions_dir, sid, row.get("claude_uuid"))
    if md_path is None:
        return False
    meta = sessions._read_session_metadata(md_path)
    if meta is None:
        return False

    # A handoff already in flight → drive its finalize half (independent of the
    # idle window; the phase field is its own guard).
    if meta.get("idle_recycle_phase") == "writing":
        return _finalize(cfg, slug, sid, meta, md_path, now)

    # Otherwise decide whether to START a recycle this tick.
    idle_age = _idle_age(row, meta, user_home, now)
    if not idle_due(idle_age, idle_timeout_sec()):
        return False
    if postpone_active(meta.get("idle_postpone_until"), now):
        return False
    if tracking_long_job(cfg, slug, sid):
        # Auto-postpone: waiting on a tracked bounded job — defer (reactively, no
        # stamp) so the moment the job clears the normal window applies again.
        log.info("idle_timeout: auto-postpone %s — waiting on a tracked long job", sid)
        return False
    return _arm(cfg, slug, sid, row, meta, md_path, now)


def _idle_age(row: dict, meta: dict, user_home: str, now: float) -> float | None:
    """Seconds since the session's last Claude turn (jsonl mtime), or None.

    Deliberately jsonl-only (NOT the row's heartbeat-folded ``activity_at``) —
    see the module docstring on THE IDLE CLOCK.
    """
    cwd = str(row.get("cwd") or meta.get("cwd") or "")
    claude_uuid = row.get("claude_uuid") or meta.get("claude_uuid")
    at = sessions._pane_activity_at(cwd, claude_uuid, user_home)
    if at is None:
        return None
    return max(0.0, now - at)


def _arm(cfg: Any, slug: str, sid: str, row: dict, meta: dict, md_path, now: float) -> bool:
    """ARM the record-and-exit: inject the universal-compact handoff prompt and
    stamp ``idle_recycle_phase: writing``. Reuses autocompact's artifact
    resolution + prompt + inject seams."""
    rec = {"sid": sid, "role": row.get("role") or meta.get("role") or "",
           "task_id": row.get("task_id") or meta.get("task_id")}
    artifact_path, role, _assignment = autocompact._resolve_role_artifact(cfg, slug, rec)
    if not artifact_path:
        # No role artifact ⇒ no clean place to record forward-state. Unlike
        # autocompact (which falls back to Claude's /compact to claw back
        # context) an IDLE low-context session gains nothing from a /compact, so
        # we simply skip and re-evaluate next window.
        log.debug("idle_timeout: no role artifact for %s — skip recycle", sid)
        return False

    # Only ever inject into an idle, composer-ready pane — never cut mid-turn.
    pane = autocompact._pane_for(sid)
    if not pane or not autocompact.composer_ready(autocompact._capture_pane(pane)):
        return False
    try:
        autocompact._inject_handoff(sid, artifact_path, role)
    except Exception:
        log.exception("idle_timeout: handoff inject failed for %s (will retry)", sid)
        return False

    meta["idle_recycle_phase"] = "writing"
    meta["idle_recycle_armed_at"] = _now_iso()
    meta["idle_recycle_arm_mtime"] = autocompact._artifact_mtime(artifact_path)
    meta["idle_recycle_artifact"] = artifact_path
    sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("idle_timeout: armed cache-window handoff for %s → %s", sid, artifact_path)
    return True


def _finalize(cfg: Any, slug: str, sid: str, meta: dict, md_path, now: float) -> bool:
    """FINALIZE an armed handoff: once the session has written its artifact and
    the pane is idle, clear (suspend) + relaunch a fresh incarnation from the
    artifact. Mirrors :func:`autocompact._maybe_finalize`, reusing its helpers."""
    armed_at = sessions._parse_ts_epoch(meta.get("idle_recycle_armed_at")) or now
    try:
        arm_mtime = float(meta.get("idle_recycle_arm_mtime") or 0.0)
    except (TypeError, ValueError):
        arm_mtime = 0.0
    artifact_path = meta.get("idle_recycle_artifact")

    # Never wedge: the session didn't record its state in time → drop the
    # handoff and let the next window re-arm (an idle session is not urgent, so —
    # unlike autocompact — there is no /compact fallback to claw back context).
    if now - armed_at > autocompact.handoff_timeout_sec():
        _clear_recycle_state(meta)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        log.warning("idle_timeout: handoff timed out for %s — dropping (re-arm next "
                    "window)", sid)
        return False

    # Wait until the session has actually written its forward-state.
    if autocompact._artifact_mtime(artifact_path) <= arm_mtime:
        return False

    # The pane must still exist and be idle/composer-ready to clear safely. A
    # vanished pane means the session already exited — drop the stamp.
    pane = autocompact._pane_for(sid)
    if not pane:
        _clear_recycle_state(meta)
        sessions._write_session_metadata(md_path, meta, atomic=True)
        return False
    if not autocompact.composer_ready(autocompact._capture_pane(pane)):
        return False

    # CLEAR + RELAUNCH: close the old pane, boot a fresh incarnation from the
    # artifact, re-bound to the same assignment (autocompact's exact sequence).
    try:
        autocompact._suspend_session(cfg, slug, sid)
    except Exception:
        log.exception("idle_timeout: finalize suspend failed for %s — retry next "
                      "tick", sid)
        return False
    role = meta.get("role") or sessions._derive_role(
        meta.get("window"), meta.get("task_id"), meta.get("initiative"))
    rec = {"sid": sid, "task_id": meta.get("task_id"), "role": role,
           "window": meta.get("window")}
    try:
        autocompact._relaunch_from_artifact(cfg, slug, rec, artifact_path)
    except Exception as e:
        # The old pane is already gone and won't be re-sampled — alert the
        # operator instead of orphaning the assignment silently.
        log.exception("idle_timeout: relaunch failed for %s after clear", sid)
        autocompact._alert_orphaned_handoff(cfg, slug, sid, repr(e))
        return False
    log.info("idle_timeout: cache-window recycle complete for %s — relaunched fresh "
             "from %s", sid, artifact_path)
    return True


# --- scheduler entry --------------------------------------------------------

def tick(cfg: Any) -> None:
    """Per-project sweep: recycle stale waiting sessions, drive in-flight handoffs.

    Sibling of the telemetry/binding_gc 60s ticks. Per-session and per-project
    errors are swallowed so one bad session/project never kills the sweep.
    """
    if not idle_timeout_enabled():
        return
    user_home = sessions._get_user_home()
    cur_user = sessions._get_current_user()
    now = time.time()
    for slug in cfg.projects:
        try:
            rows = sessions.list_sessions(cfg, slug)
        except Exception:
            log.exception("idle_timeout.tick: list_sessions failed for %s", slug)
            continue
        for row in rows:
            if row.get("status") != "active":
                continue
            # Per-user worker reads only its own ~/.claude (the idle clock lives
            # under a home we can't stat for other users).
            if (row.get("linux_user") or cur_user) != cur_user:
                continue
            try:
                maybe_recycle(cfg, slug, row, now, user_home)
            except Exception:
                log.exception("idle_timeout.tick: %s failed", row.get("sid"))
