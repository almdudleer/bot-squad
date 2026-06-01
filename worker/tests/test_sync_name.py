"""Tests for sessions.sync_session_name (T-0142 naming sync)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker.sessions import PaneInfo, sync_session_name, _write_session_metadata


def _make_cfg(tmp_path: Path) -> Any:
    from bot_squad_worker.config import Config
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
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


def test_live_rename_rotates_sid_and_migrates_md(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    old_sid = "S-u-oldname-p3"
    _write_session_metadata(
        S._session_file(cfg.data_dir, "test-project", old_sid),
        {"sid": old_sid, "status": "active", "window": "oldname",
         "claude_uuid": "abc", "task_id": "T-0001"},
    )
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [PaneInfo(pane_id="%3", window="oldname", pid="1", cwd="/x", command="claude")],
    )
    renames = []
    monkeypatch.setattr(
        S, "_run",
        lambda args, **k: renames.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    res = sync_session_name(cfg, "test-project", old_sid, "newname")
    assert res["new_sid"] == "S-u-newname-p3"
    # tmux rename-window was issued
    assert any("rename-window" in a for a in renames)
    # new md exists, old md gone
    assert S._session_file(cfg.data_dir, "test-project", "S-u-newname-p3").exists()
    assert not S._session_file(cfg.data_dir, "test-project", old_sid).exists()
    new_meta = S._read_session_metadata(
        S._session_file(cfg.data_dir, "test-project", "S-u-newname-p3"))
    assert new_meta["window"] == "newname"
    assert new_meta["task_id"] == "T-0001"  # binding preserved


def test_suspended_rename_updates_window_in_place(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    sid = "S-u-oldname-p3"
    p = S._session_file(cfg.data_dir, "test-project", sid)
    _write_session_metadata(p, {"sid": sid, "status": "suspended", "window": "oldname"})
    monkeypatch.setattr(S, "list_panes", lambda: [])  # no live pane
    res = sync_session_name(cfg, "test-project", sid, "newname")
    assert res["new_sid"] == sid  # no rotation
    assert S._read_session_metadata(p)["window"] == "newname"


def test_name_is_sanitised(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    sid = "S-u-w-p1"
    p = S._session_file(cfg.data_dir, "test-project", sid)
    _write_session_metadata(p, {"sid": sid, "status": "suspended", "window": "w"})
    monkeypatch.setattr(S, "list_panes", lambda: [])
    res = sync_session_name(cfg, "test-project", sid, "my cool name!")
    assert res["name"] == "my_cool_name_"
