"""Tests for the autonomous orchestrator module.

All subprocess.run / tmux calls are mocked — no real claude is ever spawned.
"""
from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bot_squad_worker.autonomous import (
    AutonomousState,
    _log_tick,
    _now_hour_utc,
    _parse_task,
    in_sleep_window,
    load_state,
    pane_alive,
    pick_next_task,
    run_dod_review,
    save_state,
    spawn_worker,
    state_path,
    tick,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, slug: str = "test-proj") -> Any:
    """Create a minimal namespace that looks like a Config."""
    data_dir = tmp_path / "data"
    backlog = data_dir / slug / "backlog"
    backlog.mkdir(parents=True)
    sessions = data_dir / slug / "sessions"
    sessions.mkdir(parents=True)

    project = types.SimpleNamespace(
        slug=slug,
        display_name="Test Project",
        repo_path=tmp_path / "repo",
        tg_chat="0",
    )
    cfg = types.SimpleNamespace(
        data_dir=data_dir,
        projects={slug: project},
    )
    return cfg


def _write_task(cfg: Any, slug: str, task_id: str, title: str, status: str = "open",
                body: str = "Some body text here.", priority: str | None = None) -> Path:
    """Write a sample backlog task file and return its path."""
    if priority:
        body = f"**Priority: {priority}**\n\n{body}"
    backlog_dir = cfg.data_dir / slug / "backlog"
    fname = f"{task_id}-{title.lower().replace(' ', '-')}.md"
    p = backlog_dir / fname
    p.write_text(
        f"---\nid: {task_id}\ntitle: {title}\nstatus: {status}\n---\n\n{body}\n"
    )
    return p


# ---------------------------------------------------------------------------
# State round-trip
# ---------------------------------------------------------------------------

def test_state_file_round_trip(tmp_path):
    """save_state followed by load_state reproduces the same state."""
    cfg = _make_cfg(tmp_path)
    state = AutonomousState(
        slug="test-proj",
        enabled=True,
        status="working",
        current_task_id="T-0001",
        current_pane_id="%5",
        sleep_start_hour=23,
        sleep_end_hour=7,
        fail_counts={"T-0001": 1},
        tick_log=[{"ts": "2026-05-11T00:00:00+00:00", "msg": "hello"}],
    )
    save_state(cfg, state)

    loaded = load_state(cfg, "test-proj")
    assert loaded.enabled is True
    assert loaded.status == "working"
    assert loaded.current_task_id == "T-0001"
    assert loaded.current_pane_id == "%5"
    assert loaded.sleep_start_hour == 23
    assert loaded.sleep_end_hour == 7
    assert loaded.fail_counts == {"T-0001": 1}
    assert len(loaded.tick_log) == 1
    assert loaded.tick_log[0]["msg"] == "hello"


def test_load_state_missing_file_returns_disabled(tmp_path):
    """load_state on a new project returns enabled=False."""
    cfg = _make_cfg(tmp_path, "new-proj")
    state = load_state(cfg, "new-proj")
    assert state.enabled is False
    assert state.status == "idle"
    assert state.current_task_id is None


def test_state_path(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = state_path(cfg, "my-slug")
    assert p.parent.name == "autonomous"
    assert p.name == "my-slug.json"


# ---------------------------------------------------------------------------
# in_sleep_window
# ---------------------------------------------------------------------------

def test_in_sleep_window_wrapping(monkeypatch):
    """Hour 23 is inside the 22:00–08:00 window."""
    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 23)
    state = AutonomousState(slug="s", sleep_start_hour=22, sleep_end_hour=8)
    assert in_sleep_window(state) is True


def test_in_sleep_window_daytime_outside(monkeypatch):
    """Hour 12 is outside the 22:00–08:00 window."""
    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 12)
    state = AutonomousState(slug="s", sleep_start_hour=22, sleep_end_hour=8)
    assert in_sleep_window(state) is False


