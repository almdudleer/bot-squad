"""T-0250 (WS-4 S3): admission consults the backoff governor.

``_enforce_parallel_cap`` now refuses a spawn when live sessions reach the
EFFECTIVE concurrency (``backoff.effective_limit``), not just the hard cap.
Under pressure the governor depresses effective below the cap, so admission
queues spawns earlier — and the refusal message distinguishes a hard-cap
refusal from a backoff (pressure) refusal. Both keep the task pending/queued
(the T-0237 S4 contract), never a silent drop.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import backoff as B
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from bot_squad_worker.sessions import _enforce_parallel_cap, _write_session_metadata


def _make_cfg(tmp_path: Path, cap: int = 15) -> types.SimpleNamespace:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "system_settings.toml").write_text(
        f"[caps]\nmax_parallel_sessions = {cap}\nmax_total_tokens = 0\n"
    )
    data_dir = tmp_path / "data"
    (data_dir / "p1" / "sessions").mkdir(parents=True)
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=data_dir,
                                 config_dir=tmp_path / "config")


# SID → pane_id registry backing the patched ``live_pane_map`` (T-0397): a
# session counts as live only if its SID has a genuinely live pane here.
_LIVE_PANES: dict[str, str] = {}


def _live(cfg, sid, *, pane=True) -> None:
    meta = {"sid": sid, "status": "active", "window": "w", "task_id": "~",
            "initiative": "~"}
    _write_session_metadata(cfg.data_dir / "p1" / "sessions" / f"{sid}.md", meta)
    if pane:
        _LIVE_PANES[sid] = "%0"
    else:
        _LIVE_PANES.pop(sid, None)


@pytest.fixture(autouse=True)
def _no_panes(monkeypatch):
    _LIVE_PANES.clear()
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "live_pane_map", lambda user=None: dict(_LIVE_PANES))


def test_backoff_pressure_refuses_below_hard_cap(tmp_path):
    """Governor depressed effective to 3; 3 live < cap 15 but >= effective -> queue."""
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 3, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    for i in range(3):
        _live(cfg, f"S-u-x-p{i}")
    with pytest.raises(ActionError, match="backoff"):
        _enforce_parallel_cap(cfg)


def test_hard_cap_message_when_effective_at_cap(tmp_path):
    """No depression (effective == cap): the message is the hard-cap one."""
    cfg = _make_cfg(tmp_path, cap=2)  # no backoff state -> effective == cap 2
    _live(cfg, "S-u-a-p1")
    _live(cfg, "S-u-b-p2")
    with pytest.raises(ActionError, match="capacity reached"):
        _enforce_parallel_cap(cfg)


def test_under_effective_allows(tmp_path):
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 5, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    for i in range(3):  # 3 < 5
        _live(cfg, f"S-u-x-p{i}")
    _enforce_parallel_cap(cfg)  # no raise


def test_kill_switch_restores_cap_only(tmp_path, monkeypatch):
    """BOT_SQUAD_BACKOFF=0 -> ignore depressed state, admit up to the hard cap."""
    cfg = _make_cfg(tmp_path, cap=15)
    B.save_state(cfg, {"effective_limit": 2, "last_pressure_at": 0,
                       "last_ramp_at": 0, "reason": "pressure"})
    monkeypatch.setenv("BOT_SQUAD_BACKOFF", "0")
    for i in range(5):  # 5 < cap 15, but > depressed 2
        _live(cfg, f"S-u-x-p{i}")
    _enforce_parallel_cap(cfg)  # no raise — kill-switch ignores backoff
