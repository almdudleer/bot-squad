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
from bot_squad_worker.deploy import (
    DeployResult,
    _processed_dir,
    _processing_dir,
    _recipe_path,
    _runs_dir,
    _tracked_recipe_path,
    enqueue,
    list_queued,
    prune_processed_runs,
    run_next,
)


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


@pytest.fixture(autouse=True)
def _no_systemd_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default deploy children to a plain `bash` invocation in the test suite.

    This dev host HAS a user systemd manager, so without this the recipe runner
    would wrap every recipe in a real transient scope (noise + flakiness). The
    scope-gating tests opt back IN by setting the env knob to "1" themselves.
    """
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "0")


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


def test_is_clean_ignores_claude_session_state(tmp_path: Path) -> None:
    """T-0204: a clone dirty ONLY in .claude/ reads as clean.

    Mirrors test_run_next_skips_when_dirty but for a project that git-TRACKS
    per-session Claude scratch (e.g. watchrobot's .claude/scheduled_tasks.lock,
    rewritten every session). That dirtiness must never block a deploy.
    """
    from bot_squad_worker.deploy import _is_clean

    proj = _make_project(tmp_path)
    repo = proj.repo_path

    # Commit a tracked .claude/ file, then mutate it so the tree is dirty
    # ONLY in .claude/ (as an active session would).
    claude_dir = repo / ".claude"
    claude_dir.mkdir()
    lock = claude_dir / "scheduled_tasks.lock"
    lock.write_text("v1\n")
    subprocess.run(["git", "add", ".claude/scheduled_tasks.lock"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "track claude state"], cwd=str(repo), check=True)
    assert _is_clean(repo) is True

    # Tracked .claude/ file rewritten by a live session → still clean.
    lock.write_text("v2 — session active\n")
    assert _is_clean(repo) is True

    # An untracked .claude/ file is likewise ignored.
    (claude_dir / "task_id").write_text("T-0204\n")
    assert _is_clean(repo) is True

    # But a real source change still flags the tree dirty.
    (repo / "src.py").write_text("print('changed')\n")
    assert _is_clean(repo) is False


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
# T-0205: version-controlled recipes (deploy-recipes/<slug>/<target>.sh)
# preferred over the legacy runtime copy under data/.
# ---------------------------------------------------------------------------


def _make_tracked_recipe(
    cfg: Config, slug: str, target: str, rc: int = 0, marker: str = "tracked"
) -> Path:
    """Drop a version-controlled recipe at <install>/deploy-recipes/<slug>/<target>.sh."""
    recipe_dir = cfg.config_dir.parent / "deploy-recipes" / slug
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"{target}.sh"
    recipe.write_text(
        f'#!/usr/bin/env bash\nset -euo pipefail\necho "{marker} recipe running"\nexit {rc}\n'
    )
    recipe.chmod(0o755)
    return recipe


def test_recipe_path_falls_back_to_data_when_untracked(tmp_path: Path) -> None:
    """No tracked copy → resolve the legacy data/<slug>/deploy/<target>.sh."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    legacy = _make_recipe(tmp_path, cfg, proj.slug, "staging")

    assert _recipe_path(cfg, proj.slug, "staging") == legacy


