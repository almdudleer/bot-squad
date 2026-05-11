"""Tests for worker.config."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker.config import Config, Project


def test_load_finds_signal_tracker(tmp_config_dir: Path) -> None:
    cfg = Config.load(tmp_config_dir)
    assert "test-project" in cfg.projects
    proj = cfg.projects["test-project"]
    assert isinstance(proj, Project)
    assert proj.slug == "test-project"
    assert proj.repo_path == Path("/tmp/test-repo")
    assert "staging" in proj.deploy_targets


def test_load_missing_projects_toml_raises(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "config")


def test_data_dir_resolves_relative_to_config(tmp_config_dir: Path) -> None:
    # Convention: data/ is sibling of config/.
    cfg = Config.load(tmp_config_dir)
    assert cfg.data_dir == tmp_config_dir.parent / "data"
    assert cfg.sock_path == tmp_config_dir.parent / "data" / "_sock" / "worker.sock"


def test_secrets_loads(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_bot_token == "TESTBOT:TOKEN"


def test_secrets_missing_raises(tmp_path: Path) -> None:
    # Build a config dir that has projects.toml but NOT secrets.toml.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        'repo_path = "/tmp/test-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    with pytest.raises(FileNotFoundError, match="secrets.toml"):
        Config.load(cfg_dir)
