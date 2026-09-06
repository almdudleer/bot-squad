"""T-1018: a frozen extract under any repo silently reads THAT repo's VCS state.

`git rev-parse --show-toplevel` **WALKS UP**. Measured 2026-09-06, not reasoned:
a directory with no `.git` of its own, nested inside a repo, returns the
ANCESTOR toplevel with rc 0; from a directory with no repo above it, rc 128.

So a faithful `git archive` extract placed under a repository is still a
faithful extract — the T-0819 completeness proof passes — and every
checkout-gated test in it runs against a DIFFERENT repository than the code
being certified. It is a contaminated AMBIENT rather than a contaminated tree,
and it is INDISTINGUISHABLE in every number from a correct run: same collected,
same passed, same zero skipped.

**THE ARM THAT MAKES THIS FALSIFIABLE is `test_an_extract_under_a_repo_is_REFUSED`.**
A fixture that cannot produce the bad state cannot test a guard against it, and
the whole defect is that the bad state looks exactly like the good one. The
first assertion of that test is therefore that the ambient probe really does see
the ancestor — if THAT ever stops holding, the guard is being tested against a
condition it cannot meet and every other test here is vacuous.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
bsq = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("bsq_mod", _loader))
_loader.exec_module(bsq)


def _run_child_hard_kill(argv: list, *, cwd, env=None,
                          timeout: float) -> subprocess.CompletedProcess:
    """Run ``verify-isolated`` with a HARD kill on timeout (T-1024; the T-0212
    idiom from routines.py / deploy.py). A bare ``timeout=`` on
    ``subprocess.run`` only kills the direct child on expiry — a grandchild
    still holding the stdout/stderr pipe leaves ``communicate()`` blocked past
    the stated timeout anyway. Leading its own process group
    (``start_new_session=True``) lets a timed-out run's ``killpg`` reach every
    descendant.
    """
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # own process group -> killpg reaches children
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _said(capsys) -> str:
    """Everything die() emitted — it writes to stderr and may also print."""
    cap = capsys.readouterr()
    return cap.out + cap.err


def _git(*argv, cwd):
    return subprocess.run(["git", *argv], cwd=str(cwd), check=True,
                          capture_output=True, text=True)


def _make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README").write_text("t1018\n")
    _git("init", "-q", ".", cwd=path)
    _git("config", "user.email", "t1018@example.invalid", cwd=path)
    _git("config", "user.name", "t1018", cwd=path)
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "base", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


# --- the mechanism itself ---------------------------------------------------

def test_the_probe_walks_UP_and_finds_an_ancestor_repo(tmp_path):
    """The measurement the whole ticket rests on. If this ever goes green the
    other way round, the defect does not exist and the guard is dead weight."""
    outer = tmp_path / "outer"
    _make_repo(outer)
    nested = outer / "nested" / "deep"
    nested.mkdir(parents=True)
    assert not (nested / ".git").exists(), "the nested dir must have no .git"

    top = bsq._ambient_repo_at_or_above(str(nested), dict(os.environ))
    assert top, "the probe found nothing — it does NOT walk up, ticket is moot"
    assert os.path.realpath(top) == os.path.realpath(outer), top


def test_a_directory_with_no_repo_above_it_sees_nothing(tmp_path):
    """The negative half. Without it, a probe that always returned a path would
    satisfy the test above and look correct."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert bsq._ambient_repo_at_or_above(str(lonely), dict(os.environ)) == ""


def test_the_probe_honours_the_childs_GIT_DIR_rather_than_sanitising_it(tmp_path):
    """The guard must model THE CHILD's view, not an idealised one.

    git honours GIT_DIR/GIT_WORK_TREE, and the child pytest sees whatever the
    environment says. A guard that scrubbed them would pass while the child
    still read the wrong repo — a guard that is blind to the thing it guards.
    """
    outer = tmp_path / "elsewhere"
    _make_repo(outer)
    lonely = tmp_path / "lonely2"
    lonely.mkdir()
    env = dict(os.environ)
    env["GIT_DIR"] = str(outer / ".git")
    env["GIT_WORK_TREE"] = str(outer)
    top = bsq._ambient_repo_at_or_above(str(lonely), env)
    assert os.path.realpath(top) == os.path.realpath(outer), top


# --- the guard --------------------------------------------------------------

