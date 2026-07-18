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
    sid_display_label,
    spawn,
    _append_task_session_history,
    _live_task_owner,
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


def test_compute_sid_unaffected_by_slug():
    """T-0636: compute_sid stays the ROUTING key — no slug param, no change
    to its output shape. sid_display_label carries the slug instead."""
    assert compute_sid("alice", "mywin", "%2") == "S-alice-mywin-p2"


def test_sid_display_label_prefixes_slug():
    assert sid_display_label("S-alice-mywin-p2", "bot-squad") == "[bot-squad] S-alice-mywin-p2"


def test_sid_display_label_two_projects_same_role_are_distinguishable():
    """T-0636 stakeholder complaint: two sessions in different projects under
    the same role/window render identically. The label must differ even
    when the underlying SID (the routing key) does not."""
    sid_a = compute_sid("alexey", "operator", "%2")
    sid_b = compute_sid("alexey", "operator", "%2")
    assert sid_a == sid_b  # same routing key — expected, by design
    assert sid_display_label(sid_a, "bot-squad") != sid_display_label(sid_b, "watchrobot")


def test_sid_display_label_falls_back_to_sid_when_slug_empty():
    assert sid_display_label("S-alice-mywin-p2", "") == "S-alice-mywin-p2"
    assert sid_display_label("S-alice-mywin-p2", None) == "S-alice-mywin-p2"


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
    # T-0636: every row carries a slug-qualified display label alongside the
    # raw routing SID.
    assert rows[0]["sid_label"] == "[test-project] S-testuser-mywin-p2"


def test_list_sessions_stamps_awaiting_input_from_tg_stall(tmp_path, monkeypatch):
    """T-0285: each row carries an `awaiting_input` flag derived from the
    tg_stall blocked-marker set; True only for the blocked SID."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    fake_pane_output = f"%2|w1|1234|{repo}|claude\n%3|w2|1235|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    import bot_squad_worker.tg_stall as TS
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(TS, "blocked_sids", lambda cfg, slug: {"S-testuser-w1-p2"})

    rows = list_sessions(cfg, "test-project")
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["S-testuser-w1-p2"]["awaiting_input"] is True
    assert by_sid["S-testuser-w2-p3"]["awaiting_input"] is False


def test_list_sessions_close_on_attach_clears_resumed_marker(tmp_path, monkeypatch):
    """P2-05-BE: a blocked, live (active) session that the close-on-attach
    reconcile resolves (clear_if_resumed → True) has its awaiting_input flipped
    back to False in the same list_sessions pass — even though it's still in the
    blocked_sids set at the top of the pass. The reconcile is only attempted for
    blocked active rows (w2 is not blocked → never probed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    fake_pane_output = f"%2|w1|1234|{repo}|claude\n%3|w2|1235|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    import bot_squad_worker.tg_stall as TS
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(TS, "blocked_sids", lambda cfg, slug: {"S-testuser-w1-p2"})

    resumed = "S-testuser-w1-p2"
    probed: list[str] = []

    def fake_clear_if_resumed(cfg, slug, sid, activity_at):
        probed.append(sid)
        return sid == resumed

    monkeypatch.setattr(TS, "clear_if_resumed", fake_clear_if_resumed)

    rows = list_sessions(cfg, "test-project")
    by_sid = {r["sid"]: r for r in rows}
    # Only the blocked row is probed; the resolved marker flips the flag off.
    assert probed == [resumed]
    assert by_sid["S-testuser-w1-p2"]["awaiting_input"] is False
    assert by_sid["S-testuser-w2-p3"]["awaiting_input"] is False


def test_list_sessions_awaiting_input_defaults_false_on_watchdog_error(tmp_path, monkeypatch):
    """If the tg_stall lookup raises, the flag is a safe False — never wedges
    the list."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    fake_pane_output = f"%2|w1|1234|{repo}|claude\n"

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_pane_output, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    import bot_squad_worker.tg_stall as TS

    def boom(*a, **k):
        raise RuntimeError("watchdog down")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(TS, "blocked_sids", boom)

    rows = list_sessions(cfg, "test-project")
    assert rows[0]["awaiting_input"] is False


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


def test_list_sessions_prefers_md_recorded_uuid_over_cwd_guess(tmp_path, monkeypatch):
    """T-0584: same-cwd panes emit their md-RECORDED claude_uuid, not the guess.

    Fresh-spawned panes carry no --resume/--session-id in cmdline, so the
    T-0120 /proc walk returns None and the row's claude_uuid fell through to
    discover_claude_uuid — the cwd's mtime-newest jsonl, the SAME uuid for
    every pane sharing the repo cwd. On staging every non-operator row on
    /p/<slug>/sessions linked the newest session's transcript.

    The session md already records the binding (claude_uuid field, stamped at
    spawn/resume). When the md resolves by SID, that recorded uuid must win
    over the shared-cwd guess; the /proc walk stays authoritative when it hits.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    _write_session_metadata(sessions_dir / "S-testuser-dev_a-p20.md", {
        "sid": "S-testuser-dev_a-p20", "status": "active", "window": "dev_a",
        "cwd": str(repo), "claude_uuid": "uuid-dev-a",
        "task_id": "T-0001", "started_at": "2026-07-04T10:00:00Z",
    })
    _write_session_metadata(sessions_dir / "S-testuser-dev_b-p21.md", {
        "sid": "S-testuser-dev_b-p21", "status": "active", "window": "dev_b",
        "cwd": str(repo), "claude_uuid": "uuid-dev-b",
        "task_id": "T-0002", "started_at": "2026-07-04T11:00:00Z",
    })

    # Shared encoded project dir: uuid-dev-b's jsonl is mtime-newest, so the
    # cwd guess returns "uuid-dev-b" for BOTH panes.
    encoded = str(repo).replace("/", "-")
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "uuid-dev-a.jsonl").write_text("{}")
    (proj_dir / "uuid-dev-b.jsonl").write_text("{}")
    now = time.time()
    os.utime(proj_dir / "uuid-dev-a.jsonl", (now - 600, now - 600))
    os.utime(proj_dir / "uuid-dev-b.jsonl", (now, now))

    fake_panes = (
        f"%20|dev_a|4001|{repo}|claude\n"
        f"%21|dev_b|4002|{repo}|claude\n"
    )

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, fake_panes, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    # Fresh spawns: the /proc walk finds nothing for either pane.
    monkeypatch.setattr(S, "_pane_claude_uuid_from_proc", lambda pid, home: None)

    rows = list_sessions(cfg, "test-project")
    by_sid = {r["sid"]: r for r in rows if r["status"] == "active"}

    assert by_sid["S-testuser-dev_a-p20"]["claude_uuid"] == "uuid-dev-a"
    assert by_sid["S-testuser-dev_b-p21"]["claude_uuid"] == "uuid-dev-b"


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


def test_gc_tmux_sessions_short_grace_for_constant_team(tmp_path, monkeypatch):
    """T-0350: a demand-driven constant-team sibling (paneless = its triage dev
    exited) is reaped on a SHORT grace, not the 1h default — so the empty
    bare-shell doesn't linger for an hour. A normal sibling keeps the long grace."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S

    # Mark user-feedback a constant team (constant_team_stems reads this).
    initd = cfg.data_dir / "test-project" / "vision" / "initiatives"
    initd.mkdir(parents=True, exist_ok=True)
    (initd / "user-feedback.md").write_text(
        "---\nname: user-feedback\nconstant_team: true\nteam_window: user-feedback\n---\n")

    killed: list[str] = []
    panes = [
        PaneInfo(pane_id="%1", window="_init", pid="1", cwd=str(repo),
                 command="bash", session="test-project-user-feedback"),
        PaneInfo(pane_id="%2", window="_init", pid="2", cwd=str(repo),
                 command="bash", session="test-project-feat"),  # normal sibling
    ]
    monkeypatch.setattr(S, "list_panes", lambda: panes)

    def fake_run(args, **kwargs):
        if "list-sessions" in args:
            return subprocess.CompletedProcess(
                args, 0,
                _ls_line("test-project-user-feedback", 1000) + "\n"
                + _ls_line("test-project-feat", 1000) + "\n", "")
        if "kill-session" in args:
            killed.append(args[args.index("-t") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    # 200s after activity: past the constant short grace (120) but well within the
    # 1h default grace.
    monkeypatch.setattr(S.time, "time", lambda: 1000 + 200)

    res = S.gc_tmux_sessions(cfg, "test-project")
    assert "test-project-user-feedback" in res["reaped"]   # demand-driven → fast reap
    assert "test-project-feat" not in res["reaped"]         # normal → still in grace
    assert killed == ["test-project-user-feedback"]


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


# ---------------------------------------------------------------------------
# Item 3 (audit Fork-2 Part A): spawn refuses the dup-bind AT THE OPEN under the
# .task-claim.lock + stamps the claim so GC drops to a crash-only backstop.
# ---------------------------------------------------------------------------

def test_spawn_refuses_dup_bind_when_live_owner_exists(tmp_path, monkeypatch):
    """A task already held by a LIVE session must not be bound again — spawn
    refuses BEFORE opening a tmux window (no wasted session), closing the
    dup-bind TOCTOU at the open rather than via gc_stale_bindings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    backlog = cfg.data_dir / "test-project" / "backlog"
    (backlog / "T-0009-foo.md").write_text("---\nid: T-0009\ntitle: Foo\nstatus: open\n---\n\nbody\n")
    sess_dir = cfg.data_dir / "test-project" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(
        sess_dir / "S-u-existing-p1.md",
        {"sid": "S-u-existing-p1", "task_id": "T-0009", "status": "active"},
    )

    new_window_calls: list = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            new_window_calls.append(args)
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%2|w|11|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    # T-0402: the owner gatekeeps only if its SID is a genuinely live claude
    # agent — model that so this still tests a real dup-bind refusal (not a
    # phantom, which T-0402 now correctly lets through).
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {"S-u-existing-p1"})

    from bot_squad_worker.actions import ActionError
    with pytest.raises(ActionError, match="already bound to live session"):
        spawn(cfg, "test-project", "w", task_id="T-0009")
    assert new_window_calls == [], "must refuse before opening a tmux window"