def test_in_sleep_window_non_wrapping(monkeypatch):
    """Hour 3 is inside 01:00–06:00 (non-wrapping window)."""
    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 3)
    state = AutonomousState(slug="s", sleep_start_hour=1, sleep_end_hour=6)
    assert in_sleep_window(state) is True


def test_in_sleep_window_outside_non_wrapping(monkeypatch):
    """Hour 10 is outside 01:00–06:00."""
    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 10)
    state = AutonomousState(slug="s", sleep_start_hour=1, sleep_end_hour=6)
    assert in_sleep_window(state) is False


# ---------------------------------------------------------------------------
# pick_next_task
# ---------------------------------------------------------------------------

def test_pick_next_task_empty_backlog(tmp_path):
    """Empty backlog returns None."""
    cfg = _make_cfg(tmp_path)
    result = pick_next_task(cfg, "test-proj")
    assert result is None


def test_pick_next_task_skips_closed(tmp_path):
    """Closed tasks are skipped; only open tasks are returned."""
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "Closed task", status="closed")
    _write_task(cfg, "test-proj", "T-0002", "Open task", status="open")
    result = pick_next_task(cfg, "test-proj")
    assert result is not None
    assert result["id"] == "T-0002"


def test_pick_next_task_skips_totest(tmp_path):
    """Tasks with status=totest are skipped."""
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "In testing", status="totest")
    result = pick_next_task(cfg, "test-proj")
    assert result is None


def test_pick_next_task_prefers_reopened(tmp_path):
    """Reopened tasks are prioritized over open tasks."""
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "Open task", status="open")
    _write_task(cfg, "test-proj", "T-0002", "Reopened task", status="reopened")
    result = pick_next_task(cfg, "test-proj")
    assert result is not None
    assert result["id"] == "T-0002"


def test_pick_next_task_priority_must_beats_should(tmp_path):
    """A must-priority task is picked over a should-priority task."""
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "Should task", status="open", priority="should")
    _write_task(cfg, "test-proj", "T-0002", "Must task", status="open", priority="must")
    result = pick_next_task(cfg, "test-proj")
    assert result is not None
    assert result["id"] == "T-0002"


def test_pick_next_task_respects_tactical_priorities(tmp_path):
    """Task whose title matches tactical keywords is prioritized."""
    cfg = _make_cfg(tmp_path, "tp")
    # Create tactical.md with keyword "dashboard"
    vision_dir = cfg.data_dir / "tp" / "vision"
    vision_dir.mkdir(parents=True)
    (vision_dir / "tactical.md").write_text("## Current priorities\nBuild the dashboard feature.")

    # Both open
    _write_task(cfg, "tp", "T-0001", "Generic refactor", status="open")
    _write_task(cfg, "tp", "T-0002", "Dashboard improvements", status="open")
    result = pick_next_task(cfg, "tp")
    assert result is not None
    assert result["id"] == "T-0002"


def test_pick_next_task_skips_vague_titles(tmp_path):
    """Tasks with no body or very short titles are skipped."""
    cfg = _make_cfg(tmp_path)
    # One-word title, empty body
    p = cfg.data_dir / "test-proj" / "backlog" / "T-0001-todo.md"
    p.write_text("---\nid: T-0001\ntitle: TODO\nstatus: open\n---\n\n")
    # Valid task
    _write_task(cfg, "test-proj", "T-0002", "Real feature task", status="open")
    result = pick_next_task(cfg, "test-proj")
    assert result is not None
    assert result["id"] == "T-0002"


# ---------------------------------------------------------------------------
# tick — disabled state
# ---------------------------------------------------------------------------

