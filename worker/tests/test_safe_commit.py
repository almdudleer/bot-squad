"""Tests for scripts/cli/safe-commit.sh — the shared-clone commit wrapper.

safe-commit is bash, not Python, so these shell out to the real script in a
throwaway git repo (no mocks). They lock in the two T-0145 guarantees:

  * absorb-proof: a pathspec commit never sweeps a peer's staged WIP into it
    (the T-0068 failure mode), and the `-a`/`--all` family is refused outright.
  * serialization: concurrent safe-commits queue on the flock instead of
    crashing on `.git/index.lock` (T-0093).

Manual-first (T-0158): the same scenarios were walked through by hand in a
scratch repo before being encoded here.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SAFE_COMMIT = Path(__file__).resolve().parents[2] / "scripts" / "cli" / "safe-commit.sh"


def _run(repo: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Bypass the peer-activity pre-commit hook if a global hooksPath is set —
    # we're exercising safe-commit's own guard, not the hook.
    env["BOT_SQUAD_SKIP_PEER_CHECK"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SAFE_COMMIT), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "shared-clone"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(r), check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(r), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(r), check=True)
    (r / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=str(r), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(r), check=True)
    return r


def _tracked(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "ls-tree", "--name-only", "-r", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    return set(out.split())


def _ncommits(repo: Path) -> int:
    return int(subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip())


# ---------------------------------------------------------------------------
# Absorb-proof guard (T-0145 / T-0068)
# ---------------------------------------------------------------------------


def test_pathspec_commit_does_not_absorb_peer_staged_file(repo: Path) -> None:
    """The T-0068 scenario: a peer has staged fileB in the shared index; my
    pathspec commit of fileA must NOT sweep fileB in."""
    (repo / "fileA.txt").write_text("devA work\n")
    (repo / "fileB.txt").write_text("devB WIP\n")
    # Both staged into the one shared index (bsq stages with `git add -- <path>`).
    subprocess.run(["git", "add", "--", "fileA.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "--", "fileB.txt"], cwd=str(repo), check=True)

    res = _run(repo, "-m", "devA: add fileA", "--", "fileA.txt")
    assert res.returncode == 0, res.stderr

    tracked = _tracked(repo)
    assert "fileA.txt" in tracked
    assert "fileB.txt" not in tracked  # peer WIP NOT absorbed
    # fileB is still staged, untouched, for its real author to commit.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "fileB.txt" in staged


def test_refuses_commit_all_short_flag(repo: Path) -> None:
    """`-am` (wide stage) is refused with exit 3 — no commit is created."""
    (repo / "base.txt").write_text("base\nmodified\n")  # a tracked mod -a would sweep
    before = _ncommits(repo)
    res = _run(repo, "-am", "should be refused")
    assert res.returncode == 3
    assert "REFUSED" in res.stderr
    assert _ncommits(repo) == before  # nothing committed


def test_refuses_commit_all_long_flag(repo: Path) -> None:
    (repo / "base.txt").write_text("base\nmodified\n")
    res = _run(repo, "--all", "-m", "wide")
    assert res.returncode == 3
    assert "REFUSED" in res.stderr


def test_commit_all_override_env_allows_it(repo: Path) -> None:
    """BOT_SQUAD_ALLOW_COMMIT_ALL=1 lets `-am` through (single-tenant escape hatch)."""
    (repo / "base.txt").write_text("base\nmodified\n")
    before = _ncommits(repo)
    res = _run(repo, "-am", "override", env_extra={"BOT_SQUAD_ALLOW_COMMIT_ALL": "1"})
    assert res.returncode == 0, res.stderr
    assert _ncommits(repo) == before + 1


def test_message_starting_with_dash_a_is_not_flagged(repo: Path) -> None:
    """A commit message that begins with `-a` must not be mistaken for the flag."""
    (repo / "fileC.txt").write_text("c\n")
    subprocess.run(["git", "add", "--", "fileC.txt"], cwd=str(repo), check=True)
    res = _run(repo, "-m", "-a tweak to the api surface", "--", "fileC.txt")
    assert res.returncode == 0, res.stderr
    assert "fileC.txt" in _tracked(repo)


def test_pathspec_message_backward_compatible(repo: Path) -> None:
    """The canonical bsq form `-m MSG -- <file>` still works unchanged."""
    (repo / "fileD.txt").write_text("d\n")
    subprocess.run(["git", "add", "--", "fileD.txt"], cwd=str(repo), check=True)
    res = _run(repo, "-m", "plain pathspec commit", "--", "fileD.txt")
    assert res.returncode == 0, res.stderr
    assert "fileD.txt" in _tracked(repo)


# ---------------------------------------------------------------------------
# flock serialization (T-0093)
# ---------------------------------------------------------------------------


def test_concurrent_safe_commits_serialize_without_crashing(repo: Path) -> None:
    """Two concurrent safe-commits queue on the flock; both land, neither
    crashes on `.git/index.lock`."""
    (repo / "p1.txt").write_text("1\n")
    (repo / "p2.txt").write_text("2\n")
    subprocess.run(["git", "add", "--", "p1.txt", "p2.txt"], cwd=str(repo), check=True)

    env = dict(os.environ)
    env["BOT_SQUAD_SKIP_PEER_CHECK"] = "1"
    before = _ncommits(repo)
    procs = [
        subprocess.Popen(
            ["bash", str(SAFE_COMMIT), "-m", f"dev{n}", "--", f"p{n}.txt"],
            cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for n in (1, 2)
    ]
    rcs = [p.wait(timeout=60) for p in procs]

    assert rcs == [0, 0]
    assert _ncommits(repo) == before + 2
    tracked = _tracked(repo)
    assert {"p1.txt", "p2.txt"} <= tracked