def test_live_task_owner_ignores_phantom_dead_pane_holder(tmp_path, monkeypatch):
    """T-0402: a crashed dev's md lingers ``status: active`` with the task still
    in its md, but its tmux pane is gone (or fell back to a bash shell). Such a
    PHANTOM holder must NOT gatekeep a rebind — else the task is permanently
    un-rebindable ('already bound to live session {dead}'), the exact failure
    this fn's docstring promises to prevent. Only a holder whose SID maps to a
    live claude agent (``_live_agent_sids``) counts. Mirrors the T-0397
    ``_count_live_sessions`` reconcile (d0b3cdc)."""
    import bot_squad_worker.sessions as S
    sess_dir = tmp_path / "data" / "test-project" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(
        sess_dir / "S-u-dead-p1.md",
        {"sid": "S-u-dead-p1", "task_id": "T-0042", "status": "active"},
    )
    # no live agent maps to the holder's SID → it is a phantom, not an owner
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    assert _live_task_owner(tmp_path / "data", "test-project", "T-0042") is None


def test_live_task_owner_returns_genuine_live_holder(tmp_path, monkeypatch):
    """A holder whose SID IS a live claude agent still gatekeeps the rebind —
    T-0402 must not over-correct and free a task held by a genuinely live dev."""
    import bot_squad_worker.sessions as S
    sess_dir = tmp_path / "data" / "test-project" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(
        sess_dir / "S-u-live-p1.md",
        {"sid": "S-u-live-p1", "task_id": "T-0042", "status": "active"},
    )
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {"S-u-live-p1"})
    assert _live_task_owner(tmp_path / "data", "test-project", "T-0042") == "S-u-live-p1"


def test_spawn_stamps_task_claim_active_in_seed_meta(tmp_path, monkeypatch):
    """A successful spawn stamps task_id+status:active in the new session's seed
    meta so it is IMMEDIATELY a live task owner — no TOCTOU window before the
    SessionStart hook runs (the differentiator that demotes GC to crash-only)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    backlog = cfg.data_dir / "test-project" / "backlog"
    (backlog / "T-0010-foo.md").write_text("---\nid: T-0010\ntitle: Foo\nstatus: open\n---\n\nbody\n")

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, f"%2|w|11|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "w", task_id="T-0010")

    seed = _read_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / "S-u-w-p2.md"
    )
    assert seed is not None
    assert seed.get("task_id") == "T-0010"
    assert str(seed.get("status")).lower() == "active"


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
    # T-0201: model the real composer transitions — empty before paste,
    # `[Pasted text …]` once the paste lands, empty again after the Enter
    # submits — so _deliver_prompt's confirm-then-Enter loop terminates.
    pasted = [False]
    entered = [False]

    def fake_run(args, **kwargs):
        if "load-buffer" in args or "paste-buffer" in args:
            key_calls.append((args, kwargs.get("input")))
            if "paste-buffer" in args:
                pasted[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "send-keys" in args:
            key_calls.append((args, None))
            if args[-1] == "Enter":
                entered[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "capture-pane" in args:
            if pasted[0] and not entered[0]:
                return subprocess.CompletedProcess(args, 0, "❯ [Pasted text #1 +1 lines]\n", "")
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

    # T-0144/T-0201: prompt delivery is via tmux load-buffer (stdin) +
    # paste-buffer (bracketed paste), the operator's proven-reliable primitive
    # — not a send-keys literal, and not set-buffer (which overflows tmux's
    # command parser for large briefs).
    assert any("load-buffer" in a and inp == "hello world" for a, inp in key_calls), \
        f"expected hello world via load-buffer stdin; got: {key_calls}"
    assert any("paste-buffer" in a for a, inp in key_calls), \
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
    pasted = [False]
    entered = [False]

    def fake_run(args, **kwargs):
        if "capture-pane" in args:
            capture_calls[0] += 1
            # Once the paste has landed (but Enter not yet confirmed), the
            # composer shows the bracketed-paste placeholder; after Enter it
            # clears back to an empty `❯` (T-0201 confirm-then-Enter).
            if pasted[0] and not entered[0]:
                return subprocess.CompletedProcess(args, 0, "❯ [Pasted text #1 +1 lines]\n", "")
            if pasted[0]:
                return subprocess.CompletedProcess(args, 0, "❯ \n", "")
            # Pre-paste: composer is not ready for the first two polls (still
            # bash / claude bootstrapping), then `❯` appears.
            if capture_calls[0] < 3:
                call_log.append("capture-not-ready")
                return subprocess.CompletedProcess(args, 0, "bash-5.2$\n", "")
            call_log.append("capture-ready")
            return subprocess.CompletedProcess(args, 0, "❯ \n", "")
        if "load-buffer" in args:
            # The prompt text is streamed into the paste buffer over stdin.
            call_log.append("paste-text")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "paste-buffer" in args:
            pasted[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "send-keys" in args:
            # The only send-keys in the delivery path is the trailing Enter.
            if args[-1] == "Enter":
                call_log.append("send-enter")
                entered[0] = True
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

    # The paste (load-buffer) + Enter must come strictly AFTER the first ready
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
    pasted = [False]
    entered = [False]

    def fake_run(args, **kwargs):
        if "load-buffer" in args or "paste-buffer" in args or "send-keys" in args:
            key_calls.append((args, kwargs.get("input")))
            if "paste-buffer" in args:
                pasted[0] = True
            if "send-keys" in args and args[-1] == "Enter":
                entered[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "capture-pane" in args:
            # T-0201: composer shows the paste once it lands, then clears on Enter.
            if pasted[0] and not entered[0]:
                return subprocess.CompletedProcess(args, 0, "❯ [Pasted text #1 +1 lines]\n", "")
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
    # Delivered via load-buffer stdin (bracketed paste), not a send-keys literal.
    assert any("load-buffer" in a and (inp or "").startswith("delta:") for a, inp in key_calls), \
        f"expected delta brief via load-buffer stdin; got: {key_calls}"
    assert any("paste-buffer" in a for a, inp in key_calls), \
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
        if "load-buffer" in args or "paste-buffer" in args:
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
# T-0201: _deliver_prompt confirm-then-Enter (no blind sleep before Enter)
# ---------------------------------------------------------------------------

def _CP(args, out=""):
    return subprocess.CompletedProcess(args, 0, out, "")


def test_deliver_prompt_waits_for_paste_to_land_before_enter(monkeypatch):
    """T-0201: a large bracketed paste can take ~1s to appear in the composer.
    _deliver_prompt must poll until the paste lands (composer non-empty) before
    sending Enter — never the old blind 0.4s sleep — else the Enter is swallowed
    and the brief sits unsubmitted as '[Pasted text …]'."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    events: list[str] = []
    pasted = [False]
    entered = [False]
    land_polls = [0]

    def fake_run(args, **kwargs):
        if "load-buffer" in args:
            events.append("load-buffer"); return _CP(args)
        if "paste-buffer" in args:
            events.append("paste-buffer"); pasted[0] = True; return _CP(args)
        if "send-keys" in args and args[-1] == "Enter":
            events.append("enter"); entered[0] = True; return _CP(args)
        if "capture-pane" in args:
            if not pasted[0]:
                return _CP(args, "❯ \n")
            if not entered[0]:
                # Paste takes two polls to render in the composer.
                land_polls[0] += 1
                if land_polls[0] < 2:
                    return _CP(args, "❯ \n")            # not landed yet
                return _CP(args, "❯ [Pasted text #1 +140 lines]\n")  # landed
            return _CP(args, "❯ \n")                    # cleared after Enter
        return _CP(args)

    monkeypatch.setattr(S, "_run", fake_run)
    S._deliver_prompt("%5", "a big brief")

    assert events.count("enter") == 1, events
    # Enter strictly after the paste, and the composer was polled (≥2) until the
    # paste landed before Enter went out.
    assert events.index("paste-buffer") < events.index("enter"), events
    assert land_polls[0] >= 2, land_polls


