"""T-0470 / M1-F1.7 — hook-driven lifecycle measurement (stall / timeout / recycle).

Stakeholder (voice-06, verbatim): *"use Claude hooks to measure stall time /
timeout / the lifecycle signals; the system operates cohesively with the
hooks."*

Before this, the lifecycle signals were SCATTERED — the idle clock was inferred
from the Claude transcript jsonl mtime, "is the human the bottleneck" from a
pane mtime, peer-liveness from a heartbeat file. Nothing was EMITTED at the
lifecycle transition itself; the engine polled side-effects after the fact.

This module is the unified surface those signals now ride on. Two halves:

  HOOK SIGNALS (written by Claude Code hooks, read by the engine)
  --------------------------------------------------------------
  Claude Code fires a hook at each lifecycle transition. We turn the two that
  bracket a session's idle window into per-SID marker files under the session's
  own ``<cwd>/.claude/bsq_lifecycle/`` dir:

    * ``Stop``            → ``<sid>.stop``   — the assistant finished its turn;
                            the session is now IDLE and its subscription cache
                            is going cold. This marker's mtime is the STALL
                            anchor (``session_stalled``). THE hook signal the
                            timeout decision now reads.
    * ``UserPromptSubmit``→ ``<sid>.active`` — a prompt was submitted; a turn is
                            in progress, so the session is NOT idle. A ``.active``
                            newer than ``.stop`` means "busy", so the stall clock
                            reads 0.

  The marker is keyed by SID (per-pane), NOT a cwd-level file: ``sessions.
  _pane_activity_at`` deliberately rejects ``<cwd>/.claude/last_user_prompt_ts``
  because every pane sharing one repo cwd bumps it. Keying by SID keeps the
  signal per-pane, the granularity the lifecycle engine needs.

  ENGINE EVENTS (written by the lifecycle engine, read for operator measurement)
  -----------------------------------------------------------------------------
  When the engine acts on the hook signal it records the derived lifecycle
  events into a per-session log in the SHARED install data dir
  (``<data>/<slug>/_worker/lifecycle/<sid>.json``):

    * ``session_timeout``  — emitted when ``idle_timeout`` ARMS a cache-window
                             recycle (the stall crossed the window).
    * ``session_recycled`` — emitted when a recycle FINALIZES (idle_timeout
                             finalize, or recovery respawn of a crashed session).

  So all three signals the stakeholder named — stalled / timeout / recycled —
  live behind ONE surface the operator can measure, instead of being inferred
  from three unrelated timestamps.

WHICH HOOK FIRES WHICH SIGNAL (the documented mapping, DoD bullet 3):

    Claude hook        marker / event        lifecycle signal
    ---------------    -------------------    ---------------------------
    Stop               <sid>.stop            session_stalled (idle anchor)
    UserPromptSubmit   <sid>.active          turn-in-progress (clears stall)
    (engine)           session_timeout       idle_timeout armed a recycle
    (engine)           session_recycled      a recycle finalized / crash respawn

Everything fails closed and never raises into a hook or a scheduler tick: a
missing marker reads as "no hook signal" (the engine falls back to the jsonl
mtime), and a failed event write is swallowed (measurement is best-effort, it
must never wedge a recycle).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Lifecycle signal names (the unified vocabulary; see module docstring).
SESSION_STALLED = "session_stalled"
SESSION_TIMEOUT = "session_timeout"
SESSION_RECYCLED = "session_recycled"

# Marker kinds the hooks write (per-SID files under <cwd>/.claude/bsq_lifecycle/).
MARKER_STOP = "stop"      # Stop hook — idle anchor
MARKER_ACTIVE = "active"  # UserPromptSubmit hook — turn in progress

_MARKER_SUBDIR = "bsq_lifecycle"
# Cap on the retained per-session event history (most recent first).
_HISTORY_CAP = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Hook-marker side (per-SID files the Claude hooks write; the engine reads)
# ---------------------------------------------------------------------------

def marker_dir(cwd: str) -> Path:
    """The per-cwd dir the hooks drop per-SID lifecycle markers into."""
    return Path(cwd) / ".claude" / _MARKER_SUBDIR


def marker_path(cwd: str, sid: str, kind: str) -> Path:
    """Path of the ``kind`` marker for ``sid`` (e.g. ``<cwd>/.claude/bsq_lifecycle/<sid>.stop``).

    SID is sanitised the way SIDs already are elsewhere (slashes → ``_``) so a
    pathological SID can't escape the marker dir.
    """
    safe = (sid or "").replace("/", "_")
    return marker_dir(cwd) / f"{safe}.{kind}"


def touch_marker(cwd: str, sid: str, kind: str) -> Path | None:
    """Stamp a hook marker (create dir + touch the file to *now*). Best-effort:
    returns the Path on success, None on any error — a hook must never fail.

    This is the worker-side helper the hook scripts shell out to (so the
    per-SID marker convention lives in ONE place, not duplicated in bash).
    """
    if not cwd or not sid:
        return None
    p = marker_path(cwd, sid, kind)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p
    except OSError:
        log.debug("lifecycle_events.touch_marker failed for %s/%s", sid, kind, exc_info=True)
        return None


def _marker_mtime(cwd: str, sid: str, kind: str) -> float | None:
    try:
        return marker_path(cwd, sid, kind).stat().st_mtime
    except OSError:
        return None


def hook_turn_in_progress(cwd: str, sid: str) -> bool:
    """True when a turn is RUNNING — an ``.active`` marker at or after the Stop.

    :func:`hook_idle_age` folds this case into ``0.0``, which a caller cannot
    tell apart from a turn that ended this very instant. T-1060 needed the
    distinction: the system-wake anchor may lengthen an idle reading, but never
    over a session that is mid-answer. Asked of the markers rather than derived
    from the folded return, so the two can never drift apart.
    """
    if not cwd or not sid:
        return False
    stop = _marker_mtime(cwd, sid, MARKER_STOP)
    if stop is None:
        return False
    active = _marker_mtime(cwd, sid, MARKER_ACTIVE)
    return active is not None and active >= stop


def hook_idle_age(cwd: str, sid: str, now: float) -> float | None:
    """Stall time (seconds) from the HOOK signal, or None when there is none.

    The ``Stop`` marker is the idle anchor: ``now - <stop mtime>``. A ``.active``
    marker at or after the Stop means a turn is in progress (a prompt was
    submitted and the session hasn't gone idle again) → the session is busy, so
    the stall clock reads 0.0. Returns None when no Stop marker exists yet — the
    caller then falls back to the jsonl mtime, so a marker-less (pre-hook /
    brand-new) session never regresses.
    """
    if not cwd or not sid:
        return None
    stop = _marker_mtime(cwd, sid, MARKER_STOP)
    if stop is None:
        return None
    active = _marker_mtime(cwd, sid, MARKER_ACTIVE)
    if active is not None and active >= stop:
        return 0.0
    return max(0.0, now - stop)


# ---------------------------------------------------------------------------
# Engine-event side (per-session log in the shared data dir; for measurement)
# ---------------------------------------------------------------------------

def _events_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "lifecycle"


def _event_path(cfg: Any, slug: str, sid: str) -> Path:
    safe = (sid or "").replace("/", "_")
    return _events_dir(cfg, slug) / f"{safe}.json"


def _read_event_doc(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def emit(cfg: Any, slug: str, sid: str, event: str, *, now: float | None = None,
         **fields: Any) -> bool:
    """Record a derived lifecycle ``event`` for ``sid`` into the unified surface.

    Updates a small per-session doc: ``last`` (newest ts per event kind),
    ``counts`` (per-kind tally), and a capped ``history`` (most-recent-first).
    Best-effort — returns True on a successful write, False on any error; never
    raises (an event write must never wedge a recycle/recovery action).
    """
    if not sid or not event:
        return False
    ts_epoch = time.time() if now is None else now
    ts_iso = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        path = _event_path(cfg, slug, sid)
        doc = _read_event_doc(path)
        doc.setdefault("sid", sid)
        last = doc.get("last") or {}
        counts = doc.get("counts") or {}
        history = doc.get("history") or []
        entry = {"event": event, "at": ts_iso, "epoch": round(ts_epoch, 3)}
        if fields:
            entry.update(fields)
        last[event] = entry
        counts[event] = int(counts.get(event, 0)) + 1
        history.insert(0, entry)
        doc["last"] = last
        doc["counts"] = counts
        doc["history"] = history[:_HISTORY_CAP]
        doc["updated_at"] = ts_iso
        from bot_squad_worker.mdlock import atomic_write
        atomic_write(path, json.dumps(doc, indent=1))
        return True
    except Exception:  # noqa: BLE001 — measurement must never break the caller
        log.debug("lifecycle_events.emit(%s) failed for %s", event, sid, exc_info=True)
        return False


def read_events(cfg: Any, slug: str, sid: str) -> dict:
    """Read a session's lifecycle-event doc (``{}`` when none yet)."""
    return _read_event_doc(_event_path(cfg, slug, sid))


def summarize(cfg: Any, slug: str) -> dict[str, dict]:
    """Per-SID lifecycle summary for operator measurement: ``{sid: {last, counts}}``.

    Reads every ``<sid>.json`` under the project's lifecycle dir. Never raises —
    a bad/partial doc is skipped so one corrupt file can't break the read.
    """
    out: dict[str, dict] = {}
    d = _events_dir(cfg, slug)
    if not d.exists():
        return out
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return out
    for f in files:
        doc = _read_event_doc(f)
        sid = doc.get("sid")
        if not sid:
            continue
        out[sid] = {"last": doc.get("last") or {}, "counts": doc.get("counts") or {}}
    return out


def reap_events(cfg: Any, slug: str, sid: str) -> Path | None:
    """Remove a session's lifecycle-event doc once it is archived/historical
    (mirrors ``telemetry.reap_record``). Never raises; returns the removed Path
    or None when there was nothing to remove."""
    if not sid:
        return None
    p = _event_path(cfg, slug, sid)
    try:
        if p.exists():
            p.unlink()
            return p
    except OSError:
        log.warning("lifecycle_events.reap_events: failed to remove %s", p, exc_info=True)
    return None
