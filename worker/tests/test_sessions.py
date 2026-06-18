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
    set_drift_paused,
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
    meta = {"sid": "S-x", "status": "paused", "extra_task_ids": ["T-0001"], "cwd": "/tmp"}
    _write_session_metadata(path, meta)
    result = _read_session_metadata(path)
    assert result["sid"] == "S-x"
    assert result["status"] == "paused"
    assert result["extra_task_ids"] == ["T-0001"]


def test_read_session_metadata_missing_file(tmp_path):
    result = _read_session_metadata(tmp_path / "nonexistent.md")
    assert result is None


def test_read_session_metadata_empty_list(tmp_path):
    path = tmp_path / "S-test.md"
    _write_session_metadata(path, {"extra_task_ids": []})
    result = _read_session_metadata(path)
    assert result["extra_task_ids"] == []


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


def test_tmux_session_name_no_initiative_returns_slug():
    """T-0001: without an initiative, panes live in the main `<slug>` session."""
    from bot_squad_worker.sessions import _tmux_session_name
    assert _tmux_session_name("bot-squad", None) == "bot-squad"
    assert _tmux_session_name("bot-squad", "") == "bot-squad"


def test_tmux_session_name_with_initiative_appends_stem():
    """T-0001: with an initiative, route into sibling `<slug>-<stem>` session."""
    from bot_squad_worker.sessions import _tmux_session_name
    assert _tmux_session_name(
        "bot-squad", "multi-server-installation-process.md"
    ) == "bot-squad-multi-server-installation-process"
    # Stem strips the .md extension only — multi-dot names keep the rest.
    assert _tmux_session_name("p", "a.b.md") == "p-a.b"


# ---------------------------------------------------------------------------
# T-0200: tmux session name sanitisation (the literal-quote escape bug)
# ---------------------------------------------------------------------------

def test_tmux_session_name_strips_shell_unsafe_chars():
    """T-0200: a quoted initiative must not leak a literal quote into the tmux
    session name. The live incident produced `bot-squad-"prod-support` from an
    initiative value of `"prod-support.md`. The derived session name is now
    sanitised to [A-Za-z0-9._-] so no quote / space / shell metachar survives.
    """
    from bot_squad_worker.sessions import _tmux_session_name
    assert _tmux_session_name("bot-squad", '"prod-support.md') == "bot-squad-prod-support"
    assert _tmux_session_name("bot-squad", 'prod support.md') == "bot-squad-prodsupport"
    assert _tmux_session_name("bot-squad", "feat$(rm).md") == "bot-squad-featrm"
    # A stem that sanitises to empty falls back to the main session.
    assert _tmux_session_name("bot-squad", '".md') == "bot-squad"


# ---------------------------------------------------------------------------
# T-0200: gc_tmux_sessions — reap idle empty per-initiative/per-team sessions
# ---------------------------------------------------------------------------

def _ls_line(name: str, activity: int) -> str:
    return f"{name}|{activity}"