def test_deliver_prompt_retries_enter_until_composer_clears(monkeypatch):
    """T-0201: if the first Enter is swallowed (composer still shows the paste),
    _deliver_prompt re-sends Enter until the composer clears."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    monkeypatch.setattr(S, "_PASTE_LANDED_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(S, "_PASTE_LANDED_POLL_INTERVAL_SEC", 0.1)
    monkeypatch.setattr(S, "_SUBMIT_CONFIRM_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(S, "_SUBMIT_CONFIRM_POLL_INTERVAL_SEC", 0.1)

    enters = [0]

    def fake_run(args, **kwargs):
        if "send-keys" in args and args[-1] == "Enter":
            enters[0] += 1; return _CP(args)
        if "capture-pane" in args:
            # Composer keeps showing the paste until the SECOND Enter lands.
            if enters[0] < 2:
                return _CP(args, "❯ [Pasted text #1 +140 lines]\n")
            return _CP(args, "❯ \n")
        return _CP(args)

    monkeypatch.setattr(S, "_run", fake_run)
    S._deliver_prompt("%5", "brief")
    assert enters[0] == 2, f"expected a retry Enter; got {enters[0]}"


def test_deliver_prompt_raises_when_paste_never_lands(monkeypatch):
    """T-0201: if the paste never appears in the composer, fail loudly and do
    NOT send Enter into an empty/dead pane."""
    from bot_squad_worker.actions import ActionError
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    monkeypatch.setattr(S, "_PASTE_LANDED_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(S, "_PASTE_LANDED_POLL_INTERVAL_SEC", 0.1)

    enters = [0]

    def fake_run(args, **kwargs):
        if "send-keys" in args and args[-1] == "Enter":
            enters[0] += 1; return _CP(args)
        if "capture-pane" in args:
            return _CP(args, "❯ \n")  # composer never shows the paste
        return _CP(args)

    monkeypatch.setattr(S, "_run", fake_run)
    with pytest.raises(ActionError, match="never appeared"):
        S._deliver_prompt("%5", "brief")
    assert enters[0] == 0, "must not Enter when the paste never landed"


def test_deliver_prompt_raises_when_never_submitted(monkeypatch):
    """T-0201: cap the Enter retries so a genuinely stuck composer fails loudly
    rather than looping forever."""
    from bot_squad_worker.actions import ActionError
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    monkeypatch.setattr(S, "_PASTE_LANDED_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(S, "_PASTE_LANDED_POLL_INTERVAL_SEC", 0.1)
    monkeypatch.setattr(S, "_SUBMIT_CONFIRM_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(S, "_SUBMIT_CONFIRM_POLL_INTERVAL_SEC", 0.1)
    monkeypatch.setattr(S, "_SUBMIT_MAX_RETRIES", 3)

    enters = [0]

    def fake_run(args, **kwargs):
        if "send-keys" in args and args[-1] == "Enter":
            enters[0] += 1; return _CP(args)
        if "capture-pane" in args:
            return _CP(args, "❯ [Pasted text #1 +140 lines]\n")  # never clears
        return _CP(args)

    monkeypatch.setattr(S, "_run", fake_run)
    with pytest.raises(ActionError, match="never cleared"):
        S._deliver_prompt("%5", "brief")
    assert enters[0] == 3, f"expected exactly _SUBMIT_MAX_RETRIES Enters; got {enters[0]}"


# ---------------------------------------------------------------------------
# T-0165: resume preserves a multi-bound session's extra_task_ids on rotation
# ---------------------------------------------------------------------------

def _resume_fake_run(repo, new_pane="%20", window="expert"):
    """A minimal tmux fake for a no-prompt resurrect (new window + one pane).

    Records the launched ``bash -lc`` command string on ``fake_run.launched`` so
    a test can assert the per-process env prefix (T-0525 BOT_SQUAD_TASK_ID)."""
    new_window_called = [False]
    launched: list[str] = []

    def fake_run(args, **kwargs):
        if "capture-pane" in args:
            return _CP(args, "❯ \n")
        if "new-window" in args:
            new_window_called[0] = True
            if "-lc" in args:
                launched.append(args[args.index("-lc") + 1])
            return _CP(args)
        if "list-panes" in args:
            if new_window_called[0]:
                return _CP(args, f"{new_pane}|{window}|4250|{repo}|claude\n")
            return _CP(args)
        return _CP(args)
    fake_run.launched = launched
    return fake_run


def test_resume_preserves_extra_task_ids_on_rotation(tmp_path, monkeypatch):
    """T-0165: resuming a session carrying extra_task_ids:[A,B] must keep BOTH
    on the rotated session md. (Root cause was block-vs-inline list drift the
    SessionStart hook's line reader dropped, fixed at the writer by T-0075; this
    locks the resume metadata-copy layer too.)"""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sdir = cfg.data_dir / "test-project" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sdir / "S-testuser-expert-p7.md", {
        "sid": "S-testuser-expert-p7", "status": "suspended", "window": "expert",
        "cwd": str(repo), "claude_uuid": "u7", "task_id": "T-0100",
        "extra_task_ids": ["T-0163", "T-0164"], "suspended_at": "2026-05-10T12:00:00Z",
    })

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", _resume_fake_run(repo, new_pane="%20"))
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    res = resume(cfg, "test-project", "S-testuser-expert-p7")
    new_sid = res["sid"]
    assert new_sid != "S-testuser-expert-p7", "expected SID rotation"
    meta = _read_session_metadata(sdir / f"{new_sid}.md")
    assert meta["task_id"] == "T-0100"
    assert set(meta.get("extra_task_ids") or []) == {"T-0163", "T-0164"}, meta.get("extra_task_ids")


def test_session_md_extras_survive_hook_line_reader():
    """T-0165 root-cause regression: session_start.sh reads extra_task_ids with
    a line-based partition-on-':' reader (NOT pyyaml). Pre-T-0075 the writer
    emitted block-style lists which that reader dropped to '[]', losing a
    session's extra bindings on every resume. Lock the inline list format that
    the hook reader can parse."""
    from bot_squad_worker import frontmatter as _fm
    meta = {"sid": "S-x", "status": "active", "task_id": "T-0100",
            "extra_task_ids": ["T-0163", "T-0164"], "extra_initiatives": []}
    md = "---\n" + _fm.dump_frontmatter(meta) + "---\n"
    # Replicate session_start.sh lines 134-145 + 182 verbatim:
    existing: dict = {}
    block = md.split("---", 2)[1]
    for line in block.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            existing[k.strip()] = v.strip()
    extra = existing.get("extra_task_ids") or "[]"
    assert extra == "[T-0163, T-0164]", f"hook reader would drop extras: {extra!r}"


# ---------------------------------------------------------------------------
# T-0166: resume adopts a primary task when the session has none
# ---------------------------------------------------------------------------

def test_resume_adopts_primary_when_session_has_none(tmp_path, monkeypatch):
    """T-0166: an expert whose own task closed has had its primary stripped to
    ~ (gc). Resuming it for a bsq-spawn must let it adopt the new ticket as its
    primary so the follow-up bind_task has a primary to attach extras to."""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sdir = cfg.data_dir / "test-project" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sdir / "S-testuser-expert-p7.md", {
        "sid": "S-testuser-expert-p7", "status": "suspended", "window": "expert",
        "cwd": str(repo), "claude_uuid": "u7", "task_id": "~",
        "last_task_id": "T-0001", "extra_task_ids": ["T-0050"],
        "suspended_at": "2026-05-10T12:00:00Z",
    })

    import bot_squad_worker.sessions as S
    fake = _resume_fake_run(repo, new_pane="%20")
    monkeypatch.setattr(S, "_run", fake)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    res = resume(cfg, "test-project", "S-testuser-expert-p7", task_id="T-0002")
    new_sid = res["sid"]
    meta = _read_session_metadata(sdir / f"{new_sid}.md")
    assert meta["task_id"] == "T-0002", "resumed session should adopt the new primary"
    # Pre-existing extras preserved (T-0165 too).
    assert "T-0050" in (meta.get("extra_task_ids") or [])
    # T-0525: the adopted primary now rides the PER-PROCESS env channel
    # (BOT_SQUAD_TASK_ID), not the shared `.claude/task_id` marker — so a
    # concurrent spawn can't clobber it. The race-prone marker is NOT written.
    assert any("BOT_SQUAD_TASK_ID=T-0002" in c for c in fake.launched)
    assert not (repo / ".claude" / "task_id").exists()


def test_resume_does_not_overwrite_existing_primary(tmp_path, monkeypatch):
    """T-0166: resume must NOT clobber an expert that still holds an active
    primary — the new ticket rides in via extra_task_ids (bind_task) instead."""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sdir = cfg.data_dir / "test-project" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sdir / "S-testuser-expert-p7.md", {
        "sid": "S-testuser-expert-p7", "status": "suspended", "window": "expert",
        "cwd": str(repo), "claude_uuid": "u7", "task_id": "T-0001",
        "suspended_at": "2026-05-10T12:00:00Z",
    })

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", _resume_fake_run(repo, new_pane="%20"))
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    res = resume(cfg, "test-project", "S-testuser-expert-p7", task_id="T-0002")
    meta = _read_session_metadata(sdir / f"{res['sid']}.md")
    assert meta["task_id"] == "T-0001", "existing primary must be preserved"
    assert not (repo / ".claude" / "task_id").exists(), "no marker when not adopting"


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


# ---------------------------------------------------------------------------
# T-0623: per-spawn model control
# ---------------------------------------------------------------------------


def _spawn_and_capture_shell_cmd(tmp_path, monkeypatch, window: str, **spawn_kw) -> str:
    """Run spawn() with a stubbed tmux and return the captured `bash -lc` cmd."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)

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
            return subprocess.CompletedProcess(args, 0, f"%6|{window}|123|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", window, **spawn_kw)
    assert captured_shell_cmd, "expected a new-window call"
    return captured_shell_cmd[0]


def test_spawn_explicit_model_lands_on_shell_cmd(tmp_path, monkeypatch):
    """spawn(model=X) bakes `claude --model X` into the bash -lc shell cmd."""
    cmd = _spawn_and_capture_shell_cmd(tmp_path, monkeypatch, "w", model="claude-opus-4-8")
    assert "--model claude-opus-4-8" in cmd


def test_spawn_without_model_omits_flag_for_dev_role(tmp_path, monkeypatch):
    """A plain dev window ("w") has no configured role default, so absent
    `model` means no --model flag at all — settings.json default applies."""
    cmd = _spawn_and_capture_shell_cmd(tmp_path, monkeypatch, "w")
    assert "--model" not in cmd


def test_spawn_user_conversation_window_gets_sonnet5_default(tmp_path, monkeypatch):
    """T-0623: the user-conversation role's built-in default is claude-sonnet-5,
    applied even when the caller passes no explicit model."""
    cmd = _spawn_and_capture_shell_cmd(
        tmp_path, monkeypatch, "gu_a1b2c3-user-conversation")
    assert "--model claude-sonnet-5" in cmd


def test_spawn_explicit_model_overrides_role_default(tmp_path, monkeypatch):
    """An explicit model always wins over the role-based default."""
    cmd = _spawn_and_capture_shell_cmd(
        tmp_path, monkeypatch, "gu_a1b2c3-user-conversation", model="claude-opus-4-8")
    assert "--model claude-opus-4-8" in cmd
    assert "claude-sonnet-5" not in cmd


def test_read_model_defaults_missing_file_returns_builtin(tmp_path):
    from bot_squad_worker.sessions import _read_model_defaults
    defaults = _read_model_defaults(tmp_path / "no-such-config-dir")
    assert defaults["user-conversation"] == "claude-sonnet-5"


def test_read_model_defaults_toml_override(tmp_path):
    """An admin-configured [models] section overrides/extends the built-in
    defaults — e.g. pinning a dev-role default, without a code change."""
    from bot_squad_worker.sessions import _read_model_defaults
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "system_settings.toml").write_text(
        '[models]\n"user-conversation" = "claude-opus-4-8"\ndev = "claude-haiku-4-5"\n'
    )
    defaults = _read_model_defaults(cfg_dir)
    assert defaults["user-conversation"] == "claude-opus-4-8"
    assert defaults["dev"] == "claude-haiku-4-5"


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


# ---------------------------------------------------------------------------
# T-0291: session_history_ts — a sidecar SID→first-touch-ISO map so the UI can
# show a real first-touch time for suspended/archived/legacy SIDs (instead of
# `—` when the live-sessions join misses). session_history stays the canonical
# inline SID list; the ts map is a parallel field, parsed via the shared pyyaml
# parser (no line-reader sees it).
# ---------------------------------------------------------------------------