def test_recipe_path_prefers_tracked_over_data(tmp_path: Path) -> None:
    """A version-controlled copy wins over the legacy runtime copy."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging")  # legacy data/ copy present
    tracked = _make_tracked_recipe(cfg, proj.slug, "staging")

    resolved = _recipe_path(cfg, proj.slug, "staging")
    assert resolved == tracked
    assert resolved == _tracked_recipe_path(cfg, proj.slug, "staging")
    assert resolved != cfg.data_dir / proj.slug / "deploy" / "staging.sh"


def test_run_next_executes_tracked_recipe(tmp_path: Path) -> None:
    """run_next runs the tracked recipe, not the legacy data/ copy.

    The legacy copy is rigged to FAIL (rc=17) and the tracked copy to SUCCEED
    (rc=0); a clean success + the tracked marker in the run-log proves which
    file actually ran.
    """
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=17)  # would fail if used
    _make_tracked_recipe(cfg, proj.slug, "staging", rc=0)

    enqueue(cfg, proj.slug, "staging", "tracked deploy", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is True
    assert result.returncode == 0
    assert "tracked recipe running" in result.log_path.read_text()


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


def test_resolve_target_sha_returns_origin_branch_tip(tmp_path: Path) -> None:
    """T-0458: the enqueue echo resolves origin/<deploy_branch> in the editing clone."""
    from bot_squad_worker.deploy import resolve_target_sha

    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)  # origin/bot_squad/dev == HEAD

    sha = resolve_target_sha(cfg, proj.slug, "staging")
    assert sha == _head(proj.repo_path)  # full 40-hex of the to-be-built commit


def test_resolve_target_sha_empty_on_missing_origin_ref(tmp_path: Path) -> None:
    """No origin attached → no origin/<branch> ref → "" (best-effort, never raises)."""
    from bot_squad_worker.deploy import resolve_target_sha

    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    # deliberately do NOT attach an origin

    assert resolve_target_sha(cfg, proj.slug, "staging") == ""


def test_resolve_target_sha_empty_on_unknown_slug_or_target(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import resolve_target_sha

    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)

    assert resolve_target_sha(cfg, "no-such-slug", "staging") == ""
    assert resolve_target_sha(cfg, proj.slug, "prod") == ""  # not a deploy_target


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


def test_run_next_deploy_clone_proceeds_with_unpushed_dev_commits(
    tmp_path: Path, caplog
) -> None:
    """T-0225: unpushed dev commits no longer DEFER a deploy-clone deploy.

    The deploy ships origin/<branch> from a disposable clone that never touches
    the shared editing tree, so an unpushed commit there is merely OMITTED — not
    destroyed. Hard-blocking it (the pre-T-0225 behaviour) HOL-deferred EVERY
    team's deploy whenever one team had committed-but-unpushed WIP on the shared
    clone. So the deploy PROCEEDS, logging a loud advisory naming the omitted
    commit(s) so the deployer knows to push them.
    """
    import logging

    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)

    sha = _add_local_commit(proj.repo_path)  # committed to dev, NOT pushed

    enqueue(cfg, proj.slug, "staging", "proceeds despite unpushed", "user")
    with caplog.at_level(logging.WARNING):
        result = run_next(cfg, proj.slug)

    assert result is not None
    assert result.ok is True
    # The advisory names the omitted commit (so the deployer can push it).
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert sha in joined
    assert "OMITTED" in joined


def test_run_next_deploy_clone_ships_origin_not_unpushed_dev_commit(
    tmp_path: Path,
) -> None:
    """T-0225 correctness: with an unpushed dev commit present, the deploy still
    runs from origin's tip (the deploy clone), NOT the dev clone's unpushed HEAD
    — proceeding does not leak unpushed work into the release."""
    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    head_file = tmp_path / "deploy_head.txt"
    _recipe_recording_head(tmp_path, cfg, proj.slug, "staging", head_file)

    origin_tip = _head(proj.repo_path)  # == origin (pushed by _attach_origin)
    _add_local_commit(proj.repo_path)   # dev HEAD now ahead of origin (unpushed)
    assert _head(proj.repo_path) != origin_tip

    enqueue(cfg, proj.slug, "staging", "ship origin", "user")
    result = run_next(cfg, proj.slug)

    assert result is not None and result.ok is True
    # The recipe ran in the deploy clone at ORIGIN's tip, not the unpushed HEAD.
    assert _head(proj.repo_deploy) == origin_tip
    assert head_file.read_text().strip() == origin_tip


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


# ---------------------------------------------------------------------------
# T-0520: a restart_worker=true deploy that SUCCEEDED but lost its _finish move
# to the self-restart SIGTERM leaves a marker stranded in processing/ WITH a
# success .rc sentinel. The age-fail reaper must DEFER to that sentinel and
# record the TRUE outcome (.ok / .fail.<rc>) — never blindly stamp RC_ORPHAN on
# a genuine SUCCESS — so the guarantee holds even if the reconcile pass is
# skipped/fails (defense-in-depth, independent of monitor tick ordering).
# ---------------------------------------------------------------------------


def test_reap_orphans_records_true_success_not_orphan_when_sentinel_present(
    tmp_path: Path,
) -> None:
    import os
    import time as _time
    from bot_squad_worker.deploy import RC_ORPHAN, reap_orphans, _record_run_rc

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = cfg.data_dir / proj.slug / "_jobs" / "deploy"
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    qid = "deadbeef-success"
    marker = proc / f"1700000000000-{qid}.json"
    marker.write_text(json.dumps({"queue_id": qid, "target": "staging"}))
    _record_run_rc(cfg, proj.slug, qid, 0)  # finished rc=0; _finish lost to restart
    old = _time.time() - 3 * 3600  # stale: reconcile never got to it
    os.utime(marker, (old, old))

    reaped = reap_orphans(cfg, proj.slug)  # default max-age 7200s

    processed = base / "processed"
    assert (processed / f"1700000000000-{qid}.ok").exists()
    assert list(processed.glob(f"*.fail.{RC_ORPHAN}")) == []
    assert list(proc.glob("*.json")) == []
    # a recorded success is NOT a crashed orphan → no false "🧟 reaped" alert
    assert reaped == []


def test_reap_orphans_records_true_fail_not_orphan_when_sentinel_present(
    tmp_path: Path,
) -> None:
    import os
    import time as _time
    from bot_squad_worker.deploy import RC_ORPHAN, reap_orphans, _record_run_rc

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = cfg.data_dir / proj.slug / "_jobs" / "deploy"
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    qid = "deadbeef-fail"
    marker = proc / f"1700000000000-{qid}.json"
    marker.write_text(json.dumps({"queue_id": qid, "target": "staging"}))
    _record_run_rc(cfg, proj.slug, qid, 2)  # finished rc=2 (real failure)
    old = _time.time() - 3 * 3600
    os.utime(marker, (old, old))

    reaped = reap_orphans(cfg, proj.slug)

    processed = base / "processed"
    assert (processed / f"1700000000000-{qid}.fail.2").exists()
    assert list(processed.glob(f"*.fail.{RC_ORPHAN}")) == []
    assert list(proc.glob("*.json")) == []
    assert reaped == []


def test_reap_orphans_still_orphans_when_no_sentinel(tmp_path: Path) -> None:
    """A stale marker with NO .rc sentinel (a genuinely crashed/killed run that
    never recorded an outcome) is still age-failed to RC_ORPHAN — the reaper's
    original behaviour is preserved for true orphans."""
    import os
    import time as _time
    from bot_squad_worker.deploy import RC_ORPHAN, reap_orphans

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = cfg.data_dir / proj.slug / "_jobs" / "deploy"
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    qid = "deadbeef-crashed"
    marker = proc / f"1700000000000-{qid}.json"
    marker.write_text(json.dumps({"queue_id": qid, "target": "staging"}))
    old = _time.time() - 3 * 3600
    os.utime(marker, (old, old))

    reaped = reap_orphans(cfg, proj.slug)

    processed = base / "processed"
    assert (processed / f"1700000000000-{qid}.fail.{RC_ORPHAN}").exists()
    assert [r["queue_id"] for r in reaped] == [qid]


# ---------------------------------------------------------------------------
# T-0243: reconcile FINISHED-but-orphaned processing/ markers to their ACTUAL
# recorded rc (a worker restart raced run_next's _finish move). A durable
# runs/<qid>.rc sentinel is written when the run terminates; the reconcile reads
# it and moves the marker to processed/.ok (rc=0) / .fail.<rc> — NOT blindly
# age-failed. An in-flight run (no sentinel yet) is left alone.
# ---------------------------------------------------------------------------

def _deploy_base(cfg, slug):
    return cfg.data_dir / slug / "_jobs" / "deploy"


def test_record_and_read_run_rc_roundtrip(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _record_run_rc, _read_run_rc
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    assert _read_run_rc(cfg, proj.slug, "qid-1") is None  # absent → None
    _record_run_rc(cfg, proj.slug, "qid-1", 0)
    _record_run_rc(cfg, proj.slug, "qid-2", 7)
    assert _read_run_rc(cfg, proj.slug, "qid-1") == 0
    assert _read_run_rc(cfg, proj.slug, "qid-2") == 7


def test_reconcile_finished_orphan_rc0_to_ok(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _record_run_rc, reconcile_finished_orphans
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = _deploy_base(cfg, proj.slug)
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "1700000000000-win.json"
    f.write_text(json.dumps({"queue_id": "win", "target": "staging"}))
    _record_run_rc(cfg, proj.slug, "win", 0)  # run finished rc=0, _finish was interrupted

    out = reconcile_finished_orphans(cfg, proj.slug)

    assert [o["queue_id"] for o in out] == ["win"]
    assert [o["rc"] for o in out] == [0]
    assert not f.exists()  # moved out of processing
    assert (base / "processed" / "1700000000000-win.ok").exists()


def test_reconcile_finished_orphan_nonzero_to_fail(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _record_run_rc, reconcile_finished_orphans
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = _deploy_base(cfg, proj.slug)
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "1700000000000-bad.json"
    f.write_text(json.dumps({"queue_id": "bad", "target": "staging"}))
    _record_run_rc(cfg, proj.slug, "bad", 2)

    reconcile_finished_orphans(cfg, proj.slug)

    assert (base / "processed" / "1700000000000-bad.fail.2").exists()
    assert not f.exists()


def test_reconcile_leaves_in_flight_run_with_no_rc(tmp_path: Path) -> None:
    """No terminal rc yet (build still running) → never reconciled/reaped."""
    from bot_squad_worker.deploy import reconcile_finished_orphans
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = _deploy_base(cfg, proj.slug)
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "1700000000000-live.json"
    f.write_text(json.dumps({"queue_id": "live", "target": "staging"}))

    out = reconcile_finished_orphans(cfg, proj.slug)

    assert out == []
    assert f.exists()  # left in processing


def test_reconcile_collapsed_pair_both_reconciled(tmp_path: Path) -> None:
    """Collapsed-pair: run_next records an rc sentinel per collapsed qid, so both
    orphaned markers reconcile to the surviving run's rc."""
    from bot_squad_worker.deploy import _record_run_rc, reconcile_finished_orphans
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = _deploy_base(cfg, proj.slug)
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    for qid in ("winner", "collapsed"):
        (proc / f"1700000000000-{qid}.json").write_text(
            json.dumps({"queue_id": qid, "target": "staging"})
        )
        _record_run_rc(cfg, proj.slug, qid, 0)

    out = reconcile_finished_orphans(cfg, proj.slug)

    assert sorted(o["queue_id"] for o in out) == ["collapsed", "winner"]
    assert (base / "processed" / "1700000000000-winner.ok").exists()
    assert (base / "processed" / "1700000000000-collapsed.ok").exists()


