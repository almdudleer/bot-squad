"""Tests for worker.sessions — session list/pause/resume/spawn."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker.sessions import (
    PaneInfo,
    compute_sid,
    discover_claude_uuid,
    list_panes,
    list_sessions,
    pause,
    resume,
    spawn,
    _read_session_metadata,
    _write_session_metadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, repo_path: Path | None = None) -> Any:
    """Build a minimal Config-like object for testing."""
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)

    rp = str(repo_path or (tmp_path / "repo"))
    (cfg_dir / "projects.toml").write_text(
        f'[projects.test-project]\n'
        f'slug = "test-project"\n'
        f'display_name = "Test Project"\n'
        f'repo_path = "{rp}"\n'
        f'deploy_branch = "agent_team/dev"\n'
        f'master_branch = "master"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)

    # Build a simple namespace object with the right data_dir
    import types
    patched = types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        tg_bot_token=cfg.tg_bot_token,
    )
    return patched


def _fake_pane(pane_id="%2", window="mywin", pid="1234", cwd="/tmp/repo", command="claude"):
    return PaneInfo(pane_id=pane_id, window=window, pid=pid, cwd=cwd, command=command)


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------

def test_compute_sid_strips_pct():
    assert compute_sid("alice", "mywin", "%2") == "S-alice-mywin-p2"


def test_compute_sid_no_pct():
    assert compute_sid("alice", "mywin", "3") == "S-alice-mywin-p3"


def test_discover_claude_uuid_returns_stem(tmp_path):
    proj_dir = tmp_path / ".claude" / "projects" / "home-alice-myrepo"
    proj_dir.mkdir(parents=True)
    uuid_file = proj_dir / "abc123-0000-0000-0000-000000000000.jsonl"
    uuid_file.write_text("{}")
    result = discover_claude_uuid("/home/alice/myrepo", str(tmp_path))
    assert result == "abc123-0000-0000-0000-000000000000"


def test_discover_claude_uuid_no_dir(tmp_path):
    result = discover_claude_uuid("/nonexistent/path", str(tmp_path))
    assert result is None


def test_discover_claude_uuid_no_jsonl(tmp_path):
    proj_dir = tmp_path / ".claude" / "projects" / "home-alice-myrepo"
    proj_dir.mkdir(parents=True)
    result = discover_claude_uuid("/home/alice/myrepo", str(tmp_path))
    assert result is None


def test_discover_claude_uuid_returns_latest(tmp_path):
    proj_dir = tmp_path / ".claude" / "projects" / "tmp-repo"
    proj_dir.mkdir(parents=True)
    old = proj_dir / "old-uuid.jsonl"
    new = proj_dir / "new-uuid.jsonl"
    old.write_text("{}")
    time.sleep(0.01)
    new.write_text("{}")
    result = discover_claude_uuid("/tmp/repo", str(tmp_path))
    assert result == "new-uuid"


def test_read_write_session_metadata_roundtrip(tmp_path):
    path = tmp_path / "S-test.md"
    meta = {"sid": "S-x", "status": "paused", "linked_tasks": ["T-0001"], "cwd": "/tmp"}
    _write_session_metadata(path, meta)
    result = _read_session_metadata(path)
    assert result["sid"] == "S-x"
    assert result["status"] == "paused"
    assert result["linked_tasks"] == ["T-0001"]


def test_read_session_metadata_missing_file(tmp_path):
    result = _read_session_metadata(tmp_path / "nonexistent.md")
    assert result is None


def test_read_session_metadata_empty_list(tmp_path):
    path = tmp_path / "S-test.md"
    _write_session_metadata(path, {"linked_tasks": []})
    result = _read_session_metadata(path)
    assert result["linked_tasks"] == []


# ---------------------------------------------------------------------------
# list_panes — monkeypatched subprocess
# ---------------------------------------------------------------------------

def test_list_panes_parses_output(monkeypatch):
    fake_output = "%2|mywin|1234|/tmp/repo|claude\n%3|otherwin|5678|/tmp/other|bash\n"

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=0, stdout=fake_output, stderr="")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)

    panes = list_panes()
    assert len(panes) == 2
    assert panes[0].pane_id == "%2"
    assert panes[0].window == "mywin"
    assert panes[0].command == "claude"
    assert panes[1].command == "bash"


def test_list_panes_returns_empty_on_error(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="no server")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)

    panes = list_panes()
    assert panes == []


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------

def test_list_sessions_active_pane(tmp_path, monkeypatch):
    """Active claude pane in the project's repo shows up."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    fake_pane_output = f"%2|mywin|1234|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["sid"] == "S-testuser-mywin-p2"
    assert rows[0]["window"] == "mywin"


def test_list_sessions_filters_non_claude_panes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    fake_pane_output = f"%2|mywin|1234|{repo}|bash\n%3|win2|5678|{repo}|python\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    assert rows == []