def _read_task_session_history_ts(task_md: Path) -> dict[str, str]:
    """Parse the `session_history_ts:` sidecar map out of a task md."""
    from bot_squad_worker import frontmatter as _fm
    parsed = _fm.parse_or_none(task_md.read_text())
    assert parsed is not None
    meta, _ = parsed
    val = meta.get("session_history_ts")
    return dict(val) if isinstance(val, dict) else {}


def test_session_history_ts_stamped_on_first_append(tmp_path):
    """The first append of a SID stamps a first-touch ts into session_history_ts."""
    backlog = tmp_path / "backlog"
    backlog.mkdir()
    task_md = backlog / "T-0097-ts.md"
    task_md.write_text("---\nid: T-0097\ntitle: TS\nstatus: open\n---\n\nbody\n")

    assert _append_task_session_history(backlog, "T-0097", "S-alice-w-p2", ts="2026-06-21T01:00:00Z")
    assert _read_task_session_history(task_md) == ["S-alice-w-p2"]
    assert _read_task_session_history_ts(task_md) == {"S-alice-w-p2": "2026-06-21T01:00:00Z"}


def test_session_history_ts_first_touch_wins_on_redundant_append(tmp_path):
    """Re-appending an existing SID is a no-op — the original first-touch ts is
    preserved (the dedup short-circuits before re-stamping)."""
    backlog = tmp_path / "backlog"
    backlog.mkdir()
    task_md = backlog / "T-0098-ts.md"
    task_md.write_text("---\nid: T-0098\ntitle: TS\nstatus: open\n---\n\nbody\n")

    assert _append_task_session_history(backlog, "T-0098", "S-alice-w-p2", ts="2026-06-21T01:00:00Z")
    # second append of the SAME sid with a LATER ts must not overwrite
    assert not _append_task_session_history(backlog, "T-0098", "S-alice-w-p2", ts="2026-06-21T09:00:00Z")
    assert _read_task_session_history_ts(task_md) == {"S-alice-w-p2": "2026-06-21T01:00:00Z"}


def test_session_history_ts_accumulates_per_sid(tmp_path):
    """Each distinct SID gets its own first-touch ts; the map accumulates."""
    backlog = tmp_path / "backlog"
    backlog.mkdir()
    task_md = backlog / "T-0099-ts.md"
    task_md.write_text("---\nid: T-0099\ntitle: TS\nstatus: open\n---\n\nbody\n")

    _append_task_session_history(backlog, "T-0099", "S-alice-w-p2", ts="2026-06-21T01:00:00Z")
    _append_task_session_history(backlog, "T-0099", "S-bob-w-p3", ts="2026-06-21T02:00:00Z")
    assert _read_task_session_history(task_md) == ["S-alice-w-p2", "S-bob-w-p3"]
    assert _read_task_session_history_ts(task_md) == {
        "S-alice-w-p2": "2026-06-21T01:00:00Z",
        "S-bob-w-p3": "2026-06-21T02:00:00Z",
    }


def test_session_history_ts_preserved_on_api_patch_roundtrip(tmp_path):
    """An operator PATCH (merge_task_update) preserves the sidecar ts map —
    it's an existing-but-unlisted frontmatter key, copied through verbatim."""
    backlog = tmp_path / "backlog"
    backlog.mkdir()
    task_md = backlog / "T-0100-ts.md"
    task_md.write_text("---\nid: T-0100\ntitle: TS\nstatus: open\n---\n\nbody\n")
    _append_task_session_history(backlog, "T-0100", "S-alice-w-p2", ts="2026-06-21T01:00:00Z")

    # The API writer lives in the api package; import lazily so this worker test
    # only exercises it when both packages are importable.
    pytest.importorskip("app.markdown_writer")
    from app.markdown_writer import merge_task_update
    merge_task_update(task_md, {"status": "in_progress"})
    assert _read_task_session_history_ts(task_md) == {"S-alice-w-p2": "2026-06-21T01:00:00Z"}

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
    """Zombie md (status: active + no live claude agent) → patched to suspended."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())  # no live agents

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
    # T-0444: the auto-close stamps WHY/WHO so the badge can surface it.
    assert after["suspend_source"] == "gc_sessions"
    assert "no live claude pane" in after["suspend_reason"]


def test_suspend_stamps_source_reason_when_provided(tmp_path, monkeypatch):
    """T-0444: an auto-close caller (e.g. the drained-member reaper) passes
    source/reason → stamped on the md; a bare suspend leaves them absent."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])  # no live pane → normalise path

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    md = sessions_dir / "S-testuser-feedback-p9.md"
    _write_session_metadata(md, {
        "sid": "S-testuser-feedback-p9", "status": "active",
        "task_id": "~", "claude_uuid": "uuid-x",
    })

    S.suspend(cfg, "test-project", "S-testuser-feedback-p9",
              source="gc_drained_member", reason="queue drained")
    after = _read_session_metadata(md)
    assert after["status"] == "suspended"
    assert after["suspend_source"] == "gc_drained_member"
    assert after["suspend_reason"] == "queue drained"

    # Bare suspend (user/API path) must NOT stamp a source.
    md2 = sessions_dir / "S-testuser-feedback-p10.md"
    _write_session_metadata(md2, {
        "sid": "S-testuser-feedback-p10", "status": "active",
        "task_id": "~", "claude_uuid": "uuid-y",
    })
    S.suspend(cfg, "test-project", "S-testuser-feedback-p10")
    after2 = _read_session_metadata(md2)
    assert after2["status"] == "suspended"
    assert "suspend_source" not in after2