def test_tick_disabled_noop(tmp_path, monkeypatch):
    """tick() on a disabled orchestrator is a no-op (status stays idle)."""
    cfg = _make_cfg(tmp_path)
    state = AutonomousState(slug="test-proj", enabled=False)
    save_state(cfg, state)

    # Even if there are tasks available, nothing should be spawned
    _write_task(cfg, "test-proj", "T-0001", "A real task", status="open")

    spawned = []
    monkeypatch.setattr("bot_squad_worker.autonomous.spawn_worker",
                        lambda *a, **kw: spawned.append(a) or "%1")

    tick(cfg, "test-proj")

    loaded = load_state(cfg, "test-proj")
    assert loaded.status == "idle"
    assert spawned == []


# ---------------------------------------------------------------------------
# tick — sleep window
# ---------------------------------------------------------------------------

def test_tick_sleep_window_transitions_to_sleeping(tmp_path, monkeypatch):
    """tick() inside sleep window sets status=sleeping."""
    cfg = _make_cfg(tmp_path)
    state = AutonomousState(slug="test-proj", enabled=True, sleep_start_hour=22, sleep_end_hour=8)
    save_state(cfg, state)

    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 23)

    _write_task(cfg, "test-proj", "T-0001", "Some task", status="open")
    tick(cfg, "test-proj")

    loaded = load_state(cfg, "test-proj")
    assert loaded.status == "sleeping"


# ---------------------------------------------------------------------------
# tick — idle → working (the critical spawn test — subprocess is MOCKED)
# ---------------------------------------------------------------------------

def test_tick_idle_with_task_spawns_and_transitions_to_working(tmp_path, monkeypatch):
    """tick(idle) with an available task spawns via sessions.spawn and transitions to working.

    T-0074: spawn_worker now delegates to sessions.spawn so the binding graph
    sees the orchestrator's pane. We mock sessions.spawn directly rather
    than subprocess.run so the test doesn't need to model every tmux call.
    """
    cfg = _make_cfg(tmp_path)
    state = AutonomousState(slug="test-proj", enabled=True, sleep_start_hour=22, sleep_end_hour=8)
    save_state(cfg, state)

    # Ensure we're outside sleep window
    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 12)

    _write_task(cfg, "test-proj", "T-0001", "Implement the feature", status="open")

    spawn_calls = []
    def fake_sessions_spawn(cfg_arg, slug_arg, window, **kwargs):
        spawn_calls.append({"slug": slug_arg, "window": window, **kwargs})
        # Return a sid in the canonical format so spawn_worker can derive pane_id.
        return {"ok": True, "sid": f"S-testuser-{window}-p42"}

    monkeypatch.setattr("bot_squad_worker.sessions.spawn", fake_sessions_spawn)
    monkeypatch.setattr("time.sleep", lambda x: None)

    tick(cfg, "test-proj")

    loaded = load_state(cfg, "test-proj")
    assert loaded.status == "working"
    assert loaded.current_task_id == "T-0001"
    assert loaded.current_pane_id == "%42"

    # T-0074: confirm the binding-graph plumbing (slug + task_id + window
    # prefix) is being passed through, not silently dropped.
    assert len(spawn_calls) == 1
    call = spawn_calls[0]
    assert call["slug"] == "test-proj"
    assert call["window"] == "auto-T-0001"
    assert call["task_id"] == "T-0001"


# ---------------------------------------------------------------------------
# tick — working state
# ---------------------------------------------------------------------------

def test_tick_working_pane_dead_totest_transitions_to_reviewing(tmp_path, monkeypatch):
    """tick(working) when pane is dead and task is totest → reviewing."""
    from bot_squad_worker.autonomous import _now_iso
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "Feature task", status="totest")
    state = AutonomousState(
        slug="test-proj", enabled=True, status="working",
        current_task_id="T-0001", current_pane_id="%10",
        current_started_at=_now_iso(),  # fresh timestamp to avoid timeout
    )
    save_state(cfg, state)

    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 12)
    # Pane is dead
    monkeypatch.setattr("bot_squad_worker.autonomous.pane_alive", lambda pid: False)

    tick(cfg, "test-proj")

    loaded = load_state(cfg, "test-proj")
    assert loaded.status == "reviewing"
    assert loaded.current_task_id == "T-0001"


