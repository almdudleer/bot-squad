#!/usr/bin/env python3
"""render_agents_md.py — regenerate AGENTS.md for a registered bot-squad project.

Usage:
    python3 render_agents_md.py <slug> [--config-dir /home/www/bot-squad/config]

Reads the CURRENT vision schema from data/<slug>/vision/ — `product.md` plus
the `active_initiatives` list and `initiatives/<name>.md` (T-0199; the old
north-star/strategy/tactical trio no longer exists in live data) —
interpolates it into the canonical AGENTS.md template, and writes the result
to <repo_path>/AGENTS.md.  Prints a unified diff to stdout so the caller can
review before committing.

Everything project-specific in the template is config-driven via OPTIONAL
`[projects.<slug>]` fields in projects.toml (same pattern as T-0195's
test_*_cmd fields): `ops_path`, `stack`, `stakeholders`, `tg_bot`,
`extra_hard_rules`, `test_*_cmd`.  A field-less project renders neutral
placeholders — never a KeyError and never another project's identity.
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
# Vision extraction (current schema — T-0199)
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


def extract_product(text: str) -> str:
    """Return product.md's lead statement: H1 stripped, cut at the first H2.

    product.md may carry operational subsections (e.g. bot-squad's
    `## Distribution`, `## Read once`); the always-cached AGENTS.md takes only
    the core product statement and points at the full file for the rest.
    """
    body = _strip_h1(text)
    cut = re.search(r"^## ", body, re.MULTILINE)
    if cut:
        body = body[: cut.start()].rstrip()
    return body.strip()


def _md_title(path: Path) -> str | None:
    """First H1 of a markdown file, or None when unreadable/untitled."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def render_active_initiatives(vision_dir: Path, ops: str) -> str:
    """Bullet list of the project's active initiatives.

    `vision/active_initiatives` lists one `initiatives/<basename>` per line
    (blank lines ignored).  Each bullet carries the initiative's H1 title —
    falling back to the basename stem when the file is missing or untitled —
    plus its path.  Graceful when the list file is absent or empty.
    """
    listing = vision_dir / "active_initiatives"
    names: list[str] = []
    if listing.exists():
        names = [ln.strip() for ln in listing.read_text().splitlines() if ln.strip()]
    if not names:
        return f"_(No active initiatives listed — see `{ops}/vision/initiatives/`.)_"
    lines = []
    for name in names:
        title = _md_title(vision_dir / "initiatives" / name) or Path(name).stem
        lines.append(f"- **{title}** — `{ops}/vision/initiatives/{name}`")
    return "\n".join(lines)


_NO_PRODUCT = (
    "_(No `vision/product.md` yet — write one; it anchors every build/no-build "
    "decision.)_"
)


# ---------------------------------------------------------------------------
# Per-project test commands (config-driven — T-0195)
# ---------------------------------------------------------------------------

# Ordered (field, label) spec for the "## Test commands" block. Each field is
# OPTIONAL in projects.toml; a project only renders the lines it defines, so no
# other project's commands (e.g. watchrobot's `docker exec signal-tracker`)
# leak into a file that didn't ask for them. The field VALUE is the rendered
# markdown for the command part (it carries its own backticks/annotations),
# because the lines are heterogeneous — some wrap the whole command in backticks,
# some prefix it with "from `web/`,". Keeping the markdown in the value lets one
# uniform `- {label}: {value}` line template reproduce every existing line
# byte-for-byte.
_TEST_COMMAND_FIELDS: tuple[tuple[str, str], ...] = (
    ("test_backend_cmd", "Backend"),
    ("test_build_cmd", "Build"),
    ("test_e2e_cmd", "Frontend e2e"),
    ("test_typecheck_cmd", "Type-check"),
    ("test_lint_cmd", "Lint"),
)

# Shown when a project defines none of the fields above — a neutral placeholder
# rather than a stale/wrong command (DoD: graceful fallback, no KeyError).
_NO_TEST_COMMANDS = (
    "- _(No project-specific test commands configured. Add `test_backend_cmd` "
    "etc. to this project's `[projects.<slug>]` table in `projects.toml`.)_"
)


