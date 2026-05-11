#!/usr/bin/env python3
"""render_agents_md.py — regenerate AGENTS.md for a registered bot-squad project.

Usage:
    python3 render_agents_md.py <slug> [--config-dir /home/www/bot-squad/config]

Reads vision/{north-star,strategy,tactical}.md from data/<slug>/vision/,
interpolates them into the canonical AGENTS.md template, and writes the
result to <repo_path>/AGENTS.md.  Prints a unified diff to stdout so the
caller can review before committing.
"""
from __future__ import annotations

import difflib
import re
import sys
import tomllib
from pathlib import Path


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_projects(config_dir: Path) -> dict:
    p = config_dir / "projects.toml"
    if not p.exists():
        raise FileNotFoundError(f"projects.toml not found: {p}")
    with open(p, "rb") as fh:
        return tomllib.load(fh).get("projects", {})


def get_project(config_dir: Path, slug: str) -> dict:
    projects = load_projects(config_dir)
    if slug not in projects:
        raise KeyError(f"slug '{slug}' not found in projects.toml")
    return projects[slug]


# ---------------------------------------------------------------------------
# Vision extraction
# ---------------------------------------------------------------------------

def _strip_h1(text: str) -> str:
    """Remove the leading H1 line (and its trailing blank line) from a markdown block."""
    lines = text.splitlines()
    # Skip leading blank lines
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    # Skip the H1 line if present
    if i < len(lines) and lines[i].startswith("# "):
        i += 1
    # Skip blank lines immediately after H1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:]).rstrip()


def extract_north_star(text: str) -> tuple[str, str]:
    """Return (h1_title, body_before_decision_framework).

    Stops at '## How we decide' section and before operational metadata
    lines (Prod/Staging/Dev URLs, Stack, Server, Login, Stakeholders) that
    belong in the full vision file but not in the always-cached AGENTS.md.
    """
    # Find title
    title = "North star"
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    body = _strip_h1(text)

    # Cut at the embedded "How we decide" H2 if present
    cut = re.search(r"^## How we decide", body, re.MULTILINE)
    if cut:
        body = body[: cut.start()].rstrip()

    # Also cut at operational metadata lines (Prod/Staging/Dev/Stack/Server/Login/Stakeholders)
    # These live in the vision file for background context but are too noisy for AGENTS.md.
    operational_re = re.compile(
        r"^\*\*(Prod|Staging|Dev|Stack|Server|Login|Stakeholders)\*\*",
        re.MULTILINE,
    )
    op_match = operational_re.search(body)
    if op_match:
        body = body[: op_match.start()].rstrip()

    return title, body


def extract_strategy(text: str) -> str:
    """Return the strategy body (skip H1 and meta lines, stop at PM-reference block)."""
    body = _strip_h1(text)

    # Stop at a horizontal rule (---) that precedes PM-only reference notes.
    hr_match = re.search(r"^---\s*$", body, re.MULTILINE)
    if hr_match:
        body = body[: hr_match.start()].rstrip()

    # The strategy file may have ## sub-headers; include them but trim meta lines.
    lines = body.splitlines()
    result_lines: list[str] = []
    skip_next_blank = False

    for line in lines:
        stripped = line.strip()
        # Skip meta/refresh lines
        if re.match(r"^_Refreshed by", stripped):
            skip_next_blank = True
            continue
        if skip_next_blank and not stripped:
            skip_next_blank = False
            continue
        result_lines.append(line)

    return "\n".join(result_lines).strip()


def extract_tactical(text: str) -> str:
    """Return tactical body (skip H1; keep the directive and status lines)."""
    body = _strip_h1(text)

    lines = body.splitlines()
    result_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Skip HTML comments
        if stripped.startswith("<!--") or stripped.endswith("-->"):
            continue
        result_lines.append(line)

    return "\n".join(result_lines).strip()


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

