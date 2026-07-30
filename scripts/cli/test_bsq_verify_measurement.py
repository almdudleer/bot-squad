"""T-0819: `bsq verify-isolated` must refuse a stage it did not measure.

THE FAILURE THIS PINS is not a wrong number — it is a number-shaped absence.
A pytest collection error aborts the whole session, and the summary it leaves
behind (`2 skipped, 1 warning, 4 errors in 3.10s`) reads like a nearly-clean
run. Nothing in it says NOT ONE TEST EXECUTED. That framing survived for months
as "4 pre-existing failures" and, on 2026-07-30, was about to certify a P1.

EVERY SUMMARY LINE IN THIS FILE IS A REAL ONE — transcribed from the runs
recorded on T-0819 and from measurements taken while writing it, never composed
to fit the parser. That distinction is load-bearing here: the previous attempt
at this probe was validated against self-authored lines, passed, and then
returned a FALSE NEGATIVE on the first real output it saw, because
`1336 passed, 1326 warnings in 530.26s` puts the warnings clause between
`passed` and `in` and the pattern demanded they be adjacent. A control
validated only against inputs you wrote is a control over your own
expectations.
"""
from __future__ import annotations

import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

# `bsq` is extensionless, so it's loaded via SourceFileLoader (mirrors
# test_bsq_operator.py / test_bsq_pickup.py).
_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


# --- Real outputs -----------------------------------------------------------

# Measured 2026-07-30 by the operator, reproducing the documented api-only
# mount on the shared tree. This is the exact text the ticket exists for.
REAL_COLLECTION_ABORT = """\
ERROR tests/test_backlog_frontmatter_lint.py
ERROR tests/test_backlog_ids_lint.py
ERROR tests/test_backlog_provenance_lint.py
ERROR tests/test_migrate_users_split.py
!!!!!!!!!!!!!!!!!! Interrupted: 4 errors during collection !!!!!!!!!!!!!!!!!!!
2 skipped, 1 warning, 4 errors in 3.10s
"""

# Dev p532's worker stage, run inside bot-squad-api:latest (an image with no
# worker deps). The summary is the cleanest specimen of the defect on record.
REAL_WORKER_WRONG_IMAGE = """\
ERROR tests/test_graceful_shutdown.py
!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!
1 skipped, 1 warning, 1 error in 16.40s
"""

# The line that broke the previous probe. Real api run, repo-root mount.
REAL_GREEN_WITH_WARNINGS = "1336 passed, 1326 warnings in 530.26s\n"

# Dev p535's M12 re-run with a mutant that actually compiled: a genuine red.
REAL_GENUINE_RED = "2 failed, 24 passed in 88.58s\n"

# Measured by this session, 2026-07-30, in bot-squad-api:latest under the
# repo-root mount: `-m pytest -q --collect-only`.
REAL_COLLECT_ONLY = "1384 tests collected in 0.46s\n"


# --- The parser -------------------------------------------------------------

def test_parses_real_green_summary_with_warnings_clause():
    """The warnings clause sits between `passed` and `in`. This is the case a
    self-authored fixture would have missed."""
    m = bsq._measure_pytest_output(REAL_GREEN_WITH_WARNINGS)
    assert m["passed"] == 1336
    assert m["executed"] == 1336
    assert m["counts"]["warning"] == 1326
    assert m["interrupted"] is None


def test_parses_collection_abort():
    m = bsq._measure_pytest_output(REAL_COLLECTION_ABORT)
    assert m["interrupted"] == 4
    assert m["executed"] == 0
    assert m["counts"] == {"skipped": 2, "warning": 1, "error": 4}


def test_last_summary_line_wins():
    """A captured sub-run can print its own summary; the session's is last."""
    m = bsq._measure_pytest_output(REAL_GENUINE_RED + REAL_GREEN_WITH_WARNINGS)
    assert m["passed"] == 1336


def test_elapsed_tail_is_required_so_body_lines_are_not_summaries():
    """`3 passed` inside a docstring or a log line is not a result."""
    m = bsq._measure_pytest_output("the fixture asserts 3 passed and moves on\n")
    assert m["summary_line"] is None


# --- The verdict ------------------------------------------------------------

PYTEST_CMD = ["/repo/worker/.venv/bin/python", "-m", "pytest", "-q"]


def _verdict(text, rc=0, cmd=None):
    return bsq._certify_pytest_run(text, rc, cmd or PYTEST_CMD, "caca284deadbeef")


def test_refuses_a_collection_abort():
    lines, rc = _verdict(REAL_COLLECTION_ABORT, rc=2)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    blob = "\n".join(lines)
    assert "REFUSING TO CERTIFY" in blob
    assert "NOT ONE TEST EXECUTED" in blob


def test_refuses_the_wrong_image_run_that_read_as_nearly_clean():
    lines, rc = _verdict(REAL_WORKER_WRONG_IMAGE, rc=2)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    assert "REFUSING TO CERTIFY" in "\n".join(lines)


def test_refuses_a_run_with_no_summary_at_all():
    """The no-measurement path must FAIL. A tool that cannot tell whether it
    measured anything must not return the shape of a success."""
    lines, rc = _verdict("Traceback (most recent call last):\n  boom\n", rc=1)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    assert "no pytest summary" in "\n".join(lines)


