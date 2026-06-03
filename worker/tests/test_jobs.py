"""Tests for worker jobs (heartbeat, deploy_monitor, oauth_refresh)."""
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
    oauth_refresh,
    tg_listener_tick,
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
        deploy_branch="bot_squad/dev",
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

    def send(
        self,
        *,
        chat_id: str,
        text: str,
        sid: str = "",
        user: str = "",
        urgent: bool = False,
        topic_id: int | None = None,
    ) -> bool:
        self.calls.append(
            {"chat_id": chat_id, "text": text, "sid": sid, "urgent": urgent}
        )
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


def test_deploy_monitor_pings_are_urgent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0188 regression: deploy start+finish pings MUST be urgent=True.

    Deploy events are project-bound system notifications, not idle DM flood —
    they must fire by default for a project with a tg_chat set, bypassing the
    quiet-hours gate. Before the fix the pings went through tg.send with
    urgent=False, so every deploy alert fired during the stakeholder's Tashkent
    night (17–05 UTC quiet window) was silently dropped — the reported
    "no more alerts from @bot_squad_bot" regression.
    """
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=0)

    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, proj.slug, "staging", "urgency test", "pytest")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    # A deploy event in a project with tg_chat set produced tg.send calls...
    assert fake_tg.calls, "deploy event produced no tg.send call"
    assert all(c["chat_id"] == proj.tg_chat for c in fake_tg.calls)
    # ...and EVERY one of them is urgent so quiet hours cannot drop it.
    assert all(c["urgent"] is True for c in fake_tg.calls), (
        "deploy pings must be urgent=True to bypass quiet hours; "
        f"got {[(c['text'], c['urgent']) for c in fake_tg.calls]}"
    )


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
    """Dirty tree → queue file stays, ZERO TG pings (no spam at monitor cadence)."""
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
    # No "starting" ping either — that's the fix. Previously the start
    # ping was sent before the cleanliness check, producing 60s-cadence
    # spam with no success/fail follow-up. Now the per-target cleanliness
    # check gates ALL user-visible pings.
    assert fake_tg.calls == []


def test_deploy_monitor_collapses_same_target_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 queued deploys for the same target → 1 actual deploy + 1 start/finish TG pair."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=0)

    from bot_squad_worker import deploy as _deploy
    # Need distinct millisecond timestamps so queue files sort
    for i in range(5):
        _deploy.enqueue(cfg, proj.slug, "staging", f"reason-{i}", "pytest")
        time.sleep(0.005)

    assert len(_deploy.list_queued(cfg, proj.slug)) == 5

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    # All 5 queue files should be consumed in a single run.
    assert _deploy.list_queued(cfg, proj.slug) == []
    # Exactly one start ping + one success ping, and the success ping
    # mentions the collapsed count.
    starts = [c for c in fake_tg.calls if "starting" in c["text"]]
    succs  = [c for c in fake_tg.calls if "SUCCESS"  in c["text"]]
    assert len(starts) == 1
    assert len(succs)  == 1
    assert "5 queued requests collapsed" in succs[0]["text"]


def test_deploy_monitor_paused_skips_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAUSED.json present → zero TG calls, queue preserved (same shape as dirty-tree)."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=0)

    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, proj.slug, "staging", "before pause", "pytest")
    _deploy.pause(cfg, proj.slug, "manual hold", "pytest")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    assert len(_deploy.list_queued(cfg, proj.slug)) == 1  # queue preserved
    assert fake_tg.calls == []                            # no pings at all

    # Resume → next tick runs.
    assert _deploy.resume(cfg, proj.slug) is True
    deploy_monitor(cfg)
    assert _deploy.list_queued(cfg, proj.slug) == []      # ran
    starts  = [c for c in fake_tg.calls if "starting" in c["text"]]
    success = [c for c in fake_tg.calls if "SUCCESS"  in c["text"]]
    assert len(starts) == 1
    assert len(success) == 1


def test_deploy_pause_resume_idempotent(tmp_path: Path) -> None:
    """pause() is idempotent; resume() returns False when nothing to remove."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    from bot_squad_worker import deploy as _deploy

    assert _deploy.is_paused(cfg, proj.slug) is None
    assert _deploy.resume(cfg, proj.slug) is False  # nothing to resume

    meta = _deploy.pause(cfg, proj.slug, "first", "pytest")
    assert meta["reason"] == "first"
    assert _deploy.is_paused(cfg, proj.slug) is not None

    # Re-pause overwrites reason but stays paused
    meta2 = _deploy.pause(cfg, proj.slug, "second", "pytest")
    assert meta2["reason"] == "second"

    assert _deploy.resume(cfg, proj.slug) is True
    assert _deploy.is_paused(cfg, proj.slug) is None


