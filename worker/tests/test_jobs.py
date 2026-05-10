"""Tests for worker jobs (heartbeat, deploy_monitor, kick_stuck, oauth_refresh)."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker.config import Config, Project
from bot_squad_worker.jobs import (
    deploy_monitor,
    heartbeat,
    kick_stuck,
    oauth_refresh,
)


# ---------------------------------------------------------------------------
# Helpers shared across job tests
# ---------------------------------------------------------------------------


def _make_project_with_repo(tmp_path: Path, slug: str = "job-test-proj") -> Project:
    """Create a Project backed by a real, clean git repo."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return Project(
        slug=slug,
        display_name="Job Test Project",
        repo_path=repo,
        deploy_branch="agent_team/dev",
        master_branch="master",
        prod_url="",
        staging_url="",
        dev_url="",
        deploy_targets=("staging",),
        tg_chat="TEST_CHAT_ID",
    )


def _make_config_with_project(tmp_path: Path, project: Project) -> Config:
    """Build a Config that has the given project and a temp data dir."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
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
        f'tg_chat = "{project.tg_chat}"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    return Config.load(cfg_dir)


def _make_recipe(cfg: Config, slug: str, target: str, rc: int = 0) -> Path:
    """Drop a recipe shell script."""
    recipe_dir = cfg.data_dir / slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"{target}.sh"
    recipe.write_text(f'#!/usr/bin/env bash\nset -euo pipefail\nexit {rc}\n')
    recipe.chmod(0o755)
    return recipe


class _FakeTgClient:
    """Captures send() calls without hitting the real TG API."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, chat_id: str, text: str, sid: str = "", user: str = "") -> bool:
        self.calls.append({"chat_id": chat_id, "text": text, "sid": sid})
        return True


# ---------------------------------------------------------------------------
# Heartbeat tests (existing)
# ---------------------------------------------------------------------------


def test_heartbeat_creates_file(tmp_config_dir: Path) -> None:
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    assert not cfg.heartbeat_path.exists()
    heartbeat(cfg)
    assert cfg.heartbeat_path.exists()


def test_heartbeat_updates_mtime(tmp_config_dir: Path) -> None:
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat_path.touch()
    import os

    old = os.path.getmtime(cfg.heartbeat_path)
    time.sleep(0.05)
    heartbeat(cfg)
    new = os.path.getmtime(cfg.heartbeat_path)
    assert new > old


# ---------------------------------------------------------------------------
# deploy_monitor tests
# ---------------------------------------------------------------------------


def test_deploy_monitor_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recipe succeeds → queue empty, processed has .ok, TG pinged start+success."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=0)

    # Enqueue a deploy
    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, proj.slug, "staging", "test", "pytest")

    # Inject fake TG
    fake_tg = _FakeTgClient()
    import bot_squad_worker.jobs as J
    monkeypatch.setattr(J, "_get_tg_client" if hasattr(J, "_get_tg_client") else "__noop__",
                        lambda _cfg: fake_tg, raising=False)
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    # Queue should be empty
    assert _deploy.list_queued(cfg, proj.slug) == []

    # processed has .ok
    processed_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    assert len(list(processed_dir.glob("*.ok"))) == 1

    # TG messages sent (start + success)
    assert len(fake_tg.calls) >= 2
    texts = [c["text"] for c in fake_tg.calls]
    assert any("🚚" in t or "starting" in t for t in texts)
    assert any("SUCCESS" in t or "✅" in t for t in texts)


def test_deploy_monitor_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recipe exits 7 → processed has .fail.7, TG pings start+failure."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=7)

    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, proj.slug, "staging", "fail test", "pytest")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    processed_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    assert len(list(processed_dir.glob("*.fail.7"))) == 1

    texts = [c["text"] for c in fake_tg.calls]
    assert any("FAILED" in t or "❌" in t for t in texts)
    assert any("rc=7" in t for t in texts)


def test_deploy_monitor_dirty_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dirty tree → queue file stays, no TG pings for start/success/fail."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=0)

    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, proj.slug, "staging", "dirty", "pytest")

    # Make tree dirty
    (proj.repo_path / "dirty.txt").write_text("uncommitted")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    # Queue still has the file
    assert len(_deploy.list_queued(cfg, proj.slug)) == 1
    # No success/fail pings — but "starting" ping was sent before run_next returned None
    # Per plan note 4: "🚚 starting" ping happens, then dirty-skip means no success/fail
    success_or_fail = [
        c for c in fake_tg.calls
        if "SUCCESS" in c["text"] or "FAILED" in c["text"]
    ]
    assert success_or_fail == []


# ---------------------------------------------------------------------------
# kick_stuck tests
# ---------------------------------------------------------------------------


def test_kick_stuck_no_issues_no_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean project with no queued deploys → no TG ping."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    kick_stuck(cfg)

    assert fake_tg.calls == []


def test_kick_stuck_queued_and_dirty_sends_one_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued deploy + dirty tree → exactly one consolidated TG message."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, proj.slug, "staging", "stuck test", "pytest")

    # Make tree dirty
    (proj.repo_path / "dirty.txt").write_text("uncommitted")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    kick_stuck(cfg)

    assert len(fake_tg.calls) == 1
    msg = fake_tg.calls[0]["text"]
    assert "queued" in msg.lower() or "📋" in msg
    assert "dirty" in msg.lower() or "⚠️" in msg


def test_kick_stuck_recent_failures_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recent .fail file in processed dir → TG ping mentioning failure."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    # Manually drop a .fail file
    processed_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    (processed_dir / "fake.fail.7").write_text("{}")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    kick_stuck(cfg)

    assert len(fake_tg.calls) == 1
    assert "fail" in fake_tg.calls[0]["text"].lower() or "💥" in fake_tg.calls[0]["text"]


# ---------------------------------------------------------------------------
# oauth_refresh tests
# ---------------------------------------------------------------------------


def test_oauth_refresh_ok_no_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When refresh_oauth returns ok=True, no TG ping."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    import bot_squad_worker.refresh_oauth as RO
    monkeypatch.setattr(RO, "refresh_oauth", lambda _cfg: {"ok": True, "action": "noop", "detail": "placeholder"})

    oauth_refresh(cfg)

    assert fake_tg.calls == []


def test_oauth_refresh_failure_pings_tg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When refresh_oauth returns ok=False, TG gets a failure ping."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    import bot_squad_worker.refresh_oauth as RO
    monkeypatch.setattr(RO, "refresh_oauth", lambda _cfg: {"ok": False, "action": "failed", "detail": "test failure"})

    oauth_refresh(cfg)

    assert len(fake_tg.calls) == 1
    assert "FAILED" in fake_tg.calls[0]["text"] or "failed" in fake_tg.calls[0]["text"].lower()
    assert "test failure" in fake_tg.calls[0]["text"]
