#!/usr/bin/env python3
"""T-0789: no test or script may reference an UNDEFINED name.

Why this exists
---------------
An undefined name in a *test* is not a loud error — it is a test that has
stopped testing, and the two ways it hides are both silent:

- **Behind a skip.** ``test_sessions.py`` carried three lines of copy-paste
  debris (``bind_task(cfg, …)``) after the real assertion, in a test whose
  ``pytest.importorskip`` lifted NOWHERE — not the worker venv run, not the
  lint job, not the (inert, worker[dev]-only) nightly, not a verify-isolated
  extract. It read as "+1 skipped" for as long as it existed.
- **Swallowed as the failure under test.** ``test_actions.py`` referenced a
  class defined *locally inside a different test*, so its monkeypatch lambda
  raised ``NameError`` — which ``_send_stakeholder_dm`` caught as the TG
  failure the test was trying to simulate, and the assertion passed. The test
  was GREEN because of a bug in itself.

Neither is reachable by running the suite: one never runs, the other passes.
The check that finds both is a static undefined-name scan, and it costs ~1s.

Why undefined names ONLY
------------------------
Measured at 28abd2a over the three scanned trees: pyflakes reports 75 findings,
71 of which are unused imports / unused locals / f-strings without placeholders
— pre-existing, harmless, and far too many to clear as a side effect of this
gate. The other 4 were exactly the two defects above. So this filters to the
undefined-name family and nothing else: 100% signal, zero backlog, no reason
for anyone to add a suppression.

``pyflakes`` is a hard requirement — a missing checker exits 2 rather than
passing, because "the instrument was absent" and "nothing was found" must not
look alike (T-0759/T-0761).

Usage
-----
    python3 scripts/lint/undefined_names.py [ROOT ...]

Defaults to ``worker/tests``, ``api/tests`` and ``scripts`` relative to the
repo root. Exit 0 = clean, 1 = findings, 2 = cannot check.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

DEFAULT_ROOTS = ("worker/tests", "api/tests", "scripts")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _checker_classes():
    """Return ``(Checker, undefined_message_types)`` or exit 2 loudly."""
    try:
        from pyflakes import messages as pm
        from pyflakes.checker import Checker
    except ImportError as exc:  # pragma: no cover - exercised by the CI job
        print(f"undefined_names: pyflakes is not installed ({exc}).", file=sys.stderr)
        print("undefined_names: refusing to report a clean tree it did not read — "
              "`pip install pyflakes` (it is in api's [dev] extra).", file=sys.stderr)
        raise SystemExit(2)
    kinds = tuple(
        getattr(pm, name) for name in
        ("UndefinedName", "UndefinedLocal", "UndefinedExport")
        if hasattr(pm, name)
    )
    if not kinds:  # pragma: no cover - pyflakes API moved under us
        print("undefined_names: pyflakes exposes none of UndefinedName/"
              "UndefinedLocal/UndefinedExport — cannot check.", file=sys.stderr)
        raise SystemExit(2)
    return Checker, kinds


def iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(
                p for p in sorted(root.rglob("*.py"))
                if "__pycache__" not in p.parts and ".venv" not in p.parts
            )
    return files


def check(roots: list[Path]) -> list[str]:
    """Return one string per finding (empty list == clean)."""
    Checker, undefined_kinds = _checker_classes()
    findings: list[str] = []
    for path in iter_py_files(roots):
        rel = path.relative_to(REPO_ROOT) if path.is_absolute() and REPO_ROOT in path.parents else path
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            findings.append(f"{rel}:{exc.lineno}: does not parse: {exc.msg}")
            continue
        for msg in Checker(tree, filename=str(path)).messages:
            if isinstance(msg, undefined_kinds):
                findings.append(f"{rel}:{msg.lineno}: {msg.message % msg.message_args}")
    return findings


def main(argv: list[str]) -> int:
    args = argv[1:]
    roots = [Path(a) for a in args] if args else [REPO_ROOT / r for r in DEFAULT_ROOTS]
    missing = [r for r in roots if not r.exists()]
    if missing:
        for r in missing:
            print(f"undefined_names: no such path: {r}", file=sys.stderr)
        return 2
    findings = check(roots)
    scanned = len(iter_py_files(roots))
    if findings:
        print(f"undefined_names: {len(findings)} undefined name(s) "
              f"across {scanned} file(s):", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print("\nAn undefined name in a test does not fail loudly — it either sits "
              "behind a skip\nor gets swallowed as the very error the test is "
              "simulating (T-0789). Fix the\nreference; do not suppress it.",
              file=sys.stderr)
        return 1
    print(f"undefined_names: clean — {scanned} file(s) scanned, "
          f"no undefined names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
