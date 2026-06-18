#!/usr/bin/env python3
"""T-0207: lint backlog ticket ids for out-of-band / colliding allocation.

The atomic id allocator (T-0174, ``worker/bot_squad_worker/idalloc.py``) is
the ONLY sanctioned way to mint a ``T-NNNN`` ticket: it flock's
``<project>/_counters/task.txt``, computes ``next = max(counter, max_file)+1``,
and writes the file. Every other write path — most notably an operator/dev
session hand-writing ``backlog/T-NNNN-*.md`` with a pre-chosen id — skips that
lock and races concurrent sessions, which is exactly the ID-collision incident
this ticket addresses (and T-0042 / T-0114 / T-0120 before it).

This lint catches a hand-written ticket so the drift is visible instead of
silently re-synced by hand. Three signals, all derivable from the files +
counter alone:

1. **out-of-band mint** — a ticket whose numeric id is GREATER than the
   counter's last-allocated value. The allocator always bumps the counter to
   ``>=`` the id it hands out, so a file above the high-water mark provably
   skipped the allocator. (Fires at commit time, before the allocator's
   self-heal-on-read masks it by jumping the counter past the stray file.)
2. **duplicate id** — two ticket files claiming the same ``T-NNNN`` (the
   collision symptom: two parallel hand-picks land on the same number).
3. **id mismatch** — a file whose ``T-NNNN`` filename id disagrees with its
   ``id:`` frontmatter field (the fingerprint of a manual collision-fix /
   rename that re-synced one but not the other).

Usage:
    scripts/lint/backlog_ids.py [DATA_DIR ...]

Defaults to ``$BOT_SQUAD_DATA_DIR`` or ``/home/www/bot-squad/data``. Exits 0
when every project's ticket ids are clean (or there is nothing to scan), exits
1 if any offender is found, printing one ``path: reason`` line per offender to
stderr.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# Mirrors idalloc._id_pattern for the "task"/"T" entity: digits delimited by
# "-", "." or end-of-stem so "T-12-foo.md" -> 12 but "TX-1.md" never matches.
_ID_RE = re.compile(r"^T-(\d+)(?:[-.]|$)")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_ID_LINE_RE = re.compile(r"^id:\s*(.+?)\s*$", re.MULTILINE)


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def filename_id(name: str) -> int | None:
    """Numeric ticket id from a backlog filename, or None if it isn't one."""
    m = _ID_RE.match(name)
    return int(m.group(1)) if m else None


def frontmatter_id(text: str) -> str | None:
    """Raw ``id:`` value from a md file's YAML frontmatter, or None."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    s = _ID_LINE_RE.search(m.group(1))
    return _strip_quotes(s.group(1).strip()) if s else None


def read_counter(project_dir: Path) -> int | None:
    """Last-allocated task counter for a project, or None if absent/garbage."""
    counter_path = project_dir / "_counters" / "task.txt"
    try:
        raw = counter_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def lint_project(backlog: Path) -> list[tuple[Path, str]]:
    """Return (path, reason) offenders for one ``<project>/backlog`` dir."""
    offenders: list[tuple[Path, str]] = []
    project_dir = backlog.parent
    counter = read_counter(project_dir)

    by_id: dict[int, list[Path]] = defaultdict(list)
    for md in sorted(backlog.glob("T-*.md")):
        fid = filename_id(md.name)
        if fid is None:
            continue
        by_id[fid].append(md)

        # (1) out-of-band mint — only judgeable when the counter exists.
        if counter is not None and fid > counter:
            offenders.append((
                md,
                f"id T-{fid:04d} > counter {counter} — minted out-of-band "
                f"(skipped the allocator; use `bsq task new`)",
            ))

        # (3) filename vs frontmatter id mismatch.
        try:
            fm_id = frontmatter_id(md.read_text(encoding="utf-8"))
        except OSError:
            fm_id = None
        if fm_id is not None and fm_id != f"T-{fid:04d}":
            offenders.append((
                md,
                f"filename id T-{fid:04d} != frontmatter id {fm_id!r} "
                f"(manual rename re-synced only one side)",
            ))

    # (2) duplicate ids.
    for fid, paths in sorted(by_id.items()):
        if len(paths) > 1:
            names = ", ".join(p.name for p in paths)
            for p in paths:
                offenders.append((
                    p, f"duplicate id T-{fid:04d} — also: {names}"))

    return offenders


def lint_dir(data_dir: Path) -> list[tuple[Path, str]]:
    offenders: list[tuple[Path, str]] = []
    if not data_dir.exists():
        return offenders
    for backlog in sorted(data_dir.glob("*/backlog")):
        if backlog.is_dir():
            offenders.extend(lint_project(backlog))
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
        print(
            f"backlog id lint: {len(all_offenders)} offender(s) — every ticket "
            f"id must come from the allocator (`bsq task new` / task_new action)",
            file=sys.stderr,
        )
        for path, reason in all_offenders:
            print(f"{path}: {reason}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
