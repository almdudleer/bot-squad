#!/usr/bin/env python3
"""T-0919 item 2: NEGATIVE CONTROLS for the deploy watchdog guards.

Run it:  python3 worker/tests/t0919_watchdog_negative_controls.py
Not a pytest module on purpose — it RUNS pytest, once per mutation, and pytest
cannot nest. Named without the test_ prefix so collection skips it.

"A green nobody has watched fail is not a control." For each guard this ticket
adds, mutate a SCRATCH COPY of deploy.py by exactly ONE property, run the test
that is supposed to be protecting it, and require that test to go RED.

The harness has a built-in positive control in two layers:
  1. an unmutated scratch copy must run the whole set GREEN first — if that
     fails, the harness is broken and no red below means anything;
  2. if the harness were accidentally importing the REAL tree instead of the
     scratch copy, every mutation would come back green, because the real tree
     is not mutated. So a RED is itself proof the scratch copy was executed.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# worker/ — derived from this file so the harness travels with the repo.
SRC = Path(__file__).resolve().parents[1]
SCRATCH = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/t0919-negctl")
# The api image lacks rsync/tmux/docker and gives 22 phantom failures, so the
# worker venv is the only correct substrate here (T-0824).
PY = SRC / ".venv/bin/python"
if not PY.exists():
    PY = Path(sys.executable)

# (id, human name, target test, (needle, replacement))
MUTATIONS = [
    (
        "M1", "the wall-clock budget KILLS again (the pre-T-0919 defect, restored)",
        "test_wall_clock_budget_no_longer_kills_a_progressing_build",
        ("        if ceiling_s > 0 and (now - start) >= ceiling_s:",
         "        if budget_s > 0 and (now - start) >= budget_s:\n"
         "            killed_reason = \"timeout\"\n"
         "            limit_s = budget_s\n"
         "            limit_name = \"hard timeout\"\n"
         "            rc = RC_TIMEOUT\n"
         "            break\n"
         "        if ceiling_s > 0 and (now - start) >= ceiling_s:"),
    ),
    (
        "M2", "the absolute ceiling is removed (a chatty runaway never dies)",
        "test_absolute_ceiling_kills_a_chatty_runaway",
        ("        if ceiling_s > 0 and (now - start) >= ceiling_s:",
         "        if False and ceiling_s > 0 and (now - start) >= ceiling_s:"),
    ),
    (
        "M3", "the TEMPO term is removed — the budget is the bare floor again",
        "test_silence_budget_scales_with_this_run_s_own_step_tempo",
        ("    budget = max(float(floor_s), tempo_multiplier * max(0.0, longest_done_s))",
         "    budget = float(floor_s)"),
    ),
    (
        "M4", "the FLOOR is removed — a fast run gets a budget below 600s",
        "test_silence_budget_still_kills_a_wedge_at_the_floor_on_a_fast_run",
        ("    budget = max(float(floor_s), tempo_multiplier * max(0.0, longest_done_s))",
         "    budget = tempo_multiplier * max(0.0, longest_done_s)"),
    ),
    (
        "M5", "the install-tree drift detector is disabled (T-0919 item 3)",
        "test_kill_after_the_tree_sync_flags_install_sha_drift",
        ("        killed_reason and install_tree_sha and running_sha",
         "        False and killed_reason and install_tree_sha and running_sha"),
    ),
    (
        "M6", "silence_s stops being measured — reports a silent 0.0",
        "test_silence_budget_still_kills_a_wedge_at_the_floor_on_a_fast_run",
        ("            silence_s=now - last_progress,", "            silence_s=0.0,"),
    ),
    (
        "M7", "the incremental parser stops holding back a split line",
        "test_run_progress_holds_back_a_split_line",
        ("        self._tail = lines.pop()",
         "        lines.pop()\n        self._tail = \"\""),
    ),
    (
        "M8", "the step-duration plausibility bound is removed — one forged "
              "`DONE 7200.0s` buys a 4-hour silence allowance",
        "test_an_absurd_step_duration_cannot_inflate_the_silence_budget",
        ("            if secs > elapsed_s + TEMPO_DURATION_TOLERANCE_S:",
         "            if False and secs > elapsed_s + TEMPO_DURATION_TOLERANCE_S:"),
    ),
    (
        "M9", "the tolerance is widened until the bound stops rejecting anything",
        "test_the_plausibility_tolerance_is_a_named_constant",
        ("TEMPO_DURATION_TOLERANCE_S = 60.0", "TEMPO_DURATION_TOLERANCE_S = 99999.0"),
    ),
]


def fresh_scratch() -> Path:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    shutil.copytree(SRC / "bot_squad_worker", SCRATCH / "bot_squad_worker",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(SRC / "tests", SCRATCH / "tests",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for extra in ("pyproject.toml", "setup.cfg", "pytest.ini", "conftest.py"):
        p = SRC / extra
        if p.exists():
            shutil.copy2(p, SCRATCH / extra)
    return SCRATCH


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


def run_test(test: str, timeout: int = 120) -> tuple[str, str]:
    """-> (verdict, detail). verdict in {GREEN, RED, RED(hang)}"""
    try:
        r = _run_child_hard_kill(
            [str(PY), "-m", "pytest", f"tests/test_deploy.py::{test}",
             "-q", "-p", "no:cacheprovider", "--no-header", "-x"],
            cwd=SCRATCH, timeout=timeout,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ".", "HOME": "/tmp"},
        )
    except subprocess.TimeoutExpired:
        return "RED(hang)", f"no verdict within {timeout}s — the guard is gone"
    tail = [l for l in r.stdout.splitlines() if l.strip()][-1:] or [""]
    return ("GREEN" if r.returncode == 0 else "RED"), tail[0][:120]


def main() -> int:
    print("=" * 78)
    print("T-0919 NEGATIVE CONTROLS — one mutated property each, on a scratch copy")
    print("=" * 78)
    fresh_scratch()
    deploy_py = SCRATCH / "bot_squad_worker" / "deploy.py"
    pristine = deploy_py.read_text()

    targets = sorted({m[2] for m in MUTATIONS})
    print("\n[GREEN CONTROL] unmutated scratch copy — every target test must PASS")
    ok = True
    for t in targets:
        v, d = run_test(t)
        print(f"  {v:9s}  {t}")
        if v != "GREEN":
            ok = False
            print(f"            {d}")
    if not ok:
        print("\nHARNESS BROKEN: the green control did not pass. Every red below "
              "would be meaningless. Stopping.")
        return 2

    print("\n[MUTATIONS] each must turn its target test RED")
    results = []
    for mid, name, test, (needle, repl) in MUTATIONS:
        if needle not in pristine:
            print(f"  {mid}: NEEDLE NOT FOUND — mutation could not be applied. "
                  f"This is a harness failure, not a pass.")
            results.append((mid, name, test, "NOT-APPLIED", ""))
            continue
        assert pristine.count(needle) == 1, f"{mid}: needle is not unique"
        deploy_py.write_text(pristine.replace(needle, repl, 1))
        v, d = run_test(test, timeout=90)
        deploy_py.write_text(pristine)
        results.append((mid, name, test, v, d))
        print(f"  {mid}  {v:9s}  {name}")
        print(f"        red test: {test}")

    print("\n" + "=" * 78)
    bad = [r for r in results if not r[3].startswith("RED")]
    if bad:
        print(f"FAIL — {len(bad)} guard(s) did NOT go red when removed:")
        for r in bad:
            print(f"  {r[0]} {r[2]} -> {r[3]}")
        return 1
    print(f"PASS — all {len(results)} guards go red when removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
