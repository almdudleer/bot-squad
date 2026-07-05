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
        debounce: bool = True,
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


def test_heartbeat_writes_boot_sha(tmp_config_dir: Path, monkeypatch) -> None:
    """T-0456: the heartbeat carries the worker's boot_git_sha as its body so the
    API health endpoint can detect API/worker sha drift at runtime."""
    import bot_squad_worker.deploy as D

    monkeypatch.setattr(D, "boot_git_sha", lambda: "deadbeefcafe123")
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat(cfg)
    assert cfg.heartbeat_path.read_text().strip() == "deadbeefcafe123"


def test_heartbeat_sha_write_still_freshens_mtime(tmp_config_dir: Path, monkeypatch) -> None:
    """Writing the sha must NOT break the mtime-based liveness signal."""
    import os

    import bot_squad_worker.deploy as D

    monkeypatch.setattr(D, "boot_git_sha", lambda: "abc123")
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat_path.write_text("stale\n")
    old = os.path.getmtime(cfg.heartbeat_path)
    time.sleep(0.05)
    heartbeat(cfg)
    assert os.path.getmtime(cfg.heartbeat_path) > old


def test_heartbeat_falls_back_to_empty_on_sha_error(tmp_config_dir: Path, monkeypatch) -> None:
    """A boot_git_sha lookup failure must NOT lose the liveness signal — the file
    is still (re)written so the API sees a fresh heartbeat."""
    import bot_squad_worker.deploy as D

    def _boom() -> str:
        raise RuntimeError("git missing")

    monkeypatch.setattr(D, "boot_git_sha", _boom)
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat(cfg)
    assert cfg.heartbeat_path.exists()
    assert cfg.heartbeat_path.read_text().strip() == ""


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
# T-0212: per-project isolation + no-progress watchdog + orphan reaper
# ---------------------------------------------------------------------------


def _two_project_config(tmp_path: Path) -> tuple[Config, Project, Project]:
    """A Config with two real projects A (wedger) + B (healthy)."""
    proj_a = _make_project_with_repo(tmp_path / "A", slug="proj-a")
    proj_b = _make_project_with_repo(tmp_path / "B", slug="proj-b")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    blocks = []
    for p in (proj_a, proj_b):
        blocks.append(
            f'[projects.{p.slug}]\n'
            f'slug = "{p.slug}"\n'
            f'display_name = "{p.display_name}"\n'
            f'repo_path = "{p.repo_path}"\n'
            f'deploy_branch = "{p.deploy_branch}"\n'
            f'master_branch = "{p.master_branch}"\n'
            f'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
            f'deploy_targets = ["staging"]\n'
            f'tg_chat = "{p.tg_chat}"\n'
            f'created_at = 2026-05-10\n'
        )
    (cfg_dir / "projects.toml").write_text("\n".join(blocks))
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    return Config.load(cfg_dir), proj_a, proj_b


def _hang_recipe(cfg: Config, slug: str) -> None:
    recipe_dir = cfg.data_dir / slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    r = recipe_dir / "staging.sh"
    r.write_text('#!/usr/bin/env bash\necho start\nwhile true; do sleep 60; done\n')
    r.chmod(0o755)