def test_deploy_pause_is_per_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pausing slug A must not affect slug B."""
    proj_a = _make_project_with_repo(tmp_path / "a", slug="a")
    proj_b = _make_project_with_repo(tmp_path / "b", slug="b")

    # Build a cfg with both projects
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        f'[projects.{proj_a.slug}]\n'
        f'slug = "{proj_a.slug}"\n'
        f'display_name = "A"\n'
        f'repo_path = "{proj_a.repo_path}"\n'
        f'deploy_branch = "{proj_a.deploy_branch}"\n'
        f'master_branch = "{proj_a.master_branch}"\n'
        f'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "TEST_CHAT_A"\n'
        f'created_at = 2026-05-10\n'
        f'\n'
        f'[projects.{proj_b.slug}]\n'
        f'slug = "{proj_b.slug}"\n'
        f'display_name = "B"\n'
        f'repo_path = "{proj_b.repo_path}"\n'
        f'deploy_branch = "{proj_b.deploy_branch}"\n'
        f'master_branch = "{proj_b.master_branch}"\n'
        f'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        f'deploy_targets = ["staging"]\n'
        f'tg_chat = "TEST_CHAT_B"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    _make_recipe(cfg, "a", "staging", rc=0)
    _make_recipe(cfg, "b", "staging", rc=0)

    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, "a", "staging", "a1", "pytest")
    _deploy.enqueue(cfg, "b", "staging", "b1", "pytest")
    _deploy.pause(cfg, "a", "hold a only", "pytest")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    # A is paused, queue preserved. B ran and drained.
    assert len(_deploy.list_queued(cfg, "a")) == 1
    assert _deploy.list_queued(cfg, "b") == []


def test_deploy_monitor_collapses_only_same_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """staging + prod queued together → only same-target trailing entries collapse."""
    proj = _make_project_with_repo(tmp_path)
    # Re-make project allowing two targets so we can enqueue both.
    proj = Project(
        slug=proj.slug,
        display_name=proj.display_name,
        repo_path=proj.repo_path,
        deploy_branch=proj.deploy_branch,
        master_branch=proj.master_branch,
        prod_url="",
        staging_url="",
        dev_url="",
        deploy_targets=("staging", "prod"),
        tg_chat=proj.tg_chat,
    )
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        f'[projects.{proj.slug}]\n'
        f'slug = "{proj.slug}"\n'
        f'display_name = "{proj.display_name}"\n'
        f'repo_path = "{proj.repo_path}"\n'
        f'deploy_branch = "{proj.deploy_branch}"\n'
        f'master_branch = "{proj.master_branch}"\n'
        f'prod_url = ""\n'
        f'staging_url = ""\n'
        f'dev_url = ""\n'
        f'deploy_targets = ["staging", "prod"]\n'
        f'tg_chat = "{proj.tg_chat}"\n'
        f'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    _make_recipe(cfg, proj.slug, "staging", rc=0)
    _make_recipe(cfg, proj.slug, "prod", rc=0)

    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, proj.slug, "staging", "s1", "pytest"); time.sleep(0.005)
    _deploy.enqueue(cfg, proj.slug, "staging", "s2", "pytest"); time.sleep(0.005)
    _deploy.enqueue(cfg, proj.slug, "prod",    "p1", "pytest"); time.sleep(0.005)
    _deploy.enqueue(cfg, proj.slug, "staging", "s3", "pytest")

    assert len(_deploy.list_queued(cfg, proj.slug)) == 4

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor(cfg)

    # First tick: oldest is staging → collapses s1+s2+s3 (the trailing
    # staging entries, even past the prod one in between). prod job stays.
    remaining = _deploy.list_queued(cfg, proj.slug)
    assert len(remaining) == 1
    assert "p1" in remaining[0].read_text()


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


def test_oauth_refresh_failure_ping_is_urgent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0193 regression: oauth_refresh failure ping MUST be urgent=True.

    An oauth-refresh failure is a P1 SYSTEM alert (once creds expire every
    session breaks). Before the fix the failure ping went through tg.send with
    urgent=False — the exact T-0188 class bug — so it hit the quiet-hours gate
    (22-04 UTC) and was silently dropped. It must bypass quiet hours.
    """
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    import bot_squad_worker.refresh_oauth as RO
    monkeypatch.setattr(RO, "refresh_oauth", lambda _cfg: {"ok": False, "action": "failed", "detail": "creds expired"})

    oauth_refresh(cfg)

    assert fake_tg.calls, "oauth failure produced no tg.send call"
    assert all(c["urgent"] is True for c in fake_tg.calls), (
        "oauth_refresh failure ping must be urgent=True to bypass quiet hours; "
        f"got {[(c['text'], c['urgent']) for c in fake_tg.calls]}"
    )


# ---------------------------------------------------------------------------
# tg_listener_tick tests (spec #7)
# ---------------------------------------------------------------------------


def test_tg_listener_tick_calls_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tg_listener_tick(cfg) delegates to tg_listener.tick and does not raise."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    tick_calls: list[Any] = []

    import bot_squad_worker.tg_listener as TGL
    monkeypatch.setattr(TGL, "tick", lambda c: tick_calls.append(c) or {"ok": True, "polled": 0, "handled": 0, "max_update_id": 0})

    tg_listener_tick(cfg)

    assert len(tick_calls) == 1
    assert tick_calls[0] is cfg


def test_tg_listener_tick_swallows_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tg_listener_tick must not let exceptions propagate to APScheduler."""
    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    import bot_squad_worker.tg_listener as TGL
    monkeypatch.setattr(TGL, "tick", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))

    # Should not raise
    tg_listener_tick(cfg)


# ---------------------------------------------------------------------------
# autonomous_tick tests (spec #8)
# ---------------------------------------------------------------------------


def test_autonomous_tick_skips_disabled_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """autonomous_tick with a disabled project calls tick() but it is a no-op."""
    from bot_squad_worker.jobs import autonomous_tick
    from bot_squad_worker import autonomous as _auto

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    # State defaults to disabled
    tick_calls: list[Any] = []
    monkeypatch.setattr(_auto, "tick", lambda c, s: tick_calls.append(s))

    autonomous_tick(cfg)

    # Called once per project
    assert tick_calls == [proj.slug]


def test_autonomous_tick_swallows_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """autonomous_tick must not propagate per-project exceptions."""
    from bot_squad_worker.jobs import autonomous_tick
    from bot_squad_worker import autonomous as _auto

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    monkeypatch.setattr(_auto, "tick", lambda c, s: (_ for _ in ()).throw(RuntimeError("boom")))

    # Must not raise
    autonomous_tick(cfg)
