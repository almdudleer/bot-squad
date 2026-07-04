"""T-0568: window-name repair — sessions must not stay 'bash'/blank.

The tmux window name is the naming SSOT (SID = S-<user>-<window>-p<pane>).
Sessions end up generically named when a human starts claude in an unnamed
window (default name = 'bash'), when a per-user tmux server auto-renames to
the running command, or when a rename drifts the live name away from the
registry. Nothing repaired them, so team status filled with 'bash-pN' rows
(and blank-named panes could never even register an md — hook_my_sid exits
on an empty window name).

``reconcile_window_names`` is the 60s-tick pass that renames generic/blank
live claude panes from the best available source (stored ``window`` field,
task binding) through the same rename core ``sync_session_name`` uses.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker.sessions import (
    PaneInfo,
    _read_session_metadata,
    _write_session_metadata,
)


def _make_cfg(tmp_path: Path) -> Any:
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "backlog").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo").mkdir(exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\nslug = "test-project"\ndisplay_name = "T"\n'
        f'repo_path = "{tmp_path / "repo"}"\ndeploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\nprod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\ntg_chat = "0"\ncreated_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    import types
    return types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir, tg_bot_token="")


@pytest.fixture(autouse=True)
def _user(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: "/nonexistent-home")
    # Tests drive uuid resolution explicitly; default to "no uuid found".
    monkeypatch.setattr(S, "_pane_claude_uuid_from_proc", lambda pid, home: None)


def _pane(pane_id: str, window: str, cwd: str, command: str = "claude"):
    return PaneInfo(pane_id=pane_id, window=window, pid="1234", cwd=cwd,
                    command=command, session="test-project")


def _run_recorder(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        S, "_run",
        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    return calls


# ---------------------------------------------------------------------------
# _is_generic_window
# ---------------------------------------------------------------------------

def test_generic_window_predicate():
    assert S._is_generic_window("bash")
    assert S._is_generic_window("claude")
    assert S._is_generic_window("zsh")
    assert S._is_generic_window("")
    assert S._is_generic_window("  ")
    assert S._is_generic_window("~")
    # claude version-named binaries (agent-teams subagents) are generic too
    assert S._is_generic_window("2.1.139")
    assert not S._is_generic_window("T-0568")
    assert not S._is_generic_window("detector-fp-fix")
    assert not S._is_generic_window("operator")


# ---------------------------------------------------------------------------
# reconcile_window_names
# ---------------------------------------------------------------------------

def test_generic_window_renamed_to_task_id(tmp_path, monkeypatch):
    """A live task-bound pane stuck on 'bash' is renamed to its task id and
    the md migrates to the rotated SID (full SSOT rename, not display-only)."""
    cfg = _make_cfg(tmp_path)
    repo = str(tmp_path / "repo")
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-bash-p1.md", {
        "sid": "S-u-bash-p1", "status": "active", "window": "bash",
        "cwd": repo, "claude_uuid": "u1", "task_id": "T-0042",
    })
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("%1", "bash", repo)])
    calls = _run_recorder(monkeypatch)

    res = S.reconcile_window_names(cfg, "test-project")

    assert any("rename-window" in c and "T-0042" in c for c in calls), calls
    assert (sdir / "S-u-T-0042-p1.md").exists()
    assert not (sdir / "S-u-bash-p1.md").exists()
    meta = _read_session_metadata(sdir / "S-u-T-0042-p1.md")
    assert meta["window"] == "T-0042"
    assert meta["task_id"] == "T-0042"
    assert res["renamed"] and res["renamed"][0]["new_window"] == "T-0042"


def test_generic_window_restored_from_stored_window_field(tmp_path, monkeypatch):
    """Drift case: the registry still holds the good name (md filed under the
    original SID, found via the pane's authoritative /proc uuid) while the live
    window decayed to 'bash'. The stored name wins over the task id."""
    cfg = _make_cfg(tmp_path)
    repo = str(tmp_path / "repo")
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-goodname-p1.md", {
        "sid": "S-u-goodname-p1", "status": "active", "window": "goodname",
        "cwd": repo, "claude_uuid": "uuid-x", "task_id": "T-0042",
    })
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("%1", "bash", repo)])
    monkeypatch.setattr(S, "_pane_claude_uuid_from_proc", lambda pid, home: "uuid-x")
    calls = _run_recorder(monkeypatch)

    S.reconcile_window_names(cfg, "test-project")

    assert any("rename-window" in c and "goodname" in c for c in calls), calls
    # SID rotates back onto the md's own path — no duplicate md appears.
    assert (sdir / "S-u-goodname-p1.md").exists()
    assert len(list(sdir.glob("*.md"))) == 1


def test_blank_window_gets_fallback_name(tmp_path, monkeypatch):
    """A BLANK window name breaks registration entirely (hook_my_sid exits →
    no md is ever written). The pass must give the pane SOME name so the next
    hook fire can register it."""
    cfg = _make_cfg(tmp_path)
    repo = str(tmp_path / "repo")
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("%3", "", repo)])
    calls = _run_recorder(monkeypatch)

    res = S.reconcile_window_names(cfg, "test-project")

    assert any("rename-window" in c and "claude-3" in c for c in calls), calls
    assert res["renamed"]


def test_nongeneric_window_untouched(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    repo = str(tmp_path / "repo")
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-mywork-p1.md", {
        "sid": "S-u-mywork-p1", "status": "active", "window": "mywork",
        "cwd": repo, "claude_uuid": "u1", "task_id": "T-0042",
    })
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("%1", "mywork", repo)])
    calls = _run_recorder(monkeypatch)
    res = S.reconcile_window_names(cfg, "test-project")
    assert not any("rename-window" in c for c in calls)
    assert res["renamed"] == []


def test_generic_window_without_md_or_task_left_alone(tmp_path, monkeypatch):
    """A human's own 'bash' window running claude in the repo has no registry
    record and no binding — nothing meaningful to rename it TO; leave it."""
    cfg = _make_cfg(tmp_path)
    repo = str(tmp_path / "repo")
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("%9", "bash", repo)])
    calls = _run_recorder(monkeypatch)
    res = S.reconcile_window_names(cfg, "test-project")
    assert not any("rename-window" in c for c in calls)
    assert res["renamed"] == []


def test_pane_outside_repo_untouched(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(S, "list_panes", lambda: [_pane("%1", "bash", "/somewhere/else")])
    calls = _run_recorder(monkeypatch)
    res = S.reconcile_window_names(cfg, "test-project")
    assert not any("rename-window" in c for c in calls)
    assert res["renamed"] == []


def test_non_claude_pane_untouched(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    repo = str(tmp_path / "repo")
    monkeypatch.setattr(
        S, "list_panes", lambda: [_pane("%1", "bash", repo, command="bash")])
    calls = _run_recorder(monkeypatch)
    res = S.reconcile_window_names(cfg, "test-project")
    assert not any("rename-window" in c for c in calls)
    assert res["renamed"] == []


def test_registered_in_binding_gc_tick(tmp_path, monkeypatch):
    """The pass must actually run in the 60s reconcile tick."""
    from bot_squad_worker import jobs as J
    from bot_squad_worker import close_hook as CH
    cfg = _make_cfg(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(
        S, "_run",
        lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(CH, "harvest_tick", lambda cfg, slug: {"ok": True})
    monkeypatch.setattr(
        S, "reconcile_window_names",
        lambda cfg, slug: called.append(slug) or {"ok": True, "renamed": []})
    J.binding_gc_tick(cfg)
    assert called == ["test-project"]


# ---------------------------------------------------------------------------
# list_sessions guards (T-0568 companions)
# ---------------------------------------------------------------------------

def _list_sessions_env(tmp_path, monkeypatch, panes_line: str):
    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, panes_line, "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))


def test_list_sessions_does_not_persist_generic_window_over_stored(tmp_path, monkeypatch):
    """T-0176 #4 synced the LIVE window name into the md — including when the
    live name had decayed to 'bash', destroying the only good copy of the name.
    A generic live name must never overwrite a non-generic stored one."""
    cfg = _make_cfg(tmp_path)
    repo = tmp_path / "repo"
    sdir = cfg.data_dir / "test-project" / "sessions"
    md = sdir / "S-u-goodname-p1.md"
    _write_session_metadata(md, {
        "sid": "S-u-goodname-p1", "status": "active", "window": "goodname",
        "cwd": str(repo), "claude_uuid": "uuid-x", "task_id": "T-0042",
    })
    encoded = str(repo).replace("/", "-")
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "uuid-x.jsonl").write_text("{}")

    _list_sessions_env(tmp_path, monkeypatch, f"%1|bash|1234|{repo}|claude\n")
    S.list_sessions(cfg, "test-project")
    assert _read_session_metadata(md)["window"] == "goodname"


def test_list_sessions_uuid_fallback_skips_md_owned_by_another_live_pane(tmp_path, monkeypatch):
    """The cwd-mtime uuid guess (discover_claude_uuid) is shared across every
    pane in one cwd. An md-less pane must not surface ANOTHER live pane's md
    (task attribution cross-wire — the p8/T-0568 transient the operator saw)."""
    cfg = _make_cfg(tmp_path)
    repo = tmp_path / "repo"
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-worker-b-p2.md", {
        "sid": "S-u-worker-b-p2", "status": "active", "window": "worker-b",
        "cwd": str(repo), "claude_uuid": "uuid-b", "task_id": "T-0001",
    })
    # Shared-cwd jsonl: the mtime guess resolves EVERY md-less pane to uuid-b.
    encoded = str(repo).replace("/", "-")
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "uuid-b.jsonl").write_text("{}")

    _list_sessions_env(
        tmp_path, monkeypatch,
        f"%1|scratch|1234|{repo}|claude\n%2|worker-b|1234|{repo}|claude\n")
    rows = {r["sid"]: r for r in S.list_sessions(cfg, "test-project")}
    assert rows["S-u-worker-b-p2"]["task_id"] == "T-0001"
    # The md-less pane must NOT inherit worker-b's binding via the uuid guess.
    assert rows["S-u-scratch-p1"]["task_id"] is None