def test_surface_worker_restart_fails_reports_and_tombstones(tmp_path: Path) -> None:
    """T-0335 item-13 (Fork-5): the .worker-restart.FAIL marker — written by a
    failed detached restart but never read by jobs.py — is surfaced exactly once
    and tombstoned so it doesn't re-alert every tick."""
    from bot_squad_worker.deploy import surface_worker_restart_fails, _runs_dir

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    runs = _runs_dir(cfg, proj.slug)
    runs.mkdir(parents=True, exist_ok=True)
    marker = runs / "abc123.worker-restart.FAIL"
    marker.write_text("")
    (runs / "abc123.worker-restart.log").write_text(
        "[worker-restart] SMOKE_HEALTH_FAILED — worker did not answer /health\n")

    out = surface_worker_restart_fails(cfg, proj.slug)
    assert len(out) == 1
    assert out[0]["queue_id"] == "abc123"
    assert "SMOKE_HEALTH_FAILED" in out[0]["tail"]
    # tombstoned → the FAIL marker is gone, an .alerted marker remains
    assert not marker.exists()
    assert (runs / "abc123.worker-restart.FAIL.alerted").exists()

    # second pass is a no-op — no duplicate alert
    assert surface_worker_restart_fails(cfg, proj.slug) == []


def test_surface_worker_restart_fails_empty_when_none(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import surface_worker_restart_fails
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    assert surface_worker_restart_fails(cfg, proj.slug) == []


def test_is_clean_for_target_deploy_clone_ignores_dev_state(tmp_path: Path) -> None:
    """T-0225: for a deploy-clone project, NEITHER a dirty dev tree NOR unpushed
    dev commits gate the pre-ping cleanliness check — origin is the SSOT, so one
    team's WIP on the shared clone never defers another team's deploy."""
    from bot_squad_worker.deploy import is_clean_for_target

    proj = _make_deploy_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)

    # Dirty dev tree — must NOT gate a deploy-clone deploy.
    (proj.repo_path / "WIP.txt").write_text("uncommitted")
    assert is_clean_for_target(cfg, proj.slug, "staging") is True

    # An unpushed dev commit (e.g. another team's committed-but-unpushed WIP)
    # must ALSO not gate now (pre-T-0225 this returned False — the live stall).
    _add_local_commit(proj.repo_path)
    assert is_clean_for_target(cfg, proj.slug, "staging") is True


# ---------------------------------------------------------------------------
# T-0213: systemd-scope detach (deploy survives a concurrent worker restart)
# ---------------------------------------------------------------------------


def test_use_systemd_scope_respects_explicit_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot_squad_worker.deploy import _use_systemd_scope

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "1")
    assert _use_systemd_scope() is True
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "0")
    assert _use_systemd_scope() is False