def test_gc_sessions_missing_pane_id_but_live_agent_is_spared(tmp_path, monkeypatch):
    """T-0134 regression, now activity-based (T-0401): a SessionMd with no
    recorded ``pane_id`` (legacy schema, or claude in a non-bot-squad tmux
    pane) that STILL resolves to a live claude agent via ``_live_agent_sids``
    must be spared — the exact Day-6 incident (4 live TL sessions flipped to
    suspended because they predated the pane_id field) must stay impossible,
    but the signal is now "is a claude process actually running for this
    sid", not "does this md happen to carry a pane_id field".
    """
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    # Case 1: pane_id field entirely absent (legacy SessionMd schema), but a
    # live claude agent resolves to this exact sid.
    monkeypatch.setattr(
        S, "_live_agent_sids",
        lambda: {"S-testuser-multi_server-p8", "S-testuser-teamlead-p13"},
    )

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    legacy = sessions_dir / "S-testuser-multi_server-p8.md"
    _write_session_metadata(legacy, {
        "sid": "S-testuser-multi_server-p8",
        "status": "active",
        "claude_uuid": "uuid-legacy-live",
    })
    # Case 2: pane_id explicitly null (~) — same semantic, still live.
    null_pane = sessions_dir / "S-testuser-teamlead-p13.md"
    _write_session_metadata(null_pane, {
        "sid": "S-testuser-teamlead-p13",
        "status": "active",
        "pane_id": "~",
        "claude_uuid": "uuid-null-pane",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["repaired"] == 0, (
        f"T-0134: a pane_id-less SessionMd with a genuinely live claude agent "
        f"must be spared. Got repaired={result!r}."
    )
    assert _read_session_metadata(legacy)["status"] == "active"
    assert _read_session_metadata(null_pane)["status"] == "active"


def test_gc_sessions_missing_pane_id_and_dead_flips_to_suspended(tmp_path, monkeypatch):
    """T-0401: the actual phantom-active incident — a SessionMd with no
    recorded ``pane_id`` (so the old T-0134 guard skipped it FOREVER) whose
    window no longer maps to ANY live claude agent must now be flipped.
    Real-world case: ``S-almdudleer-multi_server-TL-p30`` sat ``active`` with
    no pane_id and no live pane for weeks, silently holding its task + mail.
    """
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())  # nothing live

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    phantom = sessions_dir / "S-testuser-multi_server-TL-p30.md"
    _write_session_metadata(phantom, {
        "sid": "S-testuser-multi_server-TL-p30",
        "status": "active",
        "task_id": "~",
        "claude_uuid": "uuid-phantom",
        "started_at": "2026-06-02T10:58:05Z",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["repaired"] == 1
    assert result["sids"] == ["S-testuser-multi_server-TL-p30"]
    after = _read_session_metadata(phantom)
    assert after["status"] == "suspended"
    assert after["suspend_source"] == "gc_sessions"


def test_gc_sessions_skips_live_pane(tmp_path, monkeypatch):
    """status:active with a matching live claude agent is left alone."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {"S-testuser-multi_server-p5"})

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


def test_gc_sessions_dead_pane_with_lingering_bash_flips(tmp_path, monkeypatch):
    """T-0401/T-0397: a tmux pane can OUTLIVE the claude process that died in
    it (falls back to a bare shell). Pane existence alone must no longer
    spare the md — only a LIVE claude agent does. Regression guard for the
    gap the old ``pane_id in live_pane_ids`` check left open (it only checked
    the pane existed, never that claude was still running inside it).
    """
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    # The pane is still there (tmux never closed it), but no claude process
    # is running inside it any more — _live_agent_sids correctly excludes it.
    monkeypatch.setattr(S, "list_panes", lambda: [_fake_pane(pane_id="%5", window="multi_server")])
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())

    sessions_dir = tmp_path / "data" / "test-project" / "sessions"
    dead_claude_md = sessions_dir / "S-testuser-multi_server-p5.md"
    _write_session_metadata(dead_claude_md, {
        "sid": "S-testuser-multi_server-p5",
        "status": "active",
        "pane_id": "%5",
        "task_id": "T-0099",
    })

    result = S.gc_sessions(cfg, "test-project")
    assert result["repaired"] == 1
    assert _read_session_metadata(dead_claude_md)["status"] == "suspended"


def test_gc_sessions_skips_archived(tmp_path, monkeypatch):
    """archived: true is operator intent — janitor must not touch it."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())

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
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())

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
    # T-0227: the loser was a DEAD-pane (suspended) → crash-only strip, NOT a live
    # race → was_live False → binding_gc_tick stays silent (no operator alert).
    assert result["details"][0]["was_live"] is False


def test_gc_stale_bindings_flags_live_loser_for_surface(tmp_path, monkeypatch):
    """T-0227: when BOTH dup claimants are LIVE (the real concurrent-spawn race,
    not a crash artifact), the stripped loser is flagged was_live=True so
    binding_gc_tick surfaces it — that loser is still running claude against the
    shared worktree (the co-edit hazard) until the operator kills it."""
    import bot_squad_worker.sessions as S

    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    # BOTH live: winner (later started) %5/multi_server, loser (earlier) %60/dev.
    monkeypatch.setattr(S, "list_panes", lambda: [
        _fake_pane(pane_id="%5", window="multi_server"),
        _fake_pane(pane_id="%60", window="dev"),
    ])
    sess = tmp_path / "data" / "test-project" / "sessions"
    _write_session_metadata(sess / "S-testuser-multi_server-p5.md", {
        "sid": "S-testuser-multi_server-p5", "status": "active",
        "task_id": "T-0026", "started_at": "2026-05-20T10:00:00Z"})
    _write_session_metadata(sess / "S-testuser-dev-p60.md", {
        "sid": "S-testuser-dev-p60", "status": "active",
        "task_id": "T-0026", "started_at": "2026-05-15T11:18:04Z"})

    result = S.gc_stale_bindings(cfg, "test-project")
    assert result["stripped"] == 1
    d = result["details"][0]
    assert d["sid"] == "S-testuser-dev-p60"
    assert d["winner"] == "S-testuser-multi_server-p5"
    assert d["was_live"] is True  # the genuine concurrent-live race → surfaceable


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


# ---------------------------------------------------------------------------
# T-0197 — prod-teamlead + qa as first-class spawnable roles.
# ---------------------------------------------------------------------------

def test_derive_role_prod_teamlead_window_markers():
    from bot_squad_worker.sessions import _derive_role
    for win in (
        "prod-tl",
        "prod_tl",
        "prod-teamlead",
        "prod_teamlead",
        "bot-squad-prod-tl",
        "bot_squad_prod_teamlead",
        "PROD-TL",
    ):
        assert _derive_role(win, None, None) == "prod-teamlead", win


def test_derive_role_prod_tl_precedence_over_plain_tl():
    """A `…-prod-tl` window also ends in `tl`; prod-TL must win, not teamlead."""
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("bot-squad-prod-tl", "~", "~") == "prod-teamlead"
    # …but `prod-ops-tl` has no `prod` adjacent to the trailing `-tl`, so it
    # stays a (dev-side) teamlead — guards the existing test_..._tl_window_markers.
    assert _derive_role("prod-ops-tl", "~", "~") == "teamlead"


def test_derive_role_qa_window_markers():
    from bot_squad_worker.sessions import _derive_role
    for win in ("qa", "bot-squad-qa", "signal_tracker_qa", "QA"):
        assert _derive_role(win, None, None) == "qa", win


def test_derive_role_qa_false_positive_suffix_is_dev():
    """A window merely ending in `qa` without a separator is a dev, not qa."""
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("vodqa", "~", "~") == "dev"  # no separator ⟹ dev
    assert _derive_role("qa-runner", "~", "~") == "dev"  # marker must be a suffix


# ---------------------------------------------------------------------------
# T-0478 — user-conversation as a first-class system-spawned role.
# ---------------------------------------------------------------------------

def test_derive_role_user_conversation_window_markers():
    from bot_squad_worker.sessions import _derive_role
    for win in (
        "user-conversation",
        "user_conversation",
        "USER-CONVERSATION",
        "gu_a1b2c3-user-conversation",   # gid-prefixed (the real spawn shape)
        "gu_deadbeef_user_conversation",  # underscore separators throughout
    ):
        assert _derive_role(win, None, None) == "user-conversation", win


def test_derive_role_user_conversation_suffix_wins_over_gid_content():
    """A gid that itself contains another marker (e.g. `qa`) must NOT flip the
    role — the `user-conversation` suffix is authoritative."""
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("gu_qa-user-conversation", "~", "~") == "user-conversation"
    assert _derive_role("gu_tl-user-conversation", "~", "~") == "user-conversation"


def test_derive_role_user_conversation_false_positive_is_dev():
    """The marker must be a suffix and carry the `user` stem."""
    from bot_squad_worker.sessions import _derive_role
    assert _derive_role("user-conversation-extra", "~", "~") == "dev"  # not a suffix
    assert _derive_role("conversation", "~", "~") == "dev"  # no `user` stem


def test_user_conversation_window_builds_and_validates():
    from bot_squad_worker.sessions import user_conversation_window
    from bot_squad_worker.actions import ActionError
    assert user_conversation_window("gu_a1b2c3") == "gu_a1b2c3-user-conversation"
    # Reject anything that isn't a single safe segment (shell/tmux/path safety).
    for bad in ("", "gu a", "gu;rm", "../x", "a/b", "$(x)"):
        with pytest.raises(ActionError):
            user_conversation_window(bad)


def _write_session_md(sess_dir, sid):
    """Minimal session md (just the sid) under sess_dir, mirroring the seed md a
    fresh task-less spawn writes before the SessionStart hook stamps status."""
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / f"{sid}.md").write_text(f"---\nsid: {sid}\n---\n")


def test_window_from_sid_recovers_window():
    """The window is recoverable from the immutable SID (hook-proof), even when
    the gid carries '-' / '-p' (rsplit on the LAST -p, split(.., 2))."""
    import bot_squad_worker.sessions as S

    assert S._window_from_sid("S-u-gu_a1-user-conversation-p7") == "gu_a1-user-conversation"
    # gid containing a '-p' run must not confuse the pane split.
    assert (S._window_from_sid("S-u-gu-p1-user-conversation-p3")
            == "gu-p1-user-conversation")
    # No pane segment ⟹ "".
    assert S._window_from_sid("garbage") == ""


def test_live_user_conversation_sid_matches_live_attendant(tmp_path, monkeypatch):
    """live_user_conversation_sid identifies the attendant from this project's
    session mds (window derived from the SID) and confirms liveness via the
    process scan — independent of WHICH tmux session the pane lives in and of
    md-status timing (T-0478 reopened-fix)."""
    import types
    import bot_squad_worker.sessions as S

    data = tmp_path / "data"
    cfg = types.SimpleNamespace(data_dir=data)
    gid = "gu_a1b2c3"
    win = S.user_conversation_window(gid)
    sess_dir = data / "tp" / "sessions"
    att_sid = f"S-u-{win}-p7"
    other_sid = f"S-u-{S.user_conversation_window('gu_other')}-p8"
    _write_session_md(sess_dir, att_sid)
    _write_session_md(sess_dir, other_sid)
    # Both have md; only the attendant's pane runs a live claude.
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {att_sid, other_sid})
    assert S.live_user_conversation_sid(cfg, "tp", gid) == att_sid

    # REGRESSION: a session whose pane lives in the per-INITIATIVE tmux session
    # (not the bare slug session) must STILL be found — the old slug-scoped
    # pane-scan missed it and fanned out a duplicate. The md-scan + process-scan
    # path is tmux-session-agnostic, so this passes by construction (the md is
    # in data/<slug>/sessions/ regardless of the pane's tmux session).

    # Dead pane (md present but no live claude) ⟹ no attendant.
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    assert S.live_user_conversation_sid(cfg, "tp", gid) is None

    # Live, but no md for this gid ⟹ None.
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {att_sid})
    assert S.live_user_conversation_sid(cfg, "tp", "gu_nobody") is None

    # Another project's attendant (md under a different slug dir) never leaks in.
    assert S.live_user_conversation_sid(cfg, "other-slug", gid) is None


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


# ---------------------------------------------------------------------------
# T-0128: persist parent_sid at spawn time + list it + backfill legacy
# ---------------------------------------------------------------------------

def test_spawn_stamps_parent_sid(tmp_path, monkeypatch):
    """T-0128: spawn() writes the requesting session's SID as parent_sid
    into the new session md frontmatter (survives worker restart)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%5|devwin|2222|{repo}|claude|test-project\n", "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = spawn(cfg, "test-project", "devwin", parent_sid="S-testuser-tl-p1")
    sid = result["sid"]
    assert sid == "S-testuser-devwin-p5"
    md_path = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    meta = _read_session_metadata(md_path)
    assert meta is not None
    assert meta.get("parent_sid") == "S-testuser-tl-p1"


def test_spawn_without_parent_sid_omits_field(tmp_path, monkeypatch):
    """T-0128: backward-compat — a spawn with no parent_sid leaves the field
    unset (legacy callers keep working; backfill fills it later)."""
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
    meta = _read_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / f"{result['sid']}.md"
    )
    assert meta is not None
    assert not meta.get("parent_sid")


def test_spawn_does_not_stamp_self_as_parent(tmp_path, monkeypatch):
    """T-0128: a parent_sid equal to the new session's own SID is ignored
    (a session is never its own parent)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%5|devwin|2222|{repo}|claude|test-project\n", "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = spawn(cfg, "test-project", "devwin",
                   parent_sid="S-testuser-devwin-p5")
    meta = _read_session_metadata(
        cfg.data_dir / "test-project" / "sessions" / f"{result['sid']}.md"
    )
    assert meta is not None
    assert not meta.get("parent_sid")


