#!/usr/bin/env python3
"""T-0073 one-shot: strip stale primary ``task_id`` from duplicate-claim losers.

For each ``task_id`` claimed by >1 SessionMd (under the current linux user),
picks the winner = (live pane AND latest started_at) | (latest started_at),
strips ``task_id`` from the losers and preserves the old value under
``last_task_id``. Marks losers with ``archive_reason: stale-binding`` (does
not overwrite an existing ``archived: true``).

The scheduler's ``binding_gc`` tick covers this going forward; this script
is the one-shot to clean up the audit-confirmed 7-way T-0026 dup cluster
on the live install.

Default is dry-run; pass ``--apply`` to write.

Usage:
  ./migrate_stale_task_bindings.py --slug bot-squad
  ./migrate_stale_task_bindings.py --slug bot-squad --apply
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta


def _live_sids(user: str) -> set[str]:
    fmt = "#{pane_id}|#{window_name}"
    r = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", fmt],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return set()
    sids: set[str] = set()
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        pane_id, _, window = line.partition("|")
        sids.add(f"S-{user}-{window}-p{pane_id.lstrip('%')}")
    return sids


def _strip_task_id(text: str, old_task_id: str, archive_reason: str | None) -> str:
    fm_block, _, body_with_close = text.partition("\n---\n")
    fm_lines = fm_block.splitlines()
    out: list[str] = []
    has_last = False
    has_reason = False
    for ln in fm_lines:
        stripped = ln.lstrip()
        if stripped.startswith("task_id:"):
            out.append("task_id: ~")
            continue
        if stripped.startswith("last_task_id:"):
            out.append(f"last_task_id: {old_task_id}")
            has_last = True
            continue
        if archive_reason and stripped.startswith("archive_reason:"):
            out.append(f"archive_reason: {archive_reason}")
            has_reason = True
            continue
        out.append(ln)
    if not has_last:
        out.append(f"last_task_id: {old_task_id}")
    if archive_reason and not has_reason:
        out.append(f"archive_reason: {archive_reason}")
    return "\n".join(out) + "\n---\n" + body_with_close


def _started_at_key(value: str) -> tuple[int, str]:
    if not value or value == "~":
        return (0, "")
    return (1, value)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument("--data-root", default="/home/www/bot-squad/data")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    sessions_dir = Path(args.data_root) / args.slug / "sessions"
    if not sessions_dir.is_dir():
        print(f"ERROR: {sessions_dir} not found", file=sys.stderr)
        return 1

    user = getpass.getuser()
    user_prefix = f"S-{user}-"
    live = _live_sids(user)

    by_task: dict[str, list[tuple[Path, dict[str, str], str]]] = {}
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        text = md.read_text()
        meta = _parse_frontmatter(text)
        if not meta:
            continue
        tid = meta.get("task_id", "")
        if not tid or tid == "~":
            continue
        sid = meta.get("sid", md.stem)
        by_task.setdefault(tid, []).append((md, meta, sid))

    stripped_count = 0
    for task_id, claimants in by_task.items():
        if len(claimants) < 2:
            continue

        def _rank(item: tuple[Path, dict[str, str], str]) -> tuple[int, tuple[int, str]]:
            _, m, s = item
            return (1 if s in live else 0, _started_at_key(m.get("started_at", "")))

        ordered = sorted(claimants, key=_rank, reverse=True)
        winner_md, winner_meta, winner_sid = ordered[0]
        print(f"== {task_id} ==")
        print(f"  winner: {winner_sid} (started_at={winner_meta.get('started_at', '~')}, "
              f"live={winner_sid in live})")
        for md, meta, sid in ordered[1:]:
            archived = meta.get("archived", "").lower() == "true"
            reason = None if archived else "stale-binding"
            print(f"  strip:  {sid} (started_at={meta.get('started_at', '~')}, "
                  f"live={sid in live}, archived={archived})")
            stripped_count += 1
            if not args.apply:
                continue
            text = md.read_text()
            new_text = _strip_task_id(text, task_id, reason)
            tmp = md.with_suffix(md.suffix + ".tmp")
            tmp.write_text(new_text)
            os.rename(tmp, md)

    print()
    verb = "would strip" if not args.apply else "stripped"
    print(f"summary: {stripped_count} stale binding(s) {verb} (user={user})")
    if not args.apply and stripped_count:
        print("Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
