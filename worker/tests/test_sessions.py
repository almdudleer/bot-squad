"""Tests for worker.sessions — session list/pause/resume/spawn."""
from __future__ import annotations

import os
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
        f'deploy_branch = "bot_squad/dev"\n'
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
    proj_dir = tmp_path / ".claude" / "projects" / "-home-alice-myrepo"
    proj_dir.mkdir(parents=True)
    uuid_file = proj_dir / "abc123-0000-0000-0000-000000000000.jsonl"
    uuid_file.write_text("{}")
    result = discover_claude_uuid("/home/alice/myrepo", str(tmp_path))
    assert result == "abc123-0000-0000-0000-000000000000"


def test_discover_claude_uuid_no_dir(tmp_path):
    result = discover_claude_uuid("/nonexistent/path", str(tmp_path))
    assert result is None


def test_discover_claude_uuid_no_jsonl(tmp_path):
    proj_dir = tmp_path / ".claude" / "projects" / "-home-alice-myrepo"
    proj_dir.mkdir(parents=True)
    result = discover_claude_uuid("/home/alice/myrepo", str(tmp_path))
    assert result is None


def test_discover_claude_uuid_returns_latest(tmp_path):
    proj_dir = tmp_path / ".claude" / "projects" / "-tmp-repo"
    proj_dir.mkdir(parents=True)
    old = proj_dir / "old-uuid.jsonl"
    new = proj_dir / "new-uuid.jsonl"
    old.write_text("{}")
    time.sleep(0.01)
    new.write_text("{}")
    result = discover_claude_uuid("/tmp/repo", str(tmp_path))
    assert result == "new-uuid"


def test_discover_claude_uuid_round_trip_real_encoding(tmp_path):
    """T-0118 regression: Claude's path-encoding keeps the leading dash.

    Set up a project dir using the EXACT encoding Claude uses on real disk
    (verified against /home/almdudleer/.claude/projects/ which contains
    entries like `-home-almdudleer-bot-squad-mgmt`). Then assert that
    discover_claude_uuid locates a jsonl whose stem matches. If a future
    change re-introduces lstrip('-') or any other leading-dash mutation,
    proj_dir will be looked up at the wrong path and this test fails.
    """
    cwd = "/home/almdudleer/bot-squad-mgmt"
    # Real encoding — the leading slash becomes a leading dash and stays.
    encoded = "-home-almdudleer-bot-squad-mgmt"
    assert cwd.replace("/", "-") == encoded, (
        "round-trip premise broke: replace('/', '-') must yield the "
        "leading-dash form Claude writes to disk"
    )
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    uuid_stem = "deadbeef-1234-5678-9abc-def012345678"
    (proj_dir / f"{uuid_stem}.jsonl").write_text("{}")
    result = discover_claude_uuid(cwd, str(tmp_path))
    assert result == uuid_stem


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
    # Phase 6: initiative key is always present (empty string when unset).
    assert "initiative" in rows[0]
    assert rows[0]["initiative"] == ""


def test_list_sessions_operator_pane_in_workspace_parent(tmp_path, monkeypatch):
    """T-0003: operator pane lives in repo_workspace (parent of dev clone).

    Repo: /tmp/x/bot-squad/dev. Operator pane cwd: /tmp/x/bot-squad
    (the workspace, parent of dev). Previously the cwd-match required
    pane_cwd == or descendant of repo_path, so the operator was filtered
    out of active enumeration and re-emerged via the suspended-md loop
    as status=suspended even though the pane is alive.

    Fix: accept the parent-of-repo case when bounded by tmux session ==
    slug (here `bot-squad`) or window == 'operator'.
    """
    workspace = tmp_path / "bot-squad"
    workspace.mkdir()
    repo = workspace / "dev"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    # Override slug for clarity — _make_cfg writes test-project; rebuild with bot-squad.
    cfg_dir = tmp_path / "config"
    (cfg_dir / "projects.toml").write_text(
        f'[projects.bot-squad]\n'
        f'slug = "bot-squad"\n'
        f'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        f'deploy_branch = "bot_squad/dev"\n'
        f'master_branch = "master"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    from bot_squad_worker.config import Config
    import types
    reloaded = Config.load(cfg_dir)
    cfg = types.SimpleNamespace(
        projects=reloaded.projects,
        data_dir=cfg.data_dir,
        tg_bot_token=reloaded.tg_bot_token,
    )
    (cfg.data_dir / "bot-squad" / "backlog").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "bot-squad" / "sessions").mkdir(parents=True, exist_ok=True)

    # 6-field format: pane_id|window|pid|cwd|command|session_name
    fake_pane_output = f"%7|operator|1234|{workspace}|claude|bot-squad\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "almdudleer")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "bot-squad")
    assert len(rows) == 1, rows
    assert rows[0]["sid"] == "S-almdudleer-operator-p7"
    assert rows[0]["status"] == "active"
    assert rows[0]["window"] == "operator"
    assert rows[0]["cwd"] == str(workspace)


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
    # Paused md with no live pane is surfaced as "suspended" (resurrectable).
    assert rows[0]["status"] == "suspended"
    assert rows[0]["sid"] == "S-testuser-mywin-p5"
    # Phase 6: initiative key is always present (empty string when md omits it).
    assert "initiative" in rows[0]
    assert rows[0]["initiative"] == ""


