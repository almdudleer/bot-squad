#!/usr/bin/env python3
"""One-shot backfill: populate ``tmux_session`` on existing SessionMd files.

T-0078 — adds a ``tmux_session`` field to SessionMd so the registry can
record which tmux session each pane lives in. Fresh spawns + the
SessionStart hook write the field going forward; this script catches up
on SessionMd entries that predate the change (or were manually created
when the stakeholder spun up per-initiative sessions by hand).

Strategy: walk ``tmux list-panes -a -F '#{pane_id}|#{session_name}'`` to
build a pane → session map, then iterate every project's
``data/<slug>/sessions/*.md``; for each md whose filename matches a live
pane (by trailing ``-p<N>``), add or update ``tmux_session`` to the live
session name. SessionMds whose pane is gone (suspended sessions) are
left alone — they'll pick up the field at next resume.

Idempotent:
- Re-running is a no-op once every live SessionMd carries the field.
- An md that already carries the same value is rewritten only if the
  live tmux session has since changed (rare; usually a manual move).

Usage:
    python scripts/migrate/backfill_tmux_session.py [--dry-run] \\
        [--data-dir /home/www/bot-squad/data]

When ``--data-dir`` is omitted the script defaults to
``$BOT_SQUAD/data`` (``BOT_SQUAD`` defaulting to ``/home/www/bot-squad``)
— the same resolution rule the SessionStart hook uses.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def list_panes_session_map() -> dict[str, str]:
    """Return ``{pane_id_no_pct: session_name}`` for every tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}|#{session_name}"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return {}
    if result.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        pane_raw, _, sess = line.partition("|")
        pane_no_pct = pane_raw.lstrip("%")
        if pane_no_pct and sess:
            out[pane_no_pct] = sess
    return out


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)
_TMUX_FIELD_RE = re.compile(r"^tmux_session:.*$", re.M)
_SID_PANE_RE = re.compile(r"-p(\d+)\.md$")


def update_md_tmux_session(md_path: Path, new_value: str) -> bool:
    """Add or rewrite the ``tmux_session`` field on a SessionMd file.

    Returns True iff the file was modified. Leaves the rest of the
    frontmatter and body verbatim.
    """
    try:
        text = md_path.read_text()
    except OSError:
        return False
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False

    if _TMUX_FIELD_RE.search(text):
        new_line = f"tmux_session: {new_value}"
        # Skip rewrite if the value is already correct.
        existing_line_match = _TMUX_FIELD_RE.search(text)
        if existing_line_match and existing_line_match.group(0) == new_line:
            return False
        new_text = _TMUX_FIELD_RE.sub(new_line, text, count=1)
    else:
        # Insert before the closing `---`.
        fm_close = re.compile(r"^---\s*$", re.M)
        matches = list(fm_close.finditer(text))
        if len(matches) < 2:
            return False
        idx = matches[1].start()
        new_text = text[:idx] + f"tmux_session: {new_value}\n" + text[idx:]

    tmp = md_path.parent / (md_path.name + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.rename(tmp, md_path)
    except OSError:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False
    return True


def iter_session_mds(data_dir: Path):
    """Yield every ``<slug>/sessions/*.md`` under data_dir."""
    if not data_dir.is_dir():
        return
    for slug_dir in sorted(data_dir.iterdir()):
        sessions_dir = slug_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for md in sorted(sessions_dir.glob("*.md")):
            yield md


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=None,
                    help="path to data/ (default: $BOT_SQUAD/data)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report changes without writing")
    args = ap.parse_args(argv)

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        bot_squad = os.environ.get("BOT_SQUAD", "/home/www/bot-squad")
        data_dir = Path(bot_squad) / "data"

    if not data_dir.is_dir():
        print(f"backfill_tmux_session: data dir not found: {data_dir}",
              file=sys.stderr)
        return 1

    pane_map = list_panes_session_map()
    if not pane_map:
        print("backfill_tmux_session: tmux returned no panes — nothing to "
              "backfill (live tmux server reachable?)", file=sys.stderr)
        return 0

    updated = 0
    skipped = 0
    for md in iter_session_mds(data_dir):
        m = _SID_PANE_RE.search(md.name)
        if not m:
            continue
        pane_no_pct = m.group(1)
        sess = pane_map.get(pane_no_pct)
        if not sess:
            # No live pane for this SID — leave it alone; the next
            # resume/SessionStart will set tmux_session.
            skipped += 1
            continue
        if args.dry_run:
            print(f"[dry-run] would set tmux_session={sess!r} on {md}")
            updated += 1
            continue
        if update_md_tmux_session(md, sess):
            updated += 1
            print(f"updated tmux_session={sess!r} on {md}")

    print(f"\ndone — {updated} updated, {skipped} skipped (no live pane)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