def test_tick_working_pane_dead_not_totest_transitions_to_idle(tmp_path, monkeypatch):
    """tick(working) when pane is dead and task is NOT totest → idle, reopens task."""
    from bot_squad_worker.autonomous import _now_iso
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "Abandoned task", status="wip")
    state = AutonomousState(
        slug="test-proj", enabled=True, status="working",
        current_task_id="T-0001", current_pane_id="%11",
        current_started_at=_now_iso(),  # fresh timestamp to avoid timeout
    )
    save_state(cfg, state)

    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 12)
    monkeypatch.setattr("bot_squad_worker.autonomous.pane_alive", lambda pid: False)

    reopened_tasks = []
    original_reopen = __import__(
        "bot_squad_worker.autonomous", fromlist=["_reopen_task"]
    )._reopen_task

    def fake_reopen(cfg2, slug, task_id, comment):
        reopened_tasks.append(task_id)

    monkeypatch.setattr("bot_squad_worker.autonomous._reopen_task", fake_reopen)

    tick(cfg, "test-proj")

    loaded = load_state(cfg, "test-proj")
    assert loaded.status == "idle"
    assert loaded.current_task_id is None
    assert "T-0001" in reopened_tasks


# ---------------------------------------------------------------------------
# tick — reviewing state
# ---------------------------------------------------------------------------

def test_tick_reviewing_approve_closes_task(tmp_path, monkeypatch):
    """tick(reviewing) with DOD approval closes the task and resets to idle."""
    from bot_squad_worker.autonomous import _now_iso
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "Complete feature", status="totest")
    state = AutonomousState(
        slug="test-proj", enabled=True, status="reviewing",
        current_task_id="T-0001", current_pane_id="%12",
        current_started_at=_now_iso(),
    )
    save_state(cfg, state)

    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 12)
    monkeypatch.setattr(
        "bot_squad_worker.autonomous.run_dod_review",
        lambda cfg2, slug, task: {"approved": True, "rationale": "looks great"},
    )
    closed_tasks = []
    monkeypatch.setattr(
        "bot_squad_worker.autonomous._close_task",
        lambda cfg2, slug, task_id: closed_tasks.append(task_id),
    )

    tick(cfg, "test-proj")

    loaded = load_state(cfg, "test-proj")
    assert loaded.status == "idle"
    assert loaded.current_task_id is None
    assert "T-0001" in closed_tasks


def test_tick_reviewing_reject_reopens_with_comment(tmp_path, monkeypatch):
    """tick(reviewing) with DOD rejection reopens the task with feedback."""
    from bot_squad_worker.autonomous import _now_iso
    cfg = _make_cfg(tmp_path)
    _write_task(cfg, "test-proj", "T-0001", "Incomplete feature", status="totest")
    state = AutonomousState(
        slug="test-proj", enabled=True, status="reviewing",
        current_task_id="T-0001", current_pane_id="%13",
        current_started_at=_now_iso(),
    )
    save_state(cfg, state)

    monkeypatch.setattr("bot_squad_worker.autonomous._now_hour_utc", lambda: 12)
    monkeypatch.setattr(
        "bot_squad_worker.autonomous.run_dod_review",
        lambda cfg2, slug, task: {"approved": False, "rationale": "missing tests"},
    )
    reopened = []
    monkeypatch.setattr(
        "bot_squad_worker.autonomous._reopen_task",
        lambda cfg2, slug, task_id, comment: reopened.append((task_id, comment)),
    )

    tick(cfg, "test-proj")

    loaded = load_state(cfg, "test-proj")
    assert loaded.status == "idle"
    assert loaded.current_task_id is None
    assert loaded.fail_counts.get("T-0001", 0) == 1
    assert any("T-0001" in r[0] for r in reopened)
    assert any("missing tests" in r[1] for r in reopened)
