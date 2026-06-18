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
    # write minimal projects.toml — honour the project's optional repo_master /
    # repo_deploy / deploy_targets so deploy-clone tests round-trip through the
    # real Config.load() path.
    lines = [
        f"[projects.{project.slug}]",
        f'slug = "{project.slug}"',
        f'display_name = "{project.display_name}"',
        f'repo_path = "{project.repo_path}"',
    ]
    if project.repo_master is not None:
        lines.append(f'repo_master = "{project.repo_master}"')
    if project.repo_deploy is not None:
        lines.append(f'repo_deploy = "{project.repo_deploy}"')
    targets = ", ".join(f'"{t}"' for t in project.deploy_targets)
    lines += [
        f'deploy_branch = "{project.deploy_branch}"',
        f'master_branch = "{project.master_branch}"',
        'prod_url = ""',
        'staging_url = ""',
        'dev_url = ""',
        f"deploy_targets = [{targets}]",
        'tg_chat = "0"',
        "created_at = 2026-05-10",
    ]
    (cfg_dir / "projects.toml").write_text("\n".join(lines) + "\n")
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


# ---------------------------------------------------------------------------
# T-0116: local-only-commits guard
# ---------------------------------------------------------------------------


def _attach_origin(repo: Path, tmp_path: Path) -> Path:
    """Give ``repo`` a bare upstream at ``origin`` whose HEAD == repo's HEAD.

    Simulates a normal install: HEAD matches origin, no local-only commits.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=str(repo), check=True
    )
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "-q", "origin", branch], cwd=str(repo), check=True
    )
    return bare


def _add_local_commit(repo: Path, filename: str = "local.txt") -> str:
    """Add a commit that exists only locally. Returns the short SHA."""
    (repo / filename).write_text("direct edit on install")
    subprocess.run(["git", "add", filename], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"direct edit ({filename})"],
        cwd=str(repo), check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


def test_run_next_refuses_local_only_commits(tmp_path: Path, caplog) -> None:
    """A direct-install commit on the target clone blocks the deploy (T-0116)."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    _attach_origin(proj.repo_path, tmp_path)
    sha = _add_local_commit(proj.repo_path)

    enqueue(cfg, proj.slug, "staging", "should be blocked", "user")

    import logging
    with caplog.at_level(logging.ERROR):
        result = run_next(cfg, proj.slug)

    assert result is None
    # Queue file must remain so the agent can re-deploy after cherry-picking.
    queue_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "queue"
    assert len(list(queue_dir.glob("*.json"))) == 1
    # The SHA of the offending commit must surface in the log.
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert sha in joined
    assert "local-only" in joined


def test_run_next_env_bypass_allows_local_only_commits(
    tmp_path: Path, monkeypatch
) -> None:
    """BOT_SQUAD_DEPLOY_ALLOW_LOCAL_COMMITS=1 lets the deploy through."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    _attach_origin(proj.repo_path, tmp_path)
    _add_local_commit(proj.repo_path)

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_ALLOW_LOCAL_COMMITS", "1")

    queue_id = enqueue(cfg, proj.slug, "staging", "bypass", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is True
    assert result.queue_id == queue_id


def test_run_next_proceeds_when_in_sync_with_origin(tmp_path: Path) -> None:
    """Origin attached, no local-only commits → deploy runs normally."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    _attach_origin(proj.repo_path, tmp_path)

    queue_id = enqueue(cfg, proj.slug, "staging", "in sync", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is True
    assert result.queue_id == queue_id


def test_run_next_proceeds_when_no_origin_configured(tmp_path: Path) -> None:
    """Fresh repo with no origin (or no matching upstream ref) deploys fine.

    Fail-open is intentional: the guard exists to catch the "edited on the
    install dir" anti-pattern, not to wedge fresh-host installs where the
    upstream hasn't been wired up yet.
    """
    proj = _make_project(tmp_path)  # no origin attached
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)

    queue_id = enqueue(cfg, proj.slug, "staging", "no origin", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is True
    assert result.queue_id == queue_id


def test_is_clean_for_target_flags_local_only_commits(tmp_path: Path) -> None:
    """deploy_monitor relies on is_clean_for_target for its pre-ping gate."""
    from bot_squad_worker.deploy import is_clean_for_target

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)

    assert is_clean_for_target(cfg, proj.slug, "staging") is True

    _add_local_commit(proj.repo_path)
    assert is_clean_for_target(cfg, proj.slug, "staging") is False


