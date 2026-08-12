#!/usr/bin/env python3
"""T-0121: lint backlog task md frontmatter against the canonical status schema.

Walks ``<data_dir>/<slug>/backlog/*.md`` and refuses any ``status:`` value
outside ``{planned, open, in_progress, totest, reopened, closed}`` —
mirrors ``api/app/routes_backlog.py::_VALID_STATUSES`` (the SSOT enforced
on PATCH). Without this, direct file edits can persist legacy values like
``done`` for days (the 2026-05-26 migration is the cautionary tale).

Usage:
    scripts/lint/backlog_frontmatter.py [DATA_DIR ...]

If no DATA_DIR is given, defaults to ``$BOT_SQUAD_DATA_DIR`` or
``/home/www/bot-squad/data``. Exits 0 if every parseable md has a valid
status (or no status frontmatter at all); exits 1 if any offender is
found, printing one ``path:status`` line per offender to stderr.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

VALID_STATUSES = frozenset(
    {"planned", "open", "in_progress", "paused", "totest", "reopened", "closed"}
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_STATUS_LINE_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)


def extract_status(text: str) -> str | None:
    """Return the raw status value from a md file's YAML frontmatter, or None."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    block = m.group(1)
    s = _STATUS_LINE_RE.search(block)
    if not s:
        return None
    val = s.group(1).strip()
    # Strip surrounding quotes if present.
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val


def lint_dir(data_dir: Path) -> list[tuple[Path, str]]:
    """Return a list of (path, bad_status) for every offender under data_dir."""
    offenders: list[tuple[Path, str]] = []
    if not data_dir.exists():
        return offenders
    for backlog in sorted(data_dir.glob("*/backlog")):
        if not backlog.is_dir():
            continue
        for md in sorted(backlog.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            status = extract_status(text)
            if status is None:
                continue
            if status not in VALID_STATUSES:
                offenders.append((md, status))
    return offenders


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

    all_offenders: list[tuple[Path, str]] = []
    for root in roots:
        all_offenders.extend(lint_dir(root))

    if all_offenders:
        canon = ", ".join(sorted(VALID_STATUSES))
        print(
            f"backlog frontmatter lint: {len(all_offenders)} offender(s); "
            f"canonical statuses = {{{canon}}}",
            file=sys.stderr,
        )
        for path, status in all_offenders:
            print(f"{path}:status={status!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
