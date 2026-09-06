"""T-1010: `verify-isolated --with-git` — the arm a frozen extract cannot be.

`git archive` carries COMMITTED PATHS ONLY, so the extract the fleet was
mandated to certify on has no `.git`, and every test needing a real checkout
cannot run there. THE ISOLATION THAT MAKES THE EXTRACT TRUSTWORTHY IS THE SAME
PROPERTY THAT MAKES THOSE TESTS UNRUNNABLE — a fallback sharing the primary's
gate. T-0987 made that price visible (the skip census); this arm pays it.

THE PAIR IN `test_a_checkout_gated_test_*` IS THE WHOLE TICKET: the same test,
the same ref, the same command, RUNS under `--with-git` and SKIPS without it.
Either half alone proves nothing — a test that runs everywhere is not evidence
the arm works, and a test that skips everywhere is not evidence it was needed.

Measured on 2026-09-06 against the real trees, same ref, only the materialiser
differing: EXTRACT 71 passed / 5 skipped, CLONE 75 passed / 1 skipped. The one
still skipping is `test_t0931_task_states.py:59`, whose predicate is
`parents[3]` — the parent of the repo root. A WRONG ROOT IS WRONG IN A CLONE
TOO; that is T-1003 and this arm does not touch it.
"""
from __future__ import annotations

import importlib.util
import os
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


# A test that can only run where there is a real checkout. This is the probe the
# whole arm exists to make runnable — it is the shape of
# `test_t0878_registry_survives_deploy.py:369` and of `_skip_without_git`.
_CHECKOUT_GATED = '''\
import subprocess
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parent

def test_always_runs():
    assert True

def test_needs_a_real_checkout():
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
                          "ls-files"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "README" in out.stdout
'''


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """A throwaway repo for verify-isolated to materialise.

    ITS OWN REPO, not this checkout's (T-0983): a `git archive` extract has no
    `.git`, so a test reaching for the AMBIENT repo goes red on every clean run
    the moment this suite is certified the mandated way. The probe file is kept
    OUTSIDE the repo and committed INSIDE it under a second name, because the
    two arms are compared on the copy that rides in the tree.
    """
    d = tmp_path_factory.mktemp("t1010")
    repo = d / "repo"
    repo.mkdir()
    (repo / "README").write_text("t1010 throwaway repo\n")
    (repo / "test_checkout_gated.py").write_text(_CHECKOUT_GATED)
    for argv in (["git", "init", "-q", "."],
                 ["git", "config", "user.email", "t1010@example.invalid"],
                 ["git", "config", "user.name", "t1010"],
                 ["git", "add", "-A"],
                 ["git", "commit", "-qm", "base"]):
        subprocess.run(argv, cwd=str(repo), check=True, capture_output=True)
    # A SECOND commit, so HEAD is NOT the commit under test. Without it every
    # assertion about "the sha we asked for" is also satisfied by "whatever the
    # clone's HEAD happened to be", and a mutation swapping the sha for HEAD
    # goes GREEN — measured, that is exactly what happened to the first cut of
    # this file.
    (repo / "SECOND").write_text("a commit after the one under test\n")
    for argv in (["git", "add", "-A"], ["git", "commit", "-qm", "second"]):
        subprocess.run(argv, cwd=str(repo), check=True, capture_output=True)
    return d


def _run_verify(sandbox, cmd, with_git=False):
    argv = [sys.executable, str(_BSQ_PATH), "verify-isolated"]
    if with_git:
        argv.append("--with-git")
    argv += ["--"] + list(cmd)
    proc = subprocess.run(argv, cwd=str(sandbox / "repo"), env=dict(os.environ),
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


_PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "test_checkout_gated.py"]


# --- The pair. This IS the ticket. -----------------------------------------

def test_a_checkout_gated_test_SKIPS_in_the_archive_extract(sandbox):
    """The half that establishes the arm was needed at all.

    Without this, `test_..._RUNS_under_with_git` is satisfied by a test that
    would have run anywhere, and the whole ticket would be unfalsifiable.
    """
    rc, out = _run_verify(sandbox, _PYTEST, with_git=False)
    assert "SKIP CENSUS — 1 skipped" in out, out
    assert "not a git checkout" in out, out
    assert "1 passed, 1 skipped" in out, out
    assert rc == 0, out


def test_a_checkout_gated_test_RUNS_under_with_git(sandbox):
    """The half that establishes the arm works. Same ref, same command."""
    rc, out = _run_verify(sandbox, _PYTEST, with_git=True)
    assert "SKIP CENSUS — 0 skipped" in out, out
    assert "not a git checkout" not in out, out
    assert "2 passed" in out, out
    assert "MEASURED 2 test(s) executed" in out, out
    assert rc == 0, out


# --- The properties that make it safe to run in a shared clone -------------

def test_the_clone_is_not_hardlinked_to_the_source_repo(sandbox, tmp_path):
    """`--no-hardlinks` IS LOAD-BEARING, NOT CAUTION.

    `git clone --local` hardlinks the object store into the SOURCE repository
    by default. In this fleet the source is a clone ~7 live sessions are
    working in, so a test that writes to git inside the "isolated" tree could
    reach every one of them. A hardlinked object file has st_nlink > 1; a
    copied one does not.
    """
    dest = tmp_path / "clone"
    dest.mkdir()
    bsq._materialise_clone(str(sandbox / "repo"),
                           _first_commit_of(sandbox / "repo"), str(dest), "HEAD")
    objs = [p for p in (dest / ".git" / "objects").rglob("*") if p.is_file()]
    assert objs, "no objects in the clone — the probe itself is broken"
    shared = [p for p in objs if p.stat().st_nlink > 1]
    assert not shared, f"hardlinked into the source repo: {shared[:3]}"


