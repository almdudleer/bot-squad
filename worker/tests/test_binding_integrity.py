"""T-0324: binding-integrity — constant-team sessions must never hold a
primary, and a mis-set primary must have a safe repair path.

Incident (2026-06-20): T-0253's PRIMARY binding silently dropped off its dev
session (p179) and cross-wired as PRIMARY onto a constant-team session (p181)
via a stale shared-cwd ``.claude/task_id`` marker read by p181's SessionStart
hook — bypassing the ``bind_task`` owner=constant-team refusal. And neither
``bind_task`` nor ``unbind_task`` could repair it (no primary re-home tool).

Worker-side closures tested here:
  * ``reconcile_constant_team_primaries`` — 60s-tick pass stripping a primary
    off any ``owner: constant-team`` session md, whatever path set it.
  * ``reconcile_primary_from_history`` must never ADOPT a primary onto a
    constant-team session (the worker-side analogue of the hook guard).
  * ``rehome_primary`` — the safe re-home tool (H2).
  * ``resume()`` exports BOT_SQUAD_OWNER so the SessionStart hook's env-level
    constant-team guard also covers resumed sessions.

The hook-side marker closure lives in test_session_start_marker_guard.py.
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


def _write_ticket(cfg, tid: str, *, status: str = "open", history: list[str] | None = None):
    hist = ", ".join(history or [])
    (cfg.data_dir / "test-project" / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: x\nstatus: {status}\n"
        f"session_history: [{hist}]\n---\n\n## Verbatim request\n\nx\n"
    )


@pytest.fixture(autouse=True)
def _user(monkeypatch):
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")


# ---------------------------------------------------------------------------
# reconcile_constant_team_primaries (tick pass)
# ---------------------------------------------------------------------------

def test_constant_team_primary_is_stripped(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-user-feedback-p181.md", {
        "sid": "S-u-user-feedback-p181", "status": "active",
        "window": "user-feedback", "owner": "constant-team",
        "task_id": "T-0253",
    })
    res = S.reconcile_constant_team_primaries(cfg, "test-project")
    meta = _read_session_metadata(sdir / "S-u-user-feedback-p181.md")
    assert meta["task_id"] in (None, "~"), meta
    # The cross-wire was never a legitimate binding — it must NOT become
    # last_task_id residue (feeds idle-trim / redispatch heuristics).
    assert not meta.get("last_task_id") or meta.get("last_task_id") == "~"
    assert res["stripped"] == ["S-u-user-feedback-p181"]


def test_constant_team_without_primary_is_untouched(tmp_path):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    md = sdir / "S-u-user-feedback-p181.md"
    _write_session_metadata(md, {
        "sid": "S-u-user-feedback-p181", "status": "active",
        "window": "user-feedback", "owner": "constant-team", "task_id": "~",
    })
    before = md.read_text()
    res = S.reconcile_constant_team_primaries(cfg, "test-project")
    assert res["stripped"] == []
    assert md.read_text() == before


def test_dev_session_primary_is_untouched(tmp_path):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-mytask-p9.md", {
        "sid": "S-u-mytask-p9", "status": "active", "window": "mytask",
        "owner": "alexey", "task_id": "T-0100",
    })
    res = S.reconcile_constant_team_primaries(cfg, "test-project")
    assert res["stripped"] == []
    meta = _read_session_metadata(sdir / "S-u-mytask-p9.md")
    assert meta["task_id"] == "T-0100"


def test_registered_in_binding_gc_tick(tmp_path, monkeypatch):
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
        S, "reconcile_constant_team_primaries",
        lambda cfg, slug: called.append(slug) or {"ok": True, "stripped": []})
    J.binding_gc_tick(cfg)
    assert called == ["test-project"]


# ---------------------------------------------------------------------------
# reconcile_primary_from_history must not ADOPT onto constant-team
# ---------------------------------------------------------------------------

def test_reconcile_primary_from_history_skips_constant_team(tmp_path, monkeypatch):
    """A live, primary-less constant-team session whose SID somehow landed in a
    ticket's session_history must NOT adopt that ticket — the adopt path would
    recreate the p181 incident from the worker side."""
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    sid = "S-u-user-feedback-p181"
    _write_session_metadata(sdir / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "user-feedback",
        "owner": "constant-team", "task_id": "~",
    })
    _write_ticket(cfg, "T-0300", history=[sid])
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%181", window="user-feedback", pid="1",
                          cwd=str(tmp_path / "repo"), command="claude")])
    S.reconcile_primary_from_history(cfg, "test-project")
    meta = _read_session_metadata(sdir / f"{sid}.md")
    assert meta["task_id"] in (None, "~"), \
        "constant-team session must never adopt a primary"


# ---------------------------------------------------------------------------
# rehome_primary (H2)
# ---------------------------------------------------------------------------

def test_rehome_primary_moves_binding_and_strips_wrong_holder(tmp_path):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_ticket(cfg, "T-0253", history=["S-u-docker-install-p179"])
    # The rightful dev, primary lost (the incident's p179).
    _write_session_metadata(sdir / "S-u-docker-install-p179.md", {
        "sid": "S-u-docker-install-p179", "status": "active",
        "window": "docker-install", "owner": "alexey", "task_id": "~",
    })
    # The wrong holder (the incident's p181).
    _write_session_metadata(sdir / "S-u-user-feedback-p181.md", {
        "sid": "S-u-user-feedback-p181", "status": "active",
        "window": "user-feedback", "owner": "constant-team",
        "task_id": "T-0253",
    })

    res = S.rehome_primary(cfg, "test-project", "T-0253", "S-u-docker-install-p179")

    assert _read_session_metadata(
        sdir / "S-u-docker-install-p179.md")["task_id"] == "T-0253"
    stripped_meta = _read_session_metadata(sdir / "S-u-user-feedback-p181.md")
    assert stripped_meta["task_id"] in (None, "~")
    assert res["stripped"] == ["S-u-user-feedback-p181"]
    # session_history gains the new home (idempotent append).
    ticket = (cfg.data_dir / "test-project" / "backlog" / "T-0253-x.md").read_text()
    assert "S-u-docker-install-p179" in ticket


def test_rehome_primary_refuses_constant_team_target(tmp_path):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_ticket(cfg, "T-0253")
    _write_session_metadata(sdir / "S-u-user-feedback-p181.md", {
        "sid": "S-u-user-feedback-p181", "status": "active",
        "window": "user-feedback", "owner": "constant-team", "task_id": "~",
    })
    from bot_squad_worker.actions import ActionError
    with pytest.raises(ActionError, match="constant-team"):
        S.rehome_primary(cfg, "test-project", "T-0253", "S-u-user-feedback-p181")


def test_rehome_primary_refuses_target_holding_other_primary(tmp_path):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_ticket(cfg, "T-0253")
    _write_session_metadata(sdir / "S-u-otherwork-p5.md", {
        "sid": "S-u-otherwork-p5", "status": "active", "window": "otherwork",
        "owner": "alexey", "task_id": "T-0999",
    })
    from bot_squad_worker.actions import ActionError
    with pytest.raises(ActionError, match="T-0999"):
        S.rehome_primary(cfg, "test-project", "T-0253", "S-u-otherwork-p5")


def test_rehome_primary_refuses_teamlead_target(tmp_path):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_ticket(cfg, "T-0253")
    _write_session_metadata(sdir / "S-u-multi_server-TL-p30.md", {
        "sid": "S-u-multi_server-TL-p30", "status": "active",
        "window": "multi_server-TL", "owner": "alexey", "task_id": "~",
    })
    from bot_squad_worker.actions import ActionError
    with pytest.raises(ActionError):
        S.rehome_primary(cfg, "test-project", "T-0253", "S-u-multi_server-TL-p30")


def test_rehome_primary_unknown_task_or_sid_errors(tmp_path):
    cfg = _make_cfg(tmp_path)
    from bot_squad_worker.actions import ActionError
    with pytest.raises(ActionError):
        S.rehome_primary(cfg, "test-project", "T-9999", "S-u-nope-p1")
    _write_ticket(cfg, "T-0253")
    with pytest.raises(ActionError):
        S.rehome_primary(cfg, "test-project", "T-0253", "S-u-nope-p1")


def test_rehome_primary_idempotent_when_target_already_holds(tmp_path):
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_ticket(cfg, "T-0253", history=["S-u-docker-install-p179"])
    _write_session_metadata(sdir / "S-u-docker-install-p179.md", {
        "sid": "S-u-docker-install-p179", "status": "active",
        "window": "docker-install", "owner": "alexey", "task_id": "T-0253",
    })
    res = S.rehome_primary(cfg, "test-project", "T-0253", "S-u-docker-install-p179")
    assert res["ok"] is True
    assert _read_session_metadata(
        sdir / "S-u-docker-install-p179.md")["task_id"] == "T-0253"


# ---------------------------------------------------------------------------
# resume() exports BOT_SQUAD_OWNER (hook env guard coverage for resumes)
# ---------------------------------------------------------------------------

def _CP(args, out=""):
    return subprocess.CompletedProcess(args, 0, out, "")


def _resume_fake_run(repo, new_pane="%20", window="user-feedback"):
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


def test_resume_exports_owner_env(tmp_path, monkeypatch):
    """T-0324 H1: the SessionStart hook's env-level constant-team guard only
    sees BOT_SQUAD_OWNER — which resume() never set, so a RESUMED constant-team
    session bypassed it. resume must re-export the stored owner."""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-user-feedback-p7.md", {
        "sid": "S-u-user-feedback-p7", "status": "suspended",
        "window": "user-feedback", "cwd": str(repo), "claude_uuid": "u7",
        "task_id": "~", "owner": "constant-team",
    })
    fake = _resume_fake_run(repo)
    monkeypatch.setattr(S, "_run", fake)
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    S.resume(cfg, "test-project", "S-u-user-feedback-p7")
    assert any("BOT_SQUAD_OWNER=constant-team" in c for c in fake.launched), \
        fake.launched


def test_resume_without_owner_omits_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _make_cfg(tmp_path)
    sdir = cfg.data_dir / "test-project" / "sessions"
    _write_session_metadata(sdir / "S-u-expert-p7.md", {
        "sid": "S-u-expert-p7", "status": "suspended", "window": "expert",
        "cwd": str(repo), "claude_uuid": "u7", "task_id": "~",
    })
    fake = _resume_fake_run(repo, window="expert")
    monkeypatch.setattr(S, "_run", fake)
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    S.resume(cfg, "test-project", "S-u-expert-p7")
    assert not any("BOT_SQUAD_OWNER" in c for c in fake.launched)
