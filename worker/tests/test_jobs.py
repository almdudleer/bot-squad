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
        delivery: dict | None = None,
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
# T-0744: the heartbeat — not process start — is what retires an in-flight
# restart marker. The API's whole notion of "the worker is up" is this file, so
# clearing any earlier hands health a window where a SUCCESSFUL restart reports
# the bare sha_drift that means the opposite (62s of a measured 73s window).
# ---------------------------------------------------------------------------


def _write_inflight(cfg: Config, at: float) -> Path:
    """Drop an in-flight-restart marker recorded at `at`, as the dying worker would."""
    import json as _j

    p = cfg.data_dir / "_worker" / "restart_inflight.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_j.dumps({
        "at": at, "queue_id": "q1", "slug": "test-project",
        "reason": "worker change", "target_sha": "f" * 40,
        "expected_by": at + 180,
    }))
    return p


def _fresh_process(monkeypatch: pytest.MonkeyPatch, started_at: float) -> None:
    """Pretend this interpreter is a worker that booted at `started_at`.

    Both bits of state are per-PROCESS in production (set once at import); the
    test suite shares one interpreter, so they have to be reset explicitly.
    """
    import bot_squad_worker.jobs as J

    monkeypatch.setattr(J, "_PROCESS_STARTED_AT", started_at)
    monkeypatch.setattr(J, "_inflight_marker_cleared", False)