def test_the_clone_carries_git_and_a_clean_tree_at_the_sha(sandbox, tmp_path):
    dest = tmp_path / "clone2"
    dest.mkdir()
    sha = _first_commit_of(sandbox / "repo")
    bsq._materialise_clone(str(sandbox / "repo"), sha, str(dest), sha)
    assert (dest / ".git").is_dir(), "no .git — the one thing this arm provides"
    assert (dest / "README").exists()
    at = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest),
                        capture_output=True, text=True)
    assert at.stdout.strip() == sha, at.stdout
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(dest),
                           capture_output=True, text=True)
    assert dirty.stdout.strip() == "", dirty.stdout


def test_the_clone_passes_the_same_completeness_proof_as_an_extract(sandbox, tmp_path):
    """The SOURCE instrument (T-0819) must not be weakened by the new arm."""
    dest = tmp_path / "clone3"
    dest.mkdir()
    sha = _first_commit_of(sandbox / "repo")
    bsq._materialise_clone(str(sandbox / "repo"), sha, str(dest), "HEAD")
    tracked, missing = bsq._extract_missing_paths(str(sandbox / "repo"), sha,
                                                  str(dest))
    assert tracked, tracked
    assert missing == [], missing


def test_the_tree_is_hoisted_so_dest_IS_the_tree(sandbox, tmp_path):
    """Identical layout to the extract, so nothing downstream — the
    completeness proof, GATE_EXTRACT_ROOT, the child's cwd — needs a branch on
    which arm produced the tree."""
    dest = tmp_path / "clone4"
    dest.mkdir()
    bsq._materialise_clone(str(sandbox / "repo"),
                           _first_commit_of(sandbox / "repo"), str(dest), "HEAD")
    assert not (dest / ".src").exists(), "scaffolding left behind"
    assert (dest / "README").exists()


# --- What the arm says about itself ----------------------------------------

def test_the_run_says_which_arm_it_is_and_what_it_does_not_buy(sandbox):
    """A proposal that only lists what it buys is a sales pitch, and a
    certification log is read by someone who was not here."""
    rc, out = _run_verify(sandbox, _PYTEST, with_git=True)
    assert "cloned to" in out, out
    assert "this is the CLONE arm" in out, out
    assert "SOURCE-INTEGRITY arm" in out, out
    assert "does NOT fix a test whose path derivation is wrong" in out, out
    assert "Origin is the source clone" in out, out


def test_the_archive_arm_still_calls_itself_an_extract(sandbox):
    """The banner has to distinguish them or a log cannot be attributed to an
    arm afterwards."""
    rc, out = _run_verify(sandbox, _PYTEST, with_git=False)
    assert "extracted to" in out, out
    assert "cloned to" not in out, out
    assert "CLONE arm" not in out, out


def test_a_clone_without_git_is_refused_not_reported(monkeypatch, sandbox, tmp_path):
    """The refusal for the one thing this arm exists to provide.

    Mutating the helper's own postcondition rather than trusting it: if some
    future git or filesystem produces a tree with no `.git`, the run must stop
    rather than quietly become a slower archive arm.
    """
    dest = tmp_path / "clone5"
    dest.mkdir()
    real_listdir = bsq.os.listdir

    def listdir_hiding_git(path):
        return [n for n in real_listdir(path) if n != ".git"]

    monkeypatch.setattr(bsq.os, "listdir", listdir_hiding_git)
    with pytest.raises(SystemExit):
        bsq._materialise_clone(str(sandbox / "repo"),
                               _first_commit_of(sandbox / "repo"), str(dest), "HEAD")
    assert not (dest / ".git").exists()


def test_it_materialises_the_SHA_ASKED_FOR_not_the_clone_default_head(
        sandbox, tmp_path):
    """`--no-checkout` then an EXPLICIT detached checkout of the sha.

    The sha we are handed is not necessarily any branch's tip — that is the
    whole point of certifying a commit rather than a branch. Cloning and taking
    whatever HEAD arrives would silently certify a DIFFERENT tree than the one
    named, and the log would still say the right sha.

    THIS TEST EXISTS BECAUSE THE MUTATION BATTERY CAUGHT ITS ABSENCE: swapping
    `sha` for `"HEAD"` in the checkout left the suite fully green, because the
    fixture's only commit WAS HEAD. A control that cannot distinguish the two
    hypotheses is not a control.
    """
    dest = tmp_path / "clone6"
    dest.mkdir()
    first = _first_commit_of(sandbox / "repo")
    head = _head_of(sandbox / "repo")
    assert first != head, "fixture no longer discriminates — see the docstring"

    bsq._materialise_clone(str(sandbox / "repo"), first, str(dest), first)

    at = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest),
                        capture_output=True, text=True)
    assert at.stdout.strip() == first, at.stdout
    # The tree, not just the ref: the later commit's file must NOT be here.
    assert (dest / "README").exists()
    assert not (dest / "SECOND").exists(), (
        "the clone carries the TIP's tree, not the requested sha's")


def _head_of(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _first_commit_of(repo: Path) -> str:
    """The commit under test — deliberately NOT the tip."""
    return subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"],
                          cwd=str(repo), capture_output=True, text=True,
                          check=True).stdout.strip().splitlines()[0]
