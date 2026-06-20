"""WS-4 S5 (T-0252) — 5h-usage-limit park + retry.

When ``detector.session_pressure`` reports a session whose pane shows the 5h
usage-limit prompt, that session is stuck (blocked until its window resets) yet
still consumes a parallel slot. This tick PARKS it — ``sessions.suspend`` closes
the pane (frees the slot) while preserving ``claude_uuid`` — and RETRIES
(``sessions.resume``) once the limit has reset and a slot is free.

Blast-radius (suspends a LIVE session), so guarded like recovery:
  * ``BOT_SQUAD_PARK_RETRY`` gate, **default OFF** (opt-in once watched);
  * a DEBOUNCE — the limit marker must persist across N consecutive detector
    samples before we suspend (a single capture-pane match could be a transient
    false positive; the whole run depends on not killing live work);
  * re-admit only when pressure has cleared AND a slot is free (live < effective).

State: ``data/_worker/park/state.json`` (debounce counts) + one marker per parked
session ``data/_worker/park/parked/<safe-sid>.json``. The tick never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot_squad_worker.detector import session_pressure  # re-exported for monkeypatch

log = logging.getLogger(__name__)

_RESETS_RE = re.compile(r"resets?\s+at\s+(\d{1,2}):(\d{2})", re.IGNORECASE)


# --- gate / tunables -------------------------------------------------------

def park_enabled() -> bool:
    return os.environ.get("BOT_SQUAD_PARK_RETRY", "0").strip() == "1"


def debounce_samples() -> int:
    try:
        v = int(os.environ.get("BOT_SQUAD_PARK_DEBOUNCE", 2))
    except (TypeError, ValueError):
        return 2
    return v if v > 0 else 2


def fallback_retry_sec() -> int:
    try:
        v = int(os.environ.get("BOT_SQUAD_PARK_RETRY_SEC", 3600))
    except (TypeError, ValueError):
        return 3600
    return v if v > 0 else 3600


# --- pure: resets-at parsing -----------------------------------------------

def parse_resets_at(text: str, now_epoch: float,
                    now_hm: Optional[tuple[int, int]] = None) -> Optional[float]:
    """Parse a 'resets at HH:MM' from pane text → absolute retry epoch.

    The reset time is a wall-clock HH:MM; if it's already past for today we roll
    to tomorrow. ``now_hm`` (current hour, minute) is injectable for tests;
    production passes the real UTC now.
    """
    m = _RESETS_RE.search(text or "")
    if not m:
        return None
    rh, rm = int(m.group(1)), int(m.group(2))
    if not (0 <= rh < 24 and 0 <= rm < 60):
        return None
    if now_hm is None:
        dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
        now_hm = (dt.hour, dt.minute)
    nh, nm = now_hm
    delta_min = (rh * 60 + rm) - (nh * 60 + nm)
    if delta_min <= 0:
        delta_min += 24 * 60  # rolled to next day
    return now_epoch + delta_min * 60


# --- pure: debounce decision -----------------------------------------------

def decide_park(limit_blocked: set, prev_counts: dict, debounce: int,
                already_parked: set) -> tuple[set, dict]:
    """Given the currently-limit-blocked sids + prior consecutive-sighting
    counts, return (sids_to_park_now, new_counts). A sid is parked once its
    consecutive count reaches ``debounce``; counts reset the moment a sid stops
    being limit-blocked; already-parked sids are skipped."""
    new_counts: dict = {}
    to_park: set = set()
    for sid in limit_blocked:
        cnt = int(prev_counts.get(sid, 0)) + 1
        new_counts[sid] = cnt
        if sid in already_parked:
            continue
        if cnt >= debounce:
            to_park.add(sid)
    return to_park, new_counts


# --- state -----------------------------------------------------------------

def _park_dir(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "park"


def _state_path(cfg: Any) -> Path:
    return _park_dir(cfg) / "state.json"


def _parked_dir(cfg: Any) -> Path:
    return _park_dir(cfg) / "parked"


def _load_counts(cfg: Any) -> dict:
    try:
        d = json.loads(_state_path(cfg).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_counts(cfg: Any, counts: dict) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps(counts, indent=1))
    os.rename(tmp, p)


def _already_parked(cfg: Any) -> set:
    d = _parked_dir(cfg)
    if not d.exists():
        return set()
    return {p.stem for p in d.glob("*.json")}


# --- side effects (real) ---------------------------------------------------

def _slug_for_sid(cfg: Any, sid: str) -> Optional[str]:
    for slug in getattr(cfg, "projects", {}) or {}:
        if (cfg.data_dir / slug / "sessions" / f"{sid}.md").exists():
            return slug
    return None


def _do_park(cfg: Any, slug: str, sid: str) -> None:
    from bot_squad_worker import sessions as _sessions
    log.warning("park: suspending 5h-limit-blocked session %s (%s)", sid, slug)
    _sessions.suspend(cfg, slug, sid)


def _retry_parked(cfg: Any, now_epoch: Optional[float] = None) -> list:
    """Resume parked sessions whose retry_after has passed, pressure has cleared,
    and a slot is free. Returns the list of resumed sids."""
    d = _parked_dir(cfg)
    if not d.exists():
        return []
    now = now_epoch if now_epoch is not None else _now_epoch()
    from bot_squad_worker import backoff as _backoff
    from bot_squad_worker import sessions as _sessions
    pressure = session_pressure(cfg, now_epoch=now)
    still_blocked = set(pressure.get("limit_blocked_sids", []))
    effective = _backoff.effective_limit(cfg)
    live = _backoff._live_count(cfg)
    resumed: list = []
    for marker in sorted(d.glob("*.json")):
        data = _read_json(marker)
        if not data:
            marker.unlink(missing_ok=True)
            continue
        sid = data.get("sid")
        if sid in still_blocked:
            continue  # still limited
        if now < float(data.get("retry_after", 0)):
            continue  # not yet
        if live >= effective:
            continue  # no slot — try a later tick
        try:
            _sessions.resume(cfg, data.get("slug"), sid)
            resumed.append(sid)
            live += 1
            marker.unlink(missing_ok=True)
            log.warning("park: resumed %s after limit reset", sid)
        except Exception:
            log.exception("park: resume failed for %s", sid)
    return resumed


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _write_marker(cfg: Any, slug: str, sid: str, now: float) -> None:
    from bot_squad_worker.detector import _capture_pane
    from bot_squad_worker.sessions import live_pane_map, _read_session_metadata
    pane = live_pane_map().get(sid)
    retry_after = None
    if pane:
        retry_after = parse_resets_at(_capture_pane(pane), now)
    if retry_after is None:
        retry_after = now + fallback_retry_sec()
    meta = _read_session_metadata(cfg.data_dir / slug / "sessions" / f"{sid}.md") or {}
    d = _parked_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    safe = sid.replace("/", "_")
    (d / f"{safe}.json").write_text(json.dumps({
        "sid": sid, "slug": slug, "task_id": meta.get("task_id"),
        "claude_uuid": meta.get("claude_uuid"),
        "parked_at": now, "retry_after": retry_after,
        "reason": "5h-usage-limit",
    }, indent=1))


# --- the tick --------------------------------------------------------------

def park_tick(cfg: Any, now_epoch: Optional[float] = None) -> dict:
    if not park_enabled():
        return {"enabled": False, "parked": [], "resumed": []}
    try:
        return _run(cfg, now_epoch)
    except Exception:
        log.exception("park_tick error")
        return {"enabled": True, "parked": [], "resumed": [], "error": True}


def _run(cfg: Any, now_epoch: Optional[float]) -> dict:
    now = now_epoch if now_epoch is not None else _now_epoch()
    pressure = session_pressure(cfg, now_epoch=now)
    limit_blocked = set(pressure.get("limit_blocked_sids", []))
    prev = _load_counts(cfg)
    already = _already_parked(cfg)
    to_park, new_counts = decide_park(limit_blocked, prev, debounce_samples(), already)
    parked: list = []
    for sid in sorted(to_park):
        slug = _slug_for_sid(cfg, sid)
        if not slug:
            continue
        try:
            _write_marker(cfg, slug, sid, now)
            _do_park(cfg, slug, sid)
            parked.append(sid)
        except Exception:
            log.exception("park: failed to park %s", sid)
    _save_counts(cfg, new_counts)
    resumed = _retry_parked(cfg, now_epoch=now)
    return {"enabled": True, "parked": parked, "resumed": resumed}