def test_list_sessions_returns_parent_sid(tmp_path, monkeypatch):
    """T-0128: list_sessions surfaces parent_sid for active and suspended
    rows, and "" for a legacy row lacking the field."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # Active pane md (carries parent_sid).
    _write_session_metadata(sessions_dir / "S-testuser-devwin-p2.md", {
        "sid": "S-testuser-devwin-p2",
        "status": "active",
        "window": "devwin",
        "cwd": str(repo),
        "task_id": "T-0001",
        "parent_sid": "S-testuser-tl-p1",
    })
    # Suspended md (carries parent_sid).
    _write_session_metadata(sessions_dir / "S-testuser-old-p9.md", {
        "sid": "S-testuser-old-p9",
        "status": "suspended",
        "window": "old",
        "cwd": str(repo),
        "parent_sid": "S-testuser-tl-p1",
    })
    # Legacy suspended md (no parent_sid).
    _write_session_metadata(sessions_dir / "S-testuser-legacy-p8.md", {
        "sid": "S-testuser-legacy-p8",
        "status": "suspended",
        "window": "legacy",
        "cwd": str(repo),
    })

    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%2|devwin|1234|{repo}|claude|test-project\n", "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    rows = list_sessions(cfg, "test-project")
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["S-testuser-devwin-p2"]["parent_sid"] == "S-testuser-tl-p1"
    assert by_sid["S-testuser-old-p9"]["parent_sid"] == "S-testuser-tl-p1"
    assert by_sid["S-testuser-legacy-p8"]["parent_sid"] == ""
    # T-0647: neither of these was backfill-guessed — both are genuine
    # spawn-time-stamped values, so the heuristic flag must read False.
    assert by_sid["S-testuser-devwin-p2"]["parent_sid_heuristic"] is False
    assert by_sid["S-testuser-old-p9"]["parent_sid_heuristic"] is False


def test_backfill_parent_sid_fills_legacy_and_preserves_existing(tmp_path, monkeypatch):
    """T-0128: backfill populates parent_sid for a legacy session via the
    team heuristic, NEVER overwrites an existing value, and leaves a session
    with no resolvable parent untouched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S
    import bot_squad_worker.teams as T

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # Legacy dev — should be filled via the heuristic.
    _write_session_metadata(sessions_dir / "S-testuser-dev-p2.md", {
        "sid": "S-testuser-dev-p2", "status": "active", "window": "dev",
        "cwd": str(repo), "task_id": "T-0001",
    })
    # Already has parent_sid — must NOT be overwritten (fill-once).
    _write_session_metadata(sessions_dir / "S-testuser-dev2-p3.md", {
        "sid": "S-testuser-dev2-p3", "status": "active", "window": "dev2",
        "cwd": str(repo), "task_id": "T-0002",
        "parent_sid": "S-testuser-PRESET-p0",
    })
    # No resolvable parent (heuristic returns None) — left untouched.
    _write_session_metadata(sessions_dir / "S-testuser-root-p4.md", {
        "sid": "S-testuser-root-p4", "status": "active", "window": "root",
        "cwd": str(repo),
    })

    def fake_tl_for_sid(cfg_, slug_, sid_):
        if sid_ == "S-testuser-root-p4":
            return None
        return "S-testuser-tl-p1"

    monkeypatch.setattr(T, "tl_for_sid", fake_tl_for_sid)

    res = S.backfill_parent_sid(cfg, "test-project")
    assert res["ok"] is True
    assert res["filled"] == 1

    m1 = _read_session_metadata(sessions_dir / "S-testuser-dev-p2.md")
    assert m1["parent_sid"] == "S-testuser-tl-p1"
    # T-0647: a backfilled parent is flagged as a guess, not a genuine link.
    assert S._parent_sid_heuristic_of(m1) is True
    # Existing value preserved.
    m2 = _read_session_metadata(sessions_dir / "S-testuser-dev2-p3.md")
    assert m2["parent_sid"] == "S-testuser-PRESET-p0"
    assert S._parent_sid_heuristic_of(m2) is False
    # No-parent session left unset.
    m3 = _read_session_metadata(sessions_dir / "S-testuser-root-p4.md")
    assert not m3.get("parent_sid")

    # Idempotent: a second pass fills nothing more.
    res2 = S.backfill_parent_sid(cfg, "test-project")
    assert res2["filled"] == 0


def test_backfill_parent_sid_gates_nondev_and_heals_operator(tmp_path, monkeypatch):
    """T-0128: backfill only stamps DEV rows (task_id); never invents a parent
    for a non-dev row; self-heals an operator (always a root) unconditionally
    and other non-dev rows when their stored parent matches the heuristic's
    fingerprint; and PRESERVES a genuine spawn-time non-dev parent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    import bot_squad_worker.sessions as S
    import bot_squad_worker.teams as T

    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # Operator with a stale (wrongly-backfilled) parent_sid → cleared always.
    _write_session_metadata(sessions_dir / "S-testuser-operator-p5.md", {
        "sid": "S-testuser-operator-p5", "status": "active", "window": "operator",
        "cwd": str(repo), "parent_sid": "S-testuser-tl-p1",
    })
    # Non-dev TL with NO parent → must NOT be stamped even if tl_for_sid resolves.
    _write_session_metadata(sessions_dir / "S-testuser-tl-p1.md", {
        "sid": "S-testuser-tl-p1", "status": "active", "window": "multi-tl",
        "cwd": str(repo),
    })
    # Non-dev TL whose parent == heuristic fingerprint → cleared (artifact).
    _write_session_metadata(sessions_dir / "S-testuser-otl-p7.md", {
        "sid": "S-testuser-otl-p7", "status": "active", "window": "other-tl",
        "cwd": str(repo), "parent_sid": "S-testuser-tl-p1",
    })
    # Non-dev TL whose parent is a genuine spawn-time op (≠ heuristic) → KEPT.
    _write_session_metadata(sessions_dir / "S-testuser-spawned-tl-p8.md", {
        "sid": "S-testuser-spawned-tl-p8", "status": "active", "window": "spawned-tl",
        "cwd": str(repo), "parent_sid": "S-testuser-operator-p5",
    })
    # Dev (task_id) → stamped as a control.
    _write_session_metadata(sessions_dir / "S-testuser-dev-p2.md", {
        "sid": "S-testuser-dev-p2", "status": "active", "window": "dev",
        "cwd": str(repo), "task_id": "T-0001",
    })

    monkeypatch.setattr(T, "tl_for_sid", lambda c, s, sid: "S-testuser-tl-p1")

    res = S.backfill_parent_sid(cfg, "test-project")
    assert res["corrected"] == 2   # operator + artifact TL cleared
    assert res["filled"] == 1      # only the dev row stamped

    op = _read_session_metadata(sessions_dir / "S-testuser-operator-p5.md")
    assert not S._parent_sid_of(op)          # operator is a root again
    assert S._parent_sid_heuristic_of(op) is False   # T-0647: flag cleared too
    tl = _read_session_metadata(sessions_dir / "S-testuser-tl-p1.md")
    assert not S._parent_sid_of(tl)          # non-dev never gets invented parent
    otl = _read_session_metadata(sessions_dir / "S-testuser-otl-p7.md")
    assert not S._parent_sid_of(otl)         # artifact fingerprint cleared
    assert S._parent_sid_heuristic_of(otl) is False  # T-0647: flag cleared too
    spawned = _read_session_metadata(sessions_dir / "S-testuser-spawned-tl-p8.md")
    assert spawned["parent_sid"] == "S-testuser-operator-p5"   # genuine parent kept
    dev = _read_session_metadata(sessions_dir / "S-testuser-dev-p2.md")
    assert dev["parent_sid"] == "S-testuser-tl-p1"
    assert S._parent_sid_heuristic_of(dev) is True   # T-0647: freshly backfilled → flagged

    # Idempotent: cleared rows stay clean, genuine parent kept, nothing re-filled.
    res2 = S.backfill_parent_sid(cfg, "test-project")
    assert res2["corrected"] == 0
    assert res2["filled"] == 0


# ---------------------------------------------------------------------------
# T-0220: suspended-row role badge validates cwd, not just window name
# ---------------------------------------------------------------------------

def _suspended_cwd_cfg(tmp_path, monkeypatch):
    """Shared setup: a project repo + empty pane list (everything suspended)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    def fake_run(args, **kwargs):
        # No live panes → all sessions surface from the suspended-md loop.
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    return cfg, repo


def test_suspended_role_neutralized_on_cwd_mismatch(tmp_path, monkeypatch):
    """A suspended row with an operator window but a cwd OUTSIDE the project
    must not render the elevated badge: role falls back to "dev" and the row
    is flagged role_cwd_mismatch=True for audit."""
    cfg, repo = _suspended_cwd_cfg(tmp_path, monkeypatch)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sessions_dir / "S-testuser-test-operator-p7.md", {
        "sid": "S-testuser-test-operator-p7",
        "status": "suspended",
        "window": "test-operator",
        "cwd": "/tmp/somewhere-else",
        "claude_uuid": "uuid-op",
        "suspended_at": "2026-06-19T10:00:00Z",
    })

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["role"] == "dev"
    assert rows[0]["role_cwd_mismatch"] is True


def test_suspended_role_preserved_on_cwd_match(tmp_path, monkeypatch):
    """A suspended operator whose cwd is the workspace parent of the dev clone
    (the legitimate operator location) keeps its role and is NOT flagged."""
    cfg, repo = _suspended_cwd_cfg(tmp_path, monkeypatch)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sessions_dir / "S-testuser-test-operator-p7.md", {
        "sid": "S-testuser-test-operator-p7",
        "status": "suspended",
        "window": "test-operator",
        # operator lives in the workspace parent of repo (allow_parent match)
        "cwd": str(repo.parent),
        "claude_uuid": "uuid-op",
        "suspended_at": "2026-06-19T10:00:00Z",
    })

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["role"] == "operator"
    assert not rows[0].get("role_cwd_mismatch")


def test_suspended_teamlead_cwd_match_preserved(tmp_path, monkeypatch):
    """A suspended teamlead whose cwd IS the repo keeps the elevated role."""
    cfg, repo = _suspended_cwd_cfg(tmp_path, monkeypatch)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sessions_dir / "S-testuser-mine-TL-p3.md", {
        "sid": "S-testuser-mine-TL-p3",
        "status": "suspended",
        "window": "mine-TL",
        "cwd": str(repo),
        "claude_uuid": "uuid-tl",
        "suspended_at": "2026-06-19T10:00:00Z",
    })

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["role"] == "teamlead"
    assert not rows[0].get("role_cwd_mismatch")


def test_suspended_legacy_empty_cwd_no_false_positive(tmp_path, monkeypatch):
    """A legacy suspended row with no persisted cwd keeps its window-derived
    role (we can't validate what isn't there) and is NOT flagged."""
    cfg, repo = _suspended_cwd_cfg(tmp_path, monkeypatch)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sessions_dir / "S-testuser-old-TL-p1.md", {
        "sid": "S-testuser-old-TL-p1",
        "status": "suspended",
        "window": "old-TL",
        "cwd": "",
        "claude_uuid": "uuid-legacy",
        "suspended_at": "2026-06-19T10:00:00Z",
    })

    rows = list_sessions(cfg, "test-project")
    assert len(rows) == 1
    assert rows[0]["role"] == "teamlead"
    assert not rows[0].get("role_cwd_mismatch")


# ── T-0447 (#4): archiving/merging a session cascade-frees its sidecars ────────

