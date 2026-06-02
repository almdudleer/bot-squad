"""Tests for T-0149 drift enforcement (bot_squad_worker.drift)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from bot_squad_worker import drift, sessions as S
from tests.test_jobs import _make_config_with_project, _make_project_with_repo


@dataclass
class _FakePane:
    window: str = "dynamic-context-manager"
    pane_id: str = "%9"
    cwd: str = "/repo"
    pid: str = "111"
    session: str = "bot-squad"


SID = "S-tester-dynamic-context-manager-p9"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _setup(tmp_path: Path, *, updated_ago_min: int, activity_ago_sec: int,
           role: str = "dev", status: str = "active", task_id: str = "T-0149"):
    """Build cfg + a backlog ticket + a session md, return (cfg, slug, now, ticket)."""
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    now = time.time()

    backlog = cfg.data_dir / slug / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    ticket = backlog / f"{task_id}-dynamic-context-manager.md"
    ticket.write_text(
        f"---\nid: {task_id}\ntitle: Dynamic context manager\n"
        f"status: in_progress\nupdated: {_iso(now - updated_ago_min * 60)}\n---\n\n"
        "## DoD\n- do the thing\n"
    )

    sessions_dir = cfg.data_dir / slug / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{SID}.md").write_text(
        f"---\nsid: {SID}\nstatus: {status}\nwindow: dynamic-context-manager\n"
        f"pane_id: %9\nclaude_uuid: uuid-1\ntask_id: {task_id}\n---\n"
    )

    row = {
        "sid": SID, "status": status, "role": role, "task_id": task_id,
        "activity_at": now - activity_ago_sec, "cwd": "/repo",
        "claude_uuid": "uuid-1", "started_at": _iso(now - 3 * 3600),
    }

    def patch(monkeypatch):
        monkeypatch.setattr(S, "list_sessions", lambda c, s: [row])
        monkeypatch.setattr(S, "_get_current_user", lambda: "tester")
        monkeypatch.setattr(S, "list_panes", lambda: [_FakePane()])
        monkeypatch.setattr(S, "compute_sid", lambda u, w, p: SID)

    return cfg, slug, now, ticket, patch


def test_stale_session_gets_nudged(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=90, activity_ago_sec=30)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setenv("BOT_SQUAD_DRIFT_COOLDOWN_MINUTES", "30")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))

    res = drift.drift_check(cfg, slug)
    assert res["ok"] and len(res["nudged"]) == 1
    assert res["nudged"][0]["signal"] == "stale"
    assert delivered and "DRIFT CHECK" in delivered[0][1] and "T-0149" in delivered[0][1]


def test_disabled_by_env(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=600, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "0")
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res.get("disabled") is True and not delivered


def test_idle_session_not_nudged(tmp_path, monkeypatch):
    # Recent activity is older than the active window → waiting at a prompt.
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=600, activity_ago_sec=99999)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_non_dev_role_not_nudged(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=600, activity_ago_sec=10, role="tl")
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_superpowers_signal(tmp_path, monkeypatch):
    # Not stale (recent update) but writing to superpowers → superpowers signal.
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=1, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets",
                        lambda *_a, **_k: ["/home/u/.claude/superpowers/skills/foo.md"])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "superpowers"
    assert "T-0152" in delivered[0][1] and "superpowers" in delivered[0][1]


def test_automation_signal_only_without_scenario(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=1, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets",
                        lambda *_a, **_k: ["/repo/tests/e2e/foo.mjs"])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert len(res["nudged"]) == 1 and res["nudged"][0]["signal"] == "automation"


def test_automation_signal_suppressed_when_scenario_exists(tmp_path, monkeypatch):
    # Manual scenario already written → premature-automation signal is suppressed,
    # and the ticket was just updated so there's no stale signal either.
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=1, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setattr(drift, "_recent_write_targets",
                        lambda *_a, **_k: ["/repo/tests/e2e/foo.mjs"])
    scen = cfg.data_dir / slug / "scenarios"
    scen.mkdir(parents=True, exist_ok=True)
    (scen / "T-0149-dynamic-context-manager.md").write_text("# scenario\n")
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))
    res = drift.drift_check(cfg, slug)
    assert res["nudged"] == [] and not delivered


def test_cooldown_suppresses_second_nudge(tmp_path, monkeypatch):
    cfg, slug, now, ticket, patch = _setup(tmp_path, updated_ago_min=90, activity_ago_sec=10)
    patch(monkeypatch)
    monkeypatch.setenv("BOT_SQUAD_DRIFT_MINUTES", "45")
    monkeypatch.setenv("BOT_SQUAD_DRIFT_COOLDOWN_MINUTES", "30")
    monkeypatch.setattr(drift, "_recent_write_targets", lambda *_a, **_k: [])
    delivered = []
    monkeypatch.setattr(S, "_deliver_prompt", lambda pane, text: delivered.append((pane, text)))

    first = drift.drift_check(cfg, slug)
    assert len(first["nudged"]) == 1
    second = drift.drift_check(cfg, slug)  # within cooldown
    assert second["nudged"] == [] and len(delivered) == 1
