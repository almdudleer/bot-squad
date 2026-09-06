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
import os
import re
import subprocess
import sys
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


# --- T-0983: the measurement arm and the provenance gate, in ONE run --------
#
# THE TRAP THIS PINS is not a defect in either tool — it is what they did to
# each other. `verify-isolated` refuses to report a pass count for a command it
# cannot parse as pytest; the provenance gate requires an absolute extract root
# and deliberately refuses to default one (`realpath("")` is the CWD and is
# truthy, so a defaulted target is not a gate). Only the run knows where the
# extract landed, so arming the gate meant
#     verify-isolated ... -- bash -c 'GATE_EXTRACT_ROOT="$PWD" ... pytest ...'
# and `bash -c` is not a pytest argv, so the measurement arm switched itself
# off. Both refusals are correct. Together they forced every lane to give up
# one of them, and a report missing either arm reads exactly like a complete
# one.
#
# The stub gate below is NOT a copy of the real plugin and does not try to be.
# What is under test is the HANDOFF — that a child plugin can read the extract
# root with no wrapper, and that verify-isolated still measures the same run —
# so the stub implements only the two states that handoff has: root visible and
# correct, and a plugin that refuses via pytest.exit().

_STUB_GATE = '''\
import os
import pytest

def pytest_sessionfinish(session, exitstatus):
    root = os.environ.get("GATE_EXTRACT_ROOT")
    print(f"\\nSTUB_GATE root={root!r} cwd={os.getcwd()!r}")
    if not root:
        pytest.exit("PROVENANCE=REFUSED GATE_EXTRACT_ROOT is unset", returncode=91)
    if os.path.realpath(root) != os.path.realpath(os.getcwd()):
        pytest.exit(f"PROVENANCE=FAILED root {root} is not the run's cwd",
                    returncode=91)
    if os.environ.get("STUB_GATE_REFUSE"):
        pytest.exit("PROVENANCE=FAILED 25 module(s) resolved outside the extract",
                    returncode=91)
    print("STUB_GATE PROVENANCE=OK")
'''

_TRIVIAL_TEST = "def test_one(): assert True\ndef test_two(): assert True\n"


@pytest.fixture(scope="module")
def gate_sandbox(tmp_path_factory):
    """A plugin dir + a two-test file, both OUTSIDE the extract on purpose.

    The extract holds only what is committed, so anything the child needs that
    is not in the ref has to arrive the way a real gate does — on PYTHONPATH.
    """
    d = tmp_path_factory.mktemp("t0983")
    (d / "stub_gate.py").write_text(_STUB_GATE)
    (d / "test_trivial.py").write_text(_TRIVIAL_TEST)
    return d