def _seed_sidecars(tmp_path, slug, sid):
    """Create the full per-SID sidecar set (peer-bus triple + telemetry json)."""
    chat = tmp_path / "data" / slug / "_chat"
    chat.mkdir(parents=True, exist_ok=True)
    (chat / f"inbox-{sid}.log").write_text("hi\n")
    (chat / f"seen-{sid}").write_text("0")
    (chat / f"heartbeat-{sid}").write_text("")
    tel = tmp_path / "data" / slug / "_worker" / "telemetry"
    tel.mkdir(parents=True, exist_ok=True)
    (tel / f"{sid}.json").write_text("{}")
    return chat, tel


def _sidecars_present(chat, tel, sid):
    return (
        (chat / f"inbox-{sid}.log").exists()
        or (chat / f"seen-{sid}").exists()
        or (chat / f"heartbeat-{sid}").exists()
        or (tel / f"{sid}.json").exists()
    )


def test_archive_session_cascade_reaps_sidecars(tmp_path, monkeypatch):
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    slug = "test-project"
    sid = "S-testuser-feat-p1"
    sess = tmp_path / "data" / slug / "sessions" / f"{sid}.md"
    sess.write_text(f"---\nsid: {sid}\nstatus: suspended\npane_id: '%9'\n---\n")
    chat, tel = _seed_sidecars(tmp_path, slug, sid)
    assert _sidecars_present(chat, tel, sid)

    res = S.archive_session(cfg, slug, sid)
    assert res["archived"] is True
    assert not _sidecars_present(chat, tel, sid), "all sidecars must be freed on archive"


def test_archive_session_reap_partial_set_no_error(tmp_path, monkeypatch):
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    slug = "test-project"
    sid = "S-testuser-nosidecars-p2"
    sess = tmp_path / "data" / slug / "sessions" / f"{sid}.md"
    sess.write_text(f"---\nsid: {sid}\nstatus: suspended\npane_id: '%8'\n---\n")
    # No sidecars seeded at all → archive must still succeed cleanly.
    res = S.archive_session(cfg, slug, sid)
    assert res["archived"] is True


def test_dedup_sessions_reaps_loser_sidecars(tmp_path, monkeypatch):
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg = _make_cfg(tmp_path)
    slug = "test-project"
    sdir = tmp_path / "data" / slug / "sessions"
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    keeper = "S-testuser-feat-p10"
    loser = "S-testuser-feat-p9"
    (sdir / f"{keeper}.md").write_text(
        f"---\nsid: {keeper}\nstatus: suspended\nclaude_uuid: {uuid}\n"
        f"started_at: 2026-06-21T10:00:00Z\n---\n")
    (sdir / f"{loser}.md").write_text(
        f"---\nsid: {loser}\nstatus: suspended\nclaude_uuid: {uuid}\n"
        f"started_at: 2026-06-20T10:00:00Z\n---\n")
    chat, tel = _seed_sidecars(tmp_path, slug, loser)

    res = S.dedup_sessions(cfg, slug, dry_run=False)
    assert res["merged_count"] == 1
    assert res["merges"][0]["loser"] == loser
    assert not _sidecars_present(chat, tel, loser), "merged-away loser's sidecars must be freed"


# ---------------------------------------------------------------------------
# T-0509 (M11/F11.2): user-session role MORPHING — user → dev / teamlead /
# operator IN PLACE. "sessions are transient, system is persistent."
# ---------------------------------------------------------------------------

def _morph_cfg(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    (cfg.data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
    return cfg, repo


def _write_user_session(cfg, repo, sid, *, window="claude", status="active"):
    md = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    _write_session_metadata(md, {
        "sid": sid, "status": status, "window": window, "cwd": str(repo),
        "claude_uuid": "u-" + sid[-2:], "task_id": "~", "initiative": "~",
        "started_at": "2026-06-28T00:00:00Z",
    })
    return md


def test_role_of_stored_role_overrides_window():
    """T-0509: an explicit stored ``role`` wins over the window-derived role."""
    import bot_squad_worker.sessions as S
    # window 'claude' would derive 'dev', but a stored operator role overrides.
    assert S._role_of({"window": "claude", "role": "operator"}) == "operator"
    assert S._role_of({"window": "x-dev", "role": "teamlead"}) == "teamlead"


def test_role_of_unset_falls_through_to_derive():
    """No stored role (absent / ``~``) ⇒ derive from the window marker."""
    import bot_squad_worker.sessions as S
    assert S._role_of({"window": "x-tl"}) == "teamlead"
    assert S._role_of({"window": "x-operator", "role": "~"}) == "operator"
    assert S._role_of({"window": "claude"}) == "dev"


def test_morph_user_to_dev_sets_role_and_task(tmp_path):
    """user → dev (on taking a task): stamps role=dev + the primary task_id."""
    import bot_squad_worker.sessions as S
    cfg, repo = _morph_cfg(tmp_path)
    md = _write_user_session(cfg, repo, "S-alice-claude-p1")

    res = S.morph_session(cfg, "test-project", "S-alice-claude-p1", "dev",
                          task_id="T-0042")
    assert res["ok"] and res["role"] == "dev" and res["task_id"] == "T-0042"
    meta = _read_session_metadata(md)
    assert meta["role"] == "dev" and meta["task_id"] == "T-0042"
    # role is now honored by the canonical read.
    assert S._role_of(meta) == "dev"


def test_morph_user_to_teamlead(tmp_path):
    """user → team-lead (on spawning teammates): stamps role=teamlead."""
    import bot_squad_worker.sessions as S
    cfg, repo = _morph_cfg(tmp_path)
    md = _write_user_session(cfg, repo, "S-alice-claude-p1")

    res = S.morph_session(cfg, "test-project", "S-alice-claude-p1", "teamlead",
                          initiative="process-paradigm.md")
    assert res["role"] == "teamlead" and res["initiative"] == "process-paradigm.md"
    meta = _read_session_metadata(md)
    assert S._role_of(meta) == "teamlead"


def test_morph_user_to_operator_clears_task(tmp_path, monkeypatch):
    """user → operator (none running): stamps role=operator + clears any task
    (the operator never holds a single ticket)."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "list_panes", lambda: [])  # no canonical operator
    cfg, repo = _morph_cfg(tmp_path)
    md = _write_user_session(cfg, repo, "S-alice-claude-p1")
    # even if it carried a stray task, operator morph clears it.
    meta0 = _read_session_metadata(md); meta0["task_id"] = "T-0001"
    _write_session_metadata(md, meta0)

    res = S.morph_session(cfg, "test-project", "S-alice-claude-p1", "operator")
    assert res["role"] == "operator" and res["task_id"] is None
    meta = _read_session_metadata(md)
    assert meta["role"] == "operator"
    # operator-identity SSOT now sees it.
    from bot_squad_worker.dispatch import live_operator_sids
    assert "S-alice-claude-p1" in live_operator_sids(cfg, "test-project")


def test_morph_operator_refused_when_one_running(tmp_path, monkeypatch):
    """Operator singleton (T-0472): a second operator morph is refused while a
    live operator already holds the project."""
    import bot_squad_worker.sessions as S
    from bot_squad_worker.actions import ActionError
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg, repo = _morph_cfg(tmp_path)
    # an existing live operator (registered).
    _write_user_session(cfg, repo, "S-alice-operator-p9", window="operator")
    _write_user_session(cfg, repo, "S-bob-claude-p1")

    with pytest.raises(ActionError, match="operator already running"):
        S.morph_session(cfg, "test-project", "S-bob-claude-p1", "operator")


def test_morph_operator_re_morph_excludes_self(tmp_path, monkeypatch):
    """Re-morphing the SAME session to operator is a no-op, not a duplicate —
    the singleton check excludes self."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg, repo = _morph_cfg(tmp_path)
    md = _write_user_session(cfg, repo, "S-alice-claude-p1")
    S.morph_session(cfg, "test-project", "S-alice-claude-p1", "operator")
    # second morph by the same session must still succeed.
    res = S.morph_session(cfg, "test-project", "S-alice-claude-p1", "operator")
    assert res["role"] == "operator"


def test_morph_operator_rejects_dev_task(tmp_path, monkeypatch):
    """T-0523: an operator orchestrates and must never self-bind a dev task."""
    import bot_squad_worker.sessions as S
    from bot_squad_worker.actions import ActionError
    monkeypatch.setattr(S, "list_panes", lambda: [])
    cfg, repo = _morph_cfg(tmp_path)
    _write_user_session(cfg, repo, "S-alice-claude-p1")
    with pytest.raises(ActionError, match="must not bind a dev task"):
        S.morph_session(cfg, "test-project", "S-alice-claude-p1", "operator",
                        task_id="T-0042")


def test_morph_rejects_unknown_role(tmp_path):
    import bot_squad_worker.sessions as S
    from bot_squad_worker.actions import ActionError
    cfg, repo = _morph_cfg(tmp_path)
    _write_user_session(cfg, repo, "S-alice-claude-p1")
    with pytest.raises(ActionError, match="role must be one of"):
        S.morph_session(cfg, "test-project", "S-alice-claude-p1", "qa")


def test_morph_upserts_md_for_unregistered_session(tmp_path):
    """A manually-launched user session has NO md yet — morph creates it from
    the live-pane fields the CLI passes."""
    import bot_squad_worker.sessions as S
    cfg, repo = _morph_cfg(tmp_path)
    sid = "S-alice-claude-p7"
    md = cfg.data_dir / "test-project" / "sessions" / f"{sid}.md"
    assert not md.exists()

    res = S.morph_session(cfg, "test-project", sid, "dev", task_id="T-0042",
                          window="claude", cwd=str(repo), claude_uuid="uu-7")
    assert res["created"] is True and res["role"] == "dev"
    meta = _read_session_metadata(md)
    assert meta["role"] == "dev" and meta["task_id"] == "T-0042"
    assert meta["window"] == "claude" and meta["claude_uuid"] == "uu-7"


# ---------------------------------------------------------------------------
# T-0575 — recycle-v2 resume side: resumable_sessions() finder +
# recycled_resume_eligible() (<50k gate) + resume() consuming the state
# ---------------------------------------------------------------------------

def _write_transcript(home: Path, uuid: str, window_tokens: int | None) -> None:
    """Seed a minimal Claude transcript jsonl under <home>/.claude/projects."""
    import json
    d = home / ".claude" / "projects" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    lines = ['{"type": "user", "message": {}}']
    if window_tokens is not None:
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"model": "m", "usage": {
                "input_tokens": window_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            }},
        }))
    (d / f"{uuid}.jsonl").write_text("\n".join(lines) + "\n")


