"""Tests for scripts/cli/safe-commit.sh — the shared-clone commit wrapper.

safe-commit is bash, not Python, so these shell out to the real script in a
throwaway git repo (no mocks). They lock in the two T-0145 guarantees:

  * absorb-proof: a pathspec commit never sweeps a peer's staged WIP into it
    (the T-0068 failure mode), and the `-a`/`--all` family is refused outright.
  * serialization: concurrent safe-commits queue on the flock instead of
    crashing on `.git/index.lock` (T-0093).

and the two T-0732 guarantees added after the 2026-07-27 sweep:

  * shared-index guard: a pathspec-LESS commit of a non-empty SHARED index is
    refused; the same commit against an isolated GIT_INDEX_FILE is allowed.
  * no loaded index: a commit the pre-commit hook blocks leaves nothing of ours
    staged in the shared index while we review.

Manual-first (T-0158): the same scenarios were walked through by hand in a
scratch repo before being encoded here — including a replay of the real
incident (peer stages WIP, I commit the index with no pathspec).
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
# Shared-index guard (T-0732 / the 2026-07-27 p279+p280 sweep)
# ---------------------------------------------------------------------------


def _staged(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    return set(out.split())


def test_refuses_pathspecless_commit_of_loaded_shared_index(repo: Path) -> None:
    """Replay of the incident: a peer has staged their WIP; the documented
    post-hoc recipe (`safe-commit -m MSG` with NO pathspec) must now be
    REFUSED instead of committing that peer's work."""
    (repo / "peer_wip.txt").write_text("peer WIP\n")
    (repo / "mine.txt").write_text("my hunk\n")
    subprocess.run(["git", "add", "--", "peer_wip.txt", "mine.txt"],
                   cwd=str(repo), check=True)
    before = _ncommits(repo)

    res = _run(repo, "-m", "mine only (or so I thought)")

    assert res.returncode == 3
    assert "REFUSED" in res.stderr
    # It names what would have been swept, so the refusal is actionable.
    assert "peer_wip.txt" in res.stderr
    assert _ncommits(repo) == before        # nothing committed
    assert "peer_wip.txt" in _staged(repo)  # peer's staging left intact


def test_pathspecless_commit_allowed_against_isolated_index(repo: Path) -> None:
    """The sanctioned escape: build the commit in your OWN index (what
    `bsq commit --hunks` does). A peer's staging can't reach it, so no pathspec
    is needed and the guard stays out of the way."""
    (repo / "peer_wip.txt").write_text("peer WIP\n")
    subprocess.run(["git", "add", "--", "peer_wip.txt"], cwd=str(repo), check=True)
    (repo / "base.txt").write_text("base\nmine\n")

    tmp_index = repo / ".git" / "t0732-tmp-index"
    env = {"GIT_INDEX_FILE": str(tmp_index)}
    subprocess.run(["git", "read-tree", "HEAD"], cwd=str(repo), check=True,
                   env={**os.environ, **env})
    subprocess.run(["git", "add", "--", "base.txt"], cwd=str(repo), check=True,
                   env={**os.environ, **env})

    res = _run(repo, "-m", "hunk-isolated commit", env_extra=env)

    assert res.returncode == 0, res.stderr
    assert "peer_wip.txt" not in _tracked(repo)  # peer WIP NOT absorbed
    assert "peer_wip.txt" in _staged(repo)       # still theirs to commit


def test_pathspecless_commit_allowed_when_shared_index_is_empty(repo: Path) -> None:
    """`--amend`-style fixups absorb nothing, so the guard must not fire on an
    empty index — otherwise it would block ordinary history touch-ups."""
    (repo / "base.txt").write_text("base\namended\n")
    subprocess.run(["git", "add", "--", "base.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "to be amended"], cwd=str(repo), check=True)

    res = _run(repo, "--amend", "--no-edit")
    assert res.returncode == 0, res.stderr


def test_shared_index_guard_respects_the_override(repo: Path) -> None:
    (repo / "solo.txt").write_text("single-tenant clone\n")
    subprocess.run(["git", "add", "--", "solo.txt"], cwd=str(repo), check=True)
    res = _run(repo, "-m", "solo", env_extra={"BOT_SQUAD_ALLOW_COMMIT_ALL": "1"})
    assert res.returncode == 0, res.stderr
    assert "solo.txt" in _tracked(repo)


# ---------------------------------------------------------------------------
# No loaded index while a blocked commit is being reviewed (T-0732 / p279)
# ---------------------------------------------------------------------------


def _block_commits(repo: Path) -> None:
    """Install a pre-commit hook that always fails — stands in for the
    peer-activity hook's deliberate first-attempt block, without depending on
    its recent-activity heuristics."""
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env bash\nexit 1\n")
    hook.chmod(0o755)


def _stage_paths_file(tmp: Path, *paths: str) -> str:
    f = tmp / "stage-paths"
    f.write_bytes(b"".join(p.encode() + b"\0" for p in paths))
    return str(f)


def test_blocked_commit_leaves_nothing_of_ours_staged(repo: Path, tmp_path: Path) -> None:
    """p279's feedback: the hook blocks attempt #1 and tells you to review —
    which used to leave your files sitting staged in the SHARED index for the
    whole review window, where a peer's commit could sweep them."""
    _block_commits(repo)
    (repo / "base.txt").write_text("base\nmy edit\n")
    env = {
        "BOT_SQUAD_STAGE_PATHS_FILE": _stage_paths_file(tmp_path, "base.txt"),
        "BOT_SQUAD_SKIP_PEER_CHECK": "0",
    }
    res = _run(repo, "-m", "will be blocked", "--", "base.txt", env_extra=env)

    assert res.returncode != 0
    assert _staged(repo) == set()  # index handed back clean


def test_blocked_commit_restores_a_peers_prior_staged_entry(repo: Path, tmp_path: Path) -> None:
    """Rolling back our staging must put the index back the way we found it —
    not blanket-unstage a peer's entry for the same path."""
    _block_commits(repo)
    (repo / "base.txt").write_text("base\npeer staged this\n")
    subprocess.run(["git", "add", "--", "base.txt"], cwd=str(repo), check=True)
    peer_entry = subprocess.run(
        ["git", "ls-files", "--stage", "--", "base.txt"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    (repo / "base.txt").write_text("base\nmy own edit on top\n")

    env = {
        "BOT_SQUAD_STAGE_PATHS_FILE": _stage_paths_file(tmp_path, "base.txt"),
        "BOT_SQUAD_SKIP_PEER_CHECK": "0",
    }
    res = _run(repo, "-m", "will be blocked", "--", "base.txt", env_extra=env)

    assert res.returncode != 0
    after = subprocess.run(
        ["git", "ls-files", "--stage", "--", "base.txt"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert after == peer_entry


def test_successful_commit_keeps_its_staging(repo: Path, tmp_path: Path) -> None:
    """The rollback is failure-only — a commit that lands must not be undone."""
    (repo / "ok.txt").write_text("landed\n")
    env = {"BOT_SQUAD_STAGE_PATHS_FILE": _stage_paths_file(tmp_path, "ok.txt")}
    res = _run(repo, "-m", "lands fine", "--", "ok.txt", env_extra=env)
    assert res.returncode == 0, res.stderr
    assert "ok.txt" in _tracked(repo)


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