def test_use_systemd_scope_autodetect_needs_manager_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no explicit knob, both XDG_RUNTIME_DIR and systemd-run are required."""
    import bot_squad_worker.deploy as d

    monkeypatch.delenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", raising=False)
    monkeypatch.setattr(d.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert d._use_systemd_scope() is True

    # No user manager → off (a system-service worker, or CI).
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert d._use_systemd_scope() is False

    # Manager present but client missing → off (fall back to plain bash).
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(d.shutil, "which", lambda _name: None)
    assert d._use_systemd_scope() is False


def test_scope_wrap_wraps_when_on_and_passes_through_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot_squad_worker.deploy import _scope_wrap

    argv = ["bash", "/path/recipe.sh"]

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "0")
    assert _scope_wrap(argv, "bot-squad-deploy-xyz") == argv

    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "1")
    wrapped = _scope_wrap(argv, "bot-squad-deploy-xyz")
    assert wrapped[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
    assert "--unit=bot-squad-deploy-xyz" in wrapped
    # The original command survives intact after the `--` separator.
    assert wrapped[-2:] == argv
    assert "--" in wrapped


def test_run_next_routes_recipe_through_scope_wrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_next hands the recipe command to _scope_wrap with the queue-id unit.

    We spy on _scope_wrap (returning a plain `bash recipe` so the recipe still
    runs and the deploy succeeds), proving the integration point without
    spawning a real transient unit. The wrap/passthrough behaviour itself is
    covered by test_scope_wrap_wraps_when_on_and_passes_through_when_off.
    """
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    recipe = _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    queue_id = enqueue(cfg, proj.slug, "staging", "scoped deploy", "user")

    captured: dict = {}

    def _spy(argv, unit):
        captured["argv"] = argv
        captured["unit"] = unit
        return argv  # plain — let the recipe actually run

    monkeypatch.setattr(d, "_scope_wrap", _spy)
    result = run_next(cfg, proj.slug)

    assert result is not None and result.ok is True
    assert captured["argv"] == ["bash", str(recipe)]
    assert captured["unit"] == f"bot-squad-deploy-{queue_id}"


# ---------------------------------------------------------------------------
# T-0181: optional post-deploy worker-restart step
# ---------------------------------------------------------------------------


def test_enqueue_carries_restart_worker_flag(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    queue_dir = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "queue"

    # Default OFF.
    enqueue(cfg, proj.slug, "staging", "web-only", "user")
    data = json.loads(next(queue_dir.glob("*.json")).read_text())
    assert data["restart_worker"] is False

    # Explicit opt-in round-trips.
    for f in queue_dir.glob("*.json"):
        f.unlink()
    enqueue(cfg, proj.slug, "staging", "worker change", "user", restart_worker=True)
    data = json.loads(next(queue_dir.glob("*.json")).read_text())
    assert data["restart_worker"] is True


def test_run_next_no_restart_when_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    enqueue(cfg, proj.slug, "staging", "no restart", "user")  # default OFF

    # T-0535: pin the no-worker-change gate instead of relying on the real repo's
    # git state — under host load a concurrent commit on the shared tree can flip
    # _worker_subtree_changed_since_boot's real git probe mid-test and flake this.
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: False)
    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append(a))
    result = run_next(cfg, proj.slug)
    assert result is not None and result.ok is True
    assert calls == []


def test_run_next_triggers_restart_on_success_when_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    queue_id = enqueue(
        cfg, proj.slug, "staging", "worker change", "user", restart_worker=True
    )

    # T-0305 part-a: forced now also requires a real worker/ change — model one.
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: True)
    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append(a))
    result = run_next(cfg, proj.slug)
    assert result is not None and result.ok is True
    assert len(calls) == 1
    # _restart_worker_detached(cfg, slug, queue_id, reason)
    assert calls[0][1] == proj.slug
    assert calls[0][2] == queue_id


def test_run_next_records_ok_before_self_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0520: bookkeeping must happen-BEFORE the self-restart. We capture the
    processing/ + processed/ state at the instant the (detached) restart is
    triggered and assert the .ok marker is already on disk and the processing/
    entry is already gone — so the restart's SIGTERM can never strand the run."""
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    enqueue(cfg, proj.slug, "staging", "worker change", "user", restart_worker=True)
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: True)

    seen: dict = {}

    def _capture(_cfg, _slug, _qid, _reason) -> None:
        seen["ok"] = sorted(p.name for p in _processed_dir(cfg, proj.slug).glob("*.ok"))
        seen["processing"] = sorted(
            p.name for p in _processing_dir(cfg, proj.slug).glob("*.json")
        )

    monkeypatch.setattr(d, "_restart_worker_detached", _capture)
    result = run_next(cfg, proj.slug)

    assert result is not None and result.ok is True
    # .ok recorded AND processing/ emptied before the restart was triggered.
    assert len(seen["ok"]) == 1
    assert seen["processing"] == []


def test_run_next_no_restart_on_recipe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed deploy must NOT bounce the worker even with the flag set."""
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=5)
    enqueue(cfg, proj.slug, "staging", "broken worker change", "user", restart_worker=True)

    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append(a))
    result = run_next(cfg, proj.slug)
    assert result is not None and result.ok is False
    assert calls == []


# ---------------------------------------------------------------------------
# T-0305 part-a: a forced restart_worker:true now ALSO honors the
# no-worker-change skip gate — a deploy that doesn't touch worker/ never bounces
# the worker (kills the ~6min SIGTERM cadence churning the inbox-wait long-polls).
# ---------------------------------------------------------------------------

def test_should_restart_worker_skips_when_no_worker_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: False)
    # NEITHER forced nor auto restarts when worker/ is unchanged.
    assert d._should_restart_worker(cfg, ok=True, forced=True) is False
    assert d._should_restart_worker(cfg, ok=True, forced=False) is False


def test_should_restart_worker_forced_fires_on_worker_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: True)
    assert d._should_restart_worker(cfg, ok=True, forced=True) is True


def test_should_restart_worker_auto_fires_on_change_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: True)
    monkeypatch.delenv("BOT_SQUAD_DEPLOY_AUTO_RESTART", raising=False)
    assert d._should_restart_worker(cfg, ok=True, forced=False) is True


def test_should_restart_worker_killswitch_blocks_auto_not_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill-switch disables the AUTO restart; an explicit forced restart
    still overrides it — but BOTH still require an actual worker/ change."""
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: True)
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_AUTO_RESTART", "0")
    assert d._should_restart_worker(cfg, ok=True, forced=False) is False
    assert d._should_restart_worker(cfg, ok=True, forced=True) is True


def test_should_restart_worker_never_on_failed_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: True)
    assert d._should_restart_worker(cfg, ok=False, forced=True) is False


