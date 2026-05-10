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
