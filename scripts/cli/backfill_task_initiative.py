#!/usr/bin/env python3
"""T-0038: backfill `initiative:` into task md frontmatters.

For each backlog task missing `initiative:`, look up which session
(primary task_id, or extra_task_ids) currently owns it. If that session
carries an initiative (primary or extras), stamp it into the task md.

Default is dry-run. Pass --apply to write. Unattached tasks are reported
so the operator can classify them manually.

Usage:
  ./backfill_task_initiative.py --slug bot-squad
  ./backfill_task_initiative.py --slug bot-squad --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_BOT_SQUAD = "/home/www/bot-squad"

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)
_LIST_RE = re.compile(r"^\[(.*)\]$")


def parse_frontmatter(text: str) -> tuple[dict, str] | None:
    m = _FM_RE.match(text)
    if not m:
        return None
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta, m.group(2)


def parse_list_field(raw: str) -> list[str]:
    raw = (raw or "").strip()
    m = _LIST_RE.match(raw)
    if not m:
        return []
    inner = m.group(1).strip()
    if not inner:
        return []
    return [x.strip() for x in inner.split(",") if x.strip() and x.strip() != "~"]


def task_owners(sessions_dir: Path) -> dict[str, str]:
    """Return {task_id: initiative_basename} based on session bindings.

    A task is "owned" by a session if it appears as the session's primary
    task_id or in extra_task_ids. The session's initiative (primary or
    first extra) is the inferred initiative.
    """
    out: dict[str, str] = {}
    if not sessions_dir.exists():
        return out
    for sess_md in sorted(sessions_dir.glob("*.md")):
        try:
            text = sess_md.read_text()
        except OSError:
            continue
        parsed = parse_frontmatter(text)
        if parsed is None:
            continue
        meta, _ = parsed
        primary_init = meta.get("initiative", "").strip()
        if primary_init == "~":
            primary_init = ""
        extras_init = parse_list_field(meta.get("extra_initiatives", ""))
        chosen_init = primary_init or (extras_init[0] if extras_init else "")
        if not chosen_init:
            continue

        primary_task = meta.get("task_id", "").strip()
        if primary_task and primary_task != "~":
            out.setdefault(primary_task, chosen_init)
        for t in parse_list_field(meta.get("extra_task_ids", "")):
            out.setdefault(t, chosen_init)
    return out


def insert_initiative_after_status(fm_block: str, initiative: str) -> str:
    """Insert `initiative: <basename>` after the `status:` line. If no
    status line, append at the end of the frontmatter block."""
    lines = fm_block.splitlines()
    insert_at = len(lines)
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("status:"):
            insert_at = i + 1
            break
    lines.insert(insert_at, f"initiative: {initiative}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True, help="project slug (e.g. bot-squad)")
    p.add_argument(
        "--data-dir",
        default=os.environ.get("BOT_SQUAD_DATA", f"{DEFAULT_BOT_SQUAD}/data"),
        help="path to BOT_SQUAD/data (default: $BOT_SQUAD_DATA or /home/www/bot-squad/data)",
    )
    p.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = p.parse_args()

    project = Path(args.data_dir) / args.slug
    backlog = project / "backlog"
    sessions = project / "sessions"

    if not backlog.exists():
        print(f"error: no backlog directory at {backlog}", file=sys.stderr)
        return 2

    owners = task_owners(sessions)
    if not owners:
        print(f"note: no session-derived owners found in {sessions}", file=sys.stderr)

    inferred: list[tuple[str, str]] = []   # (task_id, initiative)
    unattached: list[str] = []
    already: list[tuple[str, str]] = []

    for path in sorted(backlog.glob("T-*.md")):
        try:
            raw = path.read_text()
        except OSError:
            continue
        parsed = parse_frontmatter(raw)
        if parsed is None:
            continue
        meta, body = parsed
        task_id = meta.get("id") or path.stem.split("-", 2)[0] + "-" + (path.stem.split("-", 2)[1] if "-" in path.stem else "")
        if not task_id:
            continue
        existing = (meta.get("initiative") or "").strip()
        if existing and existing != "~":
            already.append((task_id, existing))
            continue
        chosen = owners.get(task_id)
        if chosen:
            inferred.append((task_id, chosen))
            if args.apply:
                m = _FM_RE.match(raw)
                if not m:
                    continue
                new_fm = insert_initiative_after_status(m.group(1), chosen)
                content = f"---\n{new_fm}\n---\n{m.group(2)}"
                if not m.group(2).startswith("\n"):
                    content = f"---\n{new_fm}\n---\n\n{m.group(2)}"
                tmp = path.parent / (path.name + ".tmp")
                tmp.write_text(content, encoding="utf-8")
                os.rename(tmp, path)
        else:
            unattached.append(task_id)

    print(f"\n=== {args.slug} backfill report ===")
    print(f"already tagged   : {len(already)}")
    print(f"inferred via sess: {len(inferred)}")
    print(f"unattached       : {len(unattached)}")
    print()
    if inferred:
        verb = "wrote" if args.apply else "would write"
        print(f"[{verb}]")
        for tid, init in inferred:
            print(f"  {tid} -> {init}")
        print()
    if unattached:
        print("[unattached — needs manual classification]")
        for tid in unattached:
            print(f"  {tid}")
        print()
    if not args.apply:
        print("dry-run only. re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