def test_deploy_monitor_per_project_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE T-0212 scenario: a wedged build in project A must NOT block B.

    Wedge A with a no-output build; assert (a) A is watchdog-failed with a loud
    marker, and (b) B still deploys to completion — on its own monitor tick,
    not held behind A.
    """
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.jobs import deploy_monitor_one

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_POLL_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_TIMEOUT", "60")

    cfg, proj_a, proj_b = _two_project_config(tmp_path)
    _hang_recipe(cfg, proj_a.slug)            # A wedges
    _make_recipe(cfg, proj_b.slug, "staging", rc=0)  # B healthy

    _deploy.enqueue(cfg, proj_a.slug, "staging", "wedge", "pytest")
    _deploy.enqueue(cfg, proj_b.slug, "staging", "healthy", "pytest")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    # A's tick: wedged build is watchdog-killed (does NOT hang the test).
    deploy_monitor_one(cfg, proj_a.slug)
    a_base = cfg.data_dir / proj_a.slug / "_jobs" / "deploy"
    assert list((a_base / "processing").glob("*.json")) == []  # nothing stranded
    assert len(list((a_base / "processed").glob(f"*.fail.{_deploy.RC_NO_PROGRESS}"))) == 1

    # B's tick: deploys fine, fully independent of A's wedge.
    deploy_monitor_one(cfg, proj_b.slug)
    b_base = cfg.data_dir / proj_b.slug / "_jobs" / "deploy"
    assert len(list((b_base / "processed").glob("*.ok"))) == 1


def test_deploy_monitor_reconciles_finished_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0243: deploy_monitor_one reconciles a FINISHED-but-orphaned processing
    marker (rc sentinel present, _finish lost to a worker-restart race) to
    processed/.ok — BEFORE the age-fail reaper, so a stranded SUCCESS lands as
    .ok, not .fail.ORPHAN. (Also exercises the deploy_monitor_one wiring.)"""
    import json
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.jobs import deploy_monitor_one

    cfg, proj_a, _proj_b = _two_project_config(tmp_path)
    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    base = cfg.data_dir / proj_a.slug / "_jobs" / "deploy"
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "1700000000000-orphanwin.json"
    f.write_text(json.dumps({"queue_id": "orphanwin", "target": "staging"}))
    _deploy._record_run_rc(cfg, proj_a.slug, "orphanwin", 0)  # finished rc=0, move lost

    deploy_monitor_one(cfg, proj_a.slug)

    assert list(proc.glob("*.json")) == []  # reconciled out of processing
    assert (base / "processed" / "1700000000000-orphanwin.ok").exists()


def test_deploy_monitor_watchdog_fires_targeted_operator_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watchdog kill → loud urgent TG + a TARGETED operator peer_send.

    Not a broadcast (role=all): a literal operator SID, mirroring the telemetry
    guardrail (T-0210).
    """
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.jobs import deploy_monitor_one

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_POLL_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_TIMEOUT", "60")

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    _hang_recipe(cfg, proj.slug)
    _deploy.enqueue(cfg, proj.slug, "staging", "wedge", "pytest")

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    # One operator-role session + capture peer_sends.
    import bot_squad_worker.sessions as S
    import bot_squad_worker.intersession as IS
    monkeypatch.setattr(
        S, "list_sessions",
        lambda _cfg, _slug: [{"sid": "S-op-1", "role": "operator"},
                             {"sid": "S-dev-9", "role": "dev"}],
    )
    peers: list[tuple[str, str]] = []
    monkeypatch.setattr(
        IS, "send",
        lambda _cfg, _slug, frm, to, text, user=None: peers.append((to, text)) or {"ok": True},
    )

    deploy_monitor_one(cfg, proj.slug)

    # Urgent TG carried the loud KILLED marker.
    kill_tgs = [c for c in fake_tg.calls if "KILLED" in c["text"]]
    assert kill_tgs and all(c["urgent"] is True for c in kill_tgs)
    # Targeted peer_send went to the operator SID ONLY (never the dev / role=all).
    assert peers and all(to == "S-op-1" for to, _ in peers)
    assert all("KILLED" in t for _, t in peers)


def test_deploy_monitor_watchdog_alert_routes_tg_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-04 → T-0610 inversion: the deploy-KILLED alert routes through the
    _send_stakeholder_dm SSOT, TG-primary (one loud delivery into #deploy-logs),
    MAX reserve-only — not a raw send and not a duplicated page.
    """
    import dataclasses
    import types

    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.jobs import deploy_monitor_one

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_POLL_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_TIMEOUT", "60")

    proj = _make_project_with_repo(tmp_path)
    cfg = dataclasses.replace(
        _make_config_with_project(tmp_path, proj),
        max_default_chat_id="MAXID", max_recipient_kind="chat_id",
    )
    _hang_recipe(cfg, proj.slug)
    _deploy.enqueue(cfg, proj.slug, "staging", "wedge", "pytest")

    from bot_squad_worker import actions as A
    max_calls: list[dict] = []
    tg_calls: list[dict] = []
    monkeypatch.setattr(A, "_MAX", types.SimpleNamespace(
        send=lambda **k: (max_calls.append(k) or True)))
    monkeypatch.setattr(A, "_TG", types.SimpleNamespace(
        send=lambda **k: (tg_calls.append(k) or True)))

    import bot_squad_worker.sessions as S
    import bot_squad_worker.intersession as IS
    monkeypatch.setattr(S, "list_sessions", lambda _cfg, _slug: [])
    monkeypatch.setattr(
        IS, "send",
        lambda *a, **k: {"ok": True},
    )

    deploy_monitor_one(cfg, proj.slug)

    # TG (primary) carried the loud KILLED alert — one delivery, not dropped.
    kill_tg = [c for c in tg_calls if "KILLED" in c["text"]]
    assert kill_tg and kill_tg[0]["chat_id"] == proj.tg_chat
    assert all(c["urgent"] is True for c in kill_tg)
    # MAX is reserve-only now — no duplicate personal ping (T-0610).
    assert max_calls == []


