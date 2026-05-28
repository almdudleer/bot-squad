#!/usr/bin/env python3
"""T-0072 one-shot: classify orphan ``data/<slug>/_chat/inbox-*.log`` files.

Walks every ``inbox-<sid>.log`` in the chat dir; for each whose SID has no
matching ``sessions/<sid>.md``, attempts to map it to a successor SID via
the ``started_at`` chain.

Mapping heuristic — best-effort, no surgery on disk unless trivial:
  - Extract the rotated-window stem from the orphan SID (the part between
    ``S-<user>-`` and ``-p<N>``). If exactly one live SessionMd shares that
    stem, treat it as the successor: print as "merge candidate".
  - Otherwise, mark as "no successor" and (with ``--apply``) move the file
    into ``data/<slug>/_chat/orphan/`` for review.

Does NOT silently merge inboxes — message order vs. the successor's history
can't be reconstructed safely. The operator inspects the orphan dir and
decides whether to manually re-deliver any unread items.

Default is dry-run; pass ``--apply`` to move files.

Usage:
  ./migrate_orphan_inboxes.py --slug bot-squad
  ./migrate_orphan_inboxes.py --slug bot-squad --apply
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path


_SID_RE = re.compile(r"^S-(?P<user>[^-]+)-(?P<stem>.+)-p\d+$")


def _stem_of(sid: str) -> tuple[str, str] | None:
    """Return (user, window_stem) parsed from a SID, or None if not parseable."""
    m = _SID_RE.match(sid)
    if not m:
        return None
    return (m.group("user"), m.group("stem"))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument("--data-root", default="/home/www/bot-squad/data")
    p.add_argument("--apply", action="store_true",
                   help="move unmatched orphans to _chat/orphan/")
    args = p.parse_args()

    slug_root = Path(args.data_root) / args.slug
    chat = slug_root / "_chat"
    sessions = slug_root / "sessions"
    if not chat.is_dir():
        print(f"ERROR: {chat} not found", file=sys.stderr)
        return 1

    live_sids = {md.stem for md in sessions.glob("*.md")} if sessions.is_dir() else set()
    live_by_stem: dict[tuple[str, str], list[str]] = {}
    for sid in live_sids:
        stem = _stem_of(sid)
        if stem:
            live_by_stem.setdefault(stem, []).append(sid)

    orphan_dir = chat / "orphan"
    orphans: list[tuple[Path, str, list[str]]] = []
    for inbox in sorted(chat.glob("inbox-*.log")):
        sid = inbox.stem.removeprefix("inbox-")
        if sid in live_sids:
            continue
        stem = _stem_of(sid)
        candidates = live_by_stem.get(stem, []) if stem else []
        orphans.append((inbox, sid, candidates))

    moved = 0
    print(f"== orphans (slug={args.slug}, chat={chat}) ==")
    for inbox, sid, candidates in orphans:
        size = inbox.stat().st_size if inbox.exists() else 0
        if len(candidates) == 1:
            print(f"  candidate  → {sid} (size={size}B): merge into {candidates[0]} (MANUAL)")
            continue
        if not candidates:
            verb = "would move" if not args.apply else "moved"
            print(f"  no-match   → {sid} (size={size}B): {verb} to _chat/orphan/")
            if args.apply:
                orphan_dir.mkdir(parents=True, exist_ok=True)
                target_inbox = orphan_dir / inbox.name
                if target_inbox.exists():
                    print(f"    skip (already in orphan/): {target_inbox.name}")
                    continue
                shutil.move(str(inbox), str(target_inbox))
                for sibling_pref in ("seen-", "heartbeat-"):
                    sib = chat / f"{sibling_pref}{sid}"
                    if sib.exists():
                        shutil.move(str(sib), str(orphan_dir / sib.name))
                moved += 1
            continue
        print(f"  ambiguous  → {sid} (size={size}B): {len(candidates)} candidates "
              f"({', '.join(candidates[:3])}{'...' if len(candidates) > 3 else ''}) — MANUAL")

    print()
    print(f"summary: {len(orphans)} orphan inbox(es); moved={moved}")
    if not args.apply and orphans:
        print("Re-run with --apply to move no-match orphans into _chat/orphan/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
