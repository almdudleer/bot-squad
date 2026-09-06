"""T-1019: pin the provenance guard — that it fires, and that it doesn't.

The defect: a suite can report a clean, well-formed result about a state that
exists in no commit and never will. T-1002 paid for this twice in one evening
(two runs 33 minutes apart legitimately disagreeing, neither disagreement
about the substrate) before a reader-at-two-shas check settled it — see
``provenance.py``'s docstring for the full account.

Three things this file exists to prove, in order, because a detector nobody
first proved silent on the healthy case cannot be trusted on the alarm it
raises (operator's own instruction on this ticket, 2026-09-06):

1. **THE HEALTHY CASE FIRST.** A clean tree, fully committed, produces ZERO
   output. Every fire test below is only meaningful once this holds.
2. **Scope is imported modules, never `git status`.** A dirty file the run
   never imported must not appear, or every run in this five-lane clone would
   alarm and the alarm would be muted within a day (operator's own words).
3. **No `.git` says CANNOT CHECK, never a silent clean.** A frozen extract
   must not be indistinguishable from a verified-clean tree.

Unit tests drive the pure functions directly against a throwaway git repo
(never against this live shared clone — its dirty state changes under every
lane, T-1019's whole reason for existing). The end-to-end tests load the
hooks OUT OF THE REAL `conftest.py` by path, same construction as
`test_substrate_refusal.py`, so this file and the shipped guard cannot drift
apart.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REAL_CONFTEST = _TESTS_DIR / "conftest.py"


def _load_provenance():
    spec = importlib.util.spec_from_file_location(
        "t1019_provenance_under_test", _TESTS_DIR / "provenance.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provenance():
    return _load_provenance()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with two committed tracked files."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t1019@example.com")
    _git(root, "config", "user.name", "T-1019 test")
    (root / "a.py").write_text("A = 1\n")
    (root / "b.py").write_text("B = 1\n")
    _git(root, "add", "a.py", "b.py")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


# --- repo_root ----------------------------------------------------------------


def test_repo_root_finds_the_toplevel(provenance, repo):
    assert provenance.repo_root(repo) == repo.resolve()


def test_repo_root_is_none_with_no_git_reachable(provenance, tmp_path):
    """A frozen `git archive` extract has no `.git` by construction — this is
    the CANNOT-CHECK condition, not a clean tree."""
    lone = tmp_path / "no_git_here"
    lone.mkdir()
    assert provenance.repo_root(lone) is None


# --- imported_repo_files: scope is IMPORTED, never every dirty file -----------


def test_imported_repo_files_keeps_only_files_under_root(provenance, repo, tmp_path):
    outside = tmp_path / "elsewhere.py"
    outside.write_text("X = 1\n")

    class _Mod:
        pass

    m_in = _Mod()
    m_in.__file__ = str(repo / "a.py")
    m_out = _Mod()
    m_out.__file__ = str(outside)
    m_builtin = _Mod()  # no __file__ at all, e.g. a frozen/builtin module

    result = provenance.imported_repo_files(repo, [m_in, m_out, m_builtin])
    assert result == [Path("a.py")]


def test_imported_repo_files_dedupes_and_sorts(provenance, repo):
    class _Mod:
        pass

    m1 = _Mod()
    m1.__file__ = str(repo / "b.py")
    m2 = _Mod()
    m2.__file__ = str(repo / "a.py")
    m3 = _Mod()
    m3.__file__ = str(repo / "a.py")  # same file, e.g. two aliasing entries

    assert provenance.imported_repo_files(repo, [m1, m2, m3]) == [Path("a.py"), Path("b.py")]


# --- differs_from_head: THE HEALTHY CASE FIRST ---------------------------------


def test_a_fully_committed_tree_differs_from_nothing(provenance, repo):
    """Run this before trusting a single alarm below."""
    assert provenance.differs_from_head(repo, [Path("a.py"), Path("b.py")]) == []


def test_a_modified_tracked_file_differs(provenance, repo):
    """T-1002's own case: a peer's edit landed before the run started."""
    (repo / "a.py").write_text("A = 2\n")
    assert provenance.differs_from_head(repo, [Path("a.py"), Path("b.py")]) == [Path("a.py")]


def test_an_untracked_new_file_differs(provenance, repo):
    """Operator's live specimen: 19 of 21 extra collected items came from ONE
    untracked file. It exists in no commit just as surely as a modified one."""
    (repo / "c.py").write_text("C = 1\n")
    assert provenance.differs_from_head(repo, [Path("c.py")]) == [Path("c.py")]


def test_a_dirty_file_the_run_never_imported_is_not_reported(provenance, repo):
    """Decision 1: scope is imported modules, never `git status`. A dirty
    `b.py` this run never imported must be invisible to it — otherwise every
    run in a five-lane clone alarms and the alarm gets muted within a day."""
    (repo / "b.py").write_text("B = 2\n")
    assert provenance.differs_from_head(repo, [Path("a.py")]) == []


def test_staged_but_uncommitted_also_differs(provenance, repo):
    """Staged-not-committed is still "exists in no commit"."""
    (repo / "a.py").write_text("A = 3\n")
    _git(repo, "add", "a.py")
    assert provenance.differs_from_head(repo, [Path("a.py")]) == [Path("a.py")]


def test_empty_import_set_asks_git_nothing(provenance, repo):
    assert provenance.differs_from_head(repo, []) == []


# --- check(): the whole pipeline, and it never repairs anything ---------------


def test_check_is_clean_on_a_committed_tree(provenance, repo, monkeypatch):
    monkeypatch.setattr(provenance, "TESTS_DIR", repo)

    class _Mod:
        pass

    m = _Mod()
    m.__file__ = str(repo / "a.py")

    result = provenance.check([m])
    assert result.clean
    assert not result.cannot_check
    assert result.differing == []


def test_check_reports_cannot_check_with_no_git(provenance, tmp_path, monkeypatch):
    lone = tmp_path / "frozen_extract"
    lone.mkdir()
    monkeypatch.setattr(provenance, "TESTS_DIR", lone)
    monkeypatch.setattr(provenance, "repo_root", lambda start=lone: None)

    result = provenance.check([])
    assert result.cannot_check
    assert not result.clean


def test_check_never_modifies_the_files_it_inspects(provenance, repo, monkeypatch):
    """The warning must not repair, only report (operator's own instruction)."""
    monkeypatch.setattr(provenance, "TESTS_DIR", repo)
    before = (repo / "a.py").read_bytes()
    before_mtime = (repo / "a.py").stat().st_mtime_ns

    class _Mod:
        pass

    m = _Mod()
    m.__file__ = str(repo / "a.py")
    (repo / "a.py").write_text("A = 999\n")
    after_write = (repo / "a.py").read_bytes()

    result = provenance.check([m])
    assert result.differing == [Path("a.py")]
    assert (repo / "a.py").read_bytes() == after_write, "check() must never rewrite a file"
    assert after_write != before, "sanity: the edit really happened"
    # A working-tree edit necessarily changes mtime; the point is check()
    # itself performs no further write, staging, or revert.
    assert (repo / "a.py").stat().st_mtime_ns >= before_mtime


# --- end to end, through the REAL conftest hooks ------------------------------

_TMP_CONFTEST = f'''
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("real_conftest", r"{_REAL_CONFTEST}")
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)

# `provenance.TESTS_DIR` defaults to the SHIPPED file's own directory (the dev
# clone), which is irrelevant to this throwaway repo. Point the loaded,
# unmodified module at THIS tree instead of restating any of its logic —
# `repo_root()` reads `TESTS_DIR` dynamically for exactly this seam.
_real.provenance.TESTS_DIR = Path(__file__).resolve().parent

# The SHIPPED hooks, not a restatement of them.
pytest_terminal_summary = _real.pytest_terminal_summary
pytest_unconfigure = _real.pytest_unconfigure
'''

_ONE_TEST = '''
def test_ok():
    assert 1 == 1
'''

_TWO_TESTS_ONE_IMPORTS_HELPER = '''
import helper

def test_ok():
    assert helper.VALUE == 1
'''


def _run_child_hard_kill(argv: list, *, cwd: Path,
                          timeout: float) -> subprocess.CompletedProcess:
    """Same T-1024 hard-kill idiom as test_substrate_refusal.py: a bare
    ``timeout=`` on ``subprocess.run`` leaves a grandchild holding the pipe."""
    proc = subprocess.Popen(
        argv, cwd=str(cwd), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
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


def _git_repo_dir(tmp_path: Path) -> Path:
    root = tmp_path / "e2e_repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t1019@example.com")
    _git(root, "config", "user.name", "T-1019 test")
    return root


def _run_pytest_in(root: Path, timeout: float = 60) -> str:
    proc = _run_child_hard_kill(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_sample.py"],
        cwd=root, timeout=timeout,
    )
    return proc.stdout + proc.stderr


def test_e2e_a_fully_committed_tree_prints_nothing(tmp_path):
    """THE HEALTHY CASE, end to end. Must be zero before any fire test counts."""
    root = _git_repo_dir(tmp_path)
    (root / "conftest.py").write_text(_TMP_CONFTEST)
    (root / "test_sample.py").write_text(_ONE_TEST)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")

    out = _run_pytest_in(root)
    assert "THIS RUN MEASURED AN UNCOMMITTED TREE" not in out
    assert "PROVENANCE CANNOT BE CHECKED" not in out
    assert "1 passed" in out


def test_e2e_a_modified_imported_test_file_fires(tmp_path):
    root = _git_repo_dir(tmp_path)
    (root / "conftest.py").write_text(_TMP_CONFTEST)
    (root / "test_sample.py").write_text(_ONE_TEST)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")

    # Edit AFTER the commit — exactly T-1002's shape: old commit, new tree.
    (root / "test_sample.py").write_text(_ONE_TEST + "\n# an uncommitted edit\n")

    out = _run_pytest_in(root)
    assert "THIS RUN MEASURED AN UNCOMMITTED TREE" in out
    assert "test_sample.py" in out
    assert "1 passed" in out
    assert "!! UNCOMMITTED PROVENANCE" in out, "the trailer must ride the summary line too"


def test_e2e_an_untracked_imported_helper_fires(tmp_path):
    """Operator's specimen: an untracked file inflated a collected count by 19.
    Here it is a helper actually imported and executed by the one test."""
    root = _git_repo_dir(tmp_path)
    (root / "conftest.py").write_text(_TMP_CONFTEST)
    (root / "test_sample.py").write_text(_ONE_TEST)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")

    # A brand-new, never-committed helper module that the test imports.
    (root / "helper.py").write_text("VALUE = 1\n")
    (root / "test_sample.py").write_text(_TWO_TESTS_ONE_IMPORTS_HELPER)
    _git(root, "add", "test_sample.py")  # stage the test edit; helper.py stays untracked
    _git(root, "commit", "-q", "-m", "wire up helper (helper.py itself NOT committed)")

    out = _run_pytest_in(root)
    assert "THIS RUN MEASURED AN UNCOMMITTED TREE" in out
    assert "helper.py" in out
    assert "1 passed" in out


def test_e2e_a_dirty_unimported_file_is_silent(tmp_path):
    """Decision 1, end to end: a dirty file this run never imports is invisible."""
    root = _git_repo_dir(tmp_path)
    (root / "conftest.py").write_text(_TMP_CONFTEST)
    (root / "test_sample.py").write_text(_ONE_TEST)
    (root / "unrelated.py").write_text("NEVER_IMPORTED = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")

    (root / "unrelated.py").write_text("NEVER_IMPORTED = 2\n")  # dirty, never imported

    out = _run_pytest_in(root)
    assert "THIS RUN MEASURED AN UNCOMMITTED TREE" not in out
    assert "1 passed" in out


def test_e2e_no_git_reachable_says_cannot_check_not_clean(tmp_path):
    """A frozen extract (no .git) must not read the same as a verified clean."""
    root = tmp_path / "frozen_no_git"
    root.mkdir()
    (root / "conftest.py").write_text(_TMP_CONFTEST)
    (root / "test_sample.py").write_text(_ONE_TEST)

    out = _run_pytest_in(root)
    assert "PROVENANCE CANNOT BE CHECKED" in out
    assert "THIS RUN MEASURED AN UNCOMMITTED TREE" not in out
    assert "1 passed" in out


def test_e2e_ordinary_red_run_still_gets_the_clean_provenance_banner_or_none(tmp_path):
    """The guard must not interfere with an ordinary failing test's own report,
    and on a committed tree it says nothing about provenance either way."""
    root = _git_repo_dir(tmp_path)
    (root / "conftest.py").write_text(_TMP_CONFTEST)
    (root / "test_sample.py").write_text("def test_bug():\n    assert 1 == 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")

    out = _run_pytest_in(root)
    assert "1 failed" in out
    assert "THIS RUN MEASURED AN UNCOMMITTED TREE" not in out
    assert "PROVENANCE CANNOT BE CHECKED" not in out
