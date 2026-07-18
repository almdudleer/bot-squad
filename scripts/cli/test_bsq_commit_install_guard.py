"""Regression test for T-0651 (prevention follow-up from the T-0305 incident).

2026-07-18 incident: a dev ran `bsq commit` with cwd inside the INSTALL clone
(/home/www/bot-squad, the deploy target) instead of the dev clone. `cmd_commit`
had no location check on the plain (non `--hunks`) path — it just staged +
committed via safe-commit.sh wherever `git rev-parse --show-toplevel`
pointed — so the commit (534bf49) landed in-place in the install clone. That
failed the deploy's destroy-guard (a cherry-pick of the content alone was not
enough to unstick it) and required operator recovery (cherry-pick to dev
clone + install reset to the deployed sha + redeploy).

Fix: `cmd_commit` now refuses outright when the resolved git toplevel is the
same clone as `BOT_SQUAD` (the running installation), before any staging or
`safe-commit.sh` invocation, and points the dev at the real dev clone path.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_SAFE_COMMIT = _BSQ_PATH.parent / "safe-commit.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _run_bsq(cwd: Path, bot_squad: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["BOT_SQUAD"] = bot_squad
    env["BOT_SQUAD_SKIP_PEER_CHECK"] = "1"
    return subprocess.run(
        ["python3", str(_BSQ_PATH), *args],
        cwd=cwd, capture_output=True, text=True, env=env,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "f.txt").write_text("base\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "base")


def _wire_safe_commit(bot_squad: Path) -> None:
    """So the guard is the ONLY thing that can block a commit here — without
    this, a bypassed guard could fail for the wrong reason (missing
    safe-commit.sh) and make the refusal assertions pass vacuously."""
    (bot_squad / "scripts" / "cli").mkdir(parents=True, exist_ok=True)
    (bot_squad / "scripts" / "cli" / "safe-commit.sh").symlink_to(_SAFE_COMMIT)


@pytest.fixture()
def install_clone(tmp_path):
    """A throwaway git repo standing in for the INSTALL clone: BOT_SQUAD
    points AT this same directory, mirroring the real deploy target where
    cwd == BOT_SQUAD == the git toplevel."""
    install = tmp_path / "install"
    _init_repo(install)
    (install / "config").mkdir(parents=True)
    (install / "config" / "projects.toml").write_text(
        f'[projects.bot-squad]\nrepo_path = "{tmp_path / "dev-clone"}"\n'
    )
    _wire_safe_commit(install)
    return install


def test_commit_refused_when_cwd_is_install_clone(install_clone):
    """`bsq commit` run with cwd == BOT_SQUAD must refuse, not silently
    succeed there (the T-0305 failure mode)."""
    (install_clone / "f.txt").write_text("changed\n")
    cp = _run_bsq(install_clone, str(install_clone), "commit", "-m", "oops", "f.txt")

    assert cp.returncode != 0, cp.stdout + cp.stderr
    assert "INSTALL clone" in cp.stderr
    # Nothing landed: still just the one base commit.
    log = _git(install_clone, "log", "--format=%H").split()
    assert len(log) == 1


def test_commit_refusal_points_at_dev_clone(install_clone):
    (install_clone / "f.txt").write_text("changed\n")
    cp = _run_bsq(install_clone, str(install_clone), "commit", "-m", "oops", "f.txt")

    cfg = tomllib.loads((install_clone / "config" / "projects.toml").read_text())
    dev_clone_path = cfg["projects"]["bot-squad"]["repo_path"]
    assert dev_clone_path in cp.stderr


def test_commit_refused_via_hunks_path_too(install_clone):
    """The guard sits before the --hunks/plain dispatch, so both commit
    forms are covered."""
    (install_clone / "f.txt").write_text("changed\n")
    cp = _run_bsq(
        install_clone, str(install_clone), "commit", "--hunks", "-m", "oops", "f.txt"
    )

    assert cp.returncode != 0, cp.stdout + cp.stderr
    assert "INSTALL clone" in cp.stderr


def test_commit_still_works_from_dev_clone(tmp_path):
    """Sanity/no-regression: a commit from a real editing clone (cwd !=
    BOT_SQUAD) still succeeds — the guard must not overreach."""
    dev = tmp_path / "dev-clone"
    _init_repo(dev)
    bot_squad_dir = tmp_path / "install"  # a DIFFERENT clone stands in for BOT_SQUAD
    _init_repo(bot_squad_dir)
    _wire_safe_commit(bot_squad_dir)

    (dev / "f.txt").write_text("changed\n")
    cp = _run_bsq(dev, str(bot_squad_dir), "commit", "-m", "real edit", "f.txt")

    assert cp.returncode == 0, cp.stdout + cp.stderr
    log = _git(dev, "log", "--format=%H").split()
    assert len(log) == 2
