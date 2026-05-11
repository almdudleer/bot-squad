"""Tests for deploy queue + recipe runner (deploy.py).

Fixture: a real git repo in tmp_path + a recipe shell script.
No mocks — subprocess and filesystem are exercised directly.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from bot_squad_worker.config import Config, Project
from bot_squad_worker.deploy import DeployResult, enqueue, list_queued, run_next


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, targets: tuple[str, ...] = ("staging",)) -> Project:
    """Create a minimal Project pointing at a real git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), check=True
    )
    # initial commit so the tree is "clean"
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True
    )
    return Project(
        slug="test-deploy",
        display_name="Test Deploy",
        repo_path=repo,
        deploy_branch="bot_squad/dev",
        master_branch="master",
        prod_url="",
        staging_url="",
        dev_url="",
        deploy_targets=targets,
        tg_chat="0",
    )


def _make_config(tmp_path: Path, project: Project) -> Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    # write minimal projects.toml
    (cfg_dir / "projects.toml").write_text(
        f'[projects.{project.slug}]\n'
        f'slug = "{project.slug}"\n'
        f'display_name = "{project.display_name}"\n'
        f'repo_path = "{project.repo_path}"\n'
        f'deploy_branch = "{project.deploy_branch}"\n'
        f'master_branch = "{project.master_branch}"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "0"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    return Config.load(cfg_dir)


def _make_recipe(tmp_path: Path, cfg: Config, slug: str, target: str, rc: int = 0) -> Path:
    """Drop a recipe shell script in data/<slug>/deploy/<target>.sh."""
    recipe_dir = cfg.data_dir / slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"{target}.sh"
    recipe.write_text(f'#!/usr/bin/env bash\nset -euo pipefail\necho "recipe running"\nexit {rc}\n')
    recipe.chmod(0o755)
    return recipe


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_enqueue_writes_queue_file(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)

    queue_id = enqueue(cfg, proj.slug, "staging", "test reason", "test-user")

    queue_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "queue"
    files = list(queue_dir.glob("*.json"))
    assert len(files) == 1

    data = json.loads(files[0].read_text())
    assert data["queue_id"] == queue_id
    assert data["slug"] == proj.slug
    assert data["target"] == "staging"
    assert data["reason"] == "test reason"
    assert data["requested_by"] == "test-user"
    assert "queued_at" in data


def test_enqueue_rejects_unknown_target(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)

    with pytest.raises(ValueError, match="unknown target"):
        enqueue(cfg, proj.slug, "prod", "bad target", "test-user")


def test_run_next_executes_recipe_on_clean_tree(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)

    queue_id = enqueue(cfg, proj.slug, "staging", "clean deploy", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert isinstance(result, DeployResult)
    assert result.ok is True
    assert result.returncode == 0
    assert result.queue_id == queue_id

    # queue dir should be empty
    queue_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "queue"
    assert list(queue_dir.glob("*.json")) == []

    # processed dir should have .ok file
    processed_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    ok_files = list(processed_dir.glob("*.ok"))
    assert len(ok_files) == 1


def test_run_next_skips_when_dirty(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)

    enqueue(cfg, proj.slug, "staging", "dirty tree", "user")

    # make the tree dirty
    (proj.repo_path / "dirty.txt").write_text("uncommitted")

    result = run_next(cfg, proj.slug)

    assert result is None

    # queue file should still be there
    queue_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "queue"
    assert len(list(queue_dir.glob("*.json"))) == 1


def test_run_next_failure_records_rc(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=7)

    queue_id = enqueue(cfg, proj.slug, "staging", "failing deploy", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is False
    assert result.returncode == 7
    assert result.queue_id == queue_id

    # processed dir should have .fail.7 file
    processed_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    fail_files = list(processed_dir.glob("*.fail.7"))
    assert len(fail_files) == 1


def test_run_next_returns_none_when_empty(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)

    result = run_next(cfg, proj.slug)
    assert result is None


def test_list_queued_returns_sorted(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)

    enqueue(cfg, proj.slug, "staging", "first", "user")
    time.sleep(0.01)
    enqueue(cfg, proj.slug, "staging", "second", "user")

    queued = list_queued(cfg, proj.slug)
    assert len(queued) == 2
    # should be sorted oldest-first by filename
    assert queued[0] < queued[1]


def test_run_next_missing_recipe_fails_with_rc99(tmp_path: Path) -> None:
    """If the recipe file doesn't exist, run_next returns a DeployResult with rc=99."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    # do NOT create a recipe

    enqueue(cfg, proj.slug, "staging", "no recipe", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is False
    assert result.returncode == 99

    processed_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    fail_files = list(processed_dir.glob("*.fail.99"))
    assert len(fail_files) == 1
