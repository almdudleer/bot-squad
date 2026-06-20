"""T-0249 (WS-4 S2): parallelism backoff governor.

AIMD (TCP-congestion shape) effective-concurrency governor. Under Claude
rate-limit / 5h-usage-limit pressure it multiplicatively DECREASES effective
concurrency; when pressure clears past a cooldown it additively ramps back up
toward the cap. The cap (T-0239) is the ceiling; this governor sets the live
effective limit under it. Fork recs (operator-approved 2026-06-20):
factor 0.5, MIN_CONCURRENCY 2 (never freeze the run), ramp-step 2, global scope.
``BOT_SQUAD_BACKOFF=0`` is a kill-switch → governor is a no-op (cap-only).
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import backoff as B


def _make_cfg(tmp_path: Path, cap: int = 15) -> types.SimpleNamespace:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "system_settings.toml").write_text(
        f"[caps]\nmax_parallel_sessions = {cap}\nmax_total_tokens = 0\n"
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=data_dir,
                                 config_dir=tmp_path / "config")


def _no_pressure(c, now_epoch=None):
    return {"rate_limited_sids": [], "limit_blocked_sids": [], "any": False}


def _pressure(c, now_epoch=None):
    return {"rate_limited_sids": ["S-x-p9"], "limit_blocked_sids": [], "any": True}


# --- tunables / kill-switch ------------------------------------------------

def test_enabled_default_true(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_BACKOFF", raising=False)
    assert B.backoff_enabled() is True


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_BACKOFF", "0")
    assert B.backoff_enabled() is False


def test_tunable_defaults(monkeypatch):
    for k in ("BOT_SQUAD_BACKOFF_FACTOR", "BOT_SQUAD_BACKOFF_MIN",
              "BOT_SQUAD_BACKOFF_RAMP_STEP", "BOT_SQUAD_BACKOFF_COOLDOWN"):
        monkeypatch.delenv(k, raising=False)
    assert B.backoff_factor() == 0.5
    assert B.min_concurrency() == 2
    assert B.ramp_step() == 2
    assert B.cooldown_sec() == 300


def test_ceiling_reads_cap(tmp_path):
    cfg = _make_cfg(tmp_path, cap=15)
    assert B._ceiling(cfg) == 15


def test_ceiling_unlimited_when_cap_zero(tmp_path):
    cfg = _make_cfg(tmp_path, cap=0)
    assert B._ceiling(cfg) >= 1000  # unlimited → large sentinel


# --- governor step: pressure -> multiplicative decrease --------------------

def test_tick_pressure_drops_effective(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, cap=15)
    monkeypatch.setattr(B, "session_pressure", _pressure)
    monkeypatch.setattr(B, "_live_count", lambda c: 12)
    st = B.backoff_tick(cfg, now_epoch=1000)
    # floor(12 * 0.5) = 6, >= MIN(2)
    assert st["effective_limit"] == 6
    assert st["last_pressure_at"] == 1000
    assert "pressure" in st["reason"]


def test_tick_pressure_respects_min_floor(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, cap=15)
    monkeypatch.setattr(B, "session_pressure", _pressure)
    monkeypatch.setattr(B, "_live_count", lambda c: 1)  # floor(0.5)=0 -> clamp to MIN 2
    st = B.backoff_tick(cfg, now_epoch=1000)
    assert st["effective_limit"] == 2


# --- governor step: clear + cooldown -> additive increase ------------------

def test_tick_clear_ramps_after_cooldown(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, cap=15)
    # seed a depressed state with last pressure well in the past
    B.save_state(cfg, {"effective_limit": 6, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    monkeypatch.setattr(B, "session_pressure", _no_pressure)
    monkeypatch.setattr(B, "_live_count", lambda c: 6)
    st = B.backoff_tick(cfg, now_epoch=10_000)  # >> cooldown past pressure
    assert st["effective_limit"] == 8  # 6 + ramp_step 2
    assert "ramp" in st["reason"]


def test_tick_clear_within_cooldown_holds(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 6, "last_pressure_at": 9_950,
                       "last_ramp_at": 0, "reason": "pressure"})
    monkeypatch.setattr(B, "session_pressure", _no_pressure)
    monkeypatch.setattr(B, "_live_count", lambda c: 6)
    st = B.backoff_tick(cfg, now_epoch=10_000)  # only 50s since pressure < cooldown 300
    assert st["effective_limit"] == 6  # held


def test_tick_ramp_clamps_to_ceiling(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 14, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "ramp"})
    monkeypatch.setattr(B, "session_pressure", _no_pressure)
    monkeypatch.setattr(B, "_live_count", lambda c: 5)
    st = B.backoff_tick(cfg, now_epoch=10_000)
    assert st["effective_limit"] == 15  # 14+2=16 clamped to cap 15


# --- reader for admission --------------------------------------------------

def test_effective_limit_cold_start_returns_ceiling(tmp_path):
    cfg = _make_cfg(tmp_path, cap=15)
    assert B.effective_limit(cfg) == 15  # no state -> optimistic full cap


def test_effective_limit_reads_state(tmp_path):
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 4, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    assert B.effective_limit(cfg) == 4


def test_effective_limit_kill_switch_returns_ceiling(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 4, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    monkeypatch.setenv("BOT_SQUAD_BACKOFF", "0")
    assert B.effective_limit(cfg) == 15  # disabled -> cap-only, ignore depressed state


def test_tick_kill_switch_noop(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, cap=15)
    monkeypatch.setenv("BOT_SQUAD_BACKOFF", "0")
    monkeypatch.setattr(B, "session_pressure", _pressure)
    monkeypatch.setattr(B, "_live_count", lambda c: 12)
    st = B.backoff_tick(cfg, now_epoch=1000)
    assert st["effective_limit"] == 15  # no-op, stays at ceiling
