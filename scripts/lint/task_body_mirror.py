#!/usr/bin/env python3
"""T-0738: run the task_body mirror invariant WITHOUT a venv, so CI can gate it.

Why this exists
---------------
`worker/tests/test_task_body_mirror.py` (T-0729) pins the two `task_body.py`
copies byte-identical. It works: it caught T-0733 adding `is_legacy_body` to
only the api copy. But it caught it LATE. T-0733 pushed the divergence in
5fe547f, CI went green (the lint workflow runs four api lint tests and no
worker tests at all), and the split lived until a session happened to run the
full worker suite by hand, hours later, on an unrelated ticket. A correct guard
that nothing runs is not a guard — the window between "a fix lands in one copy"
and "someone notices" was bounded only by luck.

This script closes that window. It is the same shape as
`scripts/lint/cross_module_attr_smoke.py` (T-0186): pure stdlib, no deps, no
data tree, no venv, sub-second — so it runs in the `lint` workflow AND in
`.githooks/pre-push`, both of which have nothing but a system `python3`.

What it does NOT do
-------------------
It does not reimplement the comparison. The invariant has exactly one
definition, and it stays in the test where T-0729 put it: this script imports
`mirror_failure()` from `worker/tests/test_task_body_mirror.py` by file path
and calls it. A second byte comparison living here could drift from the test's
— which is the very failure mode (two copies of one rule) the invariant exists
to prevent.

Usage:
    scripts/lint/task_body_mirror.py [--root REPO_ROOT]

Exits 0 when the copies match (or the api copy is absent — a standalone worker
checkout has nothing to mirror), 1 when they have diverged.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_TEST_REL = Path("worker") / "tests" / "test_task_body_mirror.py"


def load_invariant(root: Path) -> ModuleType:
    """Import the mirror test module by path — it is the SSOT of the check.

    Imported as a plain file, not via pytest: the module is deliberately
    stdlib-only at import time (`import pytest` is inside the test function),
    so this works on a bare interpreter.
    """
    path = root / _TEST_REL
    if not path.is_file():
        raise FileNotFoundError(f"mirror test not found at {path}")
    spec = importlib.util.spec_from_file_location("_task_body_mirror_invariant", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repo root containing worker/ and api/ (default: inferred from this script)",
    )
    args = parser.parse_args(argv)

    invariant = load_invariant(args.root.resolve())

    if not invariant.api_copy_present():
        # Standalone worker checkout — nothing to mirror against. Same skip the
        # test takes; not a failure.
        return 0

    failure = invariant.mirror_failure()
    if failure is None:
        return 0

    print(
        "task_body mirror check FAILED — the worker and api copies of "
        "task_body.py are no longer byte-identical below their module "
        "docstrings (T-0729 invariant, gated by T-0738).",
        file=sys.stderr,
    )
    for line in failure.splitlines():
        print(f"  {line}", file=sys.stderr)
    print(
        "  Fix: copy the change into the other file, then re-run "
        "worker/tests/test_task_body_mirror.py.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
