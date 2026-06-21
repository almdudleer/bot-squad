"""WS-4 S2 (T-0249) — parallelism backoff governor.

The 2-day/15-parallel run must NOT trip Claude rate limits / 5h usage limits
into a hammering loop. This governor sits UNDER the T-0239 cap (the ceiling)
and sets the live **effective concurrency** with an AIMD (TCP-congestion)
control law:

  * **Multiplicative decrease** on pressure — when ``detector.session_pressure``
    reports any 429 or 5h-limit signal, drop effective concurrency to
    ``max(MIN, floor(current_live * FACTOR))``.
  * **Additive increase** when clear — once pressure has been gone for
    ``COOLDOWN`` seconds, ramp ``effective += RAMP_STEP`` toward the ceiling.

Admission (``sessions._enforce_parallel_cap``, T-0250) consults
``effective_limit(cfg)`` and refuses-to-queue (never silent-drops, the T-0237
S4 contract) when live sessions reach it. Operator-approved fork recs
(2026-06-20): FACTOR 0.5, MIN 2 (never freeze the run), RAMP_STEP 2, global
scope. Everything is env-tunable (no redeploy) and ``BOT_SQUAD_BACKOFF=0`` is a
kill-switch → governor no-op, cap-only behavior (current T-0239 state).

State (global — concurrency is system-wide): ``data/_worker/backoff/state.json``.
The tick never raises (mirrors the other scheduler ticks) so one bad sweep can
never wedge the run.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot_squad_worker.detector import session_pressure  # re-exported for monkeypatch

log = logging.getLogger(__name__)

_UNLIMITED = 10_000  # sentinel ceiling when the cap is 0 (unlimited)


# --- tunables --------------------------------------------------------------

def backoff_enabled() -> bool:
    return os.environ.get("BOT_SQUAD_BACKOFF", "1").strip() != "0"


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def backoff_factor() -> float:
    return _env_float("BOT_SQUAD_BACKOFF_FACTOR", 0.5)


def min_concurrency() -> int:
    return _env_int("BOT_SQUAD_BACKOFF_MIN", 2)


def ramp_step() -> int:
    return _env_int("BOT_SQUAD_BACKOFF_RAMP_STEP", 2)


def ramp_interval_sec() -> int:
    return _env_int("BOT_SQUAD_BACKOFF_RAMP_INTERVAL", 120)


def cooldown_sec() -> int:
    return _env_int("BOT_SQUAD_BACKOFF_COOLDOWN", 300)


# --- state -----------------------------------------------------------------

def _state_path(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "backoff" / "state.json"


def load_state(cfg: Any) -> Optional[dict]:
    try:
        return json.loads(_state_path(cfg).read_text())
    except (OSError, ValueError):
        return None


def save_state(cfg: Any, state: dict) -> None:
    p = _state_path(cfg)
    # T-0373: unique-tmp atomic write (state file is single-writer — apscheduler
    # max_instances=1 — so no lock needed, but a unique tmp matches the task-md
    # writers and is clobber-proof if a tick ever overlaps).
    from bot_squad_worker.mdlock import atomic_write
    atomic_write(p, json.dumps(state, indent=1))


# --- ceiling + live count --------------------------------------------------

def _ceiling(cfg: Any) -> int:
    from bot_squad_worker.sessions import _caps_config_dir, _read_caps
    cap = _read_caps(_caps_config_dir(cfg))["max_parallel_sessions"]
    return cap if cap > 0 else _UNLIMITED


def _live_count(cfg: Any) -> int:
    from bot_squad_worker.sessions import _count_live_sessions
    return _count_live_sessions(cfg)


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


# --- reader for admission --------------------------------------------------

def effective_limit(cfg: Any) -> int:
    """The live effective concurrency admission must respect. Disabled or
    cold-start → the full ceiling (optimistic; first pressure pulls it down)."""
    ceiling = _ceiling(cfg)
    if not backoff_enabled():
        return ceiling
    st = load_state(cfg)
    if not st or "effective_limit" not in st:
        return ceiling
    try:
        return max(1, min(int(st["effective_limit"]), ceiling))
    except (TypeError, ValueError):
        return ceiling


# --- the governor step -----------------------------------------------------

def backoff_tick(cfg: Any, now_epoch: Optional[float] = None) -> dict:
    """One AIMD step. Never raises — returns the (persisted) new state dict."""
    try:
        return _step(cfg, now_epoch)
    except Exception:  # never wedge the scheduler
        log.exception("backoff_tick error")
        return load_state(cfg) or {}


def _step(cfg: Any, now_epoch: Optional[float]) -> dict:
    now = now_epoch if now_epoch is not None else _now_epoch()
    ceiling = _ceiling(cfg)

    if not backoff_enabled():
        st = {"effective_limit": ceiling, "last_pressure_at": None,
              "last_ramp_at": now, "reason": "disabled (kill-switch)"}
        save_state(cfg, st)
        return st

    prev = load_state(cfg) or {"effective_limit": ceiling,
                               "last_pressure_at": None, "last_ramp_at": now}
    cur = int(prev.get("effective_limit", ceiling))
    pressure = session_pressure(cfg, now_epoch=now)

    if pressure.get("any"):
        live = _live_count(cfg)
        target = max(min_concurrency(), int(math.floor(live * backoff_factor())))
        target = min(target, ceiling)
        sids = (pressure.get("rate_limited_sids", []) +
                pressure.get("limit_blocked_sids", []))
        st = {"effective_limit": target, "last_pressure_at": now,
              "last_ramp_at": prev.get("last_ramp_at", now),
              "reason": f"pressure: {len(sids)} session(s) limited -> decrease to {target}"}
        save_state(cfg, st)
        log.warning("backoff: pressure on %s -> effective %d (was %d)", sids, target, cur)
        return st

    # clear: ramp up only once cooldown since last pressure has elapsed
    last_pressure = prev.get("last_pressure_at")
    last_ramp = prev.get("last_ramp_at", 0) or 0
    cooled = (last_pressure is None) or (now - last_pressure >= cooldown_sec())
    due = (now - last_ramp) >= ramp_interval_sec()
    if cur < ceiling and cooled and due:
        nxt = min(ceiling, cur + ramp_step())
        st = {"effective_limit": nxt, "last_pressure_at": last_pressure,
              "last_ramp_at": now, "reason": f"clear: ramp {cur} -> {nxt}"}
        save_state(cfg, st)
        return st

    # hold
    st = {"effective_limit": cur, "last_pressure_at": last_pressure,
          "last_ramp_at": last_ramp,
          "reason": "hold (clear, within cooldown/interval)" if cur < ceiling else "hold (at ceiling)"}
    save_state(cfg, st)
    return st