def test_an_extract_under_a_repo_is_REFUSED(tmp_path, capsys):
    """THE ARM THAT MAKES THE GUARD FALSIFIABLE.

    Builds the bad state deliberately — a tree with no `.git`, under a repo —
    and asserts the tool refuses. The first assertion proves the fixture really
    reached the bad state; without it a guard that never fires would pass.
    """
    outer = tmp_path / "outer"
    _make_repo(outer)
    dest = outer / "extract"
    dest.mkdir()
    assert bsq._ambient_repo_at_or_above(str(dest), dict(os.environ)), (
        "fixture did not reach the bad state — nothing here tests the guard")

    with pytest.raises(SystemExit):
        bsq._assert_vcs_isolation(str(dest), False, dict(os.environ))
    said = _said(capsys)
    assert "AMBIENT GIT REPO" in said
    assert os.path.realpath(str(outer)) in said, "the message must NAME the repo"


def test_an_extract_with_no_repo_above_it_is_allowed(tmp_path):
    """The healthy case, run FIRST in spirit: a guard that refuses everything
    would satisfy the test above and block every certification on the fleet."""
    dest = tmp_path / "clean_extract"
    dest.mkdir()
    bsq._assert_vcs_isolation(str(dest), False, dict(os.environ))  # must not raise


def test_with_git_requires_the_repo_to_BE_the_tree(tmp_path, capsys):
    """The check inverts for the clone arm — and an ancestor is not good enough.

    A clone that resolved to a repo ABOVE it would satisfy a naive "is there a
    repo?" test while reading someone else's VCS state: the same defect wearing
    the other arm's clothes.
    """
    outer = tmp_path / "outer2"
    _make_repo(outer)
    dest = outer / "not_its_own_repo"
    dest.mkdir()
    with pytest.raises(SystemExit):
        bsq._assert_vcs_isolation(str(dest), True, dict(os.environ))
    # The MESSAGE, not just the exit: a reader acts on which of the two
    # clone-arm faults fired, and they need opposite fixes.
    assert "resolves to a DIFFERENT repo" in _said(capsys)


def test_with_git_accepts_a_tree_that_is_its_own_repo(tmp_path):
    dest = tmp_path / "real_clone"
    _make_repo(dest)
    bsq._assert_vcs_isolation(str(dest), True, dict(os.environ))  # must not raise


def test_with_git_refuses_a_tree_with_no_repo_at_all(tmp_path, capsys):
    """Distinct from the test above, and the message is why.

    Deleting the no-repo branch leaves BOTH tests passing on `SystemExit`
    alone — `realpath("")` is the cwd, so the different-repo branch catches it
    too and prints an empty repo name. Right exit code, message a reader cannot
    act on. Measured as a green mutation; this assertion is the repair.
    """
    dest = tmp_path / "no_repo"
    dest.mkdir()
    with pytest.raises(SystemExit):
        bsq._assert_vcs_isolation(str(dest), True, dict(os.environ))
    assert "NO reachable git repo" in _said(capsys)


# --- end to end, through the real CLI --------------------------------------

def test_verify_isolated_REFUSES_when_TMPDIR_points_inside_a_repo(tmp_path):
    """The realistic route in, end to end.

    Nobody chooses to extract under a repo — they set TMPDIR to a scratch dir
    that happens to live inside a clone, or they point one at a worktree. The
    tool then materialises a perfectly faithful extract in a place where its
    isolation is silently gone.
    """
    outer = tmp_path / "host_repo"
    sha = _make_repo(outer)
    scratch = outer / "scratch"          # INSIDE the repo, on purpose
    scratch.mkdir()

    env = dict(os.environ)
    env["TMPDIR"] = str(scratch)
    proc = _run_child_hard_kill(
        [sys.executable, str(_BSQ_PATH), "verify-isolated", "--",
         sys.executable, "-c", "print('CHILD RAN')"],
        cwd=outer, env=env, timeout=60)
    out = proc.stdout + proc.stderr
    assert "AMBIENT GIT REPO" in out, out
    assert "CHILD RAN" not in out, "the child ran anyway — the guard is advisory"
    assert proc.returncode != 0, out


def test_verify_isolated_ALLOWS_a_normal_extract_and_says_so(tmp_path):
    """The positive control for the end-to-end arm: the same invocation with
    TMPDIR left alone must run and must announce the check it passed."""
    outer = tmp_path / "host_repo2"
    _make_repo(outer)
    proc = _run_child_hard_kill(
        [sys.executable, str(_BSQ_PATH), "verify-isolated", "--",
         sys.executable, "-c", "print('CHILD RAN')"],
        cwd=outer, env=dict(os.environ), timeout=60)
    out = proc.stdout + proc.stderr
    assert "no git repo at or above the extract" in out, out
    assert "CHILD RAN" in out, out
