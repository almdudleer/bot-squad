"""WS-4 S1 (T-0248) — run-survival pressure detector.

Surfaces the two pressure signals the parallelism-backoff governor (T-0249)
needs, without mutating any state:

  (A) **429 rate-limit** — telemetry already samples a per-session
      ``rate_limited`` flag into each session's record every 60s
      (``telemetry._sample_session`` scans the transcript for an
      ``error=="rate_limit"`` / ``apiErrorStatus==429`` line). We surface the
      sids whose flag is set within a recency window so a one-off 429 from an
      hour ago doesn't pin the governor down.

  (B) **per-session 5-hour usage-limit prompt** — this is an *interactive* pane
      state, NOT a jsonl 429, so telemetry never sees it. We scan
      ``tmux capture-pane`` text for a small, env-overridable marker set.

``session_pressure(cfg)`` is the single read the governor consults. All
thresholds are env-overridable (no redeploy); the marker list has an env
override + every match is logged so we can tune against day-1 run output.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Default markers for the interactive 5-hour usage-limit / rate-limit pane
# prompt. Lowercase substrings, matched case-insensitively. Tunable via
# BOT_SQUAD_LIMIT_MARKERS (comma-separated) once we see the real day-1 copy.
DEFAULT_LIMIT_MARKERS = [
    "usage limit",
    "5-hour limit",
    "5 hour limit",
    "limit reached",
    "resets at",
    "rate limit",
]

DEFAULT_PRESSURE_WINDOW_SEC = 180


def limit_markers() -> list[str]:
    raw = os.environ.get("BOT_SQUAD_LIMIT_MARKERS")
    if raw is None:
        return list(DEFAULT_LIMIT_MARKERS)
    return [m.strip().lower() for m in raw.split(",") if m.strip()]


def pressure_window_sec() -> int:
    try:
        v = int(os.environ.get("BOT_SQUAD_PRESSURE_WINDOW", DEFAULT_PRESSURE_WINDOW_SEC))
    except (TypeError, ValueError):
        return DEFAULT_PRESSURE_WINDOW_SEC
    return v if v > 0 else DEFAULT_PRESSURE_WINDOW_SEC


def text_has_limit_marker(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in limit_markers())


# --- (A) 429 from telemetry records ----------------------------------------

def _parse_iso(value: str | None) -> Optional[float]:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iter_telemetry_records(cfg: Any):
    """Yield each per-session telemetry record dict across every project."""
    for slug in getattr(cfg, "projects", {}) or {}:
        tdir = cfg.data_dir / slug / "_worker" / "telemetry"
        if not tdir.exists():
            continue
        for jf in tdir.glob("*.json"):
            if jf.name == "_quota.json":
                continue
            rec = _read_json(jf)
            if isinstance(rec, dict):
                yield rec


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _recent_rate_limited(cfg: Any, now_epoch: Optional[float] = None) -> set[str]:
    """Sids whose telemetry ``rate_limited`` flag is set and was sampled within
    the pressure window (a stale flag from long ago does not count)."""
    now = now_epoch if now_epoch is not None else _now_epoch()
    window = pressure_window_sec()
    out: set[str] = set()
    for rec in _iter_telemetry_records(cfg):
        if not rec.get("rate_limited"):
            continue
        sid = rec.get("sid")
        if not sid:
            continue
        ts = _parse_iso(rec.get("sampled_at"))
        if ts is None or (now - ts) > window:
            continue
        out.add(sid)
    return out


# --- (B) 5h-limit pane scan ------------------------------------------------

def _capture_pane(pane_id: str) -> str:
    try:
        return subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _live_panes(cfg: Any) -> list[tuple[str, str]]:
    """(sid, pane_id) for every registered live session that holds a tmux pane.

    The pane is resolved from the ACTUAL tmux panes via ``live_pane_map`` — the
    session md ``pane_id`` field is routinely empty for live sessions, so trusting
    it makes every live session look pane-dead (the 5h-limit detector no-op + the
    recovery false-respawn). A session counts here iff it is a persisted
    live-holder AND its SID maps to a real live pane.
    """
    from bot_squad_worker.sessions import (
        _is_live_holder, _read_session_metadata, live_pane_map)
    pane_map = live_pane_map()
    out: list[tuple[str, str]] = []
    for slug in getattr(cfg, "projects", {}) or {}:
        sess_dir = cfg.data_dir / slug / "sessions"
        if not sess_dir.exists():
            continue
        for md in sess_dir.glob("*.md"):
            meta = _read_session_metadata(md)
            if not meta or not _is_live_holder(meta):
                continue
            sid = meta.get("sid")
            if sid and sid in pane_map:
                out.append((sid, pane_map[sid]))
    return out


def _limit_blocked_panes(cfg: Any) -> set[str]:
    out: set[str] = set()
    for sid, pane in _live_panes(cfg):
        text = _capture_pane(pane)
        if text_has_limit_marker(text):
            log.warning("detector: 5h/usage-limit marker in pane %s (sid %s)", pane, sid)
            out.add(sid)
    return out


# --- combined --------------------------------------------------------------

def session_pressure(cfg: Any, now_epoch: Optional[float] = None) -> dict:
    """The single read the backoff governor consults. Never mutates state."""
    rate_limited = _recent_rate_limited(cfg, now_epoch=now_epoch)
    limit_blocked = _limit_blocked_panes(cfg)
    return {
        "rate_limited_sids": sorted(rate_limited),
        "limit_blocked_sids": sorted(limit_blocked),
        "any": bool(rate_limited or limit_blocked),
        "sampled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
