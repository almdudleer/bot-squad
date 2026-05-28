#!/usr/bin/env python3
"""T-0077 one-shot: flip ``status: active`` SessionMds with no live pane → suspended.

The scheduler's ``binding_gc`` tick handles this going forward, but the live
data dir at audit time had 25+ accumulated zombies. This script is the
one-time mop-up so the steady-state janitor doesn't have to walk through
years of accumulated drift on every tick.

Per-user scope (matches the worker's gc_sessions behaviour): only walks
SessionMds whose SID prefix matches the current linux user. Other users'
mds are left alone because we can't see their tmux server.

Default is dry-run; pass ``--apply`` to write. ``--slug`` selects which
project's data dir to walk; ``--data-root`` overrides the default
``/home/www/bot-squad/data``.

Usage:
  ./migrate_zombie_sessions.py --slug bot-squad
  ./migrate_zombie_sessions.py --slug bot-squad --apply
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
import time
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
    """Return the SID set for every live ``claude`` pane this user owns."""
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
        pane_no = pane_id.lstrip("%")
        sids.add(f"S-{user}-{window}-p{pane_no}")
    return sids


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument("--data-root", default="/home/www/bot-squad/data")
    p.add_argument("--apply", action="store_true",
                   help="write changes (default: dry-run)")
    args = p.parse_args()

    sessions_dir = Path(args.data_root) / args.slug / "sessions"
    if not sessions_dir.is_dir():
        print(f"ERROR: {sessions_dir} not found", file=sys.stderr)
        return 1

    user = getpass.getuser()
    user_prefix = f"S-{user}-"
    live = _live_sids(user)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    repaired: list[str] = []
    scanned = 0
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        text = md.read_text()
        meta = _parse_frontmatter(text)
        if not meta:
            continue
        scanned += 1
        if meta.get("status") != "active":
            continue
        sid = meta.get("sid", md.stem)
        if sid in live:
            continue
        if meta.get("archived", "").lower() == "true":
            continue
        repaired.append(sid)
        if not args.apply:
            continue
        # Patch status + add suspended_at, line-based so we preserve any
        # other field ordering exactly.
        fm_block, _, body_with_close = text.partition("\n---\n")
        fm_lines = fm_block.splitlines()  # includes leading "---"
        new_lines: list[str] = []
        has_suspended_at = False
        for ln in fm_lines:
            stripped = ln.lstrip()
            if stripped.startswith("status:"):
                new_lines.append("status: suspended")
                continue
            if stripped.startswith("suspended_at:"):
                new_lines.append(f"suspended_at: {now}")
                has_suspended_at = True
                continue
            new_lines.append(ln)
        if not has_suspended_at:
            new_lines.append(f"suspended_at: {now}")
        new_text = "\n".join(new_lines) + "\n---\n" + body_with_close
        tmp = md.with_suffix(md.suffix + ".tmp")
        tmp.write_text(new_text)
        os.rename(tmp, md)

    verb = "would patch" if not args.apply else "patched"
    print(f"scanned={scanned} zombies={len(repaired)} (user={user})")
    for sid in repaired:
        print(f"  {verb} → suspended: {sid}")
    if not args.apply and repaired:
        print()
        print("Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
