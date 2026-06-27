"""T-0475 / M2-F2.6 — operator pacing status (parallelism + best-effort weekly
quota target). Unit-tests the signal composition + recommendation in
``operator_redrive.pacing_status``; the parallelism cap value itself is owned by
``pace.py`` (TL-B / T-0482) so we monkeypatch it and test OUR logic on top."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import operator_redrive as ord_
from bot_squad_worker import pace as _pace

SLUG = "bot-squad"


@pytest.fixture
def cfg(tmp_path: Path):
    data = tmp_path / "data"
    (data / SLUG / "backlog").mkdir(parents=True, exist_ok=True)
    (data / SLUG / "_telemetry").mkdir(parents=True, exist_ok=True)
    cfgdir = tmp_path / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(data_dir=data, config_dir=cfgdir)


def _task(cfg, tid, status="open", archived=False):
    arch = f"\narchived: {str(archived).lower()}" if archived else ""
    (cfg.data_dir / SLUG / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: x\nstatus: {status}{arch}\n---\n\nbody\n", encoding="utf-8")


def _set_target(cfg, pct):
    (cfg.config_dir / "system_settings.toml").write_text(
        f"[operator]\nweekly_quota_target_pct = {pct}\n", encoding="utf-8")


def _set_quota(cfg, *, burn=None, remaining=None, r429=0):
    (cfg.data_dir / SLUG / "_telemetry" / "_quota.json").write_text(json.dumps({
        "burn_tokens_per_hr": burn, "remaining_tokens": remaining,
        "rate_limit_429": {"count": r429, "last_at": None},
    }), encoding="utf-8")


def test_fresh_project_degrades_to_ok(cfg, monkeypatch):
    """No cap, no target, no telemetry -> a usable all-defaults dashboard, 'ok'."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["max_in_progress"] == 0 and st["in_progress"] == 0
    assert st["at_cap"] is False
    assert st["weekly_target_pct"] is None
    assert st["burn_tokens_per_hr"] is None and st["remaining_tokens"] is None
    assert st["recommendation"] == "ok"


def test_in_progress_counts_only_in_progress_nonarchived(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _task(cfg, "T-1", status="in_progress")
    _task(cfg, "T-2", status="in_progress")
    _task(cfg, "T-3", status="open")          # not in_progress
    _task(cfg, "T-4", status="in_progress", archived=True)  # archived
    assert ord_.pacing_status(cfg, SLUG)["in_progress"] == 2


def test_at_cap_throttles(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 2)
    _task(cfg, "T-1", status="in_progress")
    _task(cfg, "T-2", status="in_progress")
    st = ord_.pacing_status(cfg, SLUG)
    assert st["at_cap"] is True
    assert st["recommendation"] == "throttle"


def test_under_cap_is_ok(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 3)
    _task(cfg, "T-1", status="in_progress")
    st = ord_.pacing_status(cfg, SLUG)
    assert st["at_cap"] is False
    assert st["recommendation"] == "ok"


def test_target_set_is_advisory(cfg, monkeypatch):
    """A weekly target with no cap pressure -> advisory (pace by judgement, since
    the weekly TOTAL is not queryable)."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_target(cfg, 20)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["weekly_target_pct"] == 20.0
    assert st["recommendation"] == "advisory"


def test_rate_limit_429_throttles_over_advisory(cfg, monkeypatch):
    """429 pressure forces throttle even when only a target is set."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_target(cfg, 20)
    _set_quota(cfg, burn=12345.6, remaining=None, r429=3)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["burn_tokens_per_hr"] == 12345.6
    assert st["rate_limit_429"] == 3
    assert st["recommendation"] == "throttle"


def test_burn_signal_surfaced_when_present(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_quota(cfg, burn=999.0, remaining=500000, r429=0)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["burn_tokens_per_hr"] == 999.0
    assert st["remaining_tokens"] == 500000


def test_target_unreadable_degrades_to_none(cfg, monkeypatch):
    """No system_settings.toml -> target None, never raises (graceful)."""
    monkeypatch.setattr(ord_, "_system_settings_path", lambda c: None)
    assert ord_.weekly_quota_target_pct(cfg) is None


def test_pace_failure_never_breaks_status(cfg, monkeypatch):
    """If pace.max_in_progress raises, pacing_status still returns a dashboard."""
    def boom(c, s): raise RuntimeError("pace exploded")
    monkeypatch.setattr(_pace, "max_in_progress", boom)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["max_in_progress"] == 0  # degraded
    assert "recommendation" in st