def test_list_sessions_active_pane_with_initiative(tmp_path, monkeypatch):
    """Active pane with an initiative recorded in its md surfaces it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    # Pre-write a session md with an initiative field (mimics the hook).
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    meta_path = sessions_dir / "S-testuser-mywin-p2.md"
    _write_session_metadata(meta_path, {
        "sid": "S-testuser-mywin-p2",
        "status": "active",
        "window": "mywin",
        "cwd": str(repo),
        "claude_uuid": "some-uuid",
        "task_id": "~",
        "initiative": "v0.7-news-subscriptions.md",
        "started_at": "2026-05-12T10:00:00Z",
    })

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
    assert rows[0]["initiative"] == "v0.7-news-subscriptions.md"


def test_list_sessions_recovers_started_at_after_window_rename(tmp_path, monkeypatch):
    """T-0118: live pane whose tmux window was renamed still surfaces
    started_at / task_id from the pre-rename md via claude_uuid fallback.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    # Pre-rename md file lives at the OLD SID (window was 'teamlead' when
    # the session started) — file holds the rename-invariant claude_uuid
    # plus started_at, task_id, owner.
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-teamlead-p11.md", {
        "sid": "S-testuser-teamlead-p11",
        "status": "active",
        "window": "teamlead",
        "cwd": str(repo),
        "claude_uuid": "uuid-after-rename",
        "task_id": "T-0100",
        "started_at": "2026-05-23T15:37:12Z",
        "owner": "alexey",
    })

    # Live pane reports the NEW window name — same pane_id, same uuid,
    # so the SID-keyed lookup misses but the uuid fallback should hit.
    # Encoding mirrors claude's on-disk layout — leading slash → leading dash.
    encoded = str(repo).replace("/", "-")
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "uuid-after-rename.jsonl").write_text("{}")

    fake_pane_output = f"%11|ui_polish-TL|1234|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    # Exactly one row — the suspended-md loop must NOT also emit the
    # pre-rename SID as a separate suspended session.
    assert len(rows) == 1
    assert rows[0]["sid"] == "S-testuser-ui_polish-TL-p11"
    assert rows[0]["status"] == "active"
    assert rows[0]["started_at"] == "2026-05-23T15:37:12Z"
    assert rows[0]["task_id"] == "T-0100"
    assert rows[0]["owner"] == "alexey"