# ---------------------------------------------------------------------------
# T-0143: local CI/CD deploy clone
# ---------------------------------------------------------------------------


def _make_deploy_project(
    tmp_path: Path, targets: tuple[str, ...] = ("staging",)
) -> Project:
    """Dev clone on ``bot_squad/dev`` + a (not-yet-created) sibling deploy clone.

    The deploy clone path intentionally does NOT exist yet — provisioning is
    the deploy worker's job on first deploy (T-0143).
    """
    repo = tmp_path / "devclone"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "bot_squad/dev"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
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
        repo_deploy=tmp_path / "deployclone",
    )


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _recipe_recording_head(
    tmp_path: Path, cfg: Config, slug: str, target: str, head_file: Path
) -> Path:
    """A recipe that records the HEAD of its cwd (the clone it runs in)."""
    recipe_dir = cfg.data_dir / slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"{target}.sh"
    recipe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'git rev-parse HEAD > "{head_file}"\n'
    )
    recipe.chmod(0o755)
    return recipe


def test_config_repo_for_target_prefers_deploy_clone(tmp_path: Path) -> None:
    proj = _make_deploy_project(tmp_path)
    assert proj.repo_for_target("staging") == proj.repo_deploy
    # The editing clone (where unpushed commits are guarded) stays the dev clone.
    assert proj.editing_repo_for_target("staging") == proj.repo_path
    assert proj.uses_deploy_clone("staging") is True


