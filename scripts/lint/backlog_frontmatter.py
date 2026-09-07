#!/usr/bin/env python3
"""T-0121/T-0975: lint backlog task md frontmatter.

Walks ``<data_dir>/<slug>/backlog/*.md`` and:

1. Parses each file's frontmatter with a REAL YAML reader (``yaml.safe_load``),
   splitting on the CLOSING ``---`` line found by a line-by-line scan — never
   ``text.split("---")``, which a ticket title containing a dash-run (e.g. a
   title starting with an unquoted ``"`` or holding a bare ``: ``) can shift
   onto the wrong line and make the split read the wrong field entirely
   (T-0975).
2. Refuses (``UNREADABLE`` bucket, exit 1) any file whose frontmatter fence
   is unclosed, or whose block a real YAML reader cannot parse into a
   mapping. T-0975: three files (T-0013, T-0094, T-0171) had exactly this —
   an unquoted title starting with ``"`` or containing ``: `` — and every
   consumer that walked the board silently skipped them with no reported
   omission. This lint exists so a fourth one cannot appear silently: a tool
   that returns a clean number over a partial corpus is the defect, so this
   one never returns a number without also stating how many files it read
   out of how many exist.
3. For every file that DOES parse, checks ``status:`` against the canonical
   set (mirrors ``api/app/routes_backlog.py::_VALID_STATUSES``, the SSOT
   enforced on PATCH) — the original T-0121 check.

A file with no ``---`` frontmatter fence at all (not a ticket — e.g. stray
documentation living in a backlog dir) is tolerated, same as before; a file
that OPENS a fence but can't be read as YAML is not.

Usage:
    scripts/lint/backlog_frontmatter.py [DATA_DIR ...]

If no DATA_DIR is given, defaults to ``$BOT_SQUAD_DATA_DIR`` or
``/home/www/bot-squad/data``. Always prints a one-line summary stating the
denominator (files read out of files found). Exits 0 only if every fenced
file parsed AND every parsed status is canonical; exits 1 if any file is
unreadable or has an invalid status.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

VALID_STATUSES = frozenset(
    {"planned", "open", "in_progress", "to_accept", "paused", "blocked_on_user", "totest", "reopened", "closed"}
)


class Unreadable(Exception):
    """Raised when a file's frontmatter fence is unclosed, or the enclosed
    block is not parseable by a real YAML reader as a mapping."""


def split_frontmatter(text: str) -> str | None:
    """Return the raw YAML block between the opening and closing bare
    ``---`` lines, or ``None`` if there is no such fence at all.

    Scans line-by-line for the CLOSING ``---`` on its own line — never
    ``text.split("---")`` — so a title containing a dash-run can't shift
    the split and make it read the wrong field (T-0975).
    """
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[1:i])
    return None  # opened but never closed


def parse_frontmatter(text: str) -> dict | None:
    """Parse a file's frontmatter with a real YAML reader.

    Returns ``None`` if the file has no ``---`` fence at all (not a ticket).
    Raises :class:`Unreadable` if it has a fence but a real YAML reader can't
    parse the block into a mapping — the case this lint exists to catch.
    """
    block = split_frontmatter(text)
    if block is None:
        return None
    try:
        meta = yaml.safe_load(block)
    except yaml.YAMLError as e:
        raise Unreadable(str(e).splitlines()[0]) from e
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise Unreadable(f"frontmatter is not a mapping (got {type(meta).__name__})")
    return meta


class LintResult:
    """Plain class, not ``@dataclass``: this module is loaded via
    ``importlib.util.spec_from_file_location`` by the test suite, which does
    not register it in ``sys.modules`` — ``dataclasses`` needs that entry to
    resolve postponed (``from __future__ import annotations``) type hints and
    raises ``AttributeError`` without it."""

    def __init__(self) -> None:
        self.total = 0
        self.unreadable: list[tuple[Path, str]] = []
        self.offenders: list[tuple[Path, str]] = []

    @property
    def read_ok(self) -> int:
        return self.total - len(self.unreadable)

    @property
    def ok(self) -> bool:
        return not self.unreadable and not self.offenders


def lint_dir(data_dir: Path) -> LintResult:
    """Lint every ``*.md`` under ``data_dir/*/backlog/``."""
    result = LintResult()
    if not data_dir.exists():
        return result
    for backlog in sorted(data_dir.glob("*/backlog")):
        if not backlog.is_dir():
            continue
        for md in sorted(backlog.glob("*.md")):
            result.total += 1
            try:
                text = md.read_text(encoding="utf-8")
            except OSError as e:
                result.unreadable.append((md, str(e)))
                continue
            try:
                meta = parse_frontmatter(text)
            except Unreadable as e:
                result.unreadable.append((md, str(e)))
                continue
            if meta is None:
                continue  # no frontmatter fence at all — not a ticket file
            status = meta.get("status")
            if status is None:
                continue
            status = str(status).strip()
            if status not in VALID_STATUSES:
                result.offenders.append((md, status))
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "data_dirs",
        nargs="*",
        help="Data dirs to scan. Defaults to $BOT_SQUAD_DATA_DIR or /home/www/bot-squad/data.",
    )
    args = parser.parse_args(argv)

    if args.data_dirs:
        roots = [Path(p) for p in args.data_dirs]
    else:
        default = os.environ.get("BOT_SQUAD_DATA_DIR", "/home/www/bot-squad/data")
        roots = [Path(default)]

    total = LintResult()
    for root in roots:
        r = lint_dir(root)
        total.total += r.total
        total.unreadable.extend(r.unreadable)
        total.offenders.extend(r.offenders)

    # T-0975: ALWAYS state the denominator, pass or fail — a clean number
    # over a partial corpus (no stated total, or a total that omits what
    # couldn't be read) is the defect this lint exists to prevent.
    print(
        f"backlog frontmatter lint: read {total.read_ok} of {total.total} file(s); "
        f"{len(total.unreadable)} unreadable, {len(total.offenders)} invalid status",
        file=sys.stderr,
    )

    if total.total == 0:
        # A "0 of 0" run is not a clean board — it's an empty or
        # wrong-level path, and exiting 0 here would certify NOTHING while
        # looking exactly like a healthy pass to a caller that only reads
        # the exit code. Refuse loudly instead (T-0975, caught by the
        # operator: "read 0 of 0" + rc 0 from a mistyped path).
        print(
            f"backlog frontmatter lint: REFUSING — scanned 0 files under "
            f"{', '.join(str(r) for r in roots)}; wrong path, or a data dir "
            f"with no <slug>/backlog/*.md at all",
            file=sys.stderr,
        )
        return 2

    if total.unreadable:
        print("unreadable (no real YAML reader can parse these):", file=sys.stderr)
        for path, reason in total.unreadable:
            print(f"  {path}: {reason}", file=sys.stderr)

    if total.offenders:
        canon = ", ".join(sorted(VALID_STATUSES))
        print(f"invalid status (canonical = {{{canon}}}):", file=sys.stderr)
        for path, status in total.offenders:
            print(f"  {path}:status={status!r}", file=sys.stderr)

    return 0 if total.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
