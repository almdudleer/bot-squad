"""Regression test for T-0648: bsq commit shared-index race.

2026-07-18 incident: two concurrent `bsq commit` lanes (p31/p34) with
DISJOINT staged file sets ended up with one lane's files swept into the
other's commit, leaving a dangling commit and a dev believing its work had
landed when it hadn't (silent work-loss + false READY).

Root cause (reproduced under stress before the fix): `cmd_commit` ran
`git add -- <files>` UNLOCKED, then handed off to `safe-commit.sh`, which
only takes its advisory flock around `git commit`. A concurrent lane's
locked `git commit` and this lane's unlocked `git add` both touch the one
shared `.git/index`, and can collide on `.git/index.lock`.

Fix: `bsq commit` now hands its path list to `safe-commit.sh` (via a
NUL-separated temp file, `BOT_SQUAD_STAGE_PATHS_FILE`) instead of staging
itself; `safe-commit.sh` stages AFTER taking the flock, so add+commit is one
lock-scoped critical section per lane — no interleaving with a peer's
add/commit is possible.

This test drives `bsq commit` as real concurrent OS processes (not just
sequential calls) across many iterations to hit the timing window, and
asserts no resulting commit ever mixes files from both lanes.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"

_ITERATIONS = 15


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _run_bsq(repo: Path, bot_squad: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["BOT_SQUAD"] = bot_squad
    env["BOT_SQUAD_SKIP_PEER_CHECK"] = "1"
    return subprocess.run(
        ["python3", str(_BSQ_PATH), *args],
        cwd=repo, capture_output=True, text=True, env=env,
    )


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repo registered as project 'proj' in a temp BOT_SQUAD."""
    bot_squad = tmp_path / "bs"
    repo = tmp_path / "proj"
    repo.mkdir()
    (bot_squad / "data" / "proj").mkdir(parents=True)
    (bot_squad / "config").mkdir(parents=True)
    (bot_squad / "config" / "projects.toml").write_text(
        f'[projects.proj]\nrepo_path = "{repo}"\n'
    )
    (bot_squad / "scripts" / "cli").mkdir(parents=True)
    (bot_squad / "scripts" / "cli" / "safe-commit.sh").symlink_to(
        _BSQ_PATH.parent / "safe-commit.sh"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "laneA").mkdir()
    (repo / "laneB").mkdir()
    for i in range(_ITERATIONS):
        (repo / "laneA" / f"f{i}.txt").write_text("base\n")
        (repo / "laneB" / f"g{i}.txt").write_text("base\n")
    _git(repo, "add", "laneA", "laneB")
    _git(repo, "commit", "-q", "-m", "base")
    return {"repo": repo, "bot_squad": str(bot_squad)}


def test_concurrent_disjoint_commits_never_mix(repo):
    """Two lanes committing disjoint file sets under real concurrency: every
    resulting commit contains ONLY that lane's own file, and every file
    lands (no EEXIST-style silent drops)."""
    r, bs = repo["repo"], repo["bot_squad"]
    base_sha = _git(r, "rev-parse", "HEAD").strip()

    results = {"laneA": [], "laneB": []}
    errors = []

    def lane(name: str, subdir: str, prefix: str):
        try:
            for i in range(_ITERATIONS):
                f = f"{subdir}/{prefix}{i}.txt"
                (r / f).write_text(f"{name}-{i}-{os.urandom(4).hex()}\n")
                cp = _run_bsq(r, bs, "commit", "-m", f"{name} commit {i}", f)
                results[name].append((f, cp.returncode, cp.stdout, cp.stderr))
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    tA = threading.Thread(target=lane, args=("laneA", "laneA", "f"))
    tB = threading.Thread(target=lane, args=("laneB", "laneB", "g"))
    tA.start()
    tB.start()
    tA.join(timeout=120)
    tB.join(timeout=120)

    assert not errors, errors
    assert tA.is_alive() is False and tB.is_alive() is False

    # No silent work-loss: every single-file commit attempt succeeded.
    for name in ("laneA", "laneB"):
        failed = [(f, rc, out, err) for f, rc, out, err in results[name] if rc != 0]
        assert not failed, f"{name} had failed commits: {failed}"

    # No mixing: every non-base commit touches exactly one lane's directory.
    log = _git(r, "log", "--format=%H").split()
    for sha in log:
        if sha == base_sha:
            continue
        files = _git(r, "show", "--name-only", "--format=", sha).split()
        touches_a = any(f.startswith("laneA/") for f in files)
        touches_b = any(f.startswith("laneB/") for f in files)
        assert not (touches_a and touches_b), (
            f"commit {sha} mixed both lanes' files: {files}"
        )

    # Both lanes' full file sets ended up committed (findable at HEAD).
    head_files = set(_git(r, "ls-tree", "-r", "--name-only", "HEAD").split())
    for i in range(_ITERATIONS):
        assert f"laneA/f{i}.txt" in head_files
        assert f"laneB/g{i}.txt" in head_files


def test_commit_echoes_file_list(repo):
    r, bs = repo["repo"], repo["bot_squad"]
    (r / "laneA" / "f0.txt").write_text("changed\n")
    cp = _run_bsq(r, bs, "commit", "-m", "echo test", "laneA/f0.txt")
    assert cp.returncode == 0, cp.stderr
    assert "committed 1 file(s)" in cp.stdout
    assert "laneA/f0.txt" in cp.stdout