def test_run_next_provisions_deploy_clone_on_first_deploy(tmp_path: Path) -> None:
    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)  # origin/bot_squad/dev
    head_file = tmp_path / "deploy_head.txt"
    _recipe_recording_head(tmp_path, cfg, proj.slug, "staging", head_file)

    assert not proj.repo_deploy.exists()  # not provisioned yet

    enqueue(cfg, proj.slug, "staging", "first deploy", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None and result.ok is True
    # Deploy clone was created, on bot_squad/dev, at origin's tip.
    assert (proj.repo_deploy / ".git").exists()
    assert _head(proj.repo_deploy) == _head(proj.repo_path)
    # The recipe ran INSIDE the deploy clone (its recorded HEAD matches).
    assert head_file.read_text().strip() == _head(proj.repo_deploy)


def test_run_next_deploy_clone_ignores_dirty_dev_tree(tmp_path: Path) -> None:
    """THE ticket smoke: a dirty dev tree no longer blocks the deploy."""
    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)

    # Dirty the dev clone with uncommitted edits — the old behaviour deferred.
    (proj.repo_path / "WIP.txt").write_text("uncommitted work in progress")

    queue_id = enqueue(cfg, proj.slug, "staging", "dirty dev tree", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is True
    assert result.queue_id == queue_id


def test_run_next_deploy_clone_refreshes_to_origin(tmp_path: Path) -> None:
    """An existing deploy clone fast-forwards to the latest pushed commit."""
    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    head_file = tmp_path / "deploy_head.txt"
    _recipe_recording_head(tmp_path, cfg, proj.slug, "staging", head_file)

    # First deploy provisions the clone at commit 1.
    enqueue(cfg, proj.slug, "staging", "deploy 1", "user")
    assert run_next(cfg, proj.slug).ok is True
    commit1 = _head(proj.repo_deploy)
    assert head_file.read_text().strip() == commit1

    # Land + PUSH a new commit to origin from the dev clone.
    (proj.repo_path / "feature.txt").write_text("new pushed work")
    subprocess.run(["git", "add", "feature.txt"], cwd=str(proj.repo_path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feature"], cwd=str(proj.repo_path), check=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "bot_squad/dev"],
        cwd=str(proj.repo_path), check=True,
    )
    commit2 = _head(proj.repo_path)
    assert commit2 != commit1

    # Second deploy refreshes the existing clone to the new origin tip.
    enqueue(cfg, proj.slug, "staging", "deploy 2", "user")
    assert run_next(cfg, proj.slug).ok is True
    assert _head(proj.repo_deploy) == commit2
    assert head_file.read_text().strip() == commit2


def test_run_next_deploy_clone_refuses_unpushed_dev_commits(
    tmp_path: Path, caplog
) -> None:
    """Commit-guard still fires — but now against the dev (editing) clone.

    A commit on dev that isn't on origin would be silently omitted by a deploy
    that ships origin/<branch>, so refuse until it's pushed.
    """
    import logging

    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)

    sha = _add_local_commit(proj.repo_path)  # committed to dev, NOT pushed

    enqueue(cfg, proj.slug, "staging", "should be refused", "user")
    with caplog.at_level(logging.ERROR):
        result = run_next(cfg, proj.slug)

    assert result is None
    queue_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "queue"
    assert len(list(queue_dir.glob("*.json"))) == 1  # queue file preserved
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert sha in joined
    assert "local-only" in joined


# ---------------------------------------------------------------------------
# T-0212: no-progress watchdog + hard-timeout + stale-orphan reaper
# ---------------------------------------------------------------------------


def _make_hanging_recipe(
    cfg: Config, slug: str, target: str, prelude: str = 'echo "starting"'
) -> Path:
    """Recipe that emits one line then hangs forever with NO further output.

    Mimics buildx frozen at `COPY web/ .` (0% CPU, silent run-log) — the live
    watchrobot symptom the no-progress watchdog must catch.
    """
    recipe_dir = cfg.data_dir / slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"{target}.sh"
    recipe.write_text(
        "#!/usr/bin/env bash\n" + prelude + "\nwhile true; do sleep 60; done\n"
    )
    recipe.chmod(0o755)
    return recipe


def test_run_next_no_progress_watchdog_kills_hung_build(
    tmp_path: Path, monkeypatch
) -> None:
    """A build with no run-log output for N seconds is killed + failed loudly.

    THE ticket smoke (T-0212): no exception is raised (the old
    subprocess.run(timeout=) raised TimeoutExpired, stranding the file in
    processing/ forever), the queue file lands in processed/.fail.<RC_NO_PROGRESS>,
    and the run-log carries a loud WATCHDOG marker.
    """
    from bot_squad_worker.deploy import RC_NO_PROGRESS

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_POLL_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_TIMEOUT", "60")  # backstop far away

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_hanging_recipe(cfg, proj.slug, "staging")

    enqueue(cfg, proj.slug, "staging", "hung build", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is False
    assert result.returncode == RC_NO_PROGRESS
    assert result.killed_reason == "no_progress"

    base = cfg.data_dir / proj.slug / "_jobs" / "deploy"
    assert list((base / "processing").glob("*.json")) == []  # nothing stranded
    assert len(list((base / "processed").glob(f"*.fail.{RC_NO_PROGRESS}"))) == 1
    log_text = result.log_path.read_text()
    assert "WATCHDOG" in log_text and "no run-log output" in log_text


def test_run_next_hard_timeout_kills_long_build(tmp_path: Path, monkeypatch) -> None:
    """A build that keeps printing (no-progress never fires) but runs too long
    is killed by the wall-clock backstop with RC_TIMEOUT."""
    from bot_squad_worker.deploy import RC_TIMEOUT

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS", "60")  # never fires
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_POLL_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_TIMEOUT", "2")

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    # Chatty recipe: emits output continuously so only the hard timeout can fire.
    recipe_dir = cfg.data_dir / proj.slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / "staging.sh"
    recipe.write_text(
        "#!/usr/bin/env bash\nwhile true; do echo tick; sleep 0.2; done\n"
    )
    recipe.chmod(0o755)

    enqueue(cfg, proj.slug, "staging", "slow build", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None and result.ok is False
    assert result.returncode == RC_TIMEOUT
    assert result.killed_reason == "timeout"
    assert "WATCHDOG" in result.log_path.read_text()


def test_watchdog_kills_whole_process_group(tmp_path: Path, monkeypatch) -> None:
    """The recipe's CHILD (docker/npm stand-in) is killed too, not just bash.

    The manual walkthrough surfaced this: killing only the bash wrapper would
    orphan the actual build child. start_new_session + killpg fixes it.
    """
    import os
    import time as _time

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_POLL_SECONDS", "1")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_TIMEOUT", "60")

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    pid_file = tmp_path / "child.pid"
    recipe_dir = cfg.data_dir / proj.slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / "staging.sh"
    # Background child writes its pid, then both parent + child hang silently.
    recipe.write_text(
        "#!/usr/bin/env bash\n"
        'echo "starting"\n'
        "( sleep 300 ) &\n"
        f'echo $! > "{pid_file}"\n'
        "wait\n"
    )
    recipe.chmod(0o755)

    enqueue(cfg, proj.slug, "staging", "child-leak test", "user")
    result = run_next(cfg, proj.slug)
    assert result is not None and result.killed_reason == "no_progress"

    child_pid = int(pid_file.read_text().strip())
    _time.sleep(0.5)  # let the kill settle
    # The child must be gone (os.kill 0 raises ProcessLookupError once reaped).
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_reap_orphans_sweeps_stale_processing(tmp_path: Path, monkeypatch) -> None:
    """A file stranded in processing/ past max-age is swept to processed/.fail."""
    import os
    import time as _time
    from bot_squad_worker.deploy import RC_ORPHAN, reap_orphans

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    proc_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processing"
    proc_dir.mkdir(parents=True, exist_ok=True)
    orphan = proc_dir / "1700000000000-deadbeef-orphan.json"
    orphan.write_text(json.dumps({"queue_id": "deadbeef-orphan", "target": "staging"}))
    old = _time.time() - 3 * 3600  # 3h old
    os.utime(orphan, (old, old))

    reaped = reap_orphans(cfg, proj.slug)  # default max-age 7200s

    assert len(reaped) == 1
    assert reaped[0]["queue_id"] == "deadbeef-orphan"
    assert reaped[0]["age_seconds"] >= 7200
    assert list(proc_dir.glob("*.json")) == []
    processed = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processed"
    assert len(list(processed.glob(f"*.fail.{RC_ORPHAN}"))) == 1


def test_reap_orphans_ignores_fresh_processing(tmp_path: Path) -> None:
    """A freshly-moved processing file (a live deploy) is NOT reaped."""
    from bot_squad_worker.deploy import reap_orphans

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    proc_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "processing"
    proc_dir.mkdir(parents=True, exist_ok=True)
    fresh = proc_dir / "1700000000000-fresh.json"
    fresh.write_text(json.dumps({"queue_id": "fresh", "target": "staging"}))
    # mtime = now (default) → well under the 7200s default max-age.

    reaped = reap_orphans(cfg, proj.slug)

    assert reaped == []
    assert len(list(proc_dir.glob("*.json"))) == 1


def test_is_clean_for_target_deploy_clone_ignores_dev_dirtiness(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import is_clean_for_target

    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)

    # Dirty dev tree — must NOT gate a deploy-clone deploy.
    (proj.repo_path / "WIP.txt").write_text("uncommitted")
    assert is_clean_for_target(cfg, proj.slug, "staging") is True

    # But an unpushed dev commit must still gate.
    _add_local_commit(proj.repo_path)
    assert is_clean_for_target(cfg, proj.slug, "staging") is False