def render_test_commands(project: dict) -> str:
    """Build the bullet block for the AGENTS.md '## Test commands' section.

    Pulls only the `test_*_cmd` fields the project actually defines (graceful
    fallback: a field-less project gets a neutral placeholder, never a KeyError
    and never another project's command). Returned as a plain string and
    substituted into the template as a single ``{test_commands}`` value, so
    ``str.format`` never re-scans command text for literal ``{...}`` braces.
    """
    lines = [
        f"- {label}: {project[field]}"
        for field, label in _TEST_COMMAND_FIELDS
        if project.get(field)
    ]
    return "\n".join(lines) if lines else _NO_TEST_COMMANDS


# ---------------------------------------------------------------------------
# Per-project identity blocks (config-driven — T-0199)
# ---------------------------------------------------------------------------

def render_hard_rules(project: dict, ops: str) -> str:
    """Base hard rules (project-agnostic) + optional per-project extras.

    `extra_hard_rules` is an optional TOML array of bullet bodies — e.g.
    watchrobot's "don't read signal-tracker-old" rule lives there now instead
    of being baked into every project's AGENTS.md.
    """
    lines = [
        "- Never push, never merge, never amend. Those are user actions.",
        f"- Never edit `{ops}/vision/constitution.md` — agent-immutable.",
        "- Never run destructive commands (`rm -rf`, `git reset --hard`,\n"
        "  `git push --force`, dropping DB tables) without explicit user ask.",
    ]
    for rule in project.get("extra_hard_rules", ()):
        lines.append(f"- {rule}")
    return "\n".join(lines)


def render_stack_paths(project: dict, slug: str, ops: str, data_dir: Path) -> str:
    """'## Stack & paths' bullets: optional stack + stakeholders lines, plus
    the repo path and the project's data-dir pointer (`ops_path` prefix)."""
    lines: list[str] = []
    if project.get("stack"):
        lines.append(f"- Stack: {project['stack']}")
    lines.append(f"- Repo: `{project.get('repo_path', '')}`.")
    data_path = str(data_dir / slug)
    if ops == data_path:
        # ops_path IS the absolute data dir (no in-clone symlink) — an
        # `X → X` arrow line would be noise.
        lines.append(f"- Vision / backlog / feedback: `{ops}/`.")
    else:
        lines.append(f"- Vision / backlog / feedback: `{ops}/` → `{data_path}/`.")
    if project.get("stakeholders"):
        lines.append(f"- Stakeholders: {project['stakeholders']}")
    return "\n".join(lines)


def render_telegram(project: dict) -> str:
    """'## Telegram' body. Names the project's bot only when `tg_bot` is
    configured; otherwise stays generic (no @watchbot leaking everywhere)."""
    bot = project.get("tg_bot")
    intro = f"Bot `{bot}` (token in `.env` / `secrets.toml`). " if bot else ""
    return (
        f"{intro}Ping the stakeholder rarely — hard blockers, prod errors,\n"
        'finished long-running work — via `bsq tg ping "<message>"` (worker\n'
        "action `tg_notify`)."
    )


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