def test_list_sessions_per_pane_uuid_under_shared_cwd(tmp_path, monkeypatch):
    """T-0120: panes sharing a cwd each surface their OWN md.

    On staging the 3 active bot-squad TLs (ui_polish-TL-p11,
    multi_server-TL-p9, update_delivery-TL-p13) all share
    /home/almdudleer/bot-squad-mgmt. discover_claude_uuid returns the
    cwd's mtime-latest jsonl — the SAME uuid for all three — so the
    T-0118 uuid-keyed fallback in _find_session_md attributed ONE
    pane's started_at + task_id + initiative to ALL of them.

    Fix: _pane_claude_uuid_from_proc walks each pane's /proc descendants
    for the live claude process's open jsonl fd — pane-specific. Test
    asserts each post-rename SID resolves to ITS OWN md, not a neighbor's.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Pre-rename mds carry distinct claude_uuids; post-rename SIDs (computed
    # from the live tmux window names) miss the SID-keyed lookup, so the
    # fallback path has to disambiguate using the per-pane uuid.
    _write_session_metadata(sessions_dir / "S-testuser-teamlead-p11.md", {
        "sid": "S-testuser-teamlead-p11", "status": "active", "window": "teamlead",
        "cwd": str(repo), "claude_uuid": "uuid-ui-polish",
        "task_id": "T-0120", "initiative": "ui-polish.md",
        "started_at": "2026-05-15T10:00:00Z", "owner": "alexey",
    })
    _write_session_metadata(sessions_dir / "S-testuser-multi_server-p9.md", {
        "sid": "S-testuser-multi_server-p9", "status": "active", "window": "multi_server",
        "cwd": str(repo), "claude_uuid": "uuid-multi-server",
        "task_id": "T-0119", "initiative": "multi-server.md",
        "started_at": "2026-05-15T11:00:00Z", "owner": "alexey",
    })
    _write_session_metadata(sessions_dir / "S-testuser-teamlead-p13.md", {
        "sid": "S-testuser-teamlead-p13", "status": "active", "window": "teamlead",
        "cwd": str(repo), "claude_uuid": "uuid-update-delivery",
        "task_id": "T-0117", "initiative": "update-delivery.md",
        "started_at": "2026-05-15T12:00:00Z", "owner": "alexey",
    })

    # discover_claude_uuid will collapse onto whatever was written last —
    # populate the encoded project dir so it returns a real value (proving
    # the test is exercising the disambiguation path, not a None-fallback).
    encoded = str(repo).replace("/", "-")
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    for stem in ("uuid-ui-polish", "uuid-multi-server", "uuid-update-delivery"):
        (proj_dir / f"{stem}.jsonl").write_text("{}")

    # 3 panes, shared cwd, distinct pids and post-rename window names.
    fake_panes = (
        f"%11|ui_polish-TL|3001|{repo}|claude\n"
        f"%9|multi_server-TL|3002|{repo}|claude\n"
        f"%13|update_delivery-TL|3003|{repo}|claude\n"
    )

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_panes, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    pid_to_uuid = {
        "3001": "uuid-ui-polish",
        "3002": "uuid-multi-server",
        "3003": "uuid-update-delivery",
    }

    def fake_proc(pid, user_home):
        return pid_to_uuid.get(pid)

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S, "_pane_claude_uuid_from_proc", fake_proc)

    rows = list_sessions(cfg, "test-project")
    by_sid = {r["sid"]: r for r in rows if r["status"] == "active"}

    # Each post-rename SID resolves to its OWN pre-rename md.
    assert by_sid["S-testuser-ui_polish-TL-p11"]["started_at"] == "2026-05-15T10:00:00Z"
    assert by_sid["S-testuser-ui_polish-TL-p11"]["task_id"] == "T-0120"
    assert by_sid["S-testuser-ui_polish-TL-p11"]["initiative"] == "ui-polish.md"

    assert by_sid["S-testuser-multi_server-TL-p9"]["started_at"] == "2026-05-15T11:00:00Z"
    assert by_sid["S-testuser-multi_server-TL-p9"]["task_id"] == "T-0119"
    assert by_sid["S-testuser-multi_server-TL-p9"]["initiative"] == "multi-server.md"

    assert by_sid["S-testuser-update_delivery-TL-p13"]["started_at"] == "2026-05-15T12:00:00Z"
    assert by_sid["S-testuser-update_delivery-TL-p13"]["task_id"] == "T-0117"
    assert by_sid["S-testuser-update_delivery-TL-p13"]["initiative"] == "update-delivery.md"

    # Sanity: no two active rows bleed the same started_at — the failure mode
    # this test exists to prevent.
    starts = [r["started_at"] for r in by_sid.values()]
    assert len(set(starts)) == 3, f"active TLs must have distinct started_at, got {starts}"


def test_list_sessions_suspended_with_initiative(tmp_path, monkeypatch):
    """Suspended-md path surfaces initiative from frontmatter."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    meta_path = sessions_dir / "S-testuser-mywin-p7.md"
    _write_session_metadata(meta_path, {
        "sid": "S-testuser-mywin-p7",
        "status": "suspended",
        "window": "mywin",
        "cwd": str(repo),
        "claude_uuid": "another-uuid",
        "initiative": "foo.md",
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
    assert rows[0]["initiative"] == "foo.md"


# ---------------------------------------------------------------------------
# T-0104: activity-derived "running" vs "idle" — jsonl-mtime probe
# ---------------------------------------------------------------------------

def _setup_activity_probe(tmp_path, monkeypatch):
    """Boilerplate: a live claude pane in `tmp_path/repo` + its jsonl file.

    Returns (cfg, jsonl_path) so the test can mutate the jsonl mtime to
    drive the running/idle derivation.
    """
    import bot_squad_worker.sessions as S
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    # Encode cwd to the path claude uses under ~/.claude/projects/.
    # T-0118: claude keeps the leading dash; do NOT lstrip.
    encoded = str(repo).replace("/", "-")
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    uuid = "fff00000-0000-0000-0000-000000000fff"
    jsonl = proj_dir / f"{uuid}.jsonl"
    jsonl.write_text("{}")

    fake_pane_output = f"%9|mywin|1234|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    return cfg, jsonl


def test_pane_activity_at_returns_jsonl_mtime(tmp_path):
    from bot_squad_worker.sessions import _pane_activity_at
    # Real claude encoding keeps the leading dash (T-0118).
    encoded = "-tmp-repo"
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / "abc.jsonl"
    jsonl.write_text("{}")
    expected = jsonl.stat().st_mtime
    got = _pane_activity_at("/tmp/repo", "abc", str(tmp_path))
    assert got == expected


def test_pane_activity_at_none_when_no_uuid(tmp_path):
    from bot_squad_worker.sessions import _pane_activity_at
    assert _pane_activity_at("/tmp/repo", None, str(tmp_path)) is None


def test_pane_activity_at_none_when_jsonl_missing(tmp_path):
    from bot_squad_worker.sessions import _pane_activity_at
    assert _pane_activity_at("/tmp/repo", "nope", str(tmp_path)) is None


def test_derive_activity_running_when_fresh():
    from bot_squad_worker.sessions import _derive_activity
    now = 1000.0
    assert _derive_activity("active", now - 5.0, now) == "running"


def test_derive_activity_idle_when_stale():
    from bot_squad_worker.sessions import _derive_activity
    now = 1000.0
    assert _derive_activity("active", now - 60.0, now) == "idle"


def test_derive_activity_idle_when_no_signal():
    from bot_squad_worker.sessions import _derive_activity
    assert _derive_activity("active", None, 1000.0) == "idle"


def test_derive_activity_paused_overrides_activity():
    from bot_squad_worker.sessions import _derive_activity
    # Even if jsonl is fresh, an explicit md=paused (Ctrl-C) wins so the
    # UI still offers Resume rather than confusing the operator.
    assert _derive_activity("paused", 1000.0 - 1.0, 1000.0) == "paused"


# ---------------------------------------------------------------------------
# T-0046: active-at-prompt detection (pane idle >= IDLE_AT_PROMPT_SECONDS)
# ---------------------------------------------------------------------------

def test_is_active_at_prompt_true_when_idle_past_threshold():
    from bot_squad_worker.sessions import _is_active_at_prompt
    now = 1000.0
    # 90s idle > 60s default — flag as at-prompt.
    assert _is_active_at_prompt("active", now - 90.0, now, threshold_sec=60.0) is True


def test_is_active_at_prompt_false_when_recent_write():
    from bot_squad_worker.sessions import _is_active_at_prompt
    now = 1000.0
    # 5s idle < 60s — still crunching.
    assert _is_active_at_prompt("active", now - 5.0, now, threshold_sec=60.0) is False


def test_is_active_at_prompt_false_when_paused():
    from bot_squad_worker.sessions import _is_active_at_prompt
    # Paused is its own needs-input case — caller handles separately.
    assert _is_active_at_prompt("paused", 0.0, 1000.0, threshold_sec=60.0) is False


def test_is_active_at_prompt_false_when_activity_unknown():
    from bot_squad_worker.sessions import _is_active_at_prompt
    # No write timestamp → can't measure idle time → conservative False.
    assert _is_active_at_prompt("active", None, 1000.0, threshold_sec=60.0) is False


def test_is_active_at_prompt_uses_env_default(monkeypatch):
    """Threshold is configurable via BOT_SQUAD_IDLE_AT_PROMPT_SECONDS."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "IDLE_AT_PROMPT_SECONDS", 5.0)
    now = 1000.0
    # 10s idle > 5s overridden threshold.
    assert S._is_active_at_prompt("active", now - 10.0, now) is True
    # 2s idle < 5s.
    assert S._is_active_at_prompt("active", now - 2.0, now) is False


def test_list_sessions_flags_active_at_prompt_after_threshold(tmp_path, monkeypatch):
    """jsonl mtime older than IDLE_AT_PROMPT_SECONDS → active_at_prompt=True."""
    from bot_squad_worker import sessions as S
    cfg, jsonl = _setup_activity_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "IDLE_AT_PROMPT_SECONDS", 60.0)
    stale = time.time() - 120.0  # 2 minutes idle
    os.utime(jsonl, (stale, stale))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["status"] == "active"  # raw status preserved
    assert rows[0]["active_at_prompt"] is True


def test_list_sessions_not_at_prompt_when_recent_activity(tmp_path, monkeypatch):
    """Fresh jsonl mtime → active_at_prompt=False (still crunching)."""
    from bot_squad_worker import sessions as S
    cfg, jsonl = _setup_activity_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "IDLE_AT_PROMPT_SECONDS", 60.0)
    now = time.time()
    os.utime(jsonl, (now, now))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["active_at_prompt"] is False


def test_list_sessions_suspended_row_has_active_at_prompt_false(tmp_path, monkeypatch):
    """Suspended rows always carry active_at_prompt=False."""
    import bot_squad_worker.sessions as S
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-w-p1.md", {
        "sid": "S-testuser-w-p1",
        "status": "suspended",
        "window": "w",
        "cwd": str(repo),
        "claude_uuid": "abc",
    })

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["active_at_prompt"] is False


def test_list_sessions_running_when_jsonl_fresh(tmp_path, monkeypatch):
    """Fresh jsonl mtime → activity='running' on the returned row."""
    from bot_squad_worker import sessions as S
    cfg, jsonl = _setup_activity_probe(tmp_path, monkeypatch)
    # touch — already fresh from the write above; just be explicit.
    now = time.time()
    os.utime(jsonl, (now, now))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["activity"] == "running"
    assert rows[0]["status"] == "active"  # back-compat raw status preserved
    assert rows[0]["activity_at"] is not None


def test_list_sessions_idle_when_jsonl_stale(tmp_path, monkeypatch):
    """jsonl mtime older than RUNNING_THRESHOLD_SEC → activity='idle'.

    DoD reproducer: synth a session whose jsonl mtime is >30s old → API
    returns idle. Touch the jsonl → next poll flips to running.
    """
    from bot_squad_worker import sessions as S
    cfg, jsonl = _setup_activity_probe(tmp_path, monkeypatch)
    stale = time.time() - (S.RUNNING_THRESHOLD_SEC + 5.0)
    os.utime(jsonl, (stale, stale))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["activity"] == "idle"
    assert rows[0]["status"] == "active"

    # Touch the jsonl → next poll should flip to running.
    now = time.time()
    os.utime(jsonl, (now, now))
    rows2 = list_sessions(cfg, "test-project")
    assert rows2[0]["activity"] == "running"


def test_list_sessions_suspended_md_has_activity_suspended(tmp_path, monkeypatch):
    """No live pane → activity is `suspended` regardless of md status."""
    import bot_squad_worker.sessions as S
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    meta_path = sessions_dir / "S-testuser-mywin-p7.md"
    _write_session_metadata(meta_path, {
        "sid": "S-testuser-mywin-p7",
        "status": "active",   # zombie md — pane is gone
        "window": "mywin",
        "cwd": str(repo),
        "claude_uuid": "ghost-uuid",
    })

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["activity"] == "suspended"
    assert rows[0]["activity_at"] is None


def test_peer_heartbeat_at_returns_mtime(tmp_path):
    from bot_squad_worker.sessions import _peer_heartbeat_at
    hb = tmp_path / "test-project" / "_chat" / "heartbeat-S-x-y-p1"
    hb.parent.mkdir(parents=True)
    hb.write_text("")
    expected = hb.stat().st_mtime
    got = _peer_heartbeat_at(tmp_path, "test-project", "S-x-y-p1")
    assert got == expected


def test_peer_heartbeat_at_none_when_missing(tmp_path):
    from bot_squad_worker.sessions import _peer_heartbeat_at
    assert _peer_heartbeat_at(tmp_path, "test-project", "S-nope-w-p0") is None


def test_list_sessions_heartbeat_keeps_long_idle_pane_fresh(tmp_path, monkeypatch):
    """T-0037: armed peer_inbox_wait counts as activity even if jsonl is stale.

    Reproducer: TL with a stale jsonl (last claude turn 5 minutes ago) but
    a fresh heartbeat-<sid> (inbox_wait re-armed seconds ago). Without the
    heartbeat fold, activity_at == old jsonl mtime → 'idle' label + a
    misleading "5m ago" in the Last activity column. With the fold,
    activity_at == fresh heartbeat → 'running' + a truthful "seconds ago".
    """
    from bot_squad_worker import sessions as S
    cfg, jsonl = _setup_activity_probe(tmp_path, monkeypatch)
    now = time.time()
    stale = now - 300.0
    os.utime(jsonl, (stale, stale))
    # Heartbeat written by intersession.inbox_wait/read.
    hb = cfg.data_dir / "test-project" / "_chat" / "heartbeat-S-testuser-mywin-p9"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("")
    os.utime(hb, (now, now))

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["activity"] == "running"
    assert rows[0]["activity_at"] is not None
    # Heartbeat (now) wins over jsonl (5 min ago).
    assert abs(rows[0]["activity_at"] - now) < 1.0


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
# pause (Ctrl-C only; pane stays open)
# ---------------------------------------------------------------------------

def test_pause_sends_ctrl_c_and_marks_paused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%2|mywin|1234|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    result = pause(cfg, "test-project", "S-testuser-mywin-p2")

    assert result == {"ok": True, "paused": True}

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    meta_file = sessions_dir / "S-testuser-mywin-p2.md"
    assert meta_file.exists()
    meta = _read_session_metadata(meta_file)
    assert meta["status"] == "paused"

    # Only Ctrl-C — no /exit, no kill-pane.
    send_key_calls = [c for c in calls if "send-keys" in c]
    assert len(send_key_calls) == 1
    assert "C-c" in send_key_calls[0]
    assert not any("kill-pane" in c for c in calls)


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
# suspend (close pane, preserve registry for resurrect)
# ---------------------------------------------------------------------------

def test_suspend_closes_pane_and_marks_suspended(tmp_path, monkeypatch):
    from bot_squad_worker.sessions import suspend

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "list-panes" in args:
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

    result = suspend(cfg, "test-project", "S-testuser-mywin-p2")

    assert result == {"ok": True, "suspended": True}

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    meta_file = sessions_dir / "S-testuser-mywin-p2.md"
    assert meta_file.exists()
    meta = _read_session_metadata(meta_file)
    assert meta["status"] == "suspended"

    send_key_calls = [c for c in calls if "send-keys" in c]
    assert any("C-c" in c for c in send_key_calls)
    assert any("/exit" in c for c in send_key_calls)


def test_suspend_no_live_pane_just_normalises_md(tmp_path, monkeypatch):
    """Suspending a zombie (no live pane) preserves md and marks suspended."""
    from bot_squad_worker.sessions import suspend

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    meta_path = sessions_dir / "S-testuser-zombie-p7.md"
    _write_session_metadata(meta_path, {
        "sid": "S-testuser-zombie-p7",
        "status": "active",
        "window": "zombie",
        "cwd": str(repo),
        "claude_uuid": "zombie-uuid",
        "task_id": "~",
        "started_at": "2026-05-10T12:00:00Z",
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

    result = suspend(cfg, "test-project", "S-testuser-zombie-p7")
    assert result["ok"] is True
    assert result["suspended"] is True
    assert result["already_gone"] is True

    meta = _read_session_metadata(meta_path)
    assert meta["status"] == "suspended"
    assert meta["claude_uuid"] == "zombie-uuid"


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


def test_spawn_with_task_and_initiative_stamps_task_md(tmp_path, monkeypatch):
    """T-0038: spawn(task_id=..., initiative=...) writes the initiative
    into the task md frontmatter when absent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    task_md = backlog / "T-0007-foo.md"
    task_md.write_text(
        "---\nid: T-0007\ntitle: Foo\nstatus: open\n---\n\nbody\n"
    )

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%2|w|11|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w",
          task_id="T-0007", initiative="multi-server-installation-process.md")

    txt = task_md.read_text()
    assert "initiative: multi-server-installation-process.md" in txt


def test_spawn_does_not_clobber_existing_initiative(tmp_path, monkeypatch):
    """T-0038: existing-wins. If the task already has an initiative, don't
    overwrite it on spawn."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    task_md = backlog / "T-0008-foo.md"
    task_md.write_text(
        "---\nid: T-0008\ntitle: Foo\nstatus: open\n"
        "initiative: original.md\n---\n\nbody\n"
    )

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%3|w|11|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w",
          task_id="T-0008", initiative="different.md")

    txt = task_md.read_text()
    assert "initiative: original.md" in txt
    assert "initiative: different.md" not in txt


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
        if "capture-pane" in args:
            # Composer-ready marker is present from the first poll.
            return subprocess.CompletedProcess(args, 0, "❯ \n", "")
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


def test_spawn_waits_for_composer_before_sending_initial_prompt(tmp_path, monkeypatch):
    """T-0126: spawn() must poll capture-pane for the `❯` composer rune
    BEFORE typing initial_prompt; otherwise send-keys lands on the bash
    prompt or mid-claude-init and the text/Enter is dropped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    call_log: list[str] = []  # ordered tags: "capture-not-ready", "capture-ready", "send-text", "send-enter"
    capture_calls = [0]

    def fake_run(args, **kwargs):
        if "capture-pane" in args:
            capture_calls[0] += 1
            # Composer is not ready for the first two polls (still bash /
            # claude bootstrapping), then `❯` appears.
            if capture_calls[0] < 3:
                call_log.append("capture-not-ready")
                return subprocess.CompletedProcess(args, 0, "bash-5.2$\n", "")
            call_log.append("capture-ready")
            return subprocess.CompletedProcess(args, 0, "❯ \n", "")
        if "send-keys" in args:
            # Distinguish the prompt text from the trailing Enter.
            if args[-1] == "Enter":
                call_log.append("send-enter")
            else:
                call_log.append("send-text")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%11|w|1234|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = spawn(cfg, "test-project", "w", initial_prompt="go")
    assert result["ok"] is True

    # send-keys for the prompt text + Enter must come strictly AFTER the
    # first ready capture, and the not-ready captures must come first.
    ready_idx = call_log.index("capture-ready")
    text_idx = call_log.index("send-text")
    enter_idx = call_log.index("send-enter")
    assert ready_idx < text_idx < enter_idx, f"unexpected order: {call_log}"
    # And spawn() must actually have polled — at least one not-ready capture.
    assert "capture-not-ready" in call_log, f"never polled before ready: {call_log}"


def test_spawn_raises_when_composer_never_ready(tmp_path, monkeypatch):
    """T-0126: if capture-pane never shows the composer rune within the
    timeout budget, spawn() must raise so the caller knows the prompt
    didn't land — rather than silently typing into a dead/hung pane."""
    from bot_squad_worker.actions import ActionError

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    send_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        if "capture-pane" in args:
            # Composer never renders the ❯ rune (claude crash, hang, etc.).
            return subprocess.CompletedProcess(args, 0, "still booting...\n", "")
        if "send-keys" in args:
            send_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%12|w|5678|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    # Shrink the budget so the test bounds runtime even if time.sleep was
    # not stubbed in some future refactor.
    monkeypatch.setattr(S, "_COMPOSER_READY_TIMEOUT_SEC", 0.6)
    monkeypatch.setattr(S, "_COMPOSER_READY_POLL_INTERVAL_SEC", 0.1)

    with pytest.raises(ActionError, match="composer never showed"):
        spawn(cfg, "test-project", "w", initial_prompt="go")

    # No send-keys should have been issued — we must not type into a pane
    # that never indicated readiness.
    assert not send_calls, f"expected zero send-keys; got: {send_calls}"


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


# ---------------------------------------------------------------------------
# T-0080: owner field plumbing
# ---------------------------------------------------------------------------

def test_spawn_passes_owner_env_to_shell(tmp_path, monkeypatch):
    """spawn(owner=X) bakes BOT_SQUAD_OWNER=X into the bash -lc shell cmd
    so the SessionStart hook can stamp owner into the SessionMd."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    captured_shell_cmd: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            # bash -lc <cmd> — capture the cmd string.
            try:
                i = args.index("-lc")
                captured_shell_cmd.append(args[i + 1])
            except (ValueError, IndexError):
                pass
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%4|w|123|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w", owner="aqice")

    assert captured_shell_cmd, "expected at least one new-window call"
    assert "BOT_SQUAD_OWNER=aqice" in captured_shell_cmd[0]


def test_spawn_rejects_invalid_owner(tmp_path, monkeypatch):
    """Owner must be alnum/_./-; reject shell-meaningful chars."""
    from bot_squad_worker.actions import ActionError
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S
    # Stub out tmux: ensure-session probe + list-panes. Validation fires
    # before tmux new-window, so we only need _run to return ok for the
    # ensure-session path.
    monkeypatch.setattr(S, "_run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    with pytest.raises(ActionError, match="invalid owner"):
        spawn(cfg, "test-project", "w", owner="bob; rm -rf /")


def test_spawn_without_owner_omits_env_var(tmp_path, monkeypatch):
    """Legacy callers (no owner kwarg) must not get an empty BOT_SQUAD_OWNER=."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    captured: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            try:
                i = args.index("-lc")
                captured.append(args[i + 1])
            except (ValueError, IndexError):
                pass
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%5|w|123|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w")
    assert captured
    assert "BOT_SQUAD_OWNER" not in captured[0]


def test_list_sessions_emits_owner_from_md(tmp_path, monkeypatch):
    """list_sessions surfaces SessionMd `owner:` for both active + suspended."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # Suspended (md-only) row with owner stamped.
    _write_session_metadata(sessions_dir / "S-u-sus-p1.md", {
        "sid": "S-u-sus-p1",
        "status": "suspended",
        "window": "sus",
        "cwd": str(repo),
        "claude_uuid": "abc",
        "task_id": "~",
        "started_at": "2026-05-16T10:00:00Z",
        "suspended_at": "2026-05-16T11:00:00Z",
        "owner": "aqice",
    })
    # Active row — its md exists too, owner stamped.
    _write_session_metadata(sessions_dir / "S-u-act-p2.md", {
        "sid": "S-u-act-p2",
        "status": "active",
        "window": "act",
        "cwd": str(repo),
        "claude_uuid": "def",
        "task_id": "~",
        "started_at": "2026-05-16T10:30:00Z",
        "owner": "alexey",
    })

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            # Only the "active" row has a live pane.
            return subprocess.CompletedProcess(
                args, 0, f"%2|act|111|{repo}|claude\n", "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["S-u-act-p2"]["owner"] == "alexey"
    assert by_sid["S-u-sus-p1"]["owner"] == "aqice"


def test_suspend_preserves_owner_field(tmp_path, monkeypatch):
    """suspend() rewrites the md but must keep owner stamped."""
    from bot_squad_worker.sessions import suspend
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-u-w-p3.md", {
        "sid": "S-u-w-p3",
        "status": "active",
        "window": "w",
        "cwd": str(repo),
        "claude_uuid": "uuid-1",
        "task_id": "~",
        "started_at": "2026-05-16T10:00:00Z",
        "owner": "aqice",
    })

    pane_calls = [0]

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            pane_calls[0] += 1
            # First call: pane live. After kill: gone.
            if pane_calls[0] == 1:
                return subprocess.CompletedProcess(args, 0, f"%3|w|11|{repo}|claude\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    suspend(cfg, "test-project", "S-u-w-p3")
    meta = _read_session_metadata(sessions_dir / "S-u-w-p3.md")
    assert meta["owner"] == "aqice"


# ---------------------------------------------------------------------------
# T-0105: session_history append on bind / rotate
# ---------------------------------------------------------------------------

def _read_task_session_history(task_md: Path) -> list[str]:
    """Parse the inline `session_history:` line out of a task md."""
    text = task_md.read_text()
    import re as _re
    m = _re.search(r"^session_history:\s*\[(.*?)\]\s*$", text, _re.M)
    if not m:
        return []
    inner = m.group(1).strip()
    if not inner:
        return []
    return [x.strip() for x in inner.split(",") if x.strip()]


def test_session_history_first_bind_via_spawn(tmp_path, monkeypatch):
    """spawn(task_id=X) stamps the new SID into X's session_history."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    task_md = backlog / "T-0090-hist.md"
    task_md.write_text("---\nid: T-0090\ntitle: H\nstatus: open\n---\n\nbody\n")

    def fake_run(args, **kw):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%2|w|11|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "alice")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w", task_id="T-0090")
    hist = _read_task_session_history(task_md)
    assert hist == ["S-alice-w-p2"]


