#!/usr/bin/env python3
"""T-0051 — sync the canonical project-create-modes markdown into
AGENT_INSTRUCTIONS.md.

Single source of truth: ``api/app/resources/project-create-modes.md`` in
the bot-squad repo. The same file is served at runtime by
``GET /api/projects/_/create-modes`` and rendered by the wizard's
rationale-expander. Re-running this script keeps the per-user
bot-squad-manager AGENT_INSTRUCTIONS in lock-step with the UI text.

Drift safety: the script overwrites the entire fenced block between
the markers, so any local edits in that block are wiped. Anything
outside the markers is preserved verbatim.

Usage:
    scripts/cli/sync_project_create_modes.py
    scripts/cli/sync_project_create_modes.py --check    # exit 1 on drift
    scripts/cli/sync_project_create_modes.py \\
        --target /path/to/AGENT_INSTRUCTIONS.md
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "api" / "app" / "resources" / "project-create-modes.md"
DEFAULT_TARGET = Path("/home/www/bot-squad/data/bot-squad/AGENT_INSTRUCTIONS.md")

BEGIN_MARKER = "<!-- begin: project-create-modes (managed by scripts/cli/sync_project_create_modes.py) -->"
END_MARKER = "<!-- end: project-create-modes -->"

SECTION_HEADER = "## Project creation modes"


def render_managed_block(canonical_md: str) -> str:
    """Wrap the canonical md in the marker block + a header so a
    reader scanning AGENT_INSTRUCTIONS lands on a self-contained
    section.

    We strip the leading ``# Project creation modes`` heading from
    the canonical md so the rendered section becomes a level-2
    heading inside AGENT_INSTRUCTIONS instead of duplicating the
    top-level title."""
    body = canonical_md
    if body.startswith("# "):
        body = body.split("\n", 1)[1].lstrip("\n")
    preamble = (
        "> **Auto-generated.** The block below mirrors the canonical\n"
        "> `api/app/resources/project-create-modes.md` in the bot-squad repo —\n"
        "> the same file the UI's rationale-expander fetches via\n"
        "> `GET /api/projects/_/create-modes`. **Do not edit by hand.**\n"
        "> To update, edit the canonical md in the repo, then re-run\n"
        "> `scripts/cli/sync_project_create_modes.py`.\n"
    )
    return (
        f"{BEGIN_MARKER}\n"
        f"{SECTION_HEADER}\n\n"
        f"{preamble}\n"
        f"{body.rstrip()}\n"
        f"{END_MARKER}\n"
    )


def replace_or_append(target_text: str, block: str) -> str:
    """Replace existing managed block; else append at end with one
    blank-line separator."""
    if BEGIN_MARKER in target_text and END_MARKER in target_text:
        start = target_text.index(BEGIN_MARKER)
        end = target_text.index(END_MARKER) + len(END_MARKER)
        # Consume a single trailing newline if it follows the end marker
        # so re-running doesn't keep growing the file with blank lines.
        if end < len(target_text) and target_text[end] == "\n":
            end += 1
        return target_text[:start] + block + target_text[end:]
    sep = "" if target_text.endswith("\n\n") else (
        "\n" if target_text.endswith("\n") else "\n\n"
    )
    return target_text + sep + block


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.rename(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument(
        "--check", action="store_true",
        help="Exit 1 if the target would change; do not write.",
    )
    args = ap.parse_args()

    if not SOURCE.exists():
        print(f"source file missing: {SOURCE}", file=sys.stderr)
        return 1

    target = Path(args.target)
    canonical = SOURCE.read_text()
    block = render_managed_block(canonical)

    if not target.exists():
        if args.check:
            print(f"target does not exist: {target}", file=sys.stderr)
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, block)
        print(f"wrote: {target}")
        return 0

    current = target.read_text()
    updated = replace_or_append(current, block)
    if current == updated:
        print(f"in sync: {target}")
        return 0
    if args.check:
        print(f"drift detected in: {target}", file=sys.stderr)
        return 1
    _atomic_write_text(target, updated)
    print(f"updated: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
