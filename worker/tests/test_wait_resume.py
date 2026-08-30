"""T-0930: automatic resume for WAITING (blocked_on_user, T-0931) sessions.

Pairs with test_idle_timeout (the WAITING stamp is written by
idle_timeout._terminate_and_remember) and test_graceful_exit (the other
"never block indefinitely" outcome). This file covers the resume HALF: a
suspended session carrying wait_reason="blocked_on_user" is auto-resumed the
moment its named task leaves that status, with no human in the loop.
"""
from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker import wait_resume as WR


# --- env knob ----------------------------------------------------------------

def test_enabled_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_WAIT_RESUME", raising=False)
    assert WR.wait_resume_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_WAIT_RESUME", "0")
    assert WR.wait_resume_enabled() is False


# --- pure decision helpers ----------------------------------------------------

def test_condition_cleared_only_when_status_left_blocked_on_user():
    assert WR.condition_cleared("in_progress") is True
    assert WR.condition_cleared("closed") is True
    assert WR.condition_cleared("blocked_on_user") is False
    assert WR.condition_cleared("") is False
    assert WR.condition_cleared(None) is False


def test_resume_prompt_names_the_task_and_new_status():
    text = WR.resume_prompt("T-0042", "in_progress")
    assert "T-0042" in text
    assert "in_progress" in text
    assert "no longer blocked_on_user" in text


# --- cfg + session/backlog md harness (mirrors test_graceful_exit) ----------

def _make_cfg(tmp_path: Path, *, sid: str, wait_task_id: str | None,
              wait_reason: str | None = "blocked_on_user",
              status: str = "suspended", extra_md: dict | None = None):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True)
    fm = {"sid": sid, "status": status, "window": "demo",
          "cwd": str(repo), "claude_uuid": "uuid-" + sid}
    if wait_task_id:
        fm["task_id"] = wait_task_id
    if wait_reason:
        fm["wait_reason"] = wait_reason
    if wait_task_id:
        fm["wait_task_id"] = wait_task_id
    if extra_md:
        fm.update(extra_md)
    S._write_session_metadata(sess / f"{sid}.md", fm)

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir)
    return patched, data_dir


def _write_task(data_dir: Path, task_id: str, status: str):
    backlog = data_dir / "bot-squad" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / f"{task_id}-demo.md").write_text(
        f"---\nid: {task_id}\ntitle: Demo\nstatus: {status}\n---\n\nbody\n")


@pytest.fixture
def resume_seams(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(S, "resume",
                        lambda cfg, slug, sid, initial_prompt=None, task_id=None:
                        calls.append((sid, initial_prompt)) or {"ok": True})
    return calls


# --- maybe_resume -------------------------------------------------------------

def test_resumes_when_block_lifted(tmp_path, resume_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, wait_task_id="T-0042")
    _write_task(data, "T-0042", "in_progress")
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert WR.maybe_resume(cfg, "bot-squad", sid, meta) is True
    assert len(resume_seams) == 1
    assert resume_seams[0][0] == sid
    assert "T-0042" in resume_seams[0][1]


def test_not_resumed_while_still_blocked(tmp_path, resume_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, wait_task_id="T-0042")
    _write_task(data, "T-0042", "blocked_on_user")
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert WR.maybe_resume(cfg, "bot-squad", sid, meta) is False
    assert resume_seams == []


def test_not_resumed_when_task_missing(tmp_path, resume_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, wait_task_id="T-9999")
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert WR.maybe_resume(cfg, "bot-squad", sid, meta) is False
    assert resume_seams == []


def test_not_resumed_without_wait_reason(tmp_path, resume_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, wait_task_id="T-0042", wait_reason=None)
    _write_task(data, "T-0042", "in_progress")
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert WR.maybe_resume(cfg, "bot-squad", sid, meta) is False
    assert resume_seams == []


def test_not_resumed_when_not_suspended(tmp_path, resume_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, wait_task_id="T-0042", status="active")
    _write_task(data, "T-0042", "in_progress")
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert WR.maybe_resume(cfg, "bot-squad", sid, meta) is False
    assert resume_seams == []


def test_kill_switch_disables_resume(tmp_path, resume_seams, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_WAIT_RESUME", "0")
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, wait_task_id="T-0042")
    _write_task(data, "T-0042", "in_progress")
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert WR.maybe_resume(cfg, "bot-squad", sid, meta) is False
    assert resume_seams == []


def test_resume_send_failure_is_swallowed(tmp_path, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, wait_task_id="T-0042")
    _write_task(data, "T-0042", "in_progress")

    def _raise(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(S, "resume", _raise)
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert WR.maybe_resume(cfg, "bot-squad", sid, meta) is False


# --- tick ----------------------------------------------------------------

def test_tick_resumes_every_project_and_skips_others(tmp_path, resume_seams, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True)

    # One session waiting and cleared -> should resume.
    S._write_session_metadata(sess / "S-almdudleer-bot-squad-a-p1.md", {
        "sid": "S-almdudleer-bot-squad-a-p1", "status": "suspended",
        "window": "a", "cwd": str(repo), "claude_uuid": "uuid-a",
        "task_id": "T-0001", "wait_reason": "blocked_on_user",
        "wait_task_id": "T-0001",
    })
    # One session waiting but still blocked -> should NOT resume.
    S._write_session_metadata(sess / "S-almdudleer-bot-squad-b-p2.md", {
        "sid": "S-almdudleer-bot-squad-b-p2", "status": "suspended",
        "window": "b", "cwd": str(repo), "claude_uuid": "uuid-b",
        "task_id": "T-0002", "wait_reason": "blocked_on_user",
        "wait_task_id": "T-0002",
    })
    # One ordinary suspended session with no wait_reason -> untouched.
    S._write_session_metadata(sess / "S-almdudleer-bot-squad-c-p3.md", {
        "sid": "S-almdudleer-bot-squad-c-p3", "status": "suspended",
        "window": "c", "cwd": str(repo), "claude_uuid": "uuid-c",
    })
    backlog = data_dir / "bot-squad" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / "T-0001-demo.md").write_text(
        "---\nid: T-0001\ntitle: A\nstatus: in_progress\n---\n\nbody\n")
    (backlog / "T-0002-demo.md").write_text(
        "---\nid: T-0002\ntitle: B\nstatus: blocked_on_user\n---\n\nbody\n")

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir)
    WR.tick(patched)
    assert [c[0] for c in resume_seams] == ["S-almdudleer-bot-squad-a-p1"]


def test_tick_noop_when_disabled(tmp_path, resume_seams, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_WAIT_RESUME", "0")
    cfg = types.SimpleNamespace(projects={"bot-squad": object()}, data_dir=tmp_path)
    WR.tick(cfg)
    assert resume_seams == []