TEMPLATE = """\
# {display_name} — agent quick reference

Always-cached, every-turn. If you find yourself re-reading larger context
files repeatedly, the right answer probably belongs here.

## North star — do not lose sight of

{north_star_body}

(Full: `ops/bot-squad/vision/north-star.md`.)

## How we decide what to build

1. Identify the user problem first (Jobs-to-be-Done framing).
2. Find evidence in user feedback (`ops/bot-squad/feedback/`).
3. Filter user-suggested SOLUTIONS — implement the underlying problem,
   not the literal request.
4. Score against the north-star: does this raise user catch-rate?
5. If unclear, raise to stakeholders rather than guessing.

## Current strategy (this cycle's bets)

{strategy_body}

(Full: `ops/bot-squad/vision/strategy.md`.)

## Current tactical priorities

{tactical_body}

(Full: `ops/bot-squad/vision/tactical.md`.)

## Hard rules — non-negotiable

- Never push, never merge, never amend. Those are user actions.
- Never edit `ops/bot-squad/vision/constitution.md` — agent-immutable.
- Never run destructive commands (`rm -rf`, `git reset --hard`,
  `git push --force`, dropping DB tables) without explicit user ask.
- Don't read or copy from `/home/www/bot-squad/archived/signal-tracker-old/`
  — that's the deprecated multi-role flow. Patterns there don't apply.

## Stack & paths

- FastAPI + asyncpg backend, React 19 + Vite + TS + Tailwind frontend,
  PostgreSQL, Docker + traefik.
- Repo: `{repo_path}`.
- Vision / backlog / feedback: `ops/bot-squad/` (gitignored symlink to
  `/home/www/bot-squad/data/{slug}/`).
- Stakeholders: Alexey (chat 404580642, @alexeysdk) and Timofey (@timpo).
  Equal authority — see `ops/bot-squad/vision/constitution.md`.

## Branching & commits

- Working branch: `bot_squad/dev`. Branch off master, never push, never merge,
  never amend.
- Commit prefix: `[backend]`, `[web]`, `[ops]`, `[docs]`. Imperative summary,
  ≤70 chars. Co-Authored-By auto-added.
- Squash before requesting a deploy or handing off:
    `BASE=$(git merge-base HEAD master)`
    `git reset --soft "$BASE" && git commit -m "<single-line message>"`
  (Don't squash on master/staging, with a dirty tree, or if BASE == HEAD.)

## Deploy

`ops/bot-squad-bin/deploy <target> "<reason>"` — targets: `staging`, `dev`.
- Queues a deploy. The monitor processes it within ~60s when the tree is clean.
- Tree clean = `git status --porcelain` empty (logs/cache excluded).
- TG-pings on success/failure.
- You don't manage the loop. Commit, squash, request, walk away.

Manual prod release: stakeholder reviews staging → merges bot_squad/dev (or
staging) into master → builds the prod container → deploys. Agents never
deploy prod.

## Test commands

- Backend: `docker exec signal-tracker python test_api.py`
- Frontend e2e: `npm run test:e2e` (from `web/`)
- Type-check: from `web/`, `npx tsc -b --noEmit`
- Lint: from `web/`, `npm run lint`

## When you need more (not every-turn — read on demand)

- `ops/bot-squad/vision/strategy.md` — full current bets + rationale
- `ops/bot-squad/vision/tactical.md` — full current cycle priorities
- `ops/bot-squad/vision/initiatives/` — discrete strategic bets
- `ops/bot-squad/backlog/` — all open work, one .md per task
- `ops/bot-squad/feedback/` — raw user feedback for JTBD evidence
- `ops/bot-squad/AGENT_INSTRUCTIONS.md` — recipes, gotchas, paths

## Telegram

Bot `@watchbot` (token in `.env` / `secrets.toml`). Ping the user rarely —
hard blockers, prod errors, finished long-running work — via worker action
`tg_notify` (POST to bot-squad worker over Unix socket).

## What NOT to put here

Long history, decision logs, recipes for one-off ops, detailed task
breakdowns. Those go in `AGENT_INSTRUCTIONS.md` or under
`ops/bot-squad/`. Keep this file dense and product-first.
"""


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render(slug: str, config_dir: Path, data_dir: Path) -> str:
    project = get_project(config_dir, slug)
    vision_dir = data_dir / slug / "vision"

    ns_path = vision_dir / "north-star.md"
    st_path = vision_dir / "strategy.md"
    ta_path = vision_dir / "tactical.md"

    for p in (ns_path, st_path, ta_path):
        if not p.exists():
            raise FileNotFoundError(f"Vision file missing: {p}")

    _, ns_body = extract_north_star(ns_path.read_text())
    strategy_body = extract_strategy(st_path.read_text())
    tactical_body = extract_tactical(ta_path.read_text())

    return TEMPLATE.format(
        display_name=project.get("display_name", slug),
        slug=slug,
        repo_path=project.get("repo_path", ""),
        north_star_body=ns_body,
        strategy_body=strategy_body,
        tactical_body=tactical_body,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Project slug (matches projects.toml key)")
    parser.add_argument(
        "--config-dir",
        default="/home/www/bot-squad/config",
        help="Path to bot-squad config directory",
    )
    parser.add_argument(
        "--data-dir",
        default="/home/www/bot-squad/data",
        help="Path to bot-squad data directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rendered content without writing",
    )
    args = parser.parse_args()

    config_dir = Path(args.config_dir)
    data_dir = Path(args.data_dir)

    try:
        new_content = render(args.slug, config_dir, data_dir)
    except (FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    project = get_project(config_dir, args.slug)
    agents_md = Path(project["repo_path"]) / "AGENTS.md"

    if args.dry_run:
        print(new_content)
        return

    old_content = agents_md.read_text() if agents_md.exists() else ""

    if old_content == new_content:
        print("AGENTS.md is already up to date.")
        return

    # Print diff for review.
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{agents_md.name}",
        tofile=f"b/{agents_md.name}",
    )
    sys.stdout.writelines(diff)

    agents_md.write_text(new_content)
    print(f"\nWritten: {agents_md}")


if __name__ == "__main__":
    main()