def test_run_next_no_restart_forced_but_no_worker_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-0305 part-a integration: restart_worker:true on a deploy that did NOT
    change worker/ does NOT bounce the worker."""
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    enqueue(cfg, proj.slug, "staging", "api-only change", "user", restart_worker=True)

    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: False)
    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append(a))
    result = run_next(cfg, proj.slug)
    assert result is not None and result.ok is True
    assert calls == []  # no worker change → no restart, even forced


# ---------------------------------------------------------------------------
# T-0305 part-b: minimum-restart-interval gate — journal archaeology (see
# ticket) found the ~6min cadence was overwhelmingly NON-hermetic pytest runs
# spawning REAL worker restarts (T-0574, fixed separately). This gate is
# defense-in-depth so a burst of individually-legitimate worker-changing
# events still can't bounce the worker faster than the configured floor.
# ---------------------------------------------------------------------------


def test_restart_rate_limited_false_then_true_within_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setenv("BOT_SQUAD_WORKER_RESTART_MIN_INTERVAL_SECONDS", "300")

    assert d._restart_rate_limited(cfg, source="deploy") is False
    # Second call inside the window is rate-limited, and does NOT reclaim it.
    assert d._restart_rate_limited(cfg, source="deploy") is True
    assert d._restart_rate_limited(cfg, source="deploy") is True


def test_restart_rate_limited_clears_after_window_elapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setenv("BOT_SQUAD_WORKER_RESTART_MIN_INTERVAL_SECONDS", "300")

    assert d._restart_rate_limited(cfg, source="deploy") is False
    marker = d._restart_rate_limit_path(cfg)
    stamped = json.loads(marker.read_text())
    marker.write_text(json.dumps({**stamped, "at": stamped["at"] - 301}))
    assert d._restart_rate_limited(cfg, source="deploy") is False


def test_restart_rate_limited_disabled_by_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    cfg = _make_config(tmp_path, _make_project(tmp_path))
    monkeypatch.setenv("BOT_SQUAD_WORKER_RESTART_MIN_INTERVAL_SECONDS", "0")

    assert d._restart_rate_limited(cfg, source="deploy") is False
    assert d._restart_rate_limited(cfg, source="deploy") is False


def test_run_next_second_worker_changing_deploy_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two back-to-back worker-changing deploys within the window: the first
    fires, the second is skipped as rate-limited (NOT double-restarted) — and
    is distinguishable in worker_restart_status from a no-worker-change skip."""
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda c: True)
    monkeypatch.setenv("BOT_SQUAD_WORKER_RESTART_MIN_INTERVAL_SECONDS", "300")
    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append(a))

    enqueue(cfg, proj.slug, "staging", "worker change #1", "user", restart_worker=True)
    result1 = run_next(cfg, proj.slug)
    assert result1 is not None and result1.worker_restart_status == "fired"
    assert len(calls) == 1

    enqueue(cfg, proj.slug, "staging", "worker change #2", "user", restart_worker=True)
    result2 = run_next(cfg, proj.slug)
    assert result2 is not None and result2.worker_restart_status == "skipped: rate-limited"
    assert len(calls) == 1  # NOT fired a second time


def test_run_next_worker_restart_not_rate_limited_after_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second worker-changing deploy AFTER the interval elapses still fires —
    the gate never permanently swallows a legitimate restart."""
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda c: True)
    monkeypatch.setenv("BOT_SQUAD_WORKER_RESTART_MIN_INTERVAL_SECONDS", "300")
    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append(a))

    enqueue(cfg, proj.slug, "staging", "worker change #1", "user", restart_worker=True)
    run_next(cfg, proj.slug)
    assert len(calls) == 1

    marker = d._restart_rate_limit_path(cfg)
    stamped = json.loads(marker.read_text())
    marker.write_text(json.dumps({**stamped, "at": stamped["at"] - 301}))

    enqueue(cfg, proj.slug, "staging", "worker change #2", "user", restart_worker=True)
    result2 = run_next(cfg, proj.slug)
    assert result2 is not None and result2.worker_restart_status == "fired"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# T-0446 / next-wave #2: surface the resolved sha + worker-restart decision on
# the DeployResult so the terminal #deploy-logs ping echoes which commit shipped
# and whether the worker bounced (kills the T-0436 false-stale-worker panic).
# ---------------------------------------------------------------------------

def test_parse_resolved_sha_from_run_log(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _parse_resolved_sha
    log = tmp_path / "x.log"
    log.write_text(
        "#22 building\n"
        "[bot-squad/staging] verified running container sha == deployed sha "
        "(64d42f0b76e65f38ea5a4f118392ee166464cf42)\n"
        "[bot-squad/staging] release deployed: 64d42f0\n"
    )
    assert _parse_resolved_sha(log) == "64d42f0b76e65f38ea5a4f118392ee166464cf42"


def test_parse_resolved_sha_falls_back_to_release_line(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _parse_resolved_sha
    log = tmp_path / "x.log"
    log.write_text("[watchrobot/staging] release deployed: abc1234def5678\n")
    assert _parse_resolved_sha(log) == "abc1234def5678"


def test_parse_resolved_sha_empty_when_no_marker_or_missing(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _parse_resolved_sha
    log = tmp_path / "x.log"
    log.write_text("just build output, no sha line\n")
    assert _parse_resolved_sha(log) == ""
    assert _parse_resolved_sha(tmp_path / "nope.log") == ""


def test_run_next_sets_resolved_sha_and_worker_restart_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    recipe_dir = cfg.data_dir / proj.slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / "staging.sh"
    recipe.write_text(
        "#!/usr/bin/env bash\nset -e\n"
        "echo 'verified running container sha == deployed sha (abcdef1234567)'\nexit 0\n"
    )
    recipe.chmod(0o755)
    enqueue(cfg, proj.slug, "staging", "worker change", "user", restart_worker=True)
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda c: True)
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: None)

    result = run_next(cfg, proj.slug)
    assert result is not None and result.ok is True
    assert result.resolved_sha == "abcdef1234567"
    assert result.worker_restart_status == "fired"


def test_run_next_worker_restart_status_skipped_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    enqueue(cfg, proj.slug, "staging", "api-only", "user", restart_worker=True)
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda c: False)
    result = run_next(cfg, proj.slug)
    assert result.worker_restart_status == "skipped: no worker change"


def test_restart_worker_detached_skips_without_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user systemd manager → SKIP (and never spawn a process)."""
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "0")

    popen_calls: list = []
    monkeypatch.setattr(
        d.subprocess, "Popen", lambda *a, **k: popen_calls.append(a)
    )
    d._restart_worker_detached(cfg, proj.slug, "qid-1", "worker change")

    assert popen_calls == []
    skip_log = cfg.data_dir / proj.slug / "_jobs" / "deploy" / "runs" / "qid-1.worker-restart.log"
    assert skip_log.exists()
    assert "SKIPPING" in skip_log.read_text()


