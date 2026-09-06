"""T-0774: the worker suite's green must not depend on alphabetical file order.

THE DEFECT THIS PINS, measured on 99cb674 before the fix existed. The same
commit, the same code, two answers:

    ./.venv/bin/pytest -q tests/                        -> 2793 passed, 2 skipped
    ./.venv/bin/pytest -q $(ls tests/test_*.py | tac)   -> 5 failed, 2788 passed

All five failures in ``tests/test_outbound_liveness.py``, none of them its
fault. ``outbound_log.DROPS`` is process-global ("Reset only by process
restart") and ``outbound_liveness.check`` reports DECAYED on a non-zero count,
so a leak in one file becomes a WRONG VERDICT in another — not an error, which
is the hardest kind to attribute. Alphabetically the victim ran first, so the
leak landed harmlessly behind it. That is the whole reason the suite was green.

THE POLLUTER NEVER MENTIONS THE THING IT BREAKS. ``tests/test_tg.py`` bumps
``DROPS["record"]`` seventeen times purely as a side effect of exercising the
transport, through the ``except`` arms of production ``outbound_log.record`` /
``record_response``. The string ``DROPS`` appears zero times in that file. This
is why the previous defence — a per-file autouse fixture in each module that
READS the counter, three of them by the time this was written — could never
work: the source is any test anywhere, and it has no reason to know.

The guard is now ``_isolate_outbound_drops`` in ``tests/conftest.py``, which
zeroes the counter around every worker test. These tests exist so that removing
it fails loudly and immediately, instead of silently re-arming a landmine that
only goes off when someone renames a file, parallelises CI, or writes a new
liveness consumer that happens to sort late.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from bot_squad_worker import outbound_log as OB

_WORKER_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _WORKER_ROOT / "tests"

#: What ``tests/test_tg.py`` leaks today. Reproduced here as a plain number so
#: this file does not depend on that one continuing to leak exactly 17.
_LEAK = 17


def _subprocess_env() -> dict[str, str]:
    """Environment for a nested pytest: this tree importable, nothing else new."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_WORKER_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def test_a_leak_does_not_escape_the_test_that_made_it() -> None:
    """Leak on purpose, exactly the way test_tg.py leaks by accident.

    Paired with the test BELOW, which asserts the counter is pristine again.
    Within a file pytest preserves definition order, so the pair holds under
    reverse file order too — the property under test is not itself
    order-dependent.
    """
    OB.DROPS["record"] += _LEAK
    OB.DROPS["drain"] += 1
    OB.DROPS["mirror"] += 1

    assert OB.DROPS["record"] == _LEAK


def test_the_next_test_sees_a_pristine_counter() -> None:
    """The assertion the whole ticket comes down to.

    Runs immediately after a test that leaked 19 counts. If conftest's
    ``_isolate_outbound_drops`` is deleted this goes red here, in the file that
    explains why, rather than as a bogus DECAYED verdict in an unrelated module
    several thousand tests later.
    """
    assert dict(OB.DROPS) == {"record": 0, "drain": 0, "mirror": 0}


def _run_child_hard_kill(argv: list, *, cwd, env=None,
                          timeout: float) -> subprocess.CompletedProcess:
    """Run a child pytest with a HARD kill on timeout (T-1024; the T-0212
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


def test_the_polluter_running_first_no_longer_breaks_the_reader() -> None:
    """THE regression test: the ticket's reproduction, in two files not a hundred.

    ``test_tg.py`` (the polluter) then ``test_outbound_liveness.py`` (the
    reader) — the inversion of alphabetical order that the full reverse-order
    run produces, at ~1.5s instead of ~110s. RED against the commit this was
    written on: 5 failed, 82 passed.

    A subprocess rather than an in-process ``pytest.main``: the point is a
    clean process whose ``DROPS`` starts at zero and whose file ORDER is the
    variable, and a nested in-process run would inherit this one's already-
    isolated module state and prove nothing.
    """
    polluter = _TESTS / "test_tg.py"
    reader = _TESTS / "test_outbound_liveness.py"
    # Named explicitly rather than left to pytest's "file or directory not
    # found": a rename must say WHICH pair this test was about, since finding a
    # new polluter is not something the error text below could tell you.
    assert polluter.exists() and reader.exists(), (
        "T-0774's reproduction pair has been renamed; re-point this test at the "
        "file that leaks into outbound_log.DROPS and the one that reads it")

    proc = _run_child_hard_kill(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(polluter), str(reader)],
        cwd=_WORKER_ROOT, env=_subprocess_env(), timeout=300,
    )

    assert proc.returncode == 0, (
        "the polluter running before the reader turned the suite red again — "
        "conftest's _isolate_outbound_drops is gone or no longer covers this "
        f"path.\n\nstdout:\n{proc.stdout[-4000:]}\n\nstderr:\n{proc.stderr[-2000:]}")


def test_that_subprocess_check_can_actually_go_red(tmp_path: Path) -> None:
    """Positive control for the test above (T-0740).

    ``returncode == 0`` from a pytest that collected nothing, or that failed to
    import, is indistinguishable from a real pass — and most convincingly so
    when something about the invocation has broken. Drive the SAME command at a
    file that must fail, and require it to be seen failing.
    """
    failing = tmp_path / "test_t0774_control.py"
    failing.write_text("def test_must_fail():\n    assert False\n", encoding="utf-8")

    proc = _run_child_hard_kill(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(failing)],
        cwd=_WORKER_ROOT, env=_subprocess_env(), timeout=300,
    )

    assert proc.returncode != 0
    assert "1 failed" in proc.stdout