def test_list_sessions_filters_wrong_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    fake_pane_output = "/tmp/other-repo|bash|1234|%2|win\n%2|win|1234|/tmp/other|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    assert rows == []


def test_list_sessions_includes_paused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    # Write a paused session metadata file
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    meta_path = sessions_dir / "S-testuser-mywin-p5.md"
    _write_session_metadata(meta_path, {
        "sid": "S-testuser-mywin-p5",
        "status": "paused",
        "window": "mywin",
        "cwd": str(repo),
        "claude_uuid": "some-uuid",
        "paused_at": "2026-05-10T12:00:00Z",
        "linked_tasks": [],
    })

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["status"] == "paused"
    assert rows[0]["sid"] == "S-testuser-mywin-p5"


def test_list_sessions_unknown_slug(tmp_path, monkeypatch):
    from bot_squad_worker.actions import ActionError
    cfg = _make_cfg(tmp_path)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)

    with pytest.raises(ActionError, match="unknown project slug"):
        list_sessions(cfg, "no-such-slug")


# ---------------------------------------------------------------------------
# pause
# ---------------------------------------------------------------------------

def test_pause_writes_metadata_and_sends_keys(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "list-panes" in args:
            # First call (initial pane lookup): return the pane.
            # Subsequent calls (wait-loop polling): return empty to simulate pane died.
            list_panes_calls = sum(1 for c in calls if "list-panes" in c)
            if list_panes_calls <= 1:
                return subprocess.CompletedProcess(args, 0, f"%2|mywin|1234|{repo}|claude\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = pause(cfg, "test-project", "S-testuser-mywin-p2")

    assert result == {"ok": True, "paused": True}

    # Metadata file must exist
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    meta_file = sessions_dir / "S-testuser-mywin-p2.md"
    assert meta_file.exists()
    meta = _read_session_metadata(meta_file)
    assert meta["status"] == "paused"

    # send-keys calls: C-c and /exit
    send_key_calls = [c for c in calls if "send-keys" in c]
    assert len(send_key_calls) == 2
    assert "C-c" in send_key_calls[0]
    assert "/exit" in send_key_calls[1]


def test_pause_unknown_sid_raises(tmp_path, monkeypatch):
    from bot_squad_worker.actions import ActionError
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    with pytest.raises(ActionError, match="no active pane"):
        pause(cfg, "test-project", "S-testuser-nowin-p99")


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

def test_spawn_creates_new_window(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    pane_calls = []

    def fake_run(args, **kwargs):
        pane_calls.append(args)
        if "list-panes" in args:
            # Before spawn: empty; after spawn: has the new pane
            if len([c for c in pane_calls if "list-panes" in c]) > 1:
                return subprocess.CompletedProcess(args, 0, f"%7|spec5-smoke|2222|{repo}|claude\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = spawn(cfg, "test-project", "spec5-smoke")
    assert result["ok"] is True
    assert result["sid"] == "S-testuser-spec5-smoke-p7"


def test_spawn_sends_initial_prompt(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    key_calls = []
    new_window_called = [False]

    def fake_run(args, **kwargs):
        if "send-keys" in args:
            key_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if "new-window" in args:
            new_window_called[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            # Before new-window: no panes. After: one new pane.
            if new_window_called[0]:
                return subprocess.CompletedProcess(args, 0, f"%8|newwin|3333|{repo}|claude\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "newwin", initial_prompt="hello world")

    assert any("hello world" in str(c) for c in key_calls), \
        f"expected hello world send-keys; got: {key_calls}"


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def test_resume_resumes_paused_session(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-mywin-p5.md", {
        "sid": "S-testuser-mywin-p5",
        "status": "paused",
        "window": "mywin",
        "cwd": str(repo),
        "claude_uuid": "fake-uuid-1234",
        "paused_at": "2026-05-10T12:00:00Z",
        "linked_tasks": [],
    })

    pane_list_calls = []

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            pane_list_calls.append(1)
            # First call (pre-spawn check): empty; subsequent: new pane
            if len(pane_list_calls) <= 1:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, f"%9|mywin|9999|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = resume(cfg, "test-project", "S-testuser-mywin-p5")
    assert result["ok"] is True
    new_sid = result["sid"]
    assert new_sid.startswith("S-testuser-")

    # New metadata file should exist and be active
    new_meta_file = sessions_dir / f"{new_sid}.md"
    assert new_meta_file.exists()
    meta = _read_session_metadata(new_meta_file)
    assert meta["status"] == "active"


def test_resume_unknown_sid_raises(tmp_path, monkeypatch):
    from bot_squad_worker.actions import ActionError
    cfg = _make_cfg(tmp_path)

    with pytest.raises(ActionError, match="no metadata found"):
        resume(cfg, "test-project", "S-testuser-nowin-p0")