def test_restart_worker_detached_launches_scoped_when_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope ON → launch the restart script wrapped in a transient scope, detached."""
    import bot_squad_worker.deploy as d

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "1")

    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(d.subprocess, "Popen", _FakePopen)
    d._restart_worker_detached(cfg, proj.slug, "qid-2", "worker change")

    argv = captured["argv"]
    assert argv[0] == "systemd-run"
    assert any(a == "--unit=bot-squad-worker-restart-qid-2" for a in argv)
    assert argv[-3] == "bash" and argv[-2] == "-c"
    # detached so it survives the restart it triggers
    assert captured["kwargs"].get("start_new_session") is True


def test_build_worker_restart_script_has_guard_restart_smoke(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _build_worker_restart_script

    script = _build_worker_restart_script(
        worker_dir=Path("/install/worker"),
        pip_path=Path("/install/worker/.venv/bin/pip"),
        sock_path=Path("/install/data/_sock/worker.sock"),
        log_path=Path("/runs/q.log"),
        fail_marker=Path("/runs/q.FAIL"),
        service="bot-squad-worker.service",
        delay_s=5,
        smoke_attempts=10,
    )
    # pip guard runs BEFORE the restart, and aborts (no restart) on failure.
    assert "pip" in script and "install -e" in script
    guard_idx = script.index("install -e")
    restart_idx = script.index("systemctl --user restart")
    assert guard_idx < restart_idx
    # smoke hits /health then one action over the unix socket
    assert "http://w/health" in script
    assert "scheduler_state" in script
    assert "/install/data/_sock/worker.sock" in script
    # failure paths drop the FAIL marker
    assert "/runs/q.FAIL" in script


# ---------------------------------------------------------------------------
# T-0335 item-13 (Fork-5): auto-detect worker/ change → restart → sha verify
# ---------------------------------------------------------------------------

def test_worker_needs_restart_true_on_worker_subtree_change(monkeypatch) -> None:
    import bot_squad_worker.deploy as d
    monkeypatch.delenv("BOT_SQUAD_DEPLOY_AUTO_RESTART", raising=False)
    monkeypatch.setattr(d, "boot_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(d, "_git_head_sha", lambda root: "b" * 40)
    monkeypatch.setattr(d, "_worker_subtree_changed", lambda root, a, b: True)
    assert d._worker_needs_restart(_make_config_only(monkeypatch)) is True


def test_worker_needs_restart_false_when_subtree_unchanged(monkeypatch) -> None:
    import bot_squad_worker.deploy as d
    monkeypatch.delenv("BOT_SQUAD_DEPLOY_AUTO_RESTART", raising=False)
    monkeypatch.setattr(d, "boot_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(d, "_git_head_sha", lambda root: "b" * 40)
    monkeypatch.setattr(d, "_worker_subtree_changed", lambda root, a, b: False)
    assert d._worker_needs_restart(_make_config_only(monkeypatch)) is False


def test_worker_needs_restart_false_when_sha_equal(monkeypatch) -> None:
    import bot_squad_worker.deploy as d
    monkeypatch.delenv("BOT_SQUAD_DEPLOY_AUTO_RESTART", raising=False)
    monkeypatch.setattr(d, "boot_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(d, "_git_head_sha", lambda root: "a" * 40)  # running == deployed
    called = {"subtree": False}
    monkeypatch.setattr(d, "_worker_subtree_changed",
                        lambda root, a, b: called.__setitem__("subtree", True) or True)
    assert d._worker_needs_restart(_make_config_only(monkeypatch)) is False
    assert called["subtree"] is False  # short-circuits before the diff


def test_worker_needs_restart_kill_switch(monkeypatch) -> None:
    import bot_squad_worker.deploy as d
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_AUTO_RESTART", "0")
    monkeypatch.setattr(d, "boot_git_sha", lambda: "a" * 40)
    monkeypatch.setattr(d, "_git_head_sha", lambda root: "b" * 40)
    monkeypatch.setattr(d, "_worker_subtree_changed", lambda root, a, b: True)
    assert d._worker_needs_restart(_make_config_only(monkeypatch)) is False


def _make_config_only(monkeypatch):
    from types import SimpleNamespace
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# T-0461: eager boot_git_sha freeze (the lazy-first-call hazard)
# ---------------------------------------------------------------------------

def _init_repo_at(root: Path, content: str) -> str:
    """Init a git repo at ``root`` with one commit; return its full 40-hex HEAD."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(root), check=True)
    (root / "f.txt").write_text(content)
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", content], cwd=str(root), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_move(root: Path, content: str) -> str:
    """Add a commit to simulate a deploy ff-sync moving the install tree; return new HEAD."""
    (root / "f.txt").write_text(content)
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", content], cwd=str(root), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout.strip()


def test_freeze_boot_git_sha_is_eager_and_immune_to_tree_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE T-0461 fix: freeze at startup captures the loaded sha; a later deploy
    ff-sync of the install tree must NOT change what boot_git_sha() reports."""
    import bot_squad_worker.deploy as d
    monkeypatch.setattr(d, "_BOOT_GIT_SHA", None)
    root = tmp_path / "install"
    sha_a = _init_repo_at(root, "boot-time code")
    monkeypatch.setattr(d, "_install_root", lambda: root)

    # Startup eagerly freezes to the sha the process actually loaded.
    assert d.freeze_boot_git_sha() == sha_a

    # A deploy ff-syncs the install tree to a NEW commit (process NOT restarted).
    sha_b = _commit_move(root, "post-deploy code")
    assert sha_b != sha_a

    # boot_git_sha must still report the boot-time sha, never the moved tree.
    assert d.boot_git_sha() == sha_a
    # Idempotent: re-freezing is a no-op, returns the same value.
    assert d.freeze_boot_git_sha() == sha_a


def test_boot_git_sha_hazard_without_eager_freeze_captures_moved_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression doc: WHY the eager freeze matters. If boot_git_sha() is first
    called only AFTER a tree move (no startup freeze), it captures the MOVED sha
    — a commit the running process never loaded. This is the T-0461 lie that the
    startup freeze_boot_git_sha() call prevents."""
    import bot_squad_worker.deploy as d
    monkeypatch.setattr(d, "_BOOT_GIT_SHA", None)
    root = tmp_path / "install"
    sha_a = _init_repo_at(root, "boot-time code")
    monkeypatch.setattr(d, "_install_root", lambda: root)

    # No eager freeze; tree moves before the first boot_git_sha() call.
    sha_b = _commit_move(root, "post-deploy code")
    assert d.boot_git_sha() == sha_b  # captures B (never loaded) — the hazard
    assert sha_b != sha_a