def test_deploy_monitor_reaps_orphan_with_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deploy_monitor_one sweeps a stale processing/ orphan + fires an alert."""
    import os
    import time as _time
    import json as _j
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.jobs import deploy_monitor_one

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)
    # No queued deploy — only a stranded orphan to reap.
    proc_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processing"
    proc_dir.mkdir(parents=True, exist_ok=True)
    orphan = proc_dir / "1700000000000-old-orphan.json"
    orphan.write_text(_j.dumps({"queue_id": "old-orphan", "target": "staging"}))
    old = _time.time() - 3 * 3600
    os.utime(orphan, (old, old))

    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    deploy_monitor_one(cfg, proj.slug)

    processed = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    assert len(list(processed.glob(f"*.fail.{_deploy.RC_ORPHAN}"))) == 1
    assert any("ORPHAN" in c["text"] and c["urgent"] for c in fake_tg.calls)


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


def test_oauth_refresh_failure_routes_tg_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-08 → T-0610 inversion: the oauth_refresh P1 system page routes
    through the SSOT — TG-primary, one delivery, MAX reserve-only."""
    import dataclasses
    import types

    proj = _make_project_with_repo(tmp_path)
    cfg = dataclasses.replace(
        _make_config_with_project(tmp_path, proj),
        max_default_chat_id="MAXID", max_recipient_kind="chat_id",
    )

    from bot_squad_worker import actions as A
    max_calls: list[dict] = []
    tg_calls: list[dict] = []
    monkeypatch.setattr(A, "_MAX", types.SimpleNamespace(
        send=lambda **k: (max_calls.append(k) or True)))
    monkeypatch.setattr(A, "_TG", types.SimpleNamespace(
        send=lambda **k: (tg_calls.append(k) or True)))

    import bot_squad_worker.refresh_oauth as RO
    monkeypatch.setattr(RO, "refresh_oauth", lambda _cfg: {"ok": False, "action": "failed", "detail": "creds expired"})

    oauth_refresh(cfg)

    # TG (primary) carried the urgent FAILED page — one delivery into #team-queries.
    assert len(tg_calls) == 1 and tg_calls[0]["chat_id"] == proj.tg_chat
    assert tg_calls[0]["urgent"] is True and "creds expired" in tg_calls[0]["text"]
    # MAX is reserve-only now — no duplicate personal ping (T-0610).
    assert max_calls == []


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


