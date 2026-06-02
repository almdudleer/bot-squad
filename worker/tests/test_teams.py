"""Tests for worker.teams — the persisted Team entity (T-0142)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker import teams as T
from bot_squad_worker import sessions as S
from bot_squad_worker.sessions import PaneInfo, _write_session_metadata


def _make_cfg(tmp_path: Path) -> Any:
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{tmp_path / "repo"}"\n'
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
    cfg = Config.load(cfg_dir)
    import types
    return types.SimpleNamespace(
        projects=cfg.projects, data_dir=data_dir, tg_bot_token=cfg.tg_bot_token,
    )


def _seed_session(cfg, slug, sid, **fields) -> None:
    meta = {"sid": sid, "status": "active"}
    meta.update(fields)
    _write_session_metadata(S._session_file(cfg.data_dir, slug, sid), meta)


def _seed_constant_initiative(cfg, slug, stem) -> None:
    """Write a vision/initiatives/<stem>.md flagged constant_team: true."""
    d = cfg.data_dir / slug / "vision" / "initiatives"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.md").write_text(f"---\nname: {stem}\nconstant_team: true\n---\n")


@pytest.fixture(autouse=True)
def _fixed_user(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")


def test_reconcile_folds_nonconstant_initiatives_into_project_team(tmp_path, monkeypatch):
    """T-0177: normal-initiative sub-sessions fold into the single project team
    so the project TL and its initiative devs share one roster (option 2)."""
    cfg = _make_cfg(tmp_path)
    # An initiative TL + dev (their own tmux session) and the project operator.
    _seed_session(cfg, "test-project", "S-u-feat-TL-p1",
                  window="feat-TL", tmux_session="test-project-feat",
                  initiative="feat.md", task_id="~", started_at="2026-06-01T00:00:00Z")
    _seed_session(cfg, "test-project", "S-u-feat-dev-p2",
                  window="feat-dev", tmux_session="test-project-feat",
                  initiative="feat.md", task_id="T-0001", started_at="2026-06-01T00:01:00Z")
    _seed_session(cfg, "test-project", "S-u-operator-p3",
                  window="operator", tmux_session="test-project",
                  task_id="~", started_at="2026-06-01T00:02:00Z")
    monkeypatch.setattr(S, "list_panes", lambda: [])

    res = T.reconcile_teams(cfg, "test-project")
    assert res["ok"] is True
    # feat.md is a NORMAL initiative → everything folds into one project team.
    assert set(res["teams"]) == {"test-project"}

    main = T.load_team(cfg, "test-project", "test-project")
    # operator started latest → holds the lead slot; the initiative dev AND the
    # initiative TL both appear in the project roster (nobody is dropped).
    assert main["tl"] == "S-u-operator-p3"
    assert "S-u-feat-dev-p2" in main["teammates"]
    assert "S-u-feat-TL-p1" in main["teammates"]


def test_reconcile_keeps_constant_team_separate(tmp_path, monkeypatch):
    """T-0177: a constant_team initiative keeps its own team — NOT folded."""
    cfg = _make_cfg(tmp_path)
    _seed_constant_initiative(cfg, "test-project", "user-feedback")
    _seed_session(cfg, "test-project", "S-u-dev-p1",
                  window="some-feature", tmux_session="test-project",
                  task_id="T-0001", started_at="2026-06-01T00:00:00Z")
    _seed_session(cfg, "test-project", "S-u-user-feedback-p2",
                  window="user-feedback", tmux_session="test-project-user-feedback",
                  initiative="user-feedback.md", task_id="~",
                  started_at="2026-06-01T00:01:00Z")
    monkeypatch.setattr(S, "list_panes", lambda: [])

    res = T.reconcile_teams(cfg, "test-project")
    assert set(res["teams"]) == {"test-project", "test-project-user-feedback"}
    main = T.load_team(cfg, "test-project", "test-project")
    assert main["teammates"] == ["S-u-dev-p1"]
    assert "S-u-user-feedback-p2" not in main["teammates"]
    const = T.load_team(cfg, "test-project", "test-project-user-feedback")
    assert const["teammates"] == ["S-u-user-feedback-p2"]


def test_reconcile_retains_extra_tl_candidate_as_teammate(tmp_path, monkeypatch):
    """T-0177: folding many sessions into one team means several teamlead/operator
    roles may co-occur; only one holds the lead slot, the rest stay visible as
    teammates rather than vanishing."""
    cfg = _make_cfg(tmp_path)
    _seed_session(cfg, "test-project", "S-u-a-TL-p1",
                  window="a-TL", tmux_session="test-project",
                  task_id="~", started_at="2026-06-01T00:00:00Z")
    _seed_session(cfg, "test-project", "S-u-b-TL-p2",
                  window="b-TL", tmux_session="test-project",
                  task_id="~", started_at="2026-06-01T00:05:00Z")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    T.reconcile_teams(cfg, "test-project")
    main = T.load_team(cfg, "test-project", "test-project")
    assert main["tl"] == "S-u-b-TL-p2"           # latest started wins the slot
    assert main["teammates"] == ["S-u-a-TL-p1"]  # loser retained, not dropped


def test_reconcile_prefers_live_tl_then_latest(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_session(cfg, "test-project", "S-u-feat-TL-p1",
                  window="feat-TL", tmux_session="test-project-feat",
                  initiative="feat.md", task_id="~", started_at="2026-06-01T00:00:00Z")
    # An older, dead TL-role md in the same group must lose to the live one.
    _seed_session(cfg, "test-project", "S-u-feat-TL-p9",
                  window="feat-TL", tmux_session="test-project-feat",
                  initiative="feat.md", task_id="~", started_at="2026-05-01T00:00:00Z")
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%1", window="feat-TL", pid="1", cwd="/x", command="claude")],
    )
    T.reconcile_teams(cfg, "test-project")
    # feat.md is a normal initiative → folds into the project team (T-0177).
    team = T.load_team(cfg, "test-project", "test-project")
    # %1 -> S-u-feat-TL-p1 is live; it wins the lead slot over the older dead p9.
    assert team["tl"] == "S-u-feat-TL-p1"
    # The dead loser is retained as a teammate, not dropped.
    assert team["teammates"] == ["S-u-feat-TL-p9"]


def test_reconcile_buckets_archived_members(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_session(cfg, "test-project", "S-u-w-dev-p2",
                  window="w-dev", tmux_session="test-project",
                  task_id="T-0001", archived="true")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    T.reconcile_teams(cfg, "test-project")
    team = T.load_team(cfg, "test-project", "test-project")
    assert team["archived_members"] == ["S-u-w-dev-p2"]
    assert team["teammates"] == []


def test_reconcile_preserves_created_at(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_session(cfg, "test-project", "S-u-operator-p3",
                  window="operator", tmux_session="test-project", task_id="~")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    T.reconcile_teams(cfg, "test-project")
    first = T.load_team(cfg, "test-project", "test-project")
    created = first["created_at"]
    T.reconcile_teams(cfg, "test-project")
    again = T.load_team(cfg, "test-project", "test-project")
    assert again["created_at"] == created


def test_reconcile_survives_reload_via_disk(tmp_path, monkeypatch):
    """The team md persists; a fresh cfg (worker reload) reads it back."""
    cfg = _make_cfg(tmp_path)
    _seed_session(cfg, "test-project", "S-u-operator-p3",
                  window="operator", tmux_session="test-project", task_id="~")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    T.reconcile_teams(cfg, "test-project")
    # Simulate reload: brand-new cfg pointed at the same data dir.
    import types
    cfg2 = types.SimpleNamespace(projects=cfg.projects, data_dir=cfg.data_dir, tg_bot_token="")
    res = T.list_teams(cfg2, "test-project")
    names = [t["name"] for t in res["teams"]]
    assert "test-project" in names


def test_archive_team_suspends_live_members_and_marks_archived(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # A constant-team sub-team keeps its own identity (T-0177), so archive/
    # resurrect operate on a real named team separate from the project team.
    _seed_constant_initiative(cfg, "test-project", "feat")
    _seed_session(cfg, "test-project", "S-u-feat-TL-p1",
                  window="feat-TL", tmux_session="test-project-feat",
                  initiative="feat.md", task_id="~")
    _seed_session(cfg, "test-project", "S-u-feat-dev-p2",
                  window="feat-dev", tmux_session="test-project-feat",
                  initiative="feat.md", task_id="T-0001")
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%2", window="feat-dev", pid="1", cwd="/x", command="claude")],
    )
    T.reconcile_teams(cfg, "test-project")

    suspended_calls = []
    monkeypatch.setattr(
        S, "suspend",
        lambda cfg, slug, sid: suspended_calls.append(sid) or {"ok": True},
    )
    res = T.archive_team(cfg, "test-project", "test-project-feat")
    assert res["archived"] is True
    # Only the live member (the dev) gets suspended.
    assert suspended_calls == ["S-u-feat-dev-p2"]
    team = T.load_team(cfg, "test-project", "test-project-feat")
    assert str(team["archived"]).lower() == "true"


def test_resurrect_team_resumes_tl_and_clears_flag(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _seed_constant_initiative(cfg, "test-project", "feat")
    _seed_session(cfg, "test-project", "S-u-feat-TL-p1",
                  window="feat-TL", tmux_session="test-project-feat",
                  initiative="feat.md", task_id="~")
    monkeypatch.setattr(S, "list_panes", lambda: [])
    T.reconcile_teams(cfg, "test-project")
    path = T._team_file(cfg.data_dir, "test-project", "test-project-feat")
    m = T._read_team(path)
    m["archived"] = "true"
    T._write_team(path, m)

    monkeypatch.setattr(
        S, "resume",
        lambda cfg, slug, sid: {"ok": True, "sid": "S-u-feat-TL-p5"},
    )
    res = T.resurrect_team(cfg, "test-project", "test-project-feat")
    assert res["tl"] == "S-u-feat-TL-p5"
    team = T.load_team(cfg, "test-project", "test-project-feat")
    assert str(team["archived"]).lower() == "false"


# ---------------------------------------------------------------------------
# T-0177 — prune_orphan_teams: gated fold-cleanup of stale initiative teams
# ---------------------------------------------------------------------------

def _seed_team(cfg, slug, name, **fields):
    meta = {"name": name, "tl": "~", "teammates": [], "archived_members": [],
            "archived": "false"}
    meta.update(fields)
    T._write_team(T._team_file(cfg.data_dir, slug, name), meta)


def test_prune_orphan_teams_removes_stale_initiative_team(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_constant_initiative(cfg, "test-project", "user-feedback")
    _seed_team(cfg, "test-project", "test-project")                       # project team
    _seed_team(cfg, "test-project", "test-project-user-feedback")         # constant team
    _seed_team(cfg, "test-project", "test-project-operator-ux")           # ORPHAN initiative team
    res = T.prune_orphan_teams(cfg, "test-project", dry_run=False)
    names = {t["name"] for t in T.list_teams(cfg, "test-project")["teams"]}
    assert names == {"test-project", "test-project-user-feedback"}
    assert res["pruned"] == ["test-project-operator-ux"]


def test_prune_orphan_teams_preserves_archived(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_team(cfg, "test-project", "test-project")
    _seed_team(cfg, "test-project", "test-project-old-init", archived="true")  # archived orphan
    res = T.prune_orphan_teams(cfg, "test-project", dry_run=False)
    names = {t["name"] for t in T.list_teams(cfg, "test-project")["teams"]}
    assert "test-project-old-init" in names      # archived intent preserved
    assert res["pruned"] == []


def test_prune_orphan_teams_dry_run_writes_nothing(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_team(cfg, "test-project", "test-project")
    _seed_team(cfg, "test-project", "test-project-orphan")
    res = T.prune_orphan_teams(cfg, "test-project", dry_run=True)
    names = {t["name"] for t in T.list_teams(cfg, "test-project")["teams"]}
    assert "test-project-orphan" in names        # nothing deleted
    assert res["pruned"] == ["test-project-orphan"]   # but reported