def test_refuses_a_collect_only_run():
    lines, rc = _verdict(REAL_COLLECT_ONLY, rc=0)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    assert "COLLECTION-ONLY" in "\n".join(lines)


def test_refuses_an_all_skipped_run_because_a_skip_is_not_coverage():
    lines, rc = _verdict("2 skipped, 1 warning in 3.10s\n", rc=0)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    assert "ZERO tests executed" in "\n".join(lines)


def test_does_NOT_refuse_a_genuine_red():
    """EXECUTED, not PASSED, is the evidence a suite ran. Refusing honest reds
    would teach people to route around the check — and a 24-passed run is a
    measurement whatever its verdict."""
    lines, rc = _verdict(REAL_GENUINE_RED, rc=1)
    assert rc == 1
    blob = "\n".join(lines)
    assert "REFUSING" not in blob
    assert "MEASURED 26 test(s) executed" in blob


def test_reports_the_denominator_on_a_green_run():
    lines, rc = _verdict(REAL_GREEN_WITH_WARNINGS, rc=0)
    assert rc == 0
    assert "MEASURED 1336 test(s) executed" in "\n".join(lines)


def test_non_pytest_command_says_UNMEASURED_rather_than_implying_a_green():
    lines, rc = _verdict("built in 4.10s\n", rc=0, cmd=["npm", "test"])
    assert rc == 0
    blob = "\n".join(lines)
    assert "UNMEASURED" in blob
    assert "not pytest" in blob


# --- Which commands are held to the measurement requirement ------------------

@pytest.mark.parametrize("cmd", [
    ["pytest", "-q"],
    ["/home/almdudleer/bot-squad/dev/worker/.venv/bin/pytest", "tests"],
    ["python", "-m", "pytest", "-q"],
    ["/usr/local/bin/python3.12", "-m", "pytest", "-q", "tests"],
])
def test_recognises_pytest_invocations(cmd):
    assert bsq._looks_like_pytest(cmd) is True


@pytest.mark.parametrize("cmd", [
    ["npm", "test"],
    ["./scripts/cli/git-guard.sh"],
    ["make", "check"],
])
def test_does_not_claim_a_measurement_for_non_pytest(cmd):
    assert bsq._looks_like_pytest(cmd) is False


def test_pytest_detection_reads_the_argv_not_the_output():
    """If "is this pytest?" were inferred from a missing summary, every fault
    this module exists to catch would classify itself out of scope."""
    lines, rc = _verdict("", rc=0)
    assert rc == bsq.VERIFY_UNMEASURED_RC


# --- The SOURCE instrument: is the extract complete? ------------------------
#
# Built on a REAL `git archive` of this repo rather than a fabricated tree,
# because the thing under test is agreement between git's own index and what
# landed on disk. A stub of that comparison would only agree with itself.

@pytest.fixture(scope="module", autouse=True)
def _git_trusts_this_checkout():
    """Under the docker api image the repo is a bind mount owned by another
    uid, and git refuses it with "dubious ownership" (exit 128).

    Scoped to this module's environment on purpose: the production call in
    `bsq` runs on the host as the checkout's owner, and relaxing its ownership
    check to make a TEST pass would be weakening the thing under test.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("GIT_CONFIG_COUNT", "1")
    mp.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    mp.setenv("GIT_CONFIG_VALUE_0", "*")
    yield
    mp.undo()


def _repo_root() -> str:
    return subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          cwd=str(_BSQ_PATH.parent), capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def real_extract(tmp_path_factory):
    """A genuine `git archive HEAD | tar -x`, the way verify-isolated makes one."""
    root = _repo_root()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True).stdout.strip()
    dest = tmp_path_factory.mktemp("extract")
    archive = subprocess.run(["git", "archive", "--format=tar", sha],
                             cwd=root, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    return root, sha, dest


def test_complete_extract_reports_nothing_missing(real_extract):
    """GREEN CONTROL. Without it the red below proves only that the function
    can say 'missing', not that it can tell the two cases apart."""
    root, sha, dest = real_extract
    tracked, missing = bsq._extract_missing_paths(root, sha, str(dest))
    assert tracked, "no tracked paths listed — the instrument itself is broken"
    assert missing == []


def test_a_truncated_extract_is_caught(real_extract):
    """NEGATIVE ARM. This is the fault that exits 0 while collecting nothing:
    a partial extract elsewhere on T-0819 manufactured 22 false regressions."""
    root, sha, dest = real_extract
    victim = "api/app/main.py"
    target = dest / victim
    assert target.exists(), f"{victim} should be in the extract"
    body = target.read_bytes()
    target.unlink()
    try:
        tracked, missing = bsq._extract_missing_paths(root, sha, str(dest))
        assert missing == [victim]
    finally:
        target.write_bytes(body)


def test_an_unlistable_ref_is_UNKNOWN_not_complete(real_extract):
    """A tree it could not read must not come back as 'nothing missing' — an
    empty result from a broken instrument is the defect, not the absence of one."""
    _root, _sha, dest = real_extract
    tracked, missing = bsq._extract_missing_paths(
        str(dest), "0000000000000000000000000000000000000000", str(dest))
    assert missing is None
    assert tracked == []