def test_session_history_dedup_on_repeated_spawn(tmp_path, monkeypatch):
    """If somehow the same SID stamps twice, the helper de-dupes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    task_md = backlog / "T-0091-dup.md"
    task_md.write_text(
        "---\nid: T-0091\ntitle: D\nstatus: open\n"
        "session_history: [S-alice-w-p2]\n---\n\nbody\n"
    )

    def fake_run(args, **kw):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%2|w|11|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "alice")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w", task_id="T-0091")
    hist = _read_task_session_history(task_md)
    assert hist == ["S-alice-w-p2"]  # no duplicate


def test_session_history_appended_on_bind_task(tmp_path, monkeypatch):
    """bind_task() stamps the binding SID into the extras-task's history."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    primary_md = backlog / "T-0092-primary.md"
    extra_md = backlog / "T-0093-extra.md"
    primary_md.write_text("---\nid: T-0092\ntitle: P\nstatus: open\n---\n\nbody\n")
    extra_md.write_text("---\nid: T-0093\ntitle: E\nstatus: open\n---\n\nbody\n")

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-alice-w-p2.md", {
        "sid": "S-alice-w-p2",
        "status": "active",
        "window": "w",
        "cwd": str(repo),
        "claude_uuid": "u-1",
        "task_id": "T-0092",
        "initiative": "~",
    })

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "alice")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    from bot_squad_worker.sessions import bind_task
    bind_task(cfg, "test-project", "S-alice-w-p2", "T-0093")

    assert _read_task_session_history(extra_md) == ["S-alice-w-p2"]


