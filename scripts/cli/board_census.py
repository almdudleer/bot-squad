#!/usr/bin/env python3
"""Read the board from disk, by status. Never hand-build an accept queue.

T-1026: two grep/hand-built enumerations were wrong the same night, both for
the same reason — they treated the ticket file as flat text instead of
frontmatter. This tool has two properties that a naive read does not:

1. It scans line-by-line to the CLOSING `---` on its own line, rather than
   `text.split("---", 2)` — so a ticket TITLE that happens to contain the
   substring "---" cannot shift the split and corrupt the parse.
2. A file whose frontmatter can't be parsed (missing/malformed) is bucketed
   under `<no status field>` instead of being silently dropped — a missing
   ticket in a board summary reads as "nothing to do", which is the actual
   defect class this guards against.

Rescued verbatim (structure unchanged) from a since-archived session's
scratchpad; see artifacts/orphan-guard/T-1026-board-census/README.md for the
original provenance and the two controls that were run before this landed.

Usage: board_census.py [backlog-dir]   (default: the live backlog)
"""
from __future__ import annotations

import collections
import os
import re
import sys

DEFAULT_BACKLOG = "/home/www/bot-squad/data/bot-squad/backlog"

_STATUS_RE = re.compile(r'^status:\s*"?([a-z_]+)"?\s*$')

ORDER = [
    "in_progress", "to_accept", "totest", "reopened", "open", "planned",
    "blocked_on_user", "closed",
]

NO_STATUS = "<no status field>"


def status_of(path: str) -> str | None:
    """Frontmatter `status:` value, or None if absent/unparseable.

    Scans to the closing `---` line rather than splitting on the literal
    substring, so a title containing "---" cannot break this.
    """
    with open(path, encoding="utf-8") as f:
        if f.readline().rstrip("\n") != "---":
            return None
        for line in f:
            if line.rstrip("\n") == "---":
                return None  # frontmatter closed with no status field
            m = _STATUS_RE.match(line)
            if m:
                return m.group(1)
    return None


def census(backlog_dir: str) -> tuple[dict[str, list[str]], int]:
    """Bucket every `*.md` in backlog_dir by frontmatter status.

    Unparseable files are bucketed under NO_STATUS, never dropped — this is
    the property under test in the T-0038 regression (a body line that
    looks like a status field must not win over the real one, or a missing
    one).
    """
    by: dict[str, list[str]] = collections.defaultdict(list)
    for name in sorted(os.listdir(backlog_dir)):
        if not name.endswith(".md"):
            continue
        tid = "-".join(name.split("-", 2)[:2])
        st = status_of(os.path.join(backlog_dir, name)) or NO_STATUS
        by[st].append(tid)
    seen = sum(len(ids) for ids in by.values())
    return dict(by), seen


def render(by: dict[str, list[str]], seen: int) -> str:
    lines = []
    for st in ORDER + [k for k in sorted(by) if k not in ORDER]:
        if st not in by:
            continue
        ids = sorted(by[st])
        head = ", ".join(ids) if len(ids) <= 14 else ", ".join(ids[:14]) + f", +{len(ids) - 14} more"
        lines.append(f"  {st:<16} {len(ids):>4}   {head}")
    lines.append(f"  {'TOTAL':<16} {seen:>4}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    backlog_dir = argv[1] if len(argv) > 1 else DEFAULT_BACKLOG
    by, seen = census(backlog_dir)
    print(render(by, seen))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
