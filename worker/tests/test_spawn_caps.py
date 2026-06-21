"""T-0239 slice 2: worker spawn-time enforcement of the parallel-sessions cap.

The cap is set via the API (system_settings.toml [caps].max_parallel_sessions);
the worker refuses to spawn once that many sessions are already live. 0 =
unlimited. At/over cap is a capacity-reached refusal (mirrors the T-0237 S4
admission contract — not a silent drop).
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from bot_squad_worker.sessions import (
    _count_live_sessions,
    _enforce_parallel_cap,
    _read_caps,
    _write_session_metadata,
)


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    (tmp_path / "config").mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "p1" / "sessions").mkdir(parents=True)
    return types.SimpleNamespace(
        projects={"p1": object()},
        data_dir=data_dir,
        config_dir=tmp_path / "config",
    )


def _set_caps(cfg, *, max_parallel=0, max_tokens=0) -> None:
    (cfg.config_dir / "system_settings.toml").write_text(
        f"[caps]\nmax_parallel_sessions = {max_parallel}\nmax_total_tokens = {max_tokens}\n"
    )


def _live(cfg, sid, *, status="active", archived=None) -> None:
    meta = {"sid": sid, "status": status, "window": "w", "task_id": "~",
            "initiative": "~"}
    if archived is not None:
        meta["archived"] = archived
    _write_session_metadata(cfg.data_dir / "p1" / "sessions" / f"{sid}.md", meta)


@pytest.fixture(autouse=True)
def _no_panes(monkeypatch):
    monkeypatch.setattr(S, "list_panes", lambda: [])


def test_read_caps_defaults_zero_when_missing(tmp_path):
    cfg = _make_cfg(tmp_path)  # no system_settings.toml
    caps = _read_caps(cfg.config_dir)
    assert caps == {"max_parallel_sessions": 0, "max_total_tokens": 0}


def test_count_live_excludes_suspended_and_archived(tmp_path):
    cfg = _make_cfg(tmp_path)
    _live(cfg, "S-u-a-p1", status="active")
    _live(cfg, "S-u-b-p2", status="paused")
    _live(cfg, "S-u-c-p3", status="suspended")             # not counted
    _live(cfg, "S-u-d-p4", status="active", archived="true")  # not counted
    assert _count_live_sessions(cfg) == 2


def test_enforce_refuses_at_cap(tmp_path):
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=1)
    _live(cfg, "S-u-a-p1", status="active")
    with pytest.raises(ActionError, match="capacity reached"):
        _enforce_parallel_cap(cfg)


def test_enforce_allows_under_cap(tmp_path):
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=3)
    _live(cfg, "S-u-a-p1", status="active")
    _enforce_parallel_cap(cfg)  # 1 < 3 -> no raise


def test_enforce_unlimited_when_zero(tmp_path):
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=0)
    for i in range(5):
        _live(cfg, f"S-u-x-p{i}", status="active")
    _enforce_parallel_cap(cfg)  # 0 = unlimited -> no raise


def test_spawn_refused_at_cap_before_side_effects(tmp_path):
    """The cap check fires at the top of spawn(), before any tmux work."""
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=1)
    _live(cfg, "S-u-a-p1", status="active")
    with pytest.raises(ActionError, match="capacity reached"):
        S.spawn(cfg, "p1", "newdev")


# --- T-0335 items 7 + 22: surface the ENFORCED caps utilization -------------

def test_caps_utilization_surfaces_enforced_numbers(tmp_path):
    """The meter measures what spawn actually enforces: live count vs the
    effective limit (item 22) + output_since_anchor vs max_total_tokens (item 7)."""
    from bot_squad_worker.sessions import caps_utilization
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=15, max_tokens=1_000_000)
    _live(cfg, "S-u-a-p1", status="active")
    _live(cfg, "S-u-b-p2", status="paused")
    out = caps_utilization(cfg)
    assert out["max_parallel_sessions"] == 15
    assert out["live_sessions"] == 2
    # backoff disabled/cold → effective_limit == the ceiling
    assert out["effective_limit"] == 15
    assert out["max_total_tokens"] == 1_000_000
    assert out["output_since_anchor"] == 0  # no telemetry samples yet


def test_caps_utilization_unlimited_maps_effective_to_zero(tmp_path):
    """With no parallel cap (0 = unlimited) and no pressure, effective_limit
    reports 0 (unlimited) rather than the internal 10_000 sentinel."""
    from bot_squad_worker.sessions import caps_utilization
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=0, max_tokens=0)
    out = caps_utilization(cfg)
    assert out["max_parallel_sessions"] == 0
    assert out["effective_limit"] == 0  # sentinel 10_000 → 0=unlimited on the wire


def test_caps_utilization_reflects_backoff_throttle(tmp_path, monkeypatch):
    """A depressed AIMD limit surfaces as effective_limit < ceiling (the
    "12/15, throttled to 8" strip)."""
    from bot_squad_worker import sessions as _S, backoff as _backoff
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=15, max_tokens=0)
    monkeypatch.setattr(_backoff, "effective_limit", lambda c: 8)
    out = _S.caps_utilization(cfg)
    assert out["effective_limit"] == 8
    assert out["max_parallel_sessions"] == 15