def test_run_next_auto_restarts_on_worker_change_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fork-5: even with restart_worker NOT forced, a clean deploy whose worker/
    subtree changed auto-fires the restart."""
    import bot_squad_worker.deploy as d
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    enqueue(cfg, proj.slug, "staging", "worker change", "user")  # flag OFF

    # T-0305 part-a: the auto path keys on the worker-change gate (kill-switch
    # default ON) — model a real worker/ change.
    monkeypatch.delenv("BOT_SQUAD_DEPLOY_AUTO_RESTART", raising=False)
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda c: True)
    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append((a, k)))
    result = run_next(cfg, proj.slug)
    assert result is not None and result.ok is True
    assert len(calls) == 1  # auto-fired


def test_run_next_no_auto_restart_when_worker_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot_squad_worker.deploy as d
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(tmp_path, cfg, proj.slug, "staging", rc=0)
    enqueue(cfg, proj.slug, "staging", "docs only", "user")  # flag OFF

    # T-0535: run_next's actual call path is _should_restart_worker ->
    # _worker_subtree_changed_since_boot, not _worker_needs_restart (that stale
    # target was never on the code path run_next exercises, so this monkeypatch
    # was a no-op — the assertion was passing only because the real repo's git
    # probe happened to also return "unchanged", which host load can flip).
    monkeypatch.setattr(d, "_worker_subtree_changed_since_boot", lambda _cfg: False)
    calls: list = []
    monkeypatch.setattr(d, "_restart_worker_detached", lambda *a, **k: calls.append(a))
    result = run_next(cfg, proj.slug)
    assert result is not None and result.ok is True
    assert calls == []


def test_build_worker_restart_script_asserts_deployed_sha(tmp_path: Path) -> None:
    """13d: when an expected (deployed) sha is given, the smoke asserts the
    worker came back on it; the sha is shell-quoted, never python -c source."""
    from bot_squad_worker.deploy import _build_worker_restart_script
    sha = "c0ffee" + "0" * 34
    script = _build_worker_restart_script(
        worker_dir=Path("/install/worker"),
        pip_path=Path("/install/worker/.venv/bin/pip"),
        sock_path=Path("/install/data/_sock/worker.sock"),
        log_path=Path("/runs/q.log"),
        fail_marker=Path("/runs/q.FAIL"),
        service="bot-squad-worker.service",
        delay_s=5,
        smoke_attempts=10,
        expected_sha=sha,
    )
    assert "git_sha" in script
    assert sha in script
    assert "GIT_SHA_MISMATCH" in script


def test_build_worker_restart_script_no_sha_assert_without_expected(tmp_path: Path) -> None:
    from bot_squad_worker.deploy import _build_worker_restart_script
    script = _build_worker_restart_script(
        worker_dir=Path("/install/worker"),
        pip_path=Path("/install/worker/.venv/bin/pip"),
        sock_path=Path("/install/data/_sock/worker.sock"),
        log_path=Path("/runs/q.log"),
        fail_marker=Path("/runs/q.FAIL"),
        service="bot-squad-worker.service",
        delay_s=5,
        smoke_attempts=10,
    )
    assert "GIT_SHA_MISMATCH" not in script


# ---------------------------------------------------------------------------
# T-0386 / INI-04 Phase 1: deploy_monitor sends route to the #deploy-logs topic
# ---------------------------------------------------------------------------

class _RecordingTg:
    def __init__(self):
        self.calls = []

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None, debounce=True):
        self.calls.append({"text": text, "topic_id": topic_id})
        return True


def test_deploy_monitor_sends_carry_deploy_logs_topic(tmp_config_dir, tmp_path, monkeypatch):
    from bot_squad_worker.config import Config
    from bot_squad_worker import jobs, deploy as _deploy, tg_topics
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    project = cfg.projects["test-project"]
    tg_topics.save(cfg, "test-project", {"deploy_logs": 7777})

    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps({"target": "staging"}))

    monkeypatch.setattr(_deploy, "list_queued", lambda c, s: [qfile])
    monkeypatch.setattr(_deploy, "is_paused", lambda c, s: None)
    monkeypatch.setattr(_deploy, "is_clean_for_target", lambda c, s, t: True)
    from types import SimpleNamespace
    monkeypatch.setattr(
        _deploy, "run_next",
        lambda c, s: SimpleNamespace(
            ok=True, returncode=0, collapsed_count=1, killed_reason=None,
            resolved_sha="", worker_restart_status="",  # T-0446: terminal-ping fields
        ),
    )
    rec = _RecordingTg()
    monkeypatch.setattr(A, "_get_tg_client", lambda c: rec)

    jobs._run_project_deploy(cfg, "test-project", project)

    assert rec.calls, "deploy_monitor sent no TG messages"
    assert all(c["topic_id"] == 7777 for c in rec.calls), rec.calls


# ---------------------------------------------------------------------------
# next-wave #11 (T-0451): keep-last-N retention reaper for processed/ + runs/.
# processed/ files are "<epoch_ms>-<uuid>.ok|fail.<rc>" (chronologically
# sortable); runs/ files are "<uuid>.*" (NOT sortable → retention is tied to
# the processed keep-set by queue_id). NEVER prune an in-flight (processing/)
# job; keep-last-N preserves recent forensics; never raises.
# ---------------------------------------------------------------------------


def _seed_processed_job(cfg, slug: str, ts_ms: int, uuid_str: str, *, rc: int = 0) -> str:
    """Create one terminal processed/ marker + its runs/ artifacts. Returns uuid."""
    processed = _processed_dir(cfg, slug)
    runs = _runs_dir(cfg, slug)
    processed.mkdir(parents=True, exist_ok=True)
    runs.mkdir(parents=True, exist_ok=True)
    suffix = "ok" if rc == 0 else f"fail.{rc}"
    (processed / f"{ts_ms}-{uuid_str}.{suffix}").write_text("{}")
    (runs / f"{uuid_str}.log").write_text("build log")
    (runs / f"{uuid_str}.rc").write_text(str(rc))
    return uuid_str


def test_prune_keeps_last_n_processed(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    slug = proj.slug
    for i in range(5):
        _seed_processed_job(cfg, slug, 1_700_000_000_000 + i, f"uuid{i}")

    res = prune_processed_runs(cfg, slug, keep_last_n=2)

    processed = _processed_dir(cfg, slug)
    runs = _runs_dir(cfg, slug)
    remaining_processed = sorted(p.name for p in processed.glob("*"))
    # newest two jobs survive (uuid3, uuid4); older three pruned
    assert remaining_processed == [
        "1700000000003-uuid3.ok",
        "1700000000004-uuid4.ok",
    ], remaining_processed
    assert res["processed_pruned"] == 3
    # runs tied to kept qids survive; the rest pruned
    remaining_runs = sorted(p.name for p in runs.glob("*"))
    assert remaining_runs == ["uuid3.log", "uuid3.rc", "uuid4.log", "uuid4.rc"], remaining_runs
    assert res["runs_pruned"] == 6  # 3 jobs * 2 artifacts


def test_prune_protects_in_flight(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    slug = proj.slug
    # an OLD job that is still running: it has runs/ artifacts + a processing/ marker
    processing = _processing_dir(cfg, slug)
    processing.mkdir(parents=True, exist_ok=True)
    (processing / "1700000000000-live.json").write_text("{}")
    runs = _runs_dir(cfg, slug)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "live.log").write_text("in flight")
    # plus newer terminal jobs that would push the live one past the horizon
    for i in range(1, 4):
        _seed_processed_job(cfg, slug, 1_700_000_000_000 + i, f"uuid{i}")

    prune_processed_runs(cfg, slug, keep_last_n=1)

    # the in-flight job's runs artifact must survive despite keep_last_n=1
    assert (runs / "live.log").exists()


def test_prune_disabled_when_n_non_positive(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    slug = proj.slug
    for i in range(3):
        _seed_processed_job(cfg, slug, 1_700_000_000_000 + i, f"uuid{i}")

    res = prune_processed_runs(cfg, slug, keep_last_n=0)

    assert res["processed_pruned"] == 0 and res["runs_pruned"] == 0
    assert len(list(_processed_dir(cfg, slug).glob("*"))) == 3


def test_prune_never_raises_on_missing_dirs(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    # no _jobs/deploy dirs exist yet
    res = prune_processed_runs(cfg, proj.slug, keep_last_n=5)
    assert res["processed_pruned"] == 0 and res["runs_pruned"] == 0


def test_prune_env_default(tmp_path: Path, monkeypatch) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    slug = proj.slug
    for i in range(4):
        _seed_processed_job(cfg, slug, 1_700_000_000_000 + i, f"uuid{i}")
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_RETENTION_N", "1")

    res = prune_processed_runs(cfg, slug)  # N from env

    assert res["processed_pruned"] == 3
    assert len(list(_processed_dir(cfg, slug).glob("*"))) == 1


# ---- T-0528: log-evidence recovery of the no-.rc finalization-orphan ----------
# The #10/#12 orphan: an EXTERNAL `systemctl restart bot-squad-worker` SIGTERM'd
# run_next AFTER the recipe logged "release deployed" but BEFORE _record_run_rc.
# No .rc was written, so the sentinel-only reconciler skipped it and the operator
# had to hand-finalize. Recover it from log evidence (release line) once the
# runner is demonstrably gone (marker stale past the orphan grace).


def test_reconcile_norc_orphan_with_release_log_finalizes_success(tmp_path: Path) -> None:
    import os
    from bot_squad_worker.deploy import reconcile_finished_orphans
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = _deploy_base(cfg, proj.slug)
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "1700000000000-orphan.json"
    f.write_text(json.dumps({"queue_id": "orphan", "target": "staging"}))
    runs = _runs_dir(cfg, proj.slug)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "orphan.log").write_text("building...\nrelease deployed: abc1234def5678\n")
    old = time.time() - 100000  # runner long gone (>> grace)
    os.utime(f, (old, old))

    out = reconcile_finished_orphans(cfg, proj.slug)

    assert [o["queue_id"] for o in out] == ["orphan"]
    assert [o["rc"] for o in out] == [0]
    assert (base / "processed" / "1700000000000-orphan.ok").exists()
    assert not f.exists()


def test_reconcile_norc_release_log_but_fresh_left_inflight(tmp_path: Path) -> None:
    # A fresh no-.rc marker may still be a LIVE run mid-finalization (run_next
    # records its .rc microseconds after the release line) — never finalize it
    # out from under a live runner.
    from bot_squad_worker.deploy import reconcile_finished_orphans
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = _deploy_base(cfg, proj.slug)
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "1700000000000-fresh.json"
    f.write_text(json.dumps({"queue_id": "fresh", "target": "staging"}))
    runs = _runs_dir(cfg, proj.slug)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "fresh.log").write_text("release deployed: abc1234\n")  # mtime≈now (fresh)

    out = reconcile_finished_orphans(cfg, proj.slug)

    assert out == []
    assert f.exists()


def test_reconcile_norc_no_release_log_left_inflight(tmp_path: Path) -> None:
    # Stale no-.rc marker whose log has NO release line never demonstrably
    # succeeded → NOT log-recovered (left for the age-fail reaper).
    import os
    from bot_squad_worker.deploy import reconcile_finished_orphans
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    base = _deploy_base(cfg, proj.slug)
    proc = base / "processing"
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "1700000000000-building.json"
    f.write_text(json.dumps({"queue_id": "building", "target": "staging"}))
    runs = _runs_dir(cfg, proj.slug)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "building.log").write_text("Step 5/12 : COPY web/ .\n")  # mid-build
    old = time.time() - 100000
    os.utime(f, (old, old))

    out = reconcile_finished_orphans(cfg, proj.slug)

    assert out == []
    assert f.exists()
