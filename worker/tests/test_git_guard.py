"""T-0186: regression tests for the victim-side git-add guard (git-guard.sh).

The wrapper is a PATH-shimmed `git` that refuses wide/dir `git add` forms and
passes every other git command through. We exercise it against a throwaway git
repo with the wrapper placed ahead of the real git on PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GUARD = _REPO / "scripts" / "cli" / "git-guard.sh"

_REAL_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(
    _REAL_GIT is None or not _GUARD.exists(),
    reason="git or git-guard.sh not available",
)


@pytest.fixture()
def guarded_repo(tmp_path):
    """A git repo with one tracked file + a bin dir shimming `git` to the guard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").symlink_to(_GUARD)

    env = dict(os.environ)
    # Wrapper dir first; keep the real git's dir on PATH so the guard can find it.
    env["PATH"] = f"{bindir}:{os.path.dirname(_REAL_GIT)}:{env.get('PATH', '')}"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@t"

    def run(*args, env_override=None):
        run_env = dict(env)
        if env_override:
            run_env.update(env_override)
        return subprocess.run(
            ["git", *args], cwd=repo, env=run_env,
            capture_output=True, text=True,
        )

    # init + a committed baseline so `add -u` has something to update.
    subprocess.run([_REAL_GIT, "init", "-q"], cwd=repo, check=True)
    (repo / "tracked.py").write_text("x = 1\n")
    (repo / "other.py").write_text("y = 1\n")
    subprocess.run([_REAL_GIT, "add", "tracked.py", "other.py"], cwd=repo, check=True)
    subprocess.run([_REAL_GIT, "commit", "-qm", "base"], cwd=repo, env=env, check=True)
    (repo / "tracked.py").write_text("x = 2\n")  # an unstaged modification
    return run


@pytest.mark.parametrize("wide", [["-A"], ["-u"], ["."], ["--all"], ["-An"]])
def test_wide_add_refused(guarded_repo, wide):
    r = guarded_repo("add", *wide)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "REFUSED" in r.stderr


def test_subdirectory_add_refused(guarded_repo, tmp_path):
    subdir = tmp_path / "repo" / "pkg"
    subdir.mkdir()
    (subdir / "mod.py").write_text("z = 1\n")
    r = guarded_repo("add", "pkg")  # a directory pathspec
    assert r.returncode == 3
    assert "REFUSED" in r.stderr


def test_explicit_file_add_allowed(guarded_repo):
    r = guarded_repo("add", "tracked.py")
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_non_add_passthrough(guarded_repo):
    r = guarded_repo("status", "--short")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "tracked.py" in r.stdout


def test_escape_hatch_allows_wide(guarded_repo):
    r = guarded_repo("add", "-A", env_override={"BOT_SQUAD_ALLOW_WIDE_ADD": "1"})
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "REFUSED" not in r.stderr