def test_binding_gc_tick_runs_gc_tmux_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0200: binding_gc_tick must invoke gc_tmux_sessions per project so idle
    empty per-team tmux sessions are reaped on the 60s tick."""
    from bot_squad_worker.jobs import binding_gc_tick
    from bot_squad_worker import sessions as _sessions

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    gc_calls: list[str] = []
    monkeypatch.setattr(
        _sessions, "gc_tmux_sessions",
        lambda c, s: gc_calls.append(s) or {"ok": True, "reaped": []},
    )

    binding_gc_tick(cfg)
    assert gc_calls == [proj.slug]


def test_binding_gc_tick_runs_gc_stale_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0484: binding_gc_tick must invoke gc_stale_tasks per project so the
    age-based task-cleanup pass runs alongside the throwaway GC."""
    from bot_squad_worker.jobs import binding_gc_tick
    from bot_squad_worker import task_gc as _task_gc

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    stale_calls: list[str] = []
    monkeypatch.setattr(
        _task_gc, "gc_stale_tasks",
        lambda c, s: stale_calls.append(s) or {"archived": []},
    )

    binding_gc_tick(cfg)
    assert stale_calls == [proj.slug]


def test_binding_gc_tick_swallows_gc_tmux_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing gc_tmux_sessions pass must not kill the sweep."""
    from bot_squad_worker.jobs import binding_gc_tick
    from bot_squad_worker import sessions as _sessions

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    monkeypatch.setattr(
        _sessions, "gc_tmux_sessions",
        lambda c, s: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # Must not raise.
    binding_gc_tick(cfg)


def test_voice_audio_gc_tick_runs_gc_audio_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0433 P3: voice_audio_gc_tick must call voice_intake.gc_audio per project
    so triaged/aged audio blobs are reaped."""
    from bot_squad_worker.jobs import voice_audio_gc_tick
    from bot_squad_worker import voice_intake as _vi

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    gc_calls: list[str] = []
    monkeypatch.setattr(
        _vi, "gc_audio",
        lambda c, s: gc_calls.append(s) or {"slug": s, "removed": 0, "removed_files": []},
    )

    voice_audio_gc_tick(cfg)
    assert gc_calls == [proj.slug]


def test_voice_audio_gc_tick_swallows_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing gc_audio pass must not kill the sweep."""
    from bot_squad_worker.jobs import voice_audio_gc_tick
    from bot_squad_worker import voice_intake as _vi

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    monkeypatch.setattr(
        _vi, "gc_audio",
        lambda c, s: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # Must not raise.
    voice_audio_gc_tick(cfg)


def test_surface_live_dup_reconciles_alerts_only_on_live_loser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0227: binding_gc_tick surfaces a gc_stale_bindings reconcile to the
    operator ONLY when the stripped loser was LIVE (a genuine concurrent-spawn
    race — the rogue session is still running). A crash-only strip (was_live
    False) stays silent."""
    from bot_squad_worker.jobs import _surface_live_dup_reconciles
    from bot_squad_worker import jobs as J

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    alerts: list[str] = []
    monkeypatch.setattr(J, "_alert_operators", lambda c, s, p, text: alerts.append(text))

    # Live loser → one alert that names the task + the rogue SID + "kill".
    _surface_live_dup_reconciles(cfg, proj.slug, {"details": [
        {"sid": "S-u-dev-p60", "task_id": "T-0026", "winner": "S-u-a-p5", "was_live": True},
    ]})
    assert len(alerts) == 1
    assert "T-0026" in alerts[0] and "S-u-dev-p60" in alerts[0] and "Kill" in alerts[0]

    # Crash-only strip (was_live False) → silent.
    alerts.clear()
    _surface_live_dup_reconciles(cfg, proj.slug, {"details": [
        {"sid": "S-u-x-p1", "task_id": "T-0026", "winner": "S-u-a-p5", "was_live": False},
    ]})
    assert alerts == []

    # Non-dict / no details → no raise, no alert.
    _surface_live_dup_reconciles(cfg, proj.slug, None)
    _surface_live_dup_reconciles(cfg, proj.slug, {})
    assert alerts == []