def test_gc_tmux_sessions_reaps_empty_idle_sibling(tmp_path, monkeypatch):
    """An `<slug>-*` session with 0 live claude panes, idle past the grace, is
    killed. The on-disk team md is NOT this function's concern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S

    killed: list[str] = []

    # Empty sibling holds only an _init bash pane rooted in the repo.
    panes = [
        PaneInfo(pane_id="%1", window="_init", pid="1", cwd=str(repo),
                 command="bash", session="test-project-ghost"),
    ]
    monkeypatch.setattr(S, "list_panes", lambda: panes)

    def fake_run(args, **kwargs):
        if "list-sessions" in args:
            return subprocess.CompletedProcess(
                args, 0, _ls_line("test-project-ghost", 1000) + "\n", "")
        if "kill-session" in args:
            killed.append(args[args.index("-t") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S.time, "time", lambda: 1000 + 7200)  # 2h later

    res = S.gc_tmux_sessions(cfg, "test-project")
    assert res["ok"] is True
    assert res["reaped"] == ["test-project-ghost"]
    assert killed == ["test-project-ghost"]


def test_gc_tmux_sessions_spares_staffed_sibling(tmp_path, monkeypatch):
    """A sibling session with a live claude pane is NEVER reaped, even idle."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S

    killed: list[str] = []
    panes = [
        PaneInfo(pane_id="%1", window="_init", pid="1", cwd=str(repo),
                 command="bash", session="test-project-feat"),
        PaneInfo(pane_id="%2", window="dev", pid="2", cwd=str(repo),
                 command="claude", session="test-project-feat"),
    ]
    monkeypatch.setattr(S, "list_panes", lambda: panes)

    def fake_run(args, **kwargs):
        if "list-sessions" in args:
            return subprocess.CompletedProcess(
                args, 0, _ls_line("test-project-feat", 1) + "\n", "")
        if "kill-session" in args:
            killed.append(args[args.index("-t") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S.time, "time", lambda: 999999)

    res = S.gc_tmux_sessions(cfg, "test-project")
    assert res["reaped"] == []
    assert killed == []


def test_gc_tmux_sessions_never_reaps_main_or_other_projects(tmp_path, monkeypatch):
    """The bare `<slug>` main session and other projects' sessions are untouched,
    even when empty/idle."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S

    killed: list[str] = []
    panes = [
        # main session, empty (only _init) but must be spared
        PaneInfo(pane_id="%1", window="_init", pid="1", cwd=str(repo),
                 command="bash", session="test-project"),
        # a different project's session, coincidentally prefix-ish
        PaneInfo(pane_id="%2", window="_init", pid="2", cwd="/somewhere/else",
                 command="bash", session="other-project-x"),
    ]
    monkeypatch.setattr(S, "list_panes", lambda: panes)

    def fake_run(args, **kwargs):
        if "list-sessions" in args:
            return subprocess.CompletedProcess(
                args, 0,
                _ls_line("test-project", 1) + "\n" + _ls_line("other-project-x", 1) + "\n",
                "")
        if "kill-session" in args:
            killed.append(args[args.index("-t") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S.time, "time", lambda: 999999)

    res = S.gc_tmux_sessions(cfg, "test-project")
    assert res["reaped"] == []
    assert killed == []


def test_gc_tmux_sessions_spares_recent_empty_sibling(tmp_path, monkeypatch):
    """A freshly-created empty sibling (mid-spawn) is within the idle grace and
    must survive — this is what prevents the GC racing a just-spawned team."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S

    killed: list[str] = []
    panes = [
        PaneInfo(pane_id="%1", window="_init", pid="1", cwd=str(repo),
                 command="bash", session="test-project-fresh"),
    ]
    monkeypatch.setattr(S, "list_panes", lambda: panes)

    def fake_run(args, **kwargs):
        if "list-sessions" in args:
            return subprocess.CompletedProcess(
                args, 0, _ls_line("test-project-fresh", 1000) + "\n", "")
        if "kill-session" in args:
            killed.append(args[args.index("-t") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    # Only 5 minutes later — inside the default 1h grace.
    monkeypatch.setattr(S.time, "time", lambda: 1000 + 300)

    res = S.gc_tmux_sessions(cfg, "test-project")
    assert res["reaped"] == []
    assert killed == []


def test_gc_tmux_sessions_spares_empty_session_rooted_elsewhere(tmp_path, monkeypatch):
    """Safety gate: a `<slug>-`prefixed session whose panes are NOT rooted in
    the project repo is spared (it isn't ours)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S

    killed: list[str] = []
    panes = [
        PaneInfo(pane_id="%1", window="_init", pid="1", cwd="/not/the/repo",
                 command="bash", session="test-project-alien"),
    ]
    monkeypatch.setattr(S, "list_panes", lambda: panes)

    def fake_run(args, **kwargs):
        if "list-sessions" in args:
            return subprocess.CompletedProcess(
                args, 0, _ls_line("test-project-alien", 1) + "\n", "")
        if "kill-session" in args:
            killed.append(args[args.index("-t") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S.time, "time", lambda: 999999)

    res = S.gc_tmux_sessions(cfg, "test-project")
    assert res["reaped"] == []
    assert killed == []


def test_spawn_with_initiative_targets_sibling_tmux_session(tmp_path, monkeypatch):
    """T-0001: spawn(initiative=...) issues tmux new-window into
    `<slug>-<initiative-stem>:`, not the main `<slug>:` session.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    new_window_targets: list[str] = []
    has_session_targets: list[str] = []
    new_session_targets: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            i = args.index("-t")
            new_window_targets.append(args[i + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if "has-session" in args:
            i = args.index("-t")
            has_session_targets.append(args[i + 1])
            return subprocess.CompletedProcess(args, 1, "", "")  # not present
        if "new-session" in args:
            i = args.index("-s")
            new_session_targets.append(args[i + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%4|tl-init|9|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "tl-init",
          initiative="multi-server-installation-process.md")

    sibling = "test-project-multi-server-installation-process"
    assert new_window_targets == [f"{sibling}:"], \
        f"expected sibling session target, got {new_window_targets!r}"
    assert sibling in has_session_targets, \
        f"expected has-session probe on sibling, got {has_session_targets!r}"
    assert new_session_targets == [sibling], \
        f"expected new-session for sibling, got {new_session_targets!r}"


def test_spawn_without_initiative_uses_main_project_session(tmp_path, monkeypatch):
    """T-0001: no initiative → legacy behaviour, panes go to `<slug>:`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    new_window_targets: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            i = args.index("-t")
            new_window_targets.append(args[i + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 0, "", "")  # already exists
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%5|w|9|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w")
    assert new_window_targets == ["test-project:"], \
        f"expected main project session, got {new_window_targets!r}"


def test_resume_routes_into_initiative_sibling_session(tmp_path, monkeypatch):
    """T-0001: resume() reads initiative from session md and resurrects into
    the same `<slug>-<initiative-stem>` session the spawn put it in.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    md = sessions_dir / "S-u-tl-init-p11.md"
    _write_session_metadata(md, {
        "sid": "S-u-tl-init-p11",
        "status": "suspended",
        "window": "tl-init",
        "cwd": str(repo),
        "claude_uuid": "abc-123",
        "task_id": "~",
        "initiative": "multi-server-installation-process.md",
        "started_at": "2026-05-20T00:00:00Z",
        "suspended_at": "2026-05-20T01:00:00Z",
    })

    new_window_targets: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            i = args.index("-t")
            new_window_targets.append(args[i + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 1, "", "")  # not present
        if "new-session" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            # No live pane for the old SID before resume; after new-window,
            # one fresh pane on the same window name.
            if any("new-window" in c for c in []):
                return subprocess.CompletedProcess(args, 0, "", "")
            # The check happens twice: pre and post; the post should show the
            # spawned pane. We return it unconditionally — the test only cares
            # about the new-window target.
            return subprocess.CompletedProcess(args, 0, f"%9|tl-init|22|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    resume(cfg, "test-project", "S-u-tl-init-p11")
    assert new_window_targets == [
        "test-project-multi-server-installation-process:"
    ], f"expected sibling session, got {new_window_targets!r}"


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


def test_strip_surrounding_quotes():
    """T-0208: strip ONE layer of matching surrounding single/double quotes."""
    import bot_squad_worker.sessions as S
    assert S._strip_surrounding_quotes('"x.md"') == "x.md"
    assert S._strip_surrounding_quotes("'x.md'") == "x.md"
    assert S._strip_surrounding_quotes("x.md") == "x.md"          # already bare
    assert S._strip_surrounding_quotes('"x.md') == '"x.md'        # unmatched
    assert S._strip_surrounding_quotes('x.md"') == 'x.md"'        # unmatched
    assert S._strip_surrounding_quotes('""') == ""               # empty quoted
    assert S._strip_surrounding_quotes('"') == '"'               # single char
    assert S._strip_surrounding_quotes("") == ""


def test_spawn_accepts_quoted_initiative(tmp_path, monkeypatch):
    """T-0208: a quoted frontmatter scalar (`initiative: "x.md"`) reaching
    spawn() must NOT trip the `.endswith(".md")` validator. The worker
    normalizes surrounding quotes before validating, and the unquoted value
    is what gets stamped onto the task md + baked into the shell env."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    task_md = backlog / "T-0009-foo.md"
    task_md.write_text("---\nid: T-0009\ntitle: Foo\nstatus: open\n---\n\nbody\n")

    captured_shell_cmd: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            try:
                i = args.index("-lc")
                captured_shell_cmd.append(args[i + 1])
            except (ValueError, IndexError):
                pass
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%9|w|11|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    # Does not raise ActionError("invalid initiative name ...").
    spawn(cfg, "test-project", "w",
          task_id="T-0009", initiative='"operator-ux-and-session-mgmt.md"')

    # Stamped onto the task md WITHOUT the literal quotes.
    txt = task_md.read_text()
    assert "initiative: operator-ux-and-session-mgmt.md" in txt
    assert 'initiative: "operator' not in txt
    # Baked into the shell env without literal quotes around the basename.
    assert captured_shell_cmd, "expected a new-window call"
    assert "BOT_SQUAD_INITIATIVE=operator-ux-and-session-mgmt.md" in captured_shell_cmd[0]


def test_spawn_sends_initial_prompt(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    key_calls = []
    new_window_called = [False]

    def fake_run(args, **kwargs):
        if "send-keys" in args or "set-buffer" in args or "paste-buffer" in args:
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

    # T-0144: prompt delivery is via tmux set-buffer + paste-buffer (bracketed
    # paste), the operator's proven-reliable primitive — not a send-keys literal.
    assert any("set-buffer" in c and "hello world" in str(c) for c in key_calls), \
        f"expected hello world via set-buffer; got: {key_calls}"
    assert any("paste-buffer" in c for c in key_calls), \
        f"expected a paste-buffer delivery; got: {key_calls}"


def test_spawn_waits_for_composer_before_sending_initial_prompt(tmp_path, monkeypatch):
    """T-0126: spawn() must poll capture-pane for the `❯` composer rune
    BEFORE typing initial_prompt; otherwise send-keys lands on the bash
    prompt or mid-claude-init and the text/Enter is dropped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    call_log: list[str] = []  # ordered tags: "capture-not-ready", "capture-ready", "paste-text", "send-enter"
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
        if "set-buffer" in args:
            # The prompt text is loaded into the paste buffer.
            call_log.append("paste-text")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "send-keys" in args:
            # The only send-keys in the delivery path is the trailing Enter.
            if args[-1] == "Enter":
                call_log.append("send-enter")
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

    # The paste (set-buffer) + Enter must come strictly AFTER the first ready
    # capture, and the not-ready captures must come first.
    ready_idx = call_log.index("capture-ready")
    text_idx = call_log.index("paste-text")
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


def test_resume_delivers_initial_prompt(tmp_path, monkeypatch):
    """T-0150: resurrecting a suspended session with `initial_prompt` delivers
    the delta brief into the resumed composer via the same composer-ready poll
    + paste-buffer path spawn uses (so a resumed expert gets its brief)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-expert-p7.md", {
        "sid": "S-testuser-expert-p7",
        "status": "suspended",
        "window": "expert",
        "cwd": str(repo),
        "claude_uuid": "expert-uuid-9",
        "task_id": "T-0001",
        "suspended_at": "2026-05-10T12:00:00Z",
    })

    key_calls: list[list] = []
    new_window_called = [False]

    def fake_run(args, **kwargs):
        if "set-buffer" in args or "paste-buffer" in args or "send-keys" in args:
            key_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if "capture-pane" in args:
            return subprocess.CompletedProcess(args, 0, "❯ \n", "")
        if "new-window" in args:
            new_window_called[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            if new_window_called[0]:
                return subprocess.CompletedProcess(args, 0, f"%7|expert|4242|{repo}|claude\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = resume(cfg, "test-project", "S-testuser-expert-p7",
                    initial_prompt="delta: now do T-0002 building on T-0001")
    assert result["ok"] is True
    # Delivered via set-buffer (bracketed paste), not a send-keys literal.
    assert any("set-buffer" in c and "delta:" in str(c) for c in key_calls), \
        f"expected delta brief via set-buffer; got: {key_calls}"
    assert any("paste-buffer" in c for c in key_calls), \
        f"expected a paste-buffer delivery; got: {key_calls}"


def test_resume_without_initial_prompt_delivers_nothing(tmp_path, monkeypatch):
    """T-0150: omitting initial_prompt must not paste anything (back-compat)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-expert-p8.md", {
        "sid": "S-testuser-expert-p8",
        "status": "suspended",
        "window": "expert",
        "cwd": str(repo),
        "claude_uuid": "expert-uuid-8",
        "task_id": "T-0001",
        "suspended_at": "2026-05-10T12:00:00Z",
    })

    paste_calls: list[list] = []
    new_window_called = [False]

    def fake_run(args, **kwargs):
        if "set-buffer" in args or "paste-buffer" in args:
            paste_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if "capture-pane" in args:
            return subprocess.CompletedProcess(args, 0, "❯ \n", "")
        if "new-window" in args:
            new_window_called[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            if new_window_called[0]:
                return subprocess.CompletedProcess(args, 0, f"%8|expert|4243|{repo}|claude\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    resume(cfg, "test-project", "S-testuser-expert-p8")
    assert paste_calls == [], f"no prompt expected, but pasted: {paste_calls}"


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


# ---------------------------------------------------------------------------
# T-0078: tmux_session field on SessionMd + list_sessions rows
# ---------------------------------------------------------------------------


def test_spawn_writes_tmux_session_to_session_md(tmp_path, monkeypatch):
    """T-0078: spawn() pre-stamps tmux_session on the SessionMd so the
    field is populated even before the SessionStart hook fires.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%5|w|9|{repo}|claude|test-project\n", "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = spawn(cfg, "test-project", "w")
    md_path = (
        cfg.data_dir / "test-project" / "sessions" / f"{result['sid']}.md"
    )
    meta = _read_session_metadata(md_path)
    assert meta is not None
    assert meta["tmux_session"] == "test-project"


def test_spawn_with_initiative_writes_sibling_tmux_session(tmp_path, monkeypatch):
    """T-0078: TLs spawned with an initiative get the sibling session name
    (`<slug>-<initiative-stem>`) stamped onto their SessionMd, matching the
    sibling tmux session the new-window targets.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    def fake_run(args, **kwargs):
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%9|tl|99|{repo}|claude|test-project-multi\n", "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = spawn(cfg, "test-project", "tl", initiative="multi.md")
    md_path = (
        cfg.data_dir / "test-project" / "sessions" / f"{result['sid']}.md"
    )
    meta = _read_session_metadata(md_path)
    assert meta is not None
    assert meta["tmux_session"] == "test-project-multi"


def test_list_sessions_emits_tmux_session(tmp_path, monkeypatch):
    """T-0078: list_sessions surfaces tmux_session for both active panes
    (from list-panes' session_name column) and suspended mds (from the
    SessionMd frontmatter)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-u-sus-p1.md", {
        "sid": "S-u-sus-p1",
        "status": "suspended",
        "window": "sus",
        "cwd": str(repo),
        "claude_uuid": "abc",
        "task_id": "~",
        "started_at": "2026-05-16T10:00:00Z",
        "suspended_at": "2026-05-16T11:00:00Z",
        "tmux_session": "test-project-multi",
    })

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0,
                f"%4|act|111|{repo}|claude|test-project-multi\n",
                "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["S-u-act-p4"]["tmux_session"] == "test-project-multi"
    assert by_sid["S-u-sus-p1"]["tmux_session"] == "test-project-multi"


def test_suspend_preserves_tmux_session_field(tmp_path, monkeypatch):
    """T-0078: suspend() must keep tmux_session stamped — the resume()
    that follows wants to know which tmux session the pane originally
    lived in.
    """
    from bot_squad_worker.sessions import suspend
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-u-tl-p3.md", {
        "sid": "S-u-tl-p3",
        "status": "active",
        "window": "tl",
        "cwd": str(repo),
        "claude_uuid": "uuid-1",
        "task_id": "~",
        "started_at": "2026-05-16T10:00:00Z",
        "tmux_session": "test-project-multi",
    })

    pane_calls = [0]

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            pane_calls[0] += 1
            if pane_calls[0] == 1:
                return subprocess.CompletedProcess(
                    args, 0, f"%3|tl|11|{repo}|claude|test-project-multi\n", "",
                )
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    suspend(cfg, "test-project", "S-u-tl-p3")
    meta = _read_session_metadata(sessions_dir / "S-u-tl-p3.md")
    assert meta["tmux_session"] == "test-project-multi"


def test_session_start_hook_writes_tmux_session_field():
    """T-0078: scripts/hooks/session_start.sh's inline python writes
    `tmux_session: ...` into the SessionMd frontmatter, sourcing the
    value from `tmux display-message -p -t $TMUX_PANE '#S'`.

    Skipped when the hook isn't reachable from the test cwd.
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
        pytest.skip("session_start.sh not reachable from test cwd")
    text = hook.read_text()
    # The bash block must read tmux_session from `tmux display-message #S`
    # and pass it through the env to the python md-write block.
    assert "tmux_session=\"$(tmux display-message" in text, \
        "T-0078: hook must source tmux_session from `tmux display-message`"
    assert "TMUX_SESSION=\"$tmux_session\"" in text, \
        "T-0078: hook must pass TMUX_SESSION env var to the python block"
    assert 'f"tmux_session: {tmux_session or \'~\'}\\n"' in text, \
        "T-0078: hook must write tmux_session into SessionMd frontmatter"


def _find_hook(name: str):
    import pathlib as _pl
    here = _pl.Path(__file__).resolve()
    for ancestor in here.parents:
        cand = ancestor / "scripts" / "hooks" / name
        if cand.is_file():
            return cand
    return None


def test_session_start_hook_branches_on_source():
    """T-0203: the hook must extract the SessionStart `source` and branch on it
    so resume/compact don't re-dump the full banner, and the heavy team_protocol
    + full role-doc cats are gone from the startup path (pointer-ized via
    `bsq brief`)."""
    hook = _find_hook("session_start.sh")
    if hook is None:
        pytest.skip("session_start.sh not reachable from test cwd")
    text = hook.read_text()
    assert 'get("source"' in text, "T-0203: hook must read the `source` field"
    assert ('[ "$HOOK_SOURCE" = "resume" ] || [ "$HOOK_SOURCE" = "compact" ]' in text), \
        "T-0203: hook must early-exit lean on resume/compact"
    # The post-compact reflexive AGENT_INSTRUCTIONS re-read must be suppressed.
    assert "reflexively re-read" in text, \
        "T-0203: compact/resume branch must tell the agent NOT to re-read AGENT_INSTRUCTIONS"
    # Heavy dumps removed from the banner — content now lives behind `bsq brief`.
    assert 'cat "$DATA/vision/team_protocol.md"' not in text, \
        "T-0203: team_protocol.md must no longer be cat'd into every session start"
    assert 'cat "$role_file"' not in text, \
        "T-0203: the full role doc must no longer be cat'd into every session start"
    assert "bsq brief" in text, "T-0203: hook must point at `bsq brief` for full orientation"


def test_session_start_hook_compact_output_is_lean():
    """T-0203 behavioral: exec the hook with source=compact against a minimal
    BOT_SQUAD and assert the printed context is a tiny re-anchor — no product /
    team-protocol / role-doc body — while startup still emits the orientation
    pointers + product."""
    import os, shutil, subprocess as _sp, tempfile
    real_hook = _find_hook("session_start.sh")
    derive = _find_hook("derive_role.sh")
    my_sid_sh = _find_hook("hook_my_sid.sh")
    if not (real_hook and derive):
        pytest.skip("hook scripts not reachable from test cwd")

    with tempfile.TemporaryDirectory() as td:
        bs = Path(td) / "bs"
        repo = Path(td) / "repo"
        repo.mkdir(parents=True)
        (bs / "scripts" / "hooks").mkdir(parents=True)
        (bs / "config").mkdir(parents=True)
        # Symlink the support scripts the hook sources by $BOT_SQUAD path.
        os.symlink(derive, bs / "scripts" / "hooks" / "derive_role.sh")
        if my_sid_sh:
            os.symlink(my_sid_sh, bs / "scripts" / "hooks" / "hook_my_sid.sh")
        (bs / "config" / "projects.toml").write_text(
            "[projects.test-project]\nrepo_path = \"%s\"\n" % repo)
        vision = bs / "data" / "test-project" / "vision"
        (vision / "roles").mkdir(parents=True)
        (vision / "product.md").write_text("# test-project\nPRODUCT_BODY_MARKER\n")
        (vision / "team_protocol.md").write_text("# protocol\nPROTOCOL_BODY_MARKER\n")
        (vision / "roles" / "dev.md").write_text("# dev\nROLEDOC_BODY_MARKER\n")
        (vision / "constitution.md").write_text("# constitution\n")
        (bs / "data" / "test-project" / "sessions").mkdir(parents=True)

        env = dict(os.environ)
        env["BOT_SQUAD"] = str(bs)
        for k in ("TMUX", "TMUX_PANE", "BOT_SQUAD_INITIATIVE", "BOT_SQUAD_OWNER"):
            env.pop(k, None)

        def run(source):
            return _sp.run(
                ["bash", str(real_hook)],
                input='{"source":"%s"}' % source,
                capture_output=True, text=True, cwd=str(repo), env=env,
            ).stdout

        compact = run("compact")
        assert "PROTOCOL_BODY_MARKER" not in compact
        assert "ROLEDOC_BODY_MARKER" not in compact
        assert "PRODUCT_BODY_MARKER" not in compact
        assert "reflexively re-read" in compact
        assert len(compact) < 1500, f"compact banner too big: {len(compact)} bytes"

        startup = run("startup")
        # Startup keeps the cheap product anchor + pointers, drops heavy dumps.
        assert "PRODUCT_BODY_MARKER" in startup
        assert "PROTOCOL_BODY_MARKER" not in startup
        assert "ROLEDOC_BODY_MARKER" not in startup
        assert "bsq brief" in startup
        assert len(startup) < len(
            "x" * 12780), "startup must be far smaller than the old ~12.8KB banner"


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


def test_bind_task_refuses_constant_team_session(tmp_path, monkeypatch):
    """T-0185: a constant-team / queue-consumer session must never be assigned a
    single-ticket binding (the p38 mis-bind that triggered false drift nags)."""
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker.sessions import bind_task

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    backlog = cfg.data_dir / "test-project" / "backlog"
    (backlog / "T-0176-sid-redesign.md").write_text(
        "---\nid: T-0176\ntitle: SID redesign\nstatus: in_progress\n---\n\nbody\n"
    )

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # A constant-team triage session: owner=constant-team, NO primary task_id.
    _write_session_metadata(sessions_dir / "S-alice-feedback-p38.md", {
        "sid": "S-alice-feedback-p38",
        "status": "active",
        "window": "user-feedback",
        "cwd": str(repo),
        "claude_uuid": "u-38",
        "task_id": "~",
        "owner": "constant-team",
        "initiative": "user-feedback.md",
    })

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_get_current_user", lambda: "alice")

    with pytest.raises(ActionError) as excinfo:
        bind_task(cfg, "test-project", "S-alice-feedback-p38", "T-0176")
    assert "constant-team" in str(excinfo.value)


def test_set_drift_paused_round_trip(tmp_path, monkeypatch):
    """T-0184: set_drift_paused toggles the drift_paused flag on the SessionMd."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    md = sessions_dir / "S-alice-w-p2.md"
    _write_session_metadata(md, {
        "sid": "S-alice-w-p2", "status": "active", "window": "w",
        "cwd": str(repo), "claude_uuid": "u-1", "task_id": "T-0001",
    })

    res = set_drift_paused(cfg, "test-project", "S-alice-w-p2", True)
    assert res["ok"] and res["drift_paused"] is True
    assert _read_session_metadata(md).get("drift_paused") in (True, "true", "True")

    res = set_drift_paused(cfg, "test-project", "S-alice-w-p2", False)
    assert res["drift_paused"] is False
    assert "drift_paused" not in _read_session_metadata(md)


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


# ---------------------------------------------------------------------------
# T-0077: gc_sessions — flip zombie status:active without live pane → suspended
# ---------------------------------------------------------------------------

def test_gc_sessions_flips_zombie_to_suspended(tmp_path, monkeypatch):
    """Zombie md (status: active + pane_id no longer in tmux) → patched to suspended."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])  # no live panes

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    zombie = sessions_dir / "S-testuser-T-0026-p60.md"
    _write_session_metadata(zombie, {
        "sid": "S-testuser-T-0026-p60",
        "status": "active",
        "task_id": "T-0026",
        "pane_id": "%60",
        "started_at": "2026-05-15T11:18:04Z",
        "claude_uuid": "uuid-zombie",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["ok"] is True
    assert result["repaired"] == 1
    assert result["sids"] == ["S-testuser-T-0026-p60"]

    after = _read_session_metadata(zombie)
    assert after["status"] == "suspended"
    assert after["suspended_at"]
    # T-0077: started_at + claude_uuid preserved so the session is resurrectable.
    assert after["started_at"] == "2026-05-15T11:18:04Z"
    assert after["claude_uuid"] == "uuid-zombie"


def test_gc_sessions_skips_missing_pane_id_unverifiable(tmp_path, monkeypatch):
    """T-0134 regression: SessionMd without pane_id (legacy schema, or claude
    in non-bot-squad tmux pane) must be treated as UNVERIFIABLE — skipped, not
    flagged as zombie. Caught on Day-6 deploy: gc_sessions flipped 4 live TL
    sessions to suspended on first invocation because they predated the
    pane_id field.
    """
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])  # no live panes

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    # Case 1: pane_id field entirely absent (legacy SessionMd schema).
    legacy = sessions_dir / "S-testuser-multi_server-p8.md"
    _write_session_metadata(legacy, {
        "sid": "S-testuser-multi_server-p8",
        "status": "active",
        "claude_uuid": "uuid-legacy-live",
    })
    # Case 2: pane_id explicitly null (~) — same semantic.
    null_pane = sessions_dir / "S-testuser-teamlead-p13.md"
    _write_session_metadata(null_pane, {
        "sid": "S-testuser-teamlead-p13",
        "status": "active",
        "pane_id": "~",
        "claude_uuid": "uuid-null-pane",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["repaired"] == 0, (
        f"T-0134: pane_id-less SessionMds must be skipped (unverifiable), "
        f"not flipped. Got repaired={result!r}."
    )
    assert _read_session_metadata(legacy)["status"] == "active"
    assert _read_session_metadata(null_pane)["status"] == "active"


def test_gc_sessions_skips_live_pane(tmp_path, monkeypatch):
    """status:active with a matching live pane is left alone."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [_fake_pane(pane_id="%5", window="multi_server")])

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    live_md = sessions_dir / "S-testuser-multi_server-p5.md"
    _write_session_metadata(live_md, {
        "sid": "S-testuser-multi_server-p5",
        "status": "active",
        "task_id": "T-0099",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["repaired"] == 0
    assert _read_session_metadata(live_md)["status"] == "active"


def test_gc_sessions_skips_archived(tmp_path, monkeypatch):
    """archived: true is operator intent — janitor must not touch it."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    md = sessions_dir / "S-testuser-w-p9.md"
    _write_session_metadata(md, {
        "sid": "S-testuser-w-p9",
        "status": "active",
        "archived": "true",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["repaired"] == 0
    assert _read_session_metadata(md)["status"] == "active"


def test_gc_sessions_other_user_sids_untouched(tmp_path, monkeypatch):
    """SessionMds belonging to another linux user are out of scope.

    The worker only sees its own user's tmux server via list_panes; flipping
    status on cross-user mds would mis-classify their live sessions as zombies.
    """
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    cross_user = sessions_dir / "S-alexey-claude-p1.md"
    _write_session_metadata(cross_user, {
        "sid": "S-alexey-claude-p1",
        "status": "active",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["repaired"] == 0
    assert _read_session_metadata(cross_user)["status"] == "active"


# ---------------------------------------------------------------------------
# T-0073: gc_stale_bindings — strip primary task_id from dup-claim losers
# ---------------------------------------------------------------------------

def test_gc_stale_bindings_picks_live_winner(tmp_path, monkeypatch):
    """Two sessions claim same task_id; live + latest started_at wins."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    # multi_server-p5 is live; T-0026-p60 is the dead duplicate.
    monkeypatch.setattr(S, "list_panes", lambda: [
        _fake_pane(pane_id="%5", window="multi_server"),
    ])

    sess = tmp_path / "data" / "test-project" / "sessions"
    winner = sess / "S-testuser-multi_server-p5.md"
    loser = sess / "S-testuser-T-0026-p60.md"
    _write_session_metadata(winner, {
        "sid": "S-testuser-multi_server-p5",
        "status": "active",
        "task_id": "T-0026",
        "started_at": "2026-05-20T10:00:00Z",
    })
    _write_session_metadata(loser, {
        "sid": "S-testuser-T-0026-p60",
        "status": "suspended",
        "task_id": "T-0026",
        "started_at": "2026-05-15T11:18:04Z",
    })

    result = S.gc_stale_bindings(cfg, "test-project")
    assert result["stripped"] == 1
    assert result["details"][0]["winner"] == "S-testuser-multi_server-p5"
    assert result["details"][0]["sid"] == "S-testuser-T-0026-p60"

    assert _read_session_metadata(winner)["task_id"] == "T-0026"
    loser_meta = _read_session_metadata(loser)
    assert loser_meta["task_id"] is None  # ~ → parsed as None
    assert loser_meta["last_task_id"] == "T-0026"
    assert loser_meta["archive_reason"] == "stale-binding"


def test_gc_stale_bindings_no_dup_is_noop(tmp_path, monkeypatch):
    """Lone claimant keeps its task_id."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])

    sess = tmp_path / "data" / "test-project" / "sessions"
    md = sess / "S-testuser-w-p1.md"
    _write_session_metadata(md, {
        "sid": "S-testuser-w-p1",
        "status": "suspended",
        "task_id": "T-0099",
    })

    result = S.gc_stale_bindings(cfg, "test-project")
    assert result["stripped"] == 0
    assert _read_session_metadata(md)["task_id"] == "T-0099"


def test_gc_stale_bindings_no_live_picks_latest_started_at(tmp_path, monkeypatch):
    """All claimants dead — newest started_at wins; older losers stripped."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])

    sess = tmp_path / "data" / "test-project" / "sessions"
    older = sess / "S-testuser-w-p1.md"
    newer = sess / "S-testuser-w-p2.md"
    _write_session_metadata(older, {
        "sid": "S-testuser-w-p1",
        "status": "suspended",
        "task_id": "T-0050",
        "started_at": "2026-04-01T00:00:00Z",
    })
    _write_session_metadata(newer, {
        "sid": "S-testuser-w-p2",
        "status": "suspended",
        "task_id": "T-0050",
        "started_at": "2026-05-01T00:00:00Z",
    })

    result = S.gc_stale_bindings(cfg, "test-project")
    assert result["stripped"] == 1
    assert result["details"][0]["winner"] == "S-testuser-w-p2"
    assert _read_session_metadata(newer)["task_id"] == "T-0050"
    assert _read_session_metadata(older)["task_id"] is None


# ---------------------------------------------------------------------------
# T-0072: rebind_sid — atomic peer-bus inbox triple rename on SID rotation
# ---------------------------------------------------------------------------

def test_rebind_sid_renames_inbox_triple(tmp_path):
    """All three peer-bus files migrate from old_sid to new_sid."""
    from bot_squad_worker import intersession as I
    import types
    cfg = types.SimpleNamespace(data_dir=tmp_path / "data")
    I.send(cfg, "p", "S-from", "S-old", "hi there")
    # Drain so seen-S-old has a non-zero byte offset.
    I.inbox_read(cfg, "p", "S-old")

    chat = tmp_path / "data" / "p" / "_chat"
    assert (chat / "inbox-S-old.log").exists()
    assert (chat / "seen-S-old").exists()
    assert (chat / "heartbeat-S-old").exists()

    out = I.rebind_sid(cfg, "p", "S-old", "S-new")
    assert out["ok"] is True
    assert sorted(out["renamed"]) == sorted([
        "inbox-S-old.log", "seen-S-old", "heartbeat-S-old",
    ])
    assert out["collisions"] == []

    assert not (chat / "inbox-S-old.log").exists()
    assert (chat / "inbox-S-new.log").exists()
    assert (chat / "seen-S-new").exists()
    assert (chat / "heartbeat-S-new").exists()


def test_rebind_sid_self_is_noop(tmp_path):
    """old_sid == new_sid → renamed=[]."""
    from bot_squad_worker import intersession as I
    import types
    cfg = types.SimpleNamespace(data_dir=tmp_path / "data")
    I.send(cfg, "p", "S-from", "S-same", "msg")
    out = I.rebind_sid(cfg, "p", "S-same", "S-same")
    assert out["renamed"] == []
    assert out["reason"] == "self"
    chat = tmp_path / "data" / "p" / "_chat"
    assert (chat / "inbox-S-same.log").exists()


def test_rebind_sid_collision_leaves_both(tmp_path):
    """Target inbox already exists → leave both, do NOT silently merge."""
    from bot_squad_worker import intersession as I
    import types
    cfg = types.SimpleNamespace(data_dir=tmp_path / "data")
    I.send(cfg, "p", "S-from", "S-old", "old msg")
    I.send(cfg, "p", "S-from", "S-new", "new msg")  # creates inbox-S-new.log

    out = I.rebind_sid(cfg, "p", "S-old", "S-new")
    chat = tmp_path / "data" / "p" / "_chat"
    # inbox-S-old.log left alone (collision), no merge.
    assert (chat / "inbox-S-old.log").exists()
    assert "inbox-S-new.log" in out["collisions"]
    # New still has only its original message.
    read_new = I.inbox_read(cfg, "p", "S-new")
    assert any("new msg" in m for m in read_new["messages"])
    assert not any("old msg" in m for m in read_new["messages"])


def test_rebind_sid_missing_source_is_noop(tmp_path):
    """No inbox files for old_sid → returns ok with empty renamed list."""
    from bot_squad_worker import intersession as I
    import types
    cfg = types.SimpleNamespace(data_dir=tmp_path / "data")
    out = I.rebind_sid(cfg, "p", "S-never", "S-new")
    assert out["ok"] is True
    assert out["renamed"] == []


def test_resume_rebinds_peer_inbox_on_sid_rotation(tmp_path, monkeypatch):
    """End-to-end: resume() rotates SID and migrates the peer inbox triple."""
    import bot_squad_worker.sessions as S
    from bot_squad_worker import intersession as I

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    # Pre-rotation SessionMd + a peer-bus message addressed to the old SID.
    sess = tmp_path / "data" / "test-project" / "sessions"
    old_sid = "S-testuser-w-p5"
    new_sid = "S-testuser-w-p9"
    _write_session_metadata(sess / f"{old_sid}.md", {
        "sid": old_sid,
        "status": "suspended",
        "window": "w",
        "cwd": str(repo),
        "claude_uuid": "uuid-x",
        "task_id": "~",
    })
    I.send(cfg, "test-project", "S-peer", old_sid, "pre-rotation msg")

    # Fake tmux state for resume(): no live pane initially, then one with %9.
    snapshot = {"phase": 0}

    def fake_list_panes():
        if snapshot["phase"] == 0:
            return []
        return [_fake_pane(pane_id="%9", window="w", cwd=str(repo))]

    def fake_run(args, **kwargs):
        if args[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["tmux", "new-window"]:
            snapshot["phase"] = 1
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "list_panes", fake_list_panes)
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S.time, "sleep", lambda _x: None)

    result = S.resume(cfg, "test-project", old_sid)
    assert result["sid"] == new_sid

    chat = tmp_path / "data" / "test-project" / "_chat"
    assert not (chat / f"inbox-{old_sid}.log").exists()
    assert (chat / f"inbox-{new_sid}.log").exists()
    read = I.inbox_read(cfg, "test-project", new_sid)
    assert any("pre-rotation msg" in m for m in read["messages"])


def test_session_start_hook_calls_peer_rebind_sid():
    """Hook break-pane block invokes worker action peer_rebind_sid via socket."""
    import pathlib as _pl
    here = _pl.Path(__file__).resolve()
    hook = None
    for ancestor in here.parents:
        cand = ancestor / "scripts" / "hooks" / "session_start.sh"
        if cand.is_file():
            hook = cand
            break
    if hook is None:
        pytest.skip("session_start.sh not reachable")
    text = hook.read_text()
    # T-0072: the break-pane block must rebind the peer-bus triple after the
    # SessionMd migration. Otherwise messages still addressed to old_sid land
    # in a dead inbox.
    assert "/actions/peer_rebind_sid" in text, \
        "T-0072 regression: SessionStart hook break-pane no longer rebinds peer inbox"


# ---------------------------------------------------------------------------
# T-0141 — authoritative role resolver (fixes "everything is a teamlead" leak)
# ---------------------------------------------------------------------------

def test_derive_role_explicit_tl_window_markers():
    from bot_squad_worker.sessions import _derive_role
    # Real-world TL window names seen on staging signal-tracker / bot-squad.
    for win in (
        "multi_server-TL",
        "live-news-tl",
        "prod-ops-tl",
        "trader_multitool_backbone_teamlead",
        "TL",
        "foo_teamlead",
    ):
        assert _derive_role(win, None, None) == "teamlead", win


def test_derive_role_task_bound_is_dev_even_without_tl_marker():
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("backtest_tab", "T-0033", None) == "dev"
    # extras-only binding still counts as a dev.
    assert _derive_role("somewin", "~", None, extra_task_ids=["T-9"]) == "dev"


def test_derive_role_taskless_featurewindow_is_dev_not_teamlead():
    """The core leak: a task-less feature/stream window must NOT default to TL."""
    from bot_squad_worker.sessions import _derive_role
    for win in (
        "v08-stream-a-ws-transport",
        "live-news-polish",
        "signal-page-polish",
        "newsd-matcher-tightness",
        "bash",
        "claude",
        "bsq-cli",
    ):
        assert _derive_role(win, "~", "~") == "dev", win


def test_derive_role_initiative_bound_taskless_is_dev():
    """T-0175: an initiative-bound, task-less, marker-less session is a DEV, not
    a teamlead. T-0141's rule #4 ("initiative + no task ⇒ teamlead") still leaked:
    a dev that finishes its task (task_id cleared to ~) keeps its initiative and
    flipped to teamlead. Genuine TLs carry an explicit `-TL`/`_teamlead` window
    marker (rule #2) and are unaffected; everything else defaults to dev.

    Real leak instance (TL data point 2026-06-02): the constant-team feedback
    processor `user-feedback` window, initiative=user-feedback.md, no task —
    must be `dev`, never `teamlead`.
    """
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("genericwin", None, "operator-ux-and-session-mgmt.md") == "dev"
    assert _derive_role("genericwin", "~", "~", extra_initiatives=["x.md"]) == "dev"
    assert _derive_role("user-feedback", None, "user-feedback.md") == "dev"


def test_derive_role_operator_pane():
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("operator", None, None) == "operator"
    assert _derive_role("bot-squad-operator", "~", "~") == "operator"


def test_derive_role_ctl_suffix_is_not_teamlead():
    """Guard against false-positive `tl` matches (e.g. ...ctl)."""
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("some-ctl", "~", "~") == "dev"
    assert _derive_role("html", "~", "~") == "dev"


def test_list_sessions_emits_role_for_active_and_suspended(tmp_path, monkeypatch):
    """list_sessions stamps an authoritative `role` on every row."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    # One active TL-named pane + one active task-less feature pane.
    fake_pane_output = (
        f"%9|multi_server-TL|1234|{repo}|claude|test-project\n"
        f"%11|sessions-list|1235|{repo}|claude|test-project\n"
    )

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    # A suspended dev md (task-less feature window) that must NOT leak as TL.
    sdir = tmp_path / "data" / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-testuser-v08-stream-a-p50.md", {
        "sid": "S-testuser-v08-stream-a-p50",
        "status": "suspended",
        "window": "v08-stream-a",
        "task_id": "~",
        "initiative": "~",
    })

    rows = {r["sid"]: r for r in list_sessions(cfg, "test-project")}
    assert rows["S-testuser-multi_server-TL-p9"]["role"] == "teamlead"
    assert rows["S-testuser-sessions-list-p11"]["role"] == "dev"
    assert rows["S-testuser-v08-stream-a-p50"]["role"] == "dev"


# ---------------------------------------------------------------------------
# T-0176 #5/#6 — dedup_sessions: collapse duplicate SessionMds to one keeper
# ---------------------------------------------------------------------------

def _seed_md(cfg, slug, sid, **fields):
    from bot_squad_worker.sessions import _session_file, _write_session_metadata
    meta = {"sid": sid, "status": "suspended"}
    meta.update(fields)
    _write_session_metadata(_session_file(cfg.data_dir, slug, sid), meta)


def _read_md(cfg, slug, sid):
    from bot_squad_worker.sessions import _session_file, _read_session_metadata
    return _read_session_metadata(_session_file(cfg.data_dir, slug, sid))


def test_dedup_mode_a_same_uuid_keeps_newest(tmp_path, monkeypatch):
    """Mode A: two SessionMds sharing a claude_uuid (pane rotation p8→p9) are one
    logical session — keep the most-recent, mark the other merged_into it."""
    from bot_squad_worker.sessions import dedup_sessions
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    monkeypatch.setattr("bot_squad_worker.sessions.list_panes", lambda: [])
    _seed_md(cfg, "test-project", "S-u-multi-p8", claude_uuid="UUID-1",
             window="multi", last_task_id="T-0100", started_at="2026-05-14T00:00:00Z")
    _seed_md(cfg, "test-project", "S-u-multi-p9", claude_uuid="UUID-1",
             window="multi", last_task_id="T-0100", started_at="2026-05-23T00:00:00Z")

    res = dedup_sessions(cfg, "test-project", dry_run=False)
    keep = _read_md(cfg, "test-project", "S-u-multi-p9")
    drop = _read_md(cfg, "test-project", "S-u-multi-p8")
    assert "merged_into" not in keep or keep.get("merged_into") in (None, "~")
    assert drop["merged_into"] == "S-u-multi-p9"
    assert str(drop["archived"]).lower() == "true"
    assert res["merged_count"] == 1


def test_dedup_mode_b_same_task_window_distinct_uuid(tmp_path, monkeypatch):
    """Mode B: the T-0080 p92..p99 storm — distinct uuids, same window+task —
    collapses to one keeper (newest) with the rest merged_into it."""
    from bot_squad_worker.sessions import dedup_sessions
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    monkeypatch.setattr("bot_squad_worker.sessions.list_panes", lambda: [])
    for i, ts in enumerate(["00:00", "05:00", "09:00"]):
        _seed_md(cfg, "test-project", f"S-u-T-0080-p9{i}", claude_uuid=f"UUID-{i}",
                 window="T-0080", last_task_id="T-0080",
                 started_at=f"2026-05-16T{ts}:00Z")
    res = dedup_sessions(cfg, "test-project", dry_run=False)
    keeper = _read_md(cfg, "test-project", "S-u-T-0080-p92")  # 09:00 newest
    assert keeper.get("merged_into", "~") in (None, "~")
    for loser in ("S-u-T-0080-p90", "S-u-T-0080-p91"):
        assert _read_md(cfg, "test-project", loser)["merged_into"] == "S-u-T-0080-p92"
    assert res["merged_count"] == 2


def test_dedup_excludes_constant_team_sessions(tmp_path, monkeypatch):
    """A constant-team session mis-bound to another's task must NOT be merged
    into that task's cluster (TL p38 data point)."""
    from bot_squad_worker.sessions import dedup_sessions
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    monkeypatch.setattr("bot_squad_worker.sessions.list_panes", lambda: [])
    # Two real workers on T-0080 (same window) + a constant-team feedback session
    # stamped with task_id T-0080 but a different window/owner.
    _seed_md(cfg, "test-project", "S-u-T-0080-p1", claude_uuid="U1",
             window="T-0080", last_task_id="T-0080", started_at="2026-05-16T00:00:00Z")
    _seed_md(cfg, "test-project", "S-u-T-0080-p2", claude_uuid="U2",
             window="T-0080", last_task_id="T-0080", started_at="2026-05-16T01:00:00Z")
    _seed_md(cfg, "test-project", "S-u-user-feedback-p3", claude_uuid="U3",
             window="user-feedback", task_id="T-0080", owner="constant-team",
             initiative="user-feedback.md", started_at="2026-05-16T02:00:00Z")
    res = dedup_sessions(cfg, "test-project", dry_run=False)
    fb = _read_md(cfg, "test-project", "S-u-user-feedback-p3")
    assert fb.get("merged_into", "~") in (None, "~"), "constant-team session must not be merged"
    assert res["merged_count"] == 1  # only the two real T-0080 workers dedup


def test_dedup_dry_run_writes_nothing(tmp_path, monkeypatch):
    from bot_squad_worker.sessions import dedup_sessions
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    monkeypatch.setattr("bot_squad_worker.sessions.list_panes", lambda: [])
    _seed_md(cfg, "test-project", "S-u-multi-p8", claude_uuid="UUID-1",
             window="multi", last_task_id="T-0100", started_at="2026-05-14T00:00:00Z")
    _seed_md(cfg, "test-project", "S-u-multi-p9", claude_uuid="UUID-1",
             window="multi", last_task_id="T-0100", started_at="2026-05-23T00:00:00Z")
    res = dedup_sessions(cfg, "test-project", dry_run=True)
    assert res["merged_count"] == 1            # reports what it WOULD do
    drop = _read_md(cfg, "test-project", "S-u-multi-p8")
    assert drop.get("merged_into", "~") in (None, "~")  # but wrote nothing


def test_dedup_never_archives_active_rows(tmp_path, monkeypatch):
    """Migration guardrail (TL): a status=active row is never merged/archived,
    even if it shares a uuid/task+window with a zombie — only dead rows collapse."""
    from bot_squad_worker.sessions import dedup_sessions
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    monkeypatch.setattr("bot_squad_worker.sessions.list_panes", lambda: [])
    # An active worker + an older suspended zombie sharing the same uuid.
    _seed_md(cfg, "test-project", "S-u-multi-p9", claude_uuid="UUID-1", status="active",
             window="multi", last_task_id="T-0100", started_at="2026-05-23T00:00:00Z")
    _seed_md(cfg, "test-project", "S-u-multi-p8", claude_uuid="UUID-1", status="suspended",
             window="multi", last_task_id="T-0100", started_at="2026-05-14T00:00:00Z")
    res = dedup_sessions(cfg, "test-project", dry_run=False)
    # The active p9 keeps the slot; only the suspended p8 is merged.
    assert _read_md(cfg, "test-project", "S-u-multi-p9").get("merged_into", "~") in (None, "~")
    assert _read_md(cfg, "test-project", "S-u-multi-p8")["merged_into"] == "S-u-multi-p9"
    assert res["merged_count"] == 1


def test_dedup_active_dup_pair_is_left_untouched(tmp_path, monkeypatch):
    """Two active rows sharing a uuid: neither is archived (both active = hands
    off; gc_sessions must suspend the dead one first)."""
    from bot_squad_worker.sessions import dedup_sessions
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    monkeypatch.setattr("bot_squad_worker.sessions.list_panes", lambda: [])
    _seed_md(cfg, "test-project", "S-u-multi-p9", claude_uuid="UUID-1", status="active",
             window="multi", last_task_id="T-0100", started_at="2026-05-23T00:00:00Z")
    _seed_md(cfg, "test-project", "S-u-multi-p8", claude_uuid="UUID-1", status="active",
             window="multi", last_task_id="T-0100", started_at="2026-05-14T00:00:00Z")
    res = dedup_sessions(cfg, "test-project", dry_run=False)
    assert res["merged_count"] == 0


# ---------------------------------------------------------------------------
# T-0176 #1/#2 — resolve_session: display-SID<->UUID addressability shim
# ---------------------------------------------------------------------------

def test_resolve_session_by_sid(tmp_path, monkeypatch):
    from bot_squad_worker.sessions import resolve_session
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    _seed_md(cfg, "test-project", "S-u-feat-p1", claude_uuid="UUID-1", status="active")
    res = resolve_session(cfg, "test-project", "S-u-feat-p1")
    assert res is not None and res["sid"] == "S-u-feat-p1"


def test_resolve_session_by_uuid(tmp_path, monkeypatch):
    """Peer-bus callers may address by uuid — resolves to the same SessionMd."""
    from bot_squad_worker.sessions import resolve_session
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    _seed_md(cfg, "test-project", "S-u-feat-p1", claude_uuid="UUID-1", status="active")
    res = resolve_session(cfg, "test-project", "UUID-1")
    assert res is not None and res["sid"] == "S-u-feat-p1"


def test_resolve_session_follows_merged_into_to_keeper(tmp_path, monkeypatch):
    """Addressing a deduped-away SID redirects to the surviving keeper — the
    phased shim that preserves addressability across the dedup migration."""
    from bot_squad_worker.sessions import resolve_session
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    _seed_md(cfg, "test-project", "S-u-feat-p9", claude_uuid="UUID-9", status="active")
    _seed_md(cfg, "test-project", "S-u-feat-p8", claude_uuid="UUID-8", status="suspended",
             merged_into="S-u-feat-p9", archived="true")
    res = resolve_session(cfg, "test-project", "S-u-feat-p8")
    assert res is not None and res["sid"] == "S-u-feat-p9"


def test_resolve_session_unknown_is_none(tmp_path, monkeypatch):
    from bot_squad_worker.sessions import resolve_session
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr("bot_squad_worker.sessions._get_current_user", lambda: "u")
    assert resolve_session(cfg, "test-project", "S-u-nope-p1") is None


# ---------------------------------------------------------------------------
# T-0176 #3 — sessions list groups by LIVE tmux, dead sessions → bucket
# ---------------------------------------------------------------------------

def test_list_sessions_suspended_dead_tmux_goes_to_bucket(tmp_path, monkeypatch):
    """A suspended row whose stored tmux_session is no longer live is grouped
    under '(no tmux session)', not blended into a phantom group."""
    import bot_squad_worker.sessions as S
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-old-p7.md", {
        "sid": "S-testuser-old-p7", "status": "suspended", "window": "old",
        "cwd": str(repo), "claude_uuid": "u-old", "tmux_session": "test-project-dead",
    })
    monkeypatch.setattr(S, "list_panes", lambda: [])  # no live tmux at all
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    rows = S.list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["tmux_session"] == "(no tmux session)"


def test_list_sessions_suspended_keeps_still_live_tmux(tmp_path, monkeypatch):
    """A suspended row whose tmux_session still has live panes keeps it (the
    session exists; only this pane is gone)."""
    import bot_squad_worker.sessions as S
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sessions_dir / "S-testuser-old-p7.md", {
        "sid": "S-testuser-old-p7", "status": "suspended", "window": "old",
        "cwd": str(repo), "claude_uuid": "u-old", "tmux_session": "test-project-live",
    })
    live = S.PaneInfo(pane_id="%2", window="other", pid="1", cwd=str(repo),
                      command="claude", session="test-project-live")
    monkeypatch.setattr(S, "list_panes", lambda: [live])
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    rows = S.list_sessions(cfg, "test-project")
    susp = [r for r in rows if r["sid"] == "S-testuser-old-p7"][0]
    assert susp["tmux_session"] == "test-project-live"


def test_list_sessions_syncs_renamed_window_label_to_md(tmp_path, monkeypatch):
    """T-0176 #4: claude /rename changes the live tmux window; the stored
    SessionMd window label is synced to match (it used to lag). The sid/filename
    (the peer-bus address) stays frozen — only the display label updates."""
    import bot_squad_worker.sessions as S
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    md_path = sessions_dir / "S-testuser-teamlead-p11.md"
    _write_session_metadata(md_path, {
        "sid": "S-testuser-teamlead-p11", "status": "active", "window": "teamlead",
        "cwd": str(repo), "claude_uuid": "uuid-renamed", "task_id": "T-0100",
        "started_at": "2026-05-23T15:37:12Z",
    })
    encoded = str(repo).replace("/", "-")
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "uuid-renamed.jsonl").write_text("{}")
    fake_pane_output = f"%11|ui_polish-TL|1234|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    S.list_sessions(cfg, "test-project")
    updated = _read_session_metadata(md_path)
    assert updated["window"] == "ui_polish-TL"      # label synced
    assert updated["sid"] == "S-testuser-teamlead-p11"  # address frozen
