#!/usr/bin/env python3
"""T-1017: duplicate top-level def/class census for scripts/cli/bsq.

``bsq`` is a live file shared by every session in a bot-squad clone: several
lanes commonly edit it in one evening, and a torn or duplicated top-level
``def``/``class`` there breaks the CLI for every session at once — including
whichever session is trying to fix it, since ``argparse`` subcommand wiring
and any name collision fail at RUNTIME, not at parse time. Until this ticket
the census that would catch that existed only as an instruction in a
broadcast message (T-1017 verbatim request); this module is what makes it
checkable instead of rememberable.

WHY ``tree.body`` DIRECTLY, NEVER ``ast.walk``
-----------------------------------------------
``ast.walk(tree)`` descends into every nested body, so a ``def`` nested inside
another ``def`` (or a class, an ``if``, a ``try``) comes back indistinguishable
from a real top-level one. T-1017's own Context records the measured
false-positive this produces: a traversal shaped exactly like that reported 2
module-level ``importorskip`` sites in ``worker/tests`` where the true answer
was 1 — the second was nested inside a test function. A duplicate-def census
is the same shape of bug waiting to happen, so this module iterates
``tree.body`` (the module's direct children) and nothing deeper.

WHY A COMMENT OR DOCSTRING QUOTING A DEFECT CANNOT TRIP THIS
--------------------------------------------------------------
Because the census reads the real syntax tree rather than text, a comment or
a docstring that happens to quote ``def foo():`` never becomes a
``FunctionDef`` node — ``ast`` does not care what a string or a ``#`` comment
says. A ``grep``-based census would need to strip comments/strings by hand to
get this property; an ``ast``-based one gets it for free, which is the whole
argument for using ``ast`` here in the first place (T-1017 Context, p651).

WHAT THIS DOES NOT DO
----------------------
It does not add a ``bsq`` verb, and it does not touch ``bsq`` itself — per the
operator's explicit scope for this ticket, a mid-edit break of the very file
this control protects is the risk, not a hypothetical. This module is a
standalone, stdlib-only script; the enforcement point is
``.githooks/lib/commit-policy.sh`` (gate 3), which shells out to it against
the STAGED content of the policed path.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


class Census:
    """Top-level def/class names in one module, each mapped to its line(s)."""

    def __init__(self) -> None:
        self.defs: dict[str, list[int]] = {}
        self.classes: dict[str, list[int]] = {}

    def total_defs(self) -> int:
        return sum(len(v) for v in self.defs.values())

    def total_classes(self) -> int:
        return sum(len(v) for v in self.classes.values())

    def duplicate_defs(self) -> dict[str, list[int]]:
        return {name: lines for name, lines in self.defs.items() if len(lines) > 1}

    def duplicate_classes(self) -> dict[str, list[int]]:
        return {name: lines for name, lines in self.classes.items() if len(lines) > 1}

    def is_clean(self) -> bool:
        return not self.duplicate_defs() and not self.duplicate_classes()


def census_source(source: str) -> Census:
    """Census the MODULE-LEVEL defs/classes of ``source``.

    Walks ``tree.body`` — the module's direct children — and nothing deeper.
    See the module docstring for why ``ast.walk`` is the wrong tool here.
    """
    tree = ast.parse(source)
    c = Census()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            c.defs.setdefault(node.name, []).append(node.lineno)
        elif isinstance(node, ast.ClassDef):
            c.classes.setdefault(node.name, []).append(node.lineno)
    return c


def format_summary(name: str, c: Census) -> str:
    return (
        f"{name}: {c.total_defs()} top-level def(s), {len(c.defs)} unique; "
        f"{c.total_classes()} top-level class(es), {len(c.classes)} unique"
    )


def format_duplicates(c: Census) -> list[str]:
    """One line per duplicated identifier, naming it and every line it is on.

    A guard that reports only a count "leaks" — it can be nonzero without
    telling the reader which identifier to fix, which is the exact failure the
    T-1017 broadcast singled out as usually skipped: sees a one, but does not
    name it. Every line here names the identifier.
    """
    lines: list[str] = []
    for kind, dupes in (("def", c.duplicate_defs()), ("class", c.duplicate_classes())):
        for ident in sorted(dupes):
            where = ", ".join(f"line {n}" for n in dupes[ident])
            lines.append(f"DUPLICATE {kind} {ident!r}: {where}")
    return lines


def check(name: str, source: str, *, verbose: bool = False) -> int:
    """Census ``source`` (labelled ``name`` in output) -> exit code.

    0 clean, 1 duplicate top-level def/class found (named on stdout),
    2 not parseable as Python (a census cannot certify uniqueness in a file it
    cannot read as Python, so this is treated as a failure, not a skip).
    """
    try:
        c = census_source(source)
    except SyntaxError as exc:
        print(f"{name}: cannot census — not parseable as Python ({exc})", file=sys.stderr)
        return 2

    dup_lines = format_duplicates(c)
    if dup_lines:
        print(format_summary(name, c))
        for line in dup_lines:
            print(f"  {line}")
        return 1

    if verbose:
        print(format_summary(name, c))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="duplicate_def_census.py",
        description="Refuse a file with a duplicated top-level def/class name.",
    )
    p.add_argument("files", nargs="*", help="path(s) to census")
    p.add_argument("--stdin", action="store_true", help="also census stdin")
    p.add_argument("--name", default="<stdin>", help="label for --stdin input")
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="print the count summary even when the census is clean",
    )
    args = p.parse_args(argv)

    if not args.files and not args.stdin:
        p.error("give FILE(s) and/or --stdin")

    rc = 0
    if args.stdin:
        rc = max(rc, check(args.name, sys.stdin.read(), verbose=args.verbose))
    for f in args.files:
        path = Path(f)
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"{f}: cannot read ({exc})", file=sys.stderr)
            rc = max(rc, 2)
            continue
        rc = max(rc, check(str(path), source, verbose=args.verbose))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