def test_resumable_sessions_finds_recycled_newest_first(tmp_path):
    """Only suspended+resumable mds with a real claude_uuid are returned,
    sorted newest recycled_at first; task_id '~' normalises to None."""
    import bot_squad_worker.sessions as S
    cfg = _make_cfg(tmp_path)
    sess = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sess / "S-u-older-p1.md", {
        "sid": "S-u-older-p1", "status": "suspended", "window": "older",
        "cwd": "/tmp", "claude_uuid": "uu-1", "resumable": True,
        "recycled_at": "2026-07-04T10:00:00Z", "resume_hint": "h1",
        "task_id": "T-0001",
    })
    _write_session_metadata(sess / "S-u-newer-p2.md", {
        "sid": "S-u-newer-p2", "status": "suspended", "window": "newer",
        "cwd": "/tmp", "claude_uuid": "uu-2", "resumable": True,
        "recycled_at": "2026-07-04T12:00:00Z", "resume_hint": "h2",
        "task_id": "~",
    })
    # Excluded: still active, suspended without the resumable stamp, and
    # resumable but uuid-less (--resume has no target).
    _write_session_metadata(sess / "S-u-live-p3.md", {
        "sid": "S-u-live-p3", "status": "active", "window": "live",
        "cwd": "/tmp", "claude_uuid": "uu-3", "resumable": True,
    })
    _write_session_metadata(sess / "S-u-plain-p4.md", {
        "sid": "S-u-plain-p4", "status": "suspended", "window": "plain",
        "cwd": "/tmp", "claude_uuid": "uu-4",
    })
    _write_session_metadata(sess / "S-u-nouuid-p5.md", {
        "sid": "S-u-nouuid-p5", "status": "suspended", "window": "nouuid",
        "cwd": "/tmp", "claude_uuid": "~", "resumable": True,
        "recycled_at": "2026-07-04T13:00:00Z",
    })

    rows = S.resumable_sessions(cfg, "test-project")
    assert [r["sid"] for r in rows] == ["S-u-newer-p2", "S-u-older-p1"]
    assert rows[0]["task_id"] is None
    assert rows[1]["task_id"] == "T-0001"
    assert rows[1]["claude_uuid"] == "uu-1"
    assert rows[1]["window"] == "older"
    assert rows[1]["resume_hint"] == "h1"


def test_resumable_sessions_missing_dir_returns_empty(tmp_path):
    import types
    import bot_squad_worker.sessions as S
    cfg = types.SimpleNamespace(data_dir=tmp_path / "nope")
    assert S.resumable_sessions(cfg, "test-project") == []


def test_recycled_resume_eligible_under_budget(tmp_path):
    import bot_squad_worker.sessions as S
    _write_transcript(tmp_path, "uu-small", 12_000)
    ok, tokens = S.recycled_resume_eligible("uu-small", user_home=str(tmp_path))
    assert ok is True
    assert tokens == 12_000


def test_recycled_resume_eligible_over_budget(tmp_path):
    """≥50k remembered context → fresh spawn beats resume (stakeholder rule)."""
    import bot_squad_worker.sessions as S
    _write_transcript(tmp_path, "uu-fat", 120_000)
    ok, tokens = S.recycled_resume_eligible("uu-fat", user_home=str(tmp_path))
    assert ok is False
    assert tokens == 120_000


def test_recycled_resume_eligible_missing_transcript(tmp_path):
    """No transcript on disk → --resume would fail; not eligible."""
    import bot_squad_worker.sessions as S
    ok, tokens = S.recycled_resume_eligible("uu-gone", user_home=str(tmp_path))
    assert ok is False
    assert tokens is None


def test_recycled_resume_eligible_no_usage_line_stays_eligible(tmp_path):
    """A transcript with no assistant usage line measures None but stays
    eligible — a compact-terminate-remembered session is small by design."""
    import bot_squad_worker.sessions as S
    _write_transcript(tmp_path, "uu-nousage", None)
    ok, tokens = S.recycled_resume_eligible("uu-nousage", user_home=str(tmp_path))
    assert ok is True
    assert tokens is None


def test_recycled_resume_eligible_placeholder_uuid(tmp_path):
    import bot_squad_worker.sessions as S
    assert S.recycled_resume_eligible("~", user_home=str(tmp_path)) == (False, None)
    assert S.recycled_resume_eligible(None, user_home=str(tmp_path)) == (False, None)


def test_resume_clears_recycle_remembered_state(tmp_path, monkeypatch):
    """T-0575: a resurrect CONSUMES the recycle-v2 remembered state — the
    resumed md must not carry resumable/recycled_at/resume_hint, so the
    finder never offers an already-resumed session again."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sessions_dir / "S-testuser-recwin-p5.md", {
        "sid": "S-testuser-recwin-p5",
        "status": "suspended",
        "window": "recwin",
        "cwd": str(repo),
        "claude_uuid": "uu-rec",
        "suspended_at": "2026-07-04T10:00:00Z",
        "resumable": True,
        "recycled_at": "2026-07-04T10:00:00Z",
        "resume_hint": "idle cache-window recycle (compacted)",
    })

    new_window_called = [False]

    def fake_run(args, **kwargs):
        if "new-window" in args:
            new_window_called[0] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            if new_window_called[0]:
                return subprocess.CompletedProcess(
                    args, 0, f"%9|recwin|9999|{repo}|claude\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    result = resume(cfg, "test-project", "S-testuser-recwin-p5")
    assert result["ok"] is True
    meta = _read_session_metadata(sessions_dir / f"{result['sid']}.md")
    assert meta["status"] == "active"
    for k in ("resumable", "recycled_at", "resume_hint"):
        assert k not in meta, f"{k} must be consumed by resume"


# ---------------------------------------------------------------------------
# T-0614: descriptive claude session names — spawn/resume pass --name so the
# native /resume picker shows a readable, SID-derived name instead of the
# auto-generated first-message snippet.
# ---------------------------------------------------------------------------

def _extract_claude_name(shell_cmd: str) -> str | None:
    """Parse the launched `bash -lc` string and return the --name value."""
    import shlex as _shlex
    parts = _shlex.split(shell_cmd)
    if "--name" in parts:
        i = parts.index("--name")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def test_spawn_sets_descriptive_claude_name(tmp_path, monkeypatch):
    """T-0614: spawn(window=W, task_id=T) launches claude with
    --name '<W> <T>' — the same descriptive string the SID is derived from —
    so the /resume picker line is readable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    captured: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            captured.append(args[args.index("-lc") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%4|brave-feature|123|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "brave-feature", task_id="T-0614")

    assert captured, "expected a new-window call"
    assert _extract_claude_name(captured[0]) == "brave-feature T-0614"


def test_spawn_sets_claude_name_without_task(tmp_path, monkeypatch):
    """T-0614: taskless spawns (operator/attendant) still get their window
    name as the claude display name."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)

    captured: list[str] = []

    def fake_run(args, **kwargs):
        if "new-window" in args:
            captured.append(args[args.index("-lc") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%4|operator|123|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    spawn(cfg, "test-project", "operator")

    assert captured, "expected a new-window call"
    assert _extract_claude_name(captured[0]) == "operator"


def test_claude_session_name_sanitized():
    """T-0614: the display name is sanitised to a conservative charset before
    it is shell-quoted into the launch command — a hostile/garbled window
    value (cf. the T-0200 stray-quote incident) can't leak metacharacters,
    and an all-garbage value yields '' (caller then omits --name)."""
    from bot_squad_worker.sessions import _claude_session_name
    assert _claude_session_name('w"$(rm -rf)"x', "T-1") == "wrm -rfx T-1"
    assert _claude_session_name("expert", None) == "expert"
    assert _claude_session_name("expert", "~") == "expert"
    assert _claude_session_name("", None) == ""
    assert _claude_session_name('"$()"', None) == ""
    # task id already embedded in the window name → not repeated
    assert _claude_session_name("fix-T-0614-picker", "T-0614") == "fix-T-0614-picker"


def test_resume_sets_descriptive_claude_name(tmp_path, monkeypatch):
    """T-0614: the resurrect path passes --name alongside --resume <uuid> so
    a rotated/resumed session keeps a readable /resume-picker entry (verified
    live 2026-07-05: --name combined with --resume appends a fresh
    custom-title record to the transcript)."""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sdir = cfg.data_dir / "test-project" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sdir / "S-testuser-expert-p7.md", {
        "sid": "S-testuser-expert-p7", "status": "suspended", "window": "expert",
        "cwd": str(repo), "claude_uuid": "u7", "task_id": "T-0100",
        "suspended_at": "2026-05-10T12:00:00Z",
    })

    import bot_squad_worker.sessions as S
    fake = _resume_fake_run(repo, new_pane="%20")
    monkeypatch.setattr(S, "_run", fake)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    resume(cfg, "test-project", "S-testuser-expert-p7")

    assert fake.launched, "expected a new-window call"
    assert "--resume u7" in fake.launched[0]
    assert _extract_claude_name(fake.launched[0]) == "expert T-0100"


def test_resume_claude_name_uses_adopted_task(tmp_path, monkeypatch):
    """T-0614 x T-0166: when resume ADOPTS a primary (expert-rebind), the
    display name carries the NEW task id, not the stripped-out old one."""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path, repo)
    sdir = cfg.data_dir / "test-project" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    _write_session_metadata(sdir / "S-testuser-expert-p7.md", {
        "sid": "S-testuser-expert-p7", "status": "suspended", "window": "expert",
        "cwd": str(repo), "claude_uuid": "u7", "task_id": "~",
        "last_task_id": "T-0001", "suspended_at": "2026-05-10T12:00:00Z",
    })

    import bot_squad_worker.sessions as S
    fake = _resume_fake_run(repo, new_pane="%20")
    monkeypatch.setattr(S, "_run", fake)
    monkeypatch.setattr(S, "_get_current_user", lambda: "testuser")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    resume(cfg, "test-project", "S-testuser-expert-p7", task_id="T-0002")

    assert fake.launched, "expected a new-window call"
    assert _extract_claude_name(fake.launched[0]) == "expert T-0002"