def test_session_history_rotates_on_resume(tmp_path, monkeypatch):
    """resume() rotation appends the new SID; old SID stays for forensics."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    task_md = backlog / "T-0094-rot.md"
    task_md.write_text(
        "---\nid: T-0094\ntitle: R\nstatus: open\n"
        "session_history: [S-alice-w-p2]\n---\n\nbody\n"
    )

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-alice-w-p2.md", {
        "sid": "S-alice-w-p2",
        "status": "suspended",
        "window": "w",
        "cwd": str(repo),
        "claude_uuid": "u-1",
        "task_id": "T-0094",
    })

    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if "list-panes" in args:
            n = sum(1 for c in calls if "list-panes" in c)
            if n <= 1:
                return subprocess.CompletedProcess(args, 0, "", "")
            # new pane after resume
            return subprocess.CompletedProcess(args, 0, f"%9|w|999|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "alice")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = resume(cfg, "test-project", "S-alice-w-p2")
    new_sid = result["sid"]
    hist = _read_task_session_history(task_md)
    # Both old and new SIDs present; old first, new last.
    assert hist[0] == "S-alice-w-p2"
    assert hist[-1] == new_sid
    assert len(hist) == 2


def test_session_history_unbind_rebind_no_dup(tmp_path, monkeypatch):
    """Unbinding + rebinding the same SID keeps history as [sid] — no dup,
    no reorder. (per T-0105 DoD)"""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    primary_md = backlog / "T-0095-prim.md"
    extra_md = backlog / "T-0096-ext.md"
    primary_md.write_text("---\nid: T-0095\ntitle: P\nstatus: open\n---\n\nbody\n")
    extra_md.write_text("---\nid: T-0096\ntitle: E\nstatus: open\n---\n\nbody\n")

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-alice-w-p2.md", {
        "sid": "S-alice-w-p2",
        "status": "active",
        "window": "w",
        "cwd": str(repo),
        "claude_uuid": "u-1",
        "task_id": "T-0095",
        "initiative": "~",
    })

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "alice")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    from bot_squad_worker.sessions import bind_task, unbind_task
    bind_task(cfg, "test-project", "S-alice-w-p2", "T-0096")
    assert _read_task_session_history(extra_md) == ["S-alice-w-p2"]

    # Unbind does NOT touch session_history (it's append-only/forensic).
    unbind_task(cfg, "test-project", "S-alice-w-p2", "T-0096")
    assert _read_task_session_history(extra_md) == ["S-alice-w-p2"]

    # Re-bind same SID — still no dup.
    bind_task(cfg, "test-project", "S-alice-w-p2", "T-0096")
    assert _read_task_session_history(extra_md) == ["S-alice-w-p2"]


# ---------------------------------------------------------------------------
# T-0103: break-pane uses TL pane's tmux session, not hardcoded $slug
# ---------------------------------------------------------------------------

def test_session_start_hook_uses_tl_pane_tmux_session_for_break_pane():
    """Regression guard: scripts/hooks/session_start.sh resolves
    target_session via `tmux display-message -p -t "$TMUX_PANE" '#S'`
    rather than hardcoding $slug. Teammates land in their TL's tmux
    session (e.g. bot-squad-multi_server), not always the project main
    session. (T-0103)

    Skipped when the hook isn't reachable from the test cwd (e.g. when
    only the worker/ dir is mounted into the test container — the host
    workflow runs this from the repo root and exercises it fully).
    """
    import pathlib as _pl
    here = _pl.Path(__file__).resolve()
    hook = None
    for ancestor in here.parents:
        cand = ancestor / "scripts" / "hooks" / "session_start.sh"
        if cand.is_file():
            hook = cand
            break
    if hook is None:
        pytest.skip("session_start.sh not reachable from test cwd "
                    "(repo root not mounted)")
    text = hook.read_text()
    # The primary resolution must query tmux for the TL pane's session
    # (the fallback `target_session="$slug"` inside the empty-check `if`
    # is fine — that only fires when the tmux query returned nothing).
    assert 'target_session="$(tmux display-message' in text, \
        "T-0103 regression: break-pane target reverted to hardcoded $slug"
    assert "'#S'" in text, \
        "T-0103: target_session must come from `tmux display-message ... #S`"