# NOTE: every dynamic value is substituted exactly once via str.format on this
# template; the values themselves are never re-scanned, so literal braces in
# config/vision content survive (T-0195). Keep the template itself brace-free
# except for the named placeholders.
TEMPLATE = """\
# {display_name} — agent quick reference

Always-cached, every-turn. If you find yourself re-reading larger context
files repeatedly, the right answer probably belongs here.

## Product — do not lose sight of

{product_body}

(Full: `{ops}/vision/product.md`.)

## How we decide what to build

1. Identify the user problem first (Jobs-to-be-Done framing).
2. Find evidence in user feedback (`{ops}/feedback/`).
3. Filter user-suggested SOLUTIONS — implement the underlying problem,
   not the literal request.
4. Score against the product vision: does this move its core promise
   forward for real users?
5. If unclear, raise to stakeholders rather than guessing.

## Active initiatives — this cycle's bets

{initiatives_block}

(The active set is `{ops}/vision/active_initiatives`; full texts live in
`{ops}/vision/initiatives/`.)

## Hard rules — non-negotiable

{hard_rules_block}

## Stack & paths

{stack_paths_block}

## Branching & commits

- Working branch: `{deploy_branch}`. Branch off {master_branch}, never push,
  never merge, never amend.
- Commit prefix: `[backend]`, `[web]`, `[ops]`, `[docs]`. Imperative summary,
  ≤70 chars. Co-Authored-By auto-added.
- Squash your own noise before requesting a deploy or handing off — within
  the project's git discipline (safe-commit, explicit pathspecs, no rewrite
  of pushed history; see `{ops}/AGENT_INSTRUCTIONS.md`).

## Deploy

`ops/bot-squad-bin/deploy <target> "<reason>"` — targets: {deploy_targets_md}.
- Queues a deploy. The monitor processes it within ~60s when the tree is clean.
- Tree clean = `git status --porcelain` empty (logs/cache excluded).
- TG-pings on success/failure.
- You don't manage the loop. Commit, squash, request, walk away.

Hold deploys without killing the worker: `ops/bot-squad-bin/pause-deploys
"<reason>"` writes a `PAUSED.json` marker the monitor honors — queued + new
deploys defer silently (one TG ping at pause, no per-tick spam, queue
preserved). `ops/bot-squad-bin/resume-deploys` clears it and the next tick
runs. Per-project; use it to investigate, coordinate, or land a sensitive
multi-commit ship.

Manual prod release: stakeholder reviews staging → merges `{deploy_branch}`
into `{master_branch}` → builds the prod container → deploys. Agents never
deploy prod.

## Test commands

{test_commands}

## When you need more (not every-turn — read on demand)

- `{ops}/vision/team_protocol.md` — working conventions for every session
- `{ops}/vision/roles/` — role contracts (operator, teamlead, dev)
- `{ops}/vision/initiatives/` — discrete strategic bets, full texts
- `{ops}/backlog/` — all open work, one .md per task
- `{ops}/feedback/` — raw user feedback for JTBD evidence
- `{ops}/docs/` — categorized project docs (architecture / design /
  support / runbook / product)
- `{ops}/AGENT_INSTRUCTIONS.md` — recipes, gotchas, paths

## Telegram

{telegram_block}

## What NOT to put here

Long history, decision logs, recipes for one-off ops, detailed task
breakdowns. Those go in `AGENT_INSTRUCTIONS.md` or under
`{ops}/`. Keep this file dense and product-first.
"""


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render(slug: str, config_dir: Path, data_dir: Path) -> str:
    project = get_project(config_dir, slug)
    vision_dir = data_dir / slug / "vision"
    # In-clone prefix of the project's data-dir symlink. New scaffolds plant a
    # plain `ops` symlink (project_scaffold._link_ops); legacy clones differ
    # (watchrobot: `ops/bot-squad`) and override via the optional `ops_path`.
    ops = project.get("ops_path", "ops")

    product_path = vision_dir / "product.md"
    product_body = (
        extract_product(product_path.read_text())
        if product_path.exists()
        else _NO_PRODUCT
    )
    if not product_body:
        product_body = _NO_PRODUCT

    targets = project.get("deploy_targets") or ["staging"]
    deploy_targets_md = ", ".join(f"`{t}`" for t in targets)

    return TEMPLATE.format(
        display_name=project.get("display_name", slug),
        ops=ops,
        product_body=product_body,
        initiatives_block=render_active_initiatives(vision_dir, ops),
        hard_rules_block=render_hard_rules(project, ops),
        stack_paths_block=render_stack_paths(project, slug, ops, data_dir),
        deploy_branch=project.get("deploy_branch", "bot_squad/dev"),
        master_branch=project.get("master_branch", "master"),
        deploy_targets_md=deploy_targets_md,
        telegram_block=render_telegram(project),
        test_commands=render_test_commands(project),
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
