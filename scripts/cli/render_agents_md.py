#!/usr/bin/env python3
"""render_agents_md.py — regenerate AGENTS.md for a registered bot-squad project.

Usage:
    python3 render_agents_md.py <slug> [--config-dir /home/www/bot-squad/config]

Reads the CURRENT vision schema from data/<slug>/vision/ — `product.md` (T-0199;
the old north-star/strategy/tactical trio no longer exists in live data) — plus
active initiatives, interpolates it into the canonical AGENTS.md template, and
writes the result to <repo_path>/AGENTS.md.  Prints a unified diff to stdout so
the caller can review before committing.

Initiatives (T-0562): a `kind: initiative` backlog task (T-0480 — an initiative
IS a task now) is the PRIMARY source. The pre-migration `vision/active_initiatives`
list + `initiatives/<name>.md` files are read too, as a fallback for a project
that hasn't run `migrate_initiatives_to_tasks.py` yet.

Everything project-specific in the template is config-driven via OPTIONAL
`[projects.<slug>]` fields in projects.toml (same pattern as T-0195's
test_*_cmd fields): `ops_path`, `stack`, `stakeholders`, `tg_bot`,
`extra_hard_rules`, `test_*_cmd`.  A field-less project renders neutral
placeholders — never a KeyError and never another project's identity.
"""
from __future__ import annotations

import difflib
import os
import re
import sys
import tomllib
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
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def registry_file(config_dir: Path) -> Path:
    """The registry to read — live file first, tracked seed as fallback.

    T-0878: ``config/projects.toml`` is install-owned live state and is
    git-ignored, so a fresh clone (and CI) only has the tracked seed
    ``config/projects.default.toml``. Mirrors
    ``worker/bot_squad_worker/registry.resolve`` — this script is standalone
    (imported by path, no package) so it cannot import it.
    """
    live = config_dir / "projects.toml"
    if live.exists():
        return live
    return config_dir / "projects.default.toml"


def load_projects(config_dir: Path) -> dict:
    p = registry_file(config_dir)
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


def _initiative_tasks(backlog_dir: Path) -> list[tuple[str, str]]:
    """(title, filename) for open `kind: initiative` backlog tasks (T-0480).

    An initiative IS a task now — `closed` is the only terminal status (see
    `operator_redrive._TERMINAL_STATUSES`), so anything else (open, planned,
    in_progress, totest) counts as active.
    """
    out: list[tuple[str, str]] = []
    if not backlog_dir.exists():
        return out
    for path in sorted(backlog_dir.glob("*.md")):
        try:
            meta = _parse_frontmatter(path.read_text())
        except OSError:
            continue
        if meta.get("kind") != "initiative" or meta.get("status") == "closed":
            continue
        out.append((meta.get("title") or path.stem, path.name))
    return out