def test_heartbeat_clears_the_marker_of_the_restart_that_produced_it(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core boundary move: the marker survives right up to the first
    heartbeat and is gone the moment that heartbeat lands — so `restart_pending`
    covers the whole span in which the API still sees the OLD worker."""
    import bot_squad_worker.deploy as D

    cfg = Config.load(tmp_config_dir)
    now = time.time()
    marker = _write_inflight(cfg, at=now - 30)       # written by the dying worker
    _fresh_process(monkeypatch, started_at=now - 10)  # ...we booted after it

    assert D.restart_pending_state(cfg)["state"] == "in_flight"
    heartbeat(cfg)
    assert not marker.exists()
    assert D.restart_pending_state(cfg) is None
    heartbeat(cfg)  # idempotent — a steady-state worker with nothing to clear


def test_a_failed_heartbeat_write_leaves_the_marker_alone(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ORDERING PIN the DoD asks for, at runtime: clearing is not reachable
    before a heartbeat has actually been written. If the write fails the worker
    is not observable to the API, so the restart has NOT landed and the marker
    must survive — and the next heartbeat gets another go at it."""
    import os

    import bot_squad_worker.deploy as D

    cfg = Config.load(tmp_config_dir)
    now = time.time()
    marker = _write_inflight(cfg, at=now - 30)
    _fresh_process(monkeypatch, started_at=now - 10)

    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        heartbeat(cfg)
    assert marker.exists(), "the marker was cleared without the worker becoming visible"
    assert D.restart_pending_state(cfg)["state"] == "in_flight"

    monkeypatch.setattr(os, "replace", real_replace)
    heartbeat(cfg)
    assert not marker.exists()


def test_heartbeat_never_clears_a_marker_this_process_wrote(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest-lifetime guard in the other direction (T-0717's alarm).

    A worker that LAUNCHES a restart writes the marker and then keeps
    heartbeating until systemd stops it. Those heartbeats must not clear it: if
    the restart fails to land, the "launched at X and never came up" record is
    the whole alarm. Only a marker predating this process is ours to retire.
    """
    import bot_squad_worker.deploy as D

    cfg = Config.load(tmp_config_dir)
    now = time.time()
    _fresh_process(monkeypatch, started_at=now - 600)  # long-running worker...
    marker = _write_inflight(cfg, at=now)              # ...launches a restart now

    heartbeat(cfg)
    assert marker.exists()
    state = D.restart_pending_state(cfg)
    assert state["state"] == "in_flight" and state["overdue"] is False
    # ...and once expected_by lapses it is overdue → health reverts to a bare
    # sha_drift. Nothing here is a blanket post-deploy grace period.
    _write_inflight(cfg, at=now - 10_000)
    assert D.restart_pending_state(cfg)["overdue"] is True


def test_only_the_heartbeat_may_clear_the_in_flight_marker(
    tmp_config_dir: Path,
) -> None:
    """The ordering pin at the source level, and the one that survives a refactor.

    T-0739 cleared the marker from ``__main__`` at process start, ~60s before the
    API could see the new worker. The runtime tests above pin the heartbeat path;
    this pins that no OTHER caller reintroduces an earlier one — a second clear
    site anywhere in the worker silently restores the bug with nothing red to
    notice, which is exactly how it survived T-0739 review.
    """
    import bot_squad_worker

    pkg = Path(bot_squad_worker.__file__).parent
    callers = sorted(
        p.name for p in pkg.glob("*.py")
        if "clear_restart_inflight(" in p.read_text()
    )
    # deploy.py DEFINES it; jobs.py (the heartbeat) is the only caller.
    assert callers == ["deploy.py", "jobs.py"], (
        f"unexpected clear_restart_inflight call site(s): {callers}"
    )
    assert "clear_restart_inflight" not in (pkg / "__main__.py").read_text()


def test_scheduler_fires_the_first_heartbeat_immediately(
    tmp_config_dir: Path,
) -> None:
    """T-0744: apscheduler's interval trigger defaults the first run to
    start+interval, so the new worker stayed invisible to the API for a full 60s
    after boot — most of every measured restart window, and (now that the clear
    rides the heartbeat) it would also hold the marker open that long."""
    import datetime as _dt

    from bot_squad_worker.scheduler import build_scheduler

    cfg = Config.load(tmp_config_dir)
    sched = build_scheduler(cfg)
    hb = next(j for j in sched.get_jobs() if j.id == "heartbeat")
    # A job the scheduler has not started yet only carries next_run_time when one
    # was passed explicitly — absent IS the bug (apscheduler would then compute
    # start+interval, i.e. 60s out) so say so rather than raising AttributeError.
    first_run = getattr(hb, "next_run_time", None)
    assert first_run is not None, "heartbeat job has no explicit first run time"
    lead = (first_run - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    assert lead < 5, f"first heartbeat is {lead:.0f}s away; must be ~immediate"


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

    # A real roster on disk: one LIVE operator, one dev, and one SUPERSEDED
    # operator in the SAME tmux window as the live one.
    #
    # T-0920 moved the recipient resolution off a hand-rolled list_sessions walk
    # and onto dispatch.live_operator_sids (the T-0523 identity SSOT), so this
    # writes session mds and lets the REAL filter run rather than stubbing the
    # answer. That is a stronger test than the stub it replaces: the stub could
    # only show the caller passing through whatever it was handed, while the
    # suspended predecessor below is the thing that actually broke — its literal
    # SID resolves to the live successor (T-0790), which is how ONE kill became
    # 32 identical lines in one operator's inbox on 2026-08-18 13:18Z.
    import bot_squad_worker.dispatch as DISP
    import bot_squad_worker.intersession as IS
    sess = cfg.data_dir / proj.slug / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    for sid, window, role, status in (
        ("S-u-operator-p2", "operator", "operator", "active"),
        ("S-u-operator-p1", "operator", "operator", "suspended"),
        ("S-u-dev-p9", "dev", "dev", "active"),
    ):
        (sess / f"{sid}.md").write_text(
            f"---\nsid: {sid}\nwindow: {window}\nrole: {role}\n"
            f"status: {status}\narchived: false\nlinux_user: u\n---\n"
        )
    # No tmux in a test env — pin the SSOT's pane-scan half so the md scan is
    # what the assertions read.
    monkeypatch.setattr(DISP, "_live_operator_sids_from_tmux", lambda slug, seen: [])
    peers: list[tuple[str, str]] = []
    monkeypatch.setattr(
        IS, "send",
        lambda _cfg, _slug, frm, to, text, user=None: peers.append((to, text)) or {"ok": True},
    )

    deploy_monitor_one(cfg, proj.slug)

    # Urgent TG carried the loud KILLED marker.
    kill_tgs = [c for c in fake_tg.calls if "KILLED" in c["text"]]
    assert kill_tgs and all(c["urgent"] is True for c in kill_tgs)
    # Targeted peer_send went to the LIVE operator SID only — never the dev,
    # never role=all, and never the suspended predecessor (which would land a
    # SECOND copy in the same live inbox).
    assert [to for to, _ in peers] == ["S-u-operator-p2"], peers
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


def test_binding_gc_tick_runs_auto_pause_unheld_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0889: the auto-pause has no other trigger. It is the 60s reconcile tick
    or it never runs — which is what a status "auto-set when the last session
    working a ticket terminates" means."""
    from bot_squad_worker.jobs import binding_gc_tick
    from bot_squad_worker import task_gc as _task_gc

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    pause_calls: list[str] = []
    monkeypatch.setattr(
        _task_gc, "auto_pause_unheld_tasks",
        lambda c, s: pause_calls.append(s) or {"paused": []},
    )

    binding_gc_tick(cfg)
    assert pause_calls == [proj.slug]


def test_binding_gc_tick_swallows_auto_pause_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0889: a bad board file must cost the auto-pause, not the whole sweep —
    every session-graph reconciler in this tick runs after it."""
    from bot_squad_worker.jobs import binding_gc_tick
    from bot_squad_worker import task_gc as _task_gc
    from bot_squad_worker import teams as _teams

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    monkeypatch.setattr(
        _task_gc, "auto_pause_unheld_tasks",
        lambda c, s: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    later: list[str] = []
    monkeypatch.setattr(
        _teams, "reconcile_teams", lambda c, s: later.append(s) or {"ok": True})

    binding_gc_tick(cfg)  # must not raise
    assert later == [proj.slug]  # and the passes AFTER it still ran


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


# ---------------------------------------------------------------------------
# T-0824: the heartbeat also publishes the INSTALL TREE's sha
# ---------------------------------------------------------------------------
# The API container cannot compute this itself — it mounts ./config and ./data
# and nothing else, so there is no git tree inside it. The worker is the only
# process running FROM the install tree, which is why the term has to be
# published rather than read. It rides the heartbeat because that is the tick
# whose output the API already treats as this install's ground truth.


def test_heartbeat_publishes_the_install_tree_sha(tmp_config_dir: Path, monkeypatch) -> None:
    """The term exists on disk after one tick, in its own file — and it is the
    TREE's sha, not the worker's. Those are different values whenever it matters
    (see test_deploy.py's real-repo cases); here they are stubbed apart so a
    regression that published `effective_worker_git_sha()` twice is caught."""
    import json as _j

    import bot_squad_worker.deploy as D

    monkeypatch.setattr(D, "effective_worker_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(D, "install_tree_git_sha", lambda: "e" * 40)
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat(cfg)

    assert cfg.heartbeat_path.read_text().strip() == "a" * 40
    marker = _j.loads((cfg.heartbeat_path.parent / "install_tree.json").read_text())
    assert marker["git_sha"] == "e" * 40


def test_a_separate_file_so_an_OLD_api_container_reads_an_unchanged_heartbeat(
    tmp_config_dir: Path, monkeypatch
) -> None:
    """Why not a second line in the heartbeat body: `routes_health` reads that
    file as `read_text().strip()` — the WHOLE body is the sha. An api container
    predating T-0824 seeing a two-line body would compare "sha\\nsha" against its
    own and report a false `sha_drift` for the entire window between a worker
    restart and the next api rebuild. A new file is invisible to an old reader,
    so the heartbeat body must stay exactly one sha."""
    import bot_squad_worker.deploy as D

    monkeypatch.setattr(D, "effective_worker_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(D, "install_tree_git_sha", lambda: "e" * 40)
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat(cfg)

    body = cfg.heartbeat_path.read_text()
    assert body == "a" * 40 + "\n"
    assert body.strip().splitlines() == ["a" * 40]


def test_a_failed_install_publish_never_costs_the_heartbeat(
    tmp_config_dir: Path, monkeypatch
) -> None:
    """Ordering is load-bearing: liveness is the signal an outage depends on, the
    install term is a diagnostic. Losing the former to publish the latter would
    be a strictly worse trade — a dead_heartbeat alarm on a healthy worker."""
    import bot_squad_worker.deploy as D

    def _boom(_cfg):
        raise RuntimeError("disk full")

    monkeypatch.setattr(D, "effective_worker_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(D, "publish_install_tree_sha", _boom)
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat(cfg)

    assert cfg.heartbeat_path.read_text().strip() == "a" * 40
    assert not (cfg.heartbeat_path.parent / "install_tree.json").exists()


# ---------------------------------------------------------------------------
# T-0880: the heartbeat carries ONE sha and it is the coordinator's. A per-user
# worker running four-day-old code appeared in no field the system had, so an
# install reported converged while the process serving the deploy's own target
# path was days behind. The heartbeat now publishes a census beside itself.
# ---------------------------------------------------------------------------

def _serve_health(sock_path: Path, sha: str):
    """A real worker-shaped /health on a real UDS (see test_worker_census)."""
    import http.server
    import json as _json
    import socket
    import socketserver
    import threading

    class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
        address_family = socket.AF_UNIX
        daemon_threads = True

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = 0

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = _json.dumps(
                {"ok": True, "git_sha": sha, "boot_git_sha": sha, "uptime": 1.0}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    s = Srv(str(sock_path), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def test_heartbeat_publishes_a_census_naming_a_stale_per_user_worker(
    tmp_config_dir: Path, monkeypatch
) -> None:
    """DoD 4: two workers on ONE config — a converged coordinator and a stale
    per-user worker — and the published report must distinguish them.

    Goes red against pre-T-0880 behaviour for the reason the ticket names: there
    was no per-worker report at all, so "release deployed" could mean "one of N
    workers reloaded"."""
    import json

    import bot_squad_worker.deploy as D

    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    sock_dir = cfg.data_dir / "_sock"
    sock_dir.mkdir(parents=True, exist_ok=True)

    deployed = "d" * 40
    monkeypatch.setattr(D, "install_tree_git_sha", lambda: deployed)
    monkeypatch.setattr(D, "effective_worker_git_sha", lambda: deployed)

    servers = [
        _serve_health(sock_dir / "worker.sock", deployed),
        _serve_health(sock_dir / "user-flomaster.sock", "5" * 40),
    ]
    try:
        heartbeat(cfg)
        body = json.loads((cfg.heartbeat_path.parent / "workers.json").read_text())
    finally:
        for s in servers:
            s.shutdown()

    assert body["all_converged"] is False
    states = {w["kind"]: w["state"] for w in body["workers"]}
    assert states == {"coordinator": "converged", "user": "stale"}
    # NAMED, not just counted.
    assert "flomaster" in body["line"]


def test_census_failure_never_costs_the_heartbeat(tmp_config_dir: Path, monkeypatch) -> None:
    """The census sits beside the liveness signal and must never be able to take
    it down — same contract as the install-tree marker it follows."""
    from bot_squad_worker import worker_census

    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)

    def _boom(*a, **k):
        raise RuntimeError("census exploded")

    monkeypatch.setattr(worker_census, "census", _boom)

    heartbeat(cfg)

    assert cfg.heartbeat_path.exists()
    assert not (cfg.heartbeat_path.parent / "workers.json").exists()


def test_t1062_stalled_input_queue_rings_once_and_rearms_on_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-1062 DoD 2: a queue nothing can drain must ring — once.

    Undeliverability was silent by construction, so a deaf session and a quiet
    one looked identical and the fleet's alarm handler reported "all quiet" for
    two hours. The two failure modes of the fix are missing the stall and
    crying every tick until someone mutes it; this pins both ends.
    """
    from bot_squad_worker.jobs import _surface_stalled_inputs
    from bot_squad_worker import jobs as J

    proj = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, proj)

    alerts: list[str] = []
    monkeypatch.setattr(J, "_alert_operators", lambda c, s, p, text: alerts.append(text))
    monkeypatch.setattr(J, "_STALL_SEEN", set())

    stalled = {"stalled": [{"sid": "S-u-routine-handler-p780", "pane": "%42",
                            "records": 2, "oldest_age_sec": 680.0,
                            "blocked_by": "draft"}]}

    _surface_stalled_inputs(cfg, stalled)
    assert len(alerts) == 1
    assert "S-u-routine-handler-p780" in alerts[0] and "%42" in alerts[0]
    assert "11 min" in alerts[0]

    # Still stalled on the next tick → NOT a second page.
    _surface_stalled_inputs(cfg, stalled)
    assert len(alerts) == 1

    # Recovered (the sweep no longer reports it) → the alarm re-arms, so the
    # NEXT stall of the same session is heard.
    _surface_stalled_inputs(cfg, {"stalled": []})
    _surface_stalled_inputs(cfg, stalled)
    assert len(alerts) == 2

    # Junk in, no raise, no page.
    alerts.clear()
    _surface_stalled_inputs(cfg, None)
    _surface_stalled_inputs(cfg, {})
    assert alerts == []