def _run_verify(gate_sandbox, extra_env=None, cmd=()):
    """Invoke the REAL `bsq verify-isolated` against HEAD. Returns (rc, output)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(gate_sandbox)
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(_BSQ_PATH), "verify-isolated", "--"] + list(cmd),
        cwd=_repo_root(), env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def test_the_extract_root_reaches_the_child_with_no_wrapper(gate_sandbox):
    """THE FIX. `GATE_EXTRACT_ROOT` is exported by the tool that made the
    extract, so no `bash -c` stands between verify-isolated and pytest."""
    rc, out = _run_verify(gate_sandbox, cmd=[
        sys.executable, "-c",
        "import os;print('CHILD_ROOT=' + os.environ.get('GATE_EXTRACT_ROOT','<unset>'));"
        "print('CHILD_CWD=' + os.getcwd())"])
    # Anchored: verify-isolated ECHOES the command it is about to run, so an
    # unanchored search matches the `-c` source text before the child's output.
    root = re.search(r"^CHILD_ROOT=(\S+)$", out, re.M)
    cwd = re.search(r"^CHILD_CWD=(\S+)$", out, re.M)
    assert root and cwd, out
    assert root.group(1) != "<unset>", out
    assert os.path.isabs(root.group(1)), out
    # It must be THIS run's extract, not merely some absolute path.
    assert os.path.realpath(root.group(1)) == os.path.realpath(cwd.group(1)), out
    # ...and the tool must still refuse to call a non-pytest command measured.
    assert "UNMEASURED" in out, out


def test_all_three_arms_fire_in_one_run(gate_sandbox):
    """SOURCE + EXECUTION + PROVENANCE from a single process, no wrapper.

    Composing three separate runs of the same sha is an ARGUMENT; this is the
    measurement it was standing in for."""
    rc, out = _run_verify(gate_sandbox, cmd=[
        sys.executable, "-m", "pytest", "-q", "-p", "stub_gate",
        str(gate_sandbox / "test_trivial.py")])
    assert "tracked path(s) present" in out, out          # SOURCE
    assert "STUB_GATE PROVENANCE=OK" in out, out          # PROVENANCE
    assert "MEASURED 2 test(s) executed" in out, out      # EXECUTION
    assert rc == 0, out


def test_a_refusing_gate_is_reported_as_RAN_AND_REFUSED_not_as_a_bad_command(
        gate_sandbox):
    """NEGATIVE ARM, and the reason this ticket is not just the export.

    `pytest.exit()` ends the session before the summary prints, so a gate that
    refuses leaves the same summary-shaped hole as a command that never ran.
    Told "no pytest summary line was found", a reader goes after their own
    invocation while the gate's verdict sits four lines above, already correct.
    That is the did-not-run / ran-and-refused collapse arriving through
    COMPOSITION rather than through a wrong number.
    """
    rc, out = _run_verify(gate_sandbox, extra_env={"STUB_GATE_REFUSE": "1"},
                          cmd=[sys.executable, "-m", "pytest", "-q", "-p",
                               "stub_gate", str(gate_sandbox / "test_trivial.py")])
    assert rc == bsq.VERIFY_UNMEASURED_RC, out            # still REFUSES
    assert "ENDED EARLY by pytest.exit()" in out, out
    assert "PROVENANCE=FAILED 25 module(s)" in out, out   # the verdict is quoted
    assert "The child exited 91" in out, out              # the gate's rc survives
    assert "no pytest summary line was found" not in out, out


def test_an_inherited_extract_root_is_overridden_and_says_so(gate_sandbox):
    """A stale root from the ambient environment would let the gate compare
    against some OTHER tree — a previous extract, or the shared clone — and a
    contaminated run would pass. This run's own extract wins, out loud."""
    rc, out = _run_verify(gate_sandbox,
                          extra_env={"GATE_EXTRACT_ROOT": "/tmp/some-stale-extract"},
                          cmd=[sys.executable, "-m", "pytest", "-q", "-p",
                               "stub_gate", str(gate_sandbox / "test_trivial.py")])
    assert "ignoring inherited GATE_EXTRACT_ROOT=/tmp/some-stale-extract" in out, out
    assert "STUB_GATE PROVENANCE=OK" in out, out
    assert rc == 0, out


# The real contaminated output, transcribed from the run recorded on T-0983
# (2026-09-06, by this session: `--import-mode=importlib` with the live clone
# ahead of the extract on PYTHONPATH). Not composed to fit the parser.
REAL_GATE_REFUSAL = (
    "...                                                                      "
    "[100%]Exit: PROVENANCE=FAILED 25 bot_squad_worker module(s) resolved "
    "outside the extract, first=/home/almdudleer/bot-squad-mgmt/worker/"
    "bot_squad_worker/__init__.py\n"
)


def test_the_ended_early_verdict_is_read_off_a_real_gate_refusal():
    """pytest appends `Exit:` to the PROGRESS LINE with no newline, in both
    `-q` and default modes (measured, pytest 9.0.3). A line-anchored pattern
    would miss every real one."""
    lines, rc = _verdict(REAL_GATE_REFUSAL, rc=91)
    blob = "\n".join(lines)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    assert "ENDED EARLY" in blob
    assert "The child exited 91" in blob
    assert "PROVENANCE=FAILED 25 bot_squad_worker module(s)" in blob


def test_a_summaryless_run_with_no_pytest_exit_keeps_the_original_verdict():
    """GREEN CONTROL for the branch above: without an `Exit:` line the old
    diagnosis must stand, or the new one has simply swallowed the old fault."""
    lines, rc = _verdict("", rc=0)
    blob = "\n".join(lines)
    assert rc == bsq.VERIFY_UNMEASURED_RC
    assert "no pytest summary line was found" in blob
    assert "ENDED EARLY" not in blob