def render_active_initiatives(vision_dir: Path, backlog_dir: Path, ops: str) -> str:
    """Bullet list of the project's active initiatives.

    T-0562: `kind: initiative` backlog tasks (T-0480) are the PRIMARY source.
    The pre-migration `vision/active_initiatives` sidecar (one
    `initiatives/<basename>` per line) is also read, as a fallback for a
    project that hasn't run `migrate_initiatives_to_tasks.py` yet — so this
    keeps working for both a migrated project (bot-squad) and one still on
    the legacy scheme (e.g. watchrobot). Graceful when both are absent/empty.
    """
    lines = [
        f"- **{title}** — `{ops}/backlog/{filename}`"
        for title, filename in _initiative_tasks(backlog_dir)
    ]

    listing = vision_dir / "active_initiatives"
    if listing.exists():
        names = [ln.strip() for ln in listing.read_text().splitlines() if ln.strip()]
        for name in names:
            title = _md_title(vision_dir / "initiatives" / name) or Path(name).stem
            lines.append(f"- **{title}** — `{ops}/vision/initiatives/{name}`")

    if not lines:
        return f"_(No active initiatives — see `{ops}/backlog/` for open `kind: initiative` tasks.)_"
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
    # T-0724: watchrobot has a real frontend UNIT suite (node:test) that is a
    # separate command from e2e and gates the staging image build. Without a
    # field for it the only way to name it in AGENTS.md was a hand edit — i.e.
    # exactly the drift this field exists to stop.
    ("test_frontend_unit_cmd", "Frontend unit"),
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
    configured; otherwise stays generic (no @watchbot leaking everywhere).

    T-0726: the token location is config-driven (`tg_token_source`) and is
    SILENT when unset. Until 2026-08-18 this line told EVERY project its token
    was "in `.env` / `secrets.toml`", which is a guess the template cannot
    make — measured, neither half is universally true: watchrobot has no
    `secrets.toml` anywhere (`find -maxdepth 2` — empty) and reads
    `TELEGRAM_BOT_TOKEN` from `.env` (`backend/config.py:127`), while
    bot-squad has no project `.env` and reads `config/secrets.toml`. Naming a
    file that does not exist costs a reader a search that can only fail, so an
    unconfigured project now gets the bot name and no claim about where its
    secret lives.
    """
    bot = project.get("tg_bot")
    source = project.get("tg_token_source")
    if bot and source:
        intro = f"Bot `{bot}` (token: {source}). "
    elif bot:
        intro = f"Bot `{bot}`. "
    else:
        intro = ""
    return (
        f"{intro}Ping the stakeholder rarely — hard blockers, prod errors,\n"
        'finished long-running work — via `bsq tg ping "<message>"` (worker\n'
        "action `tg_notify`)."
    )


# ---------------------------------------------------------------------------
# Path resolvability gate (T-0726)
# ---------------------------------------------------------------------------

# The rendered AGENTS.md is a map, and until 2026-08-18 nothing ever walked it.
# `ops_path` had pointed watchrobot's clone symlink at `data/signal-tracker` —
# a directory renamed away on 2026-06-02 (T-0189) — so `ops/bot-squad` was a
# DANGLING SYMLINK for ~2.5 months while the every-turn file kept naming 23
# paths under it. Nothing reddened, because nothing touched it. Worse, a
# RELATIVE `ops_path` is only ever planted in the clone someone happened to
# plant it in: measured the same day, `~/watchrobot/master/ops/` and
# `~/watchrobot/deploy/ops/` held only `.gitignore`, so every one of those 23
# paths was dead for a session doing prod hotfixes in the master clone.
#
# Hence this predicate, and the shape it has:
#   * it follows symlinks, so a DANGLING one is "broken", not merely missing —
#     that is the failure that actually happened;
#   * a relative `ops_path` is checked in EVERY registered clone
#     (`repo_path`/`repo_master`/`repo_deploy`), because that is where the hole
#     was;
#   * "cannot read" is NOT "broken". `os.path.exists()` answers False for a
#     PermissionError just as it does for a deletion, and the registry holds a
#     project owned by another unix user (guestent, under /home/flomaster) — a
#     gate that cannot tell those apart reds on something no one can fix and is
#     then muted, which is how the 2.5 months happened in the first place.

_UNVERIFIABLE = "unverifiable"

# Statuses that mean the map lies. Anything else is fine or unknowable.
RED_STATUSES = ("missing", "broken")


def probe_path(path: str | Path) -> tuple[str, str]:
    """(status, detail) for one filesystem path.

    status is "ok" | "missing" | "broken" | "unverifiable".

    Deliberately NOT `os.path.exists()`: that swallows PermissionError and
    answers False, making another user's unreadable clone indistinguishable
    from a deleted directory. We stat explicitly and keep the errno.
    """
    path = os.fspath(path)
    try:
        os.stat(path)  # follows symlinks
        return "ok", ""
    except FileNotFoundError:
        # Either nothing is there, or a symlink is there pointing at nothing.
        try:
            os.lstat(path)
        except FileNotFoundError:
            return "missing", "no such path"
        except OSError as exc:
            return _UNVERIFIABLE, f"lstat: {exc.strerror}"
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return "broken", f"dangling symlink -> {target}"
    except OSError as exc:  # PermissionError, ELOOP, ENOTDIR, ...
        return _UNVERIFIABLE, f"stat: {exc.strerror}"


def check_project_paths(project: dict, slug: str) -> list[dict]:
    """Every path the rendered AGENTS.md promises, probed where it must resolve.

    Returns one finding dict per probe:
    `{slug, what, path, where, status, detail}`. Callers red on
    `status in RED_STATUSES`; `unverifiable` is reported and never fails.
    """
    findings: list[dict] = []
    ops = project.get("ops_path", "ops")

    def add(what, path, where, status, detail):
        findings.append({
            "slug": slug, "what": what, "path": str(path),
            "where": where, "status": status, "detail": detail,
        })

    if os.path.isabs(ops):
        # One absolute path, named identically from every clone.
        status, detail = probe_path(ops)
        add("ops_path", ops, "absolute", status, detail)
        return findings

    # Relative: it has to resolve in EVERY clone a session may be working in.
    for field in ("repo_path", "repo_master", "repo_deploy"):
        clone = project.get(field)
        if not clone:
            continue
        clone_status, clone_detail = probe_path(clone)
        if clone_status != "ok":
            # A clone that is absent or unreadable says nothing about ops_path.
            # repo_deploy in particular is created on the first deploy.
            add("ops_path", f"{clone}/{ops}", field, _UNVERIFIABLE,
                f"clone {clone_status}: {clone_detail}")
            continue
        status, detail = probe_path(Path(clone) / ops)
        add("ops_path", f"{clone}/{ops}", field, status, detail)

    return findings


def check_install_paths(install_root: str | Path = "/home/www/bot-squad") -> list[dict]:
    """The install-rooted scripts the rendered '## Deploy' section names."""
    findings: list[dict] = []
    for name in ("deploy.sh", "pause-deploys.sh", "resume-deploys.sh"):
        target = Path(install_root) / "scripts" / "cli" / name
        status, detail = probe_path(target)
        findings.append({
            "slug": "-", "what": "deploy shim", "path": str(target),
            "where": "install", "status": status, "detail": detail,
        })
    return findings


def format_findings(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        mark = {"ok": "ok  ", "missing": "RED ", "broken": "RED ",
                _UNVERIFIABLE: "??  "}.get(f["status"], "??  ")
        tail = f" — {f['detail']}" if f["detail"] else ""
        lines.append(
            f"{mark}{f['slug']:<12} {f['what']:<11} [{f['where']}] "
            f"{f['path']}{tail}"
        )
    return "\n".join(lines)


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

## Feedback is welcome and expected

Hit product friction, a confusing flow, a missing capability, or a broken
recipe? Run `bsq feedback submit "<note>"` to send it upstream to the
operator/stakeholder — no permission needed, and small notes are valuable.
It lands in `{ops}/feedback/`; it's how the process improves.

## Active initiatives — this cycle's bets

{initiatives_block}

(Open `kind: initiative` tasks live in `{ops}/backlog/`; a project still on
the pre-migration scheme also lists them in `{ops}/vision/active_initiatives`,
full texts under `{ops}/vision/initiatives/`.)

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

`$BOT_SQUAD/scripts/cli/deploy.sh <target> "<reason>"` — targets:
{deploy_targets_md} (`$BOT_SQUAD` = the bot-squad install root, default
`/home/www/bot-squad`).
- **Run it from inside `{repo_path}`.** The shim resolves the project from
  `$PWD` against `repo_path` in `projects.toml` — that field ONLY — so the
  same command from a master or deploy clone exits `CWD ... not in any
  registered project` before it queues anything (T-0726).
- Older docs named an in-clone `ops/…-bin/deploy` symlink. **The scaffold does
  not plant one** (`project_scaffold._link_ops` creates the `ops` data symlink
  and nothing else), so on any clone that lacks a hand-made copy that path is
  simply absent. Name the install path above instead — it resolves from every
  clone and cannot rot per-clone.
- Queues a deploy; the monitor picks it up within ~60s. TG-pings on
  success/failure. You don't manage the loop — commit, squash, request, walk away.
- **The clean-tree gate is PER TARGET — check yours before you wait on it.**
  bot-squad's `worker/bot_squad_worker/deploy.py` `is_clean_for_target` returns
  `True` UNCONDITIONALLY for a target whose recipe runs in a separate,
  origin-synced deploy clone (`Project.uses_deploy_clone(target)`: exec clone ≠
  the clone you edit). Deliberate — gating the shared clone HOL-blocked every
  team's deploy on one team's WIP (T-0225). For such a target a dirty tree,
  yours or a peer's, delays nothing and is no reason to wait or micro-commit.
- **A target that deploys IN PLACE is the one the gate is live for**: exec clone
  = the clone you edit, so `git status --porcelain` must be empty (logs/cache
  excluded) AND local commits not on origin BLOCK the run — the recipe's
  `git merge --ff-only` would wipe them. A project with neither `repo_deploy`
  nor `repo_master` in bot-squad's `config/projects.toml` deploys in place on
  EVERY target, so there the gate is live everywhere.
- **Where a deploy clone is used, the real constraint is PUBLICATION, not
  cleanliness.** The run force-syncs that clone to `origin/{deploy_branch}`, so
  an unpushed commit is simply OMITTED from the build — a log advisory names it,
  nothing fails. Ask "is it pushed?", never "is the tree clean?".
- **What actually got built is in the RUN LOG** (the recipe's own build-identity
  lines), NOT the job record's `target_sha` — that is an enqueue-time echo of a
  local ref resolved without a fetch, while the recipe re-fetches origin at
  build time (T-0699).

Hold deploys without killing the worker: `$BOT_SQUAD/scripts/cli/pause-deploys.sh
"<reason>"` writes a `PAUSED.json` marker the monitor honors — queued + new
deploys defer silently (one TG ping at pause, no per-tick spam, queue
preserved). `$BOT_SQUAD/scripts/cli/resume-deploys.sh` clears it and the next
tick runs. Per-project; use it to investigate, coordinate, or land a sensitive
multi-commit ship.

Manual prod release: stakeholder reviews staging → merges `{deploy_branch}`
into `{master_branch}` → builds the prod container → deploys. Agents never
deploy prod.

## Test commands

{test_commands}

## When you need more (not every-turn — read on demand)

- `{ops}/vision/team_protocol.md` — working conventions for every session
- `$BOT_SQUAD/api/app/resources/roles/` — role contracts (operator, teamlead,
  dev): the git SSOT every spawn brief reads (D-0043;
  `$BOT_SQUAD` = the bot-squad install root, default `/home/www/bot-squad`).
  NOT `{ops}/vision/roles/`, which is a scaffold-time display copy and drifts.
- `$BOT_SQUAD/api/app/resources/specs/` — framework specs no session is briefed
  from but shipped code cites as canonical: `role-hierarchy.md` (the six fixed
  user/permission roles) and `session-lifecycle-contract.md` (T-0730).
- `{ops}/backlog/` — all open work, one .md per task, incl. `kind: initiative`
  (discrete strategic bets; `{ops}/vision/initiatives/` is the legacy home
  for a project that hasn't migrated to task-based initiatives yet)
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
        repo_path=project.get("repo_path", ""),
        product_body=product_body,
        initiatives_block=render_active_initiatives(
            vision_dir, data_dir / slug / "backlog", ops
        ),
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
    parser.add_argument(
        "slug",
        nargs="?",
        help="Project slug (matches projects.toml key). Optional with "
             "--check-paths, which then walks the whole registry.",
    )
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
    parser.add_argument(
        "--check-paths",
        action="store_true",
        help="Probe every path the rendered AGENTS.md promises (`ops_path` in "
             "EVERY registered clone, plus the install's deploy shims) and "
             "exit 1 if any is missing or a dangling symlink. Renders and "
             "writes nothing. Omit the slug to walk the whole registry.",
    )
    parser.add_argument(
        "--install-root",
        default="/home/www/bot-squad",
        help="Install root the '## Deploy' block's $BOT_SQUAD expands to",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print the unified diff against the on-disk AGENTS.md and exit "
             "WITHOUT writing (the review artifact of a real run, without "
             "touching a shared working tree)",
    )
    args = parser.parse_args()

    config_dir = Path(args.config_dir)
    data_dir = Path(args.data_dir)

    if args.check_paths:
        try:
            projects = load_projects(config_dir)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.slug:
            if args.slug not in projects:
                print(f"error: slug '{args.slug}' not found in projects.toml",
                      file=sys.stderr)
                sys.exit(1)
            projects = {args.slug: projects[args.slug]}
        findings: list[dict] = []
        for slug, project in sorted(projects.items()):
            findings.extend(check_project_paths(project, slug))
        findings.extend(check_install_paths(args.install_root))
        print(format_findings(findings))
        red = [f for f in findings if f["status"] in RED_STATUSES]
        if red:
            print(f"\n{len(red)} unresolvable path(s) — AGENTS.md names paths "
                  f"that do not resolve.", file=sys.stderr)
            sys.exit(1)
        unknown = sum(1 for f in findings if f["status"] == _UNVERIFIABLE)
        print(f"\nAll {len(findings) - unknown} checked path(s) resolve"
              + (f"; {unknown} unverifiable (see ?? above)." if unknown else "."))
        return

    if not args.slug:
        parser.error("slug is required unless --check-paths is given")

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

    if args.diff:
        print(f"\n--diff: nothing written to {agents_md}")
        return

    agents_md.write_text(new_content)
    print(f"\nWritten: {agents_md}")


if __name__ == "__main__":
    main()
