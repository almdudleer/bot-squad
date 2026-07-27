#!/usr/bin/env python3
"""Run the duplicated-module mirror invariant WITHOUT a venv, so CI can gate it.

Why this exists (T-0738)
------------------------
The mirror invariant lives in `worker/tests/test_module_mirrors.py`. It works:
it caught T-0733 adding `is_legacy_body` to only the api copy of
`task_body.py`. But it caught it LATE. T-0733 pushed the divergence in 5fe547f,
CI went green (the lint workflow runs four api lint tests and no worker tests
at all), and the split lived until a session happened to run the full worker
suite by hand, hours later, on an unrelated ticket. A correct guard that
nothing runs is not a guard — the window between "a fix lands in one copy" and
"someone notices" was bounded only by luck.

This script closes that window. It is the same shape as
`scripts/lint/cross_module_attr_smoke.py` (T-0186): pure stdlib, no deps, no
data tree, no venv, sub-second — so it runs in the `lint` workflow AND in
`.githooks/pre-push`, both of which have nothing but a system `python3`.

What it does NOT do
-------------------
It does not reimplement the comparison, and it does not keep its own list of
pairs. Both live exactly once, in the test module: `MIRRORS` declares the pairs
and `mirror_failure()` compares one. This script imports that module by file
path and calls it. A second byte comparison — or a second copy of the pair list
— living here could drift from the test's, which is the very failure mode (two
copies of one rule) the invariant exists to prevent. T-0743 collapsed five such
copies into one; this script must not become the sixth.

Usage:
    scripts/lint/module_mirrors.py [--root REPO_ROOT] [--list]

Exits 0 when every declared pair matches (pairs with a copy absent — e.g. a
standalone worker checkout with no api tree — are skipped), 1 on divergence.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_TEST_REL = Path("worker") / "tests" / "test_module_mirrors.py"


def load_invariant(root: Path) -> ModuleType:
    """Import the mirror test module by path — it is the SSOT of the check.

    Imported as a plain file, not via pytest: the module is deliberately
    stdlib-only at import time (`import pytest` is inside the test function),
    so this works on a bare interpreter.
    """
    path = root / _TEST_REL
    if not path.is_file():
        raise FileNotFoundError(f"mirror test not found at {path}")
    spec = importlib.util.spec_from_file_location("_module_mirrors_invariant", path)
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
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the declared pairs this gate checks, then exit 0",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    invariant = load_invariant(root)

    if args.list:
        for mirror in invariant.MIRRORS:
            present = invariant.missing_side(mirror, root) is None
            state = "checked" if present else "SKIPPED (a copy is absent)"
            print(f"{mirror.name:22s} {state}\n  {mirror.left}\n  {mirror.right}")
        return 0

    checked = invariant.checked_mirrors(root)
    if not checked:
        # Standalone worker checkout — nothing to mirror against. Same skip the
        # test takes; not a failure.
        return 0

    failures = invariant.mirror_failures(root)
    if not failures:
        return 0

    print(
        f"module mirror check FAILED — {len(failures)} of {len(checked)} "
        "duplicated-by-design module pair(s) are no longer identical "
        "(registry: worker/tests/test_module_mirrors.py, gated by T-0738/T-0743).",
        file=sys.stderr,
    )
    for failure in failures:
        for line in failure.splitlines():
            print(f"  {line}", file=sys.stderr)
    print(
        "  Fix: copy the change into the other file, then re-run "
        "worker/tests/test_module_mirrors.py.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
