#!/usr/bin/env python3
"""Seed bot-squad's layered vision artifacts from a legacy signal-tracker repo.

Inputs (paths are absolute):
  --constitution PATH    Legacy CONSTITUTION.md
  --agents PATH          Legacy AGENTS.md (the 15kB version)
  --backlog PATH         Legacy BACKLOG.md
  --feature-v06 PATH     Legacy ops/feature_v06.md (optional)
  --out PATH             Target data/<slug>/ dir

Outputs:
  <out>/vision/constitution.md             — verbatim copy
  <out>/vision/north-star.md               — extracted from AGENTS.md "Project summary"
  <out>/vision/strategy.md                 — extracted from BACKLOG (Should + Deferred)
  <out>/vision/tactical.md                 — extracted from BACKLOG (Must + active Should + Current directive)
  <out>/vision/initiatives/v0.6-topic-monitoring.md
                                            — consolidated from ops/feature_v06.md (if given)
  <out>/MIGRATION_REPORT.md                — log of what was extracted from where

Hand-editing afterward is expected; this is a starter, not an oracle.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def extract_section(md: str, heading: str) -> str:
    """Return the body of the first '## heading' or '# heading' (case-sensitive)."""
    pattern = rf"(?ms)^#+\s+{re.escape(heading)}\s*$\n(.*?)(?=^#+\s+|\Z)"
    m = re.search(pattern, md)
    return (m.group(1).strip() + "\n") if m else ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--constitution", type=Path, required=True)
    p.add_argument("--agents", type=Path, required=True)
    p.add_argument("--backlog", type=Path, required=True)
    p.add_argument("--feature-v06", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    vision = args.out / "vision"
    inits = vision / "initiatives"
    inits.mkdir(parents=True, exist_ok=True)
    report: list[str] = []

    # 1. constitution — verbatim
    const_text = args.constitution.read_text()
    (vision / "constitution.md").write_text(const_text)
    report.append(f"- vision/constitution.md ← {args.constitution} (verbatim)")

    # 2. north-star from AGENTS.md
    agents = args.agents.read_text()
    project_summary = extract_section(agents, "Project summary")
    feature_test = extract_section(agents, "Feature test")
    if not project_summary:
        # Fallback: grab the leading paragraphs.
        project_summary = "\n".join(agents.splitlines()[:30]) + "\n"
    north_star = (
        "# North star\n\n"
        f"{project_summary.strip()}\n\n"
        f"{feature_test.strip()}\n\n"
        "## How we decide what to build\n\n"
        "1. Identify the user problem first (Jobs-to-be-Done framing).\n"
        "2. Find evidence in user feedback (`ops/bot-squad/feedback/`).\n"
        "3. Filter user-suggested SOLUTIONS — implement the underlying problem.\n"
        "4. Score against the north-star: does this raise user catch-rate?\n"
        "5. If unclear, raise to stakeholders rather than guessing.\n"
    )
    (vision / "north-star.md").write_text(north_star)
    report.append(f"- vision/north-star.md ← {args.agents} (extracted Project summary + Feature test + appended decision framework)")

    # 3. strategy from BACKLOG Should + Deferred
    backlog = args.backlog.read_text()
    should = extract_section(backlog, "Should")
    deferred = extract_section(backlog, "Deferred to v0.6 — UI overhaul (topic monitoring)") or extract_section(backlog, "Deferred to v0.6")
    strategy = (
        "# Current strategy (this cycle's bets)\n\n"
        "_Refreshed by the Vision Crystallizer when bets change._\n\n"
        "## Should — committed for this cycle\n\n"
        f"{should.strip() or '(empty)'}\n\n"
        "## Deferred / on-deck\n\n"
        f"{deferred.strip() or '(empty)'}\n"
    )
    (vision / "strategy.md").write_text(strategy)
    report.append(f"- vision/strategy.md ← {args.backlog} (Should + Deferred sections)")

    # 4. tactical from Must + active Should + (truncated) Current directive
    must = extract_section(backlog, "Must")
    in_progress = extract_section(backlog, "In Progress")
    current_directive = extract_section(agents, "Current directive")
    tactical = (
        "# Current tactical priorities (right now, ordered)\n\n"
        "## Must\n\n"
        f"{must.strip() or '(empty)'}\n\n"
        "## In progress\n\n"
        f"{in_progress.strip() or '(empty)'}\n\n"
        "## Current directive\n\n"
        f"{current_directive.strip() or '(no active directive)'}\n"
    )
    (vision / "tactical.md").write_text(tactical)
    report.append(f"- vision/tactical.md ← {args.backlog} (Must + In Progress) + AGENTS.md (Current directive)")

    # 5. v0.6 initiative
    if args.feature_v06 and args.feature_v06.exists():
        (inits / "v0.6-topic-monitoring.md").write_text(args.feature_v06.read_text())
        report.append(f"- vision/initiatives/v0.6-topic-monitoring.md ← {args.feature_v06} (verbatim)")

    # MIGRATION_REPORT
    (args.out / "MIGRATION_REPORT.md").write_text(
        "# Vision seed — migration report\n\n"
        "Generated by `scripts/cli/seed_vision.py`. Hand-edit each layer before going live.\n\n"
        + "\n".join(report) + "\n"
    )
    print("seed complete; review", args.out / "MIGRATION_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
