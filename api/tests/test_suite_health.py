"""T-0819: the api suite asserts its own denominator.

WHY THIS FILE EXISTS. On 2026-07-30 the primary api-test recipe in
AGENT_INSTRUCTIONS mounted `api/` alone. Four test modules reach out to
`scripts/`, which is not on that filesystem, so they failed during COLLECTION
— and **a collection error aborts the whole pytest session**. The documented
result was `2 skipped, 1 warning, 4 errors in 3.10s`: zero passes, zero
failures, NOT ONE TEST EXECUTED. It had been recorded for months as "4
pre-existing failures", a phrase that names a bounded number and invites the
reader to assume the other ~1300 tests passed. They never ran.

The fix that was shipped first was a better document. It did not hold: a dev
carrying that exact lesson in writing reproduced the failure the same day,
because the wrong output looks like a mild right one and nothing forces you to
look past it. So the control has to live where a machine reads it.

WHAT THIS FILE CAN AND CANNOT SEE — read this before treating it as coverage.

  It CANNOT catch the abort above. If collection dies, this module does not run
  either. Nothing inside a suite can report that the suite did not start; that
  is the caller's job, and `bsq verify-isolated` now refuses any stage whose
  pass count is zero (T-0819 D1).

  It CAN catch the quieter successor: a suite that still runs and has silently
  SHRUNK. That is the failure mode with no symptom at all — every number in the
  summary looks healthy and the denominator is simply smaller than you think.
  In particular, if the four `scripts/`-reaching modules are ever "fixed" by
  skipping instead of erroring, the api-only mount stops aborting and starts
  quietly dropping them. That reads as a clean run forever.

A test that passes vacuously is the defect it is meant to prevent, so on a
subset run (`-k`, `-m`, or explicit paths) these assertions SKIP WITH A STATED
REASON rather than pass.
"""
from __future__ import annotations

import pytest

# Measured 2026-07-30 in bot-squad-api:latest under the repo-root mount
# (`-v "$R":/repo -w /repo/api`): **1384 collected**. Earlier measurements on
# the same recipe: 1336 (2026-07-30), 1246 (T-0768, 2026-07-28).
#
# This is a FLOOR, not a pin — the suite grows daily and pinning it exactly
# would red every session that adds a test. It is set well below the measured
# count so ordinary deletions do not trip it, while a mount/collection fault
# that drops whole directories cannot pass. If you legitimately remove enough
# tests to cross it, lower it in the same commit and say what you removed.
COLLECTED_FLOOR = 1300

# The modules that reach outside `api/` for `scripts/`. Their presence in the
# collection is the direct evidence that this run can see the repo root.
SCRIPTS_REACHING_MODULES = (
    "test_backlog_frontmatter_lint.py",
    "test_backlog_ids_lint.py",
    "test_backlog_provenance_lint.py",
    "test_migrate_users_split.py",
)


def _subset_reason(config) -> str | None:
    """Return why this session is a deliberate subset run, or None if it is not.

    The floor is only meaningful over a whole-suite run. Guessing wrong in the
    permissive direction turns this file into a rubber stamp, so the check is
    on the SELECTION MECHANISMS pytest exposes, not on a heuristic about counts.
    """
    if getattr(config.option, "keyword", None):
        return f"-k {config.option.keyword!r} selects a subset"
    if getattr(config.option, "markexpr", None):
        return f"-m {config.option.markexpr!r} selects a subset"
    if getattr(config.option, "lf", False) or getattr(config.option, "failedfirst", False):
        return "--lf/--ff selects a subset"
    # `pytest` / `pytest .` / `pytest tests` are whole-suite; anything naming a
    # file or a node id is not.
    for arg in config.args:
        head = arg.split("::", 1)[0]
        if head.endswith(".py"):
            return f"explicit path {arg!r} selects a subset"
    return None


def test_suite_collected_enough_tests(request):
    """EXECUTION/COVERAGE floor: the session collected a plausible denominator.

    'No failures' is not a result. The pass count is, and this is its
    collection-time twin.
    """
    reason = _subset_reason(request.config)
    if reason is not None:
        pytest.skip(f"suite-health floor not asserted — {reason}")
    collected = len(request.session.items)
    assert collected >= COLLECTED_FLOOR, (
        f"api suite collected only {collected} tests, below the floor of "
        f"{COLLECTED_FLOOR} (measured 1384 on 2026-07-30). Either the suite "
        f"shrank and this floor needs lowering in the same commit as the "
        f"removal, or this run cannot see part of the tree — check the mount: "
        f"the repo ROOT must be mounted, not api/."
    )


def test_scripts_reaching_modules_were_collected(request):
    """COVERAGE: the four modules that need the repo root are in the run.

    Today an api-only mount makes these ERROR, which aborts everything and is
    at least loud. If that is ever softened to a skip, this is the only thing
    standing between a hidden coverage hole and a permanently clean report.
    """
    reason = _subset_reason(request.config)
    if reason is not None:
        pytest.skip(f"collection membership not asserted — {reason}")
    collected_files = {
        item.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
        for item in request.session.items
    }
    missing = [m for m in SCRIPTS_REACHING_MODULES if m not in collected_files]
    assert not missing, (
        f"these modules reach out to scripts/ and were NOT collected: "
        f"{missing}. They are the tests that prove this run can see the repo "
        f"root. If they were made to skip rather than error when scripts/ is "
        f"absent, an api-only mount now runs and silently omits them — which "
        f"is the T-0819 failure mode with its one loud symptom removed."
    )
