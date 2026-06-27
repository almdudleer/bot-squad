"""Project scaffolding for T-0051's `+ New project` deep flow.

Three modes, summarised here and canonically documented in
`api/app/data/project-create-modes.md` (served at
`GET /api/projects/_/create-modes`):

1. `new_from_scratch`     — mother dir + dev/master clones + ops links
2. `attach_destructive`   — rename existing repo into mother dir (NOT
                            shipped in T-0051; peeled to follow-up)
3. `paths_as_they_are`    — mother dir + .bot-squad.toml + ops links
                            on already-existing dev/master clones

Each scaffold helper is best-effort transactional: pre-conditions are
checked up front, the mother dir is created last so it can be removed
on rollback if a later step fails. Existing user dirs (`repo_path`,
`repo_master`) are never deleted by scaffolding — ops symlinks plant
themselves only if `<clone>/ops` does not already exist.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ScaffoldError(Exception):
    """Pre-condition or step failure during project scaffolding."""


# T-0123 — canonical role mds bundled with the API image. Same pattern
# as T-0051's project-create-modes.md (`api/app/resources/...`); qa.md
# is excluded until T-0124 lands a canonical version.
_RESOURCES_DIR = Path(__file__).parent / "resources"
_VISION_ROLE_FILENAMES = (
    "operator.md",
    "teamlead.md",
    "dev.md",
    "prod-teamlead.md",
)


def _seed_vision_roles(install_data_dir: Path, slug: str) -> None:
    """Copy bundled role mds into ``<install_data_dir>/<slug>/vision/roles/``.

    T-0053's onboarding spotlight reads role definitions from the vision
    endpoint; freshly-created projects had an empty vision/ until this
    hook landed (the FE worked around it with condensed blurbs in
    ``web/src/onboarding/copy.ts``). A missing source md raises
    ScaffoldError so a packaging regression surfaces at create time, not
    at first `GET /api/projects/<slug>/vision`."""
    roles_src = _RESOURCES_DIR / "roles"
    roles_dst = install_data_dir / slug / "vision" / "roles"
    roles_dst.mkdir(parents=True, exist_ok=True)
    for name in _VISION_ROLE_FILENAMES:
        src = roles_src / name
        if not src.exists():
            raise ScaffoldError(f"bundled role md missing from API image: {src}")
        shutil.copyfile(src, roles_dst / name)


@dataclass(frozen=True)
class ScaffoldResult:
    """What the scaffold actually created, surfaced back to the API for
    the projects.toml writer and the FE confirmation pane."""
    repo_path: Path
    repo_master: Path
    repo_workspace: Path
    ops_linked: tuple[Path, ...]
    ops_skipped: tuple[Path, ...]


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.rename(tmp, path)


def _toml_escape(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def render_per_project_toml(
    slug: str,
    repo_path: Path,
    repo_master: Path,
    repo_workspace: Path,
) -> str:
    """Render <mother>/.bot-squad.toml. Hand-rolled so we don't drag in
    a TOML-writer dep (same pattern as routes_users._serialize_auth_toml
    and routes_projects._serialize_projects_toml)."""
    lines = [
        f"# bot-squad per-project config for {slug}. Written by the",
        "# project-create wizard (T-0051). Edit by hand only if you",
        "# know which clones moved.",
        "",
        f'slug           = "{_toml_escape(slug)}"',
        f'repo_path      = "{_toml_escape(repo_path)}"',
        f'repo_master    = "{_toml_escape(repo_master)}"',
        f'repo_workspace = "{_toml_escape(repo_workspace)}"',
        "",
    ]
    return "\n".join(lines)


def _link_ops(clone: Path, install_data_dir: Path, slug: str) -> tuple[bool, str]:
    """Plant `<clone>/ops -> <install_data_dir>/<slug>` symlink.

    Returns (linked, note). If `<clone>/ops` already exists (any kind),
    we leave it alone and return linked=False with a note explaining why
    — clobbering a real `ops/` dir (signal-tracker's layout has one with
    its own contents) would be much worse than skipping the convenience.
    """
    target = (install_data_dir / slug).resolve()
    link = clone / "ops"
    if link.exists() or link.is_symlink():
        return False, f"{link} already exists; left in place"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return True, f"{link} -> {target}"


# T-0502 — shared, git-ignored project memory dir. A `memory/` dir planted
# INSIDE each clone, shared by ALL sessions/users of the project (the dev tree
# is shared across linux users), git-IGNORED so its scratch never enters git
# history or ships in the product. See docs/architecture/D-0041 (the
# "Memory" substrate) for the read/write contract this scaffold realises.
_MEMORY_DIRNAME = "memory"
_MEMORY_INDEX_NAME = "MEMORY.md"


def _render_memory_index(slug: str) -> str:
    """The MEMORY.md index stub seeded into a fresh `memory/` dir.

    Mirrors D-0041: this dir is SHARED across sessions/users of the project,
    git-ignored, and disposable (never gated by review). Each entry is a
    one-line pointer to a fact file, same shape as the framework's own
    memory index."""
    return (
        f"# Project memory — {slug}\n"
        "\n"
        "Shared, git-IGNORED scratch for ALL sessions/users of this project\n"
        "(the dev tree is shared across linux users). Append decisions,\n"
        "findings, and gotchas here so the next session sees them; index each\n"
        "as a one-line pointer below. NOT shipped, NOT git-tracked, never\n"
        "gated by review. See docs/architecture/D-0041 (the Memory substrate).\n"
        "\n"
        "_(no entries yet)_\n"
    )


def _git_ignore_local(clone: Path, entry: str) -> None:
    """Idempotently add ``entry`` to the clone's LOCAL git exclude.

    Uses ``.git/info/exclude`` (per-clone, never committed) rather than the
    tracked ``.gitignore`` so we never modify a file the project owns — the
    git-ignore lands the same way the per-clone ``.claude/`` symlinks do.
    Best-effort: a worktree-pointer ``.git`` (a file, not a dir) or a
    non-repo clone is skipped silently — the memory dir still exists, it just
    isn't excluded, which is harmless for a fresh scaffold."""
    git_dir = clone / ".git"
    if not git_dir.is_dir():
        return
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text() if exclude.exists() else ""
    if entry in existing.splitlines():
        return
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with exclude.open("a") as fh:
        fh.write(f"{sep}{entry}\n")


def _seed_memory(clone: Path, slug: str) -> bool:
    """Plant a shared, git-ignored ``<clone>/memory/`` dir + MEMORY.md index.

    Returns True when a fresh index was seeded, False when an existing
    ``memory/MEMORY.md`` was preserved (we never clobber accumulated memory).
    The local git-exclude entry is (re)asserted idempotently either way so the
    dir stays out of git regardless of seed order."""
    mem_dir = clone / _MEMORY_DIRNAME
    index = mem_dir / _MEMORY_INDEX_NAME
    seeded = False
    if not index.exists():
        mem_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(index, _render_memory_index(slug))
        seeded = True
    _git_ignore_local(clone, f"/{_MEMORY_DIRNAME}/")
    return seeded


# T-0501 — project-level Claude settings + lifecycle hooks. voice-07: put the
# hooks at the PROJECT level (not user level) so wiring a project in gives it
# everything the lifecycle needs and non-bot-squad dirs stay untouched. The
# bot-squad clones today carry a hand-written `.claude/settings.json` whose
# hook commands are hardcoded to `/home/www/bot-squad/scripts/hooks/...`; we
# seed the same shape but with paths DERIVED from the install location so a
# non-default install is portable. The hooks themselves are the SSOT scripts
# shipped with the install — we only point each new clone at them.
_CLAUDE_DIRNAME = ".claude"
# (Claude Code hook event -> the install hook script that handles it.)
_LIFECYCLE_HOOKS = (
    ("SessionStart", "session_start.sh"),
    ("UserPromptSubmit", "user_prompt_submit.sh"),
    ("Stop", "stop.sh"),
)


def _install_root(install_data_dir: Path) -> Path:
    """The install root for an ``install_data_dir`` (``<root>/data``).

    Mirrors the worker's ``Config.data_dir = config_dir.parent / "data"`` and
    the hook's ``BOT_SQUAD=<root>`` default — the hook scripts + config live
    under ``<root>/{scripts,config}``."""
    return install_data_dir.parent


def _hooks_dir(install_data_dir: Path) -> Path:
    return _install_root(install_data_dir) / "scripts" / "hooks"


def _render_claude_settings(install_data_dir: Path) -> str:
    """The project-level ``.claude/settings.json`` body.

    Same shape as bot-squad's own committed settings (env flag, playwright MCP,
    the three lifecycle hooks, baseline tool permissions) but with the hook
    command paths + ``BOT_SQUAD`` env derived from the install location instead
    of hardcoded — so the hook resolves the project slug from cwd against the
    right install's ``config/projects.toml`` regardless of where it lives."""
    root = _install_root(install_data_dir)
    hooks_dir = _hooks_dir(install_data_dir)
    settings = {
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
            # The hook scripts read BOT_SQUAD to find config/ + data/; pin it to
            # THIS install so a non-default install path still resolves.
            "BOT_SQUAD": str(root),
        },
        "enabledMcpjsonServers": ["playwright"],
        "hooks": {
            event: [
                {"hooks": [{"type": "command", "command": str(hooks_dir / script)}]}
            ]
            for event, script in _LIFECYCLE_HOOKS
        },
        "permissions": {"allow": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]},
    }
    return json.dumps(settings, indent=2) + "\n"


def _render_claude_local_settings() -> str:
    """A minimal ``settings.local.json`` stub (per-clone, git-ignored).

    Kept tiny on purpose — Claude Code appends runtime grants here. We only
    seed the MCP enable so playwright is usable from a fresh clone; the allow
    list starts empty."""
    return json.dumps(
        {"enabledMcpjsonServers": ["playwright"], "permissions": {"allow": []}},
        indent=2,
    ) + "\n"


def _seed_claude(clone: Path, install_data_dir: Path) -> bool:
    """Seed ``<clone>/.claude/settings.json`` (+ ``settings.local.json``) with
    the project-level lifecycle hooks, git-ignored per-clone.

    Returns True when settings were freshly seeded, False when an existing
    ``settings.json`` was preserved — we never overwrite a project's own Claude
    config (voice-07: non-bot-squad dirs unaffected). The local git-exclude
    entry is asserted idempotently either way."""
    claude_dir = clone / _CLAUDE_DIRNAME
    settings = claude_dir / "settings.json"
    seeded = False
    if not settings.exists():
        claude_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(settings, _render_claude_settings(install_data_dir))
        local = claude_dir / "settings.local.json"
        if not local.exists():
            _atomic_write_text(local, _render_claude_local_settings())
        seeded = True
    _git_ignore_local(clone, f"/{_CLAUDE_DIRNAME}/")
    return seeded


def _run_git(cwd: Path, *args: str) -> None:
    """Run a git command, raising ScaffoldError with stderr on failure.

    Stays the simplest possible wrapper — T-0051 explicitly says no
    `git worktree`/`git clone --bare` cleverness."""
    try:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise ScaffoldError(f"git not found on PATH: {e}") from e
    except subprocess.CalledProcessError as e:
        raise ScaffoldError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"rc={e.returncode} stderr={e.stderr.strip()}"
        ) from e


def scaffold_paths_as_they_are(
    *,
    slug: str,
    mother_dir: Path,
    repo_path: Path,
    repo_master: Path,
    install_data_dir: Path,
) -> ScaffoldResult:
    """Mode 3 — paths-as-they-are.

    Pre-conditions: mother_dir does NOT exist; both repo_path and
    repo_master DO exist and look like git repos. We don't enforce
    "is a git checkout" strictly (a `.git` may be a worktree pointer)
    — only check the dir exists.

    Steps:
      1. Create mother_dir.
      2. Plant ops symlinks into both clones.
      3. Write <mother_dir>/.bot-squad.toml.
    """
    if mother_dir.exists():
        raise ScaffoldError(f"mother dir already exists: {mother_dir}")
    if not repo_path.exists():
        raise ScaffoldError(f"repo_path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise ScaffoldError(f"repo_path is not a directory: {repo_path}")
    if not repo_master.exists():
        raise ScaffoldError(f"repo_master does not exist: {repo_master}")
    if not repo_master.is_dir():
        raise ScaffoldError(f"repo_master is not a directory: {repo_master}")

    mother_dir.mkdir(parents=True)
    try:
        ops_linked: list[Path] = []
        ops_skipped: list[Path] = []
        for clone in (repo_path, repo_master):
            linked, _note = _link_ops(clone, install_data_dir, slug)
            (ops_linked if linked else ops_skipped).append(clone / "ops")
            _seed_memory(clone, slug)  # T-0502 shared git-ignored memory dir
            _seed_claude(clone, install_data_dir)  # T-0501 project-level Claude settings + hooks

        toml_text = render_per_project_toml(
            slug, repo_path, repo_master, mother_dir
        )
        _atomic_write_text(mother_dir / ".bot-squad.toml", toml_text)

        _seed_vision_roles(install_data_dir, slug)
    except Exception:
        # Best-effort rollback: remove anything we touched in mother_dir.
        # We deliberately do NOT unlink ops symlinks on the user's
        # existing clones — leaving them planted is harmless and avoids a
        # second failure during cleanup. Seeded vision/roles/ files under
        # `install_data_dir/<slug>/` are likewise left in place, matching
        # the ops-symlink policy (the per-slug data dir is owned by the
        # install, not by the scaffolder).
        shutil.rmtree(mother_dir, ignore_errors=True)
        raise

    return ScaffoldResult(
        repo_path=repo_path,
        repo_master=repo_master,
        repo_workspace=mother_dir,
        ops_linked=tuple(ops_linked),
        ops_skipped=tuple(ops_skipped),
    )


def scaffold_new_from_scratch(
    *,
    slug: str,
    mother_dir: Path,
    git_remote: str | None,
    install_data_dir: Path,
) -> ScaffoldResult:
    """Mode 1 — new from scratch.

    Pre-conditions: mother_dir does NOT exist.

    Steps:
      1. Create mother_dir.
      2. Create dev/ — `git init dev` OR `git clone <remote> dev`.
      3. Create master/ — `git clone <mother>/dev master`.
      4. Plant ops symlinks into both clones.
      5. Write <mother_dir>/.bot-squad.toml.

    On any failure after step 1 we rmtree the mother_dir so a retry
    starts clean (matching the DoD's "scaffold is idempotent at the
    boundary" expectation).
    """
    if mother_dir.exists():
        raise ScaffoldError(f"mother dir already exists: {mother_dir}")

    mother_dir.mkdir(parents=True)
    try:
        dev = mother_dir / "dev"
        master = mother_dir / "master"

        if git_remote:
            _run_git(mother_dir, "clone", git_remote, "dev")
        else:
            dev.mkdir()
            _run_git(dev, "init", "--quiet")
            # An empty `git init` has no HEAD yet, which makes the
            # follow-up `git clone <dev> master` fail with "remote HEAD
            # refers to nonexistent ref". Plant a single empty commit so
            # the clone has something to grab. This is the lightest
            # touch — no README, no .gitignore — leaves the project
            # owner free to set up the actual scaffolding.
            #
            # The `-c user.name/-c user.email` flags inline a bot
            # identity for this single commit so the scaffold works on
            # hosts (including the API docker container) that don't
            # have a global git identity configured.
            _run_git(
                dev,
                "-c", "user.name=bot-squad scaffold",
                "-c", "user.email=scaffold@bot-squad.local",
                "commit", "--allow-empty", "--quiet",
                "-m", f"chore: bot-squad scaffold ({slug})",
            )

        _run_git(mother_dir, "clone", "--quiet", str(dev), "master")

        ops_linked: list[Path] = []
        ops_skipped: list[Path] = []
        for clone in (dev, master):
            linked, _note = _link_ops(clone, install_data_dir, slug)
            (ops_linked if linked else ops_skipped).append(clone / "ops")
            _seed_memory(clone, slug)  # T-0502 shared git-ignored memory dir
            _seed_claude(clone, install_data_dir)  # T-0501 project-level Claude settings + hooks

        toml_text = render_per_project_toml(slug, dev, master, mother_dir)
        _atomic_write_text(mother_dir / ".bot-squad.toml", toml_text)

        _seed_vision_roles(install_data_dir, slug)
    except Exception:
        shutil.rmtree(mother_dir, ignore_errors=True)
        raise

    return ScaffoldResult(
        repo_path=dev,
        repo_master=master,
        repo_workspace=mother_dir,
        ops_linked=tuple(ops_linked),
        ops_skipped=tuple(ops_skipped),
    )


def scaffold_attach_destructive(
    *,
    slug: str,
    mother_dir: Path,
    existing: Path,
    existing_becomes: str,
    install_data_dir: Path,
) -> ScaffoldResult:
    """Mode 2 — destructive attach-move (T-0122).

    Pre-conditions: mother_dir does NOT exist; existing IS a directory;
    existing_becomes is "dev" or "master".

    Steps:
      1. Create mother_dir.
      2. ``os.rename(existing, mother_dir/<existing_becomes>)`` — atomic.
         Cross-filesystem moves (EXDEV) are refused with a clear pointer at
         Mode 3 (paths_as_they_are) as the alternative, rather than falling
         back to copy+delete (Mode 2 only makes sense as a true atomic move).
      3. ``git clone <renamed> <mother>/<other>`` for whichever side wasn't
         the renamed one — same wire shape as Mode 1's master-from-dev cut.
      4. Plant ops symlinks on both clones.
      5. Write ``<mother>/.bot-squad.toml``.

    On failure after the rename we attempt a best-effort ``os.rename`` back
    to ``existing`` so the user's repo isn't stranded inside a half-built
    mother dir. The whole mother dir is then ``rmtree``d so a retry starts
    clean (same boundary contract as Mode 1).
    """
    if existing_becomes not in ("dev", "master"):
        raise ScaffoldError(
            f"existing_becomes must be 'dev' or 'master'; got {existing_becomes!r}"
        )
    if mother_dir.exists():
        raise ScaffoldError(f"mother dir already exists: {mother_dir}")
    if not existing.exists():
        raise ScaffoldError(f"existing path does not exist: {existing}")
    if not existing.is_dir():
        raise ScaffoldError(f"existing path is not a directory: {existing}")

    renamed = mother_dir / existing_becomes
    other_name = "master" if existing_becomes == "dev" else "dev"

    mother_dir.mkdir(parents=True)
    rename_done = False
    try:
        try:
            os.rename(existing, renamed)
        except OSError as e:
            if getattr(e, "errno", None) == errno.EXDEV:
                raise ScaffoldError(
                    f"cannot move {existing} to {renamed}: cross-filesystem "
                    "rename is not supported by Mode 2 (atomic move requires "
                    "same device). Use Mode 3 ('paths_as_they_are') to attach "
                    "the existing clone in place instead."
                ) from e
            raise ScaffoldError(
                f"could not rename {existing} -> {renamed}: {e}"
            ) from e
        rename_done = True

        _run_git(mother_dir, "clone", "--quiet", str(renamed), other_name)

        dev = mother_dir / "dev"
        master = mother_dir / "master"
        ops_linked: list[Path] = []
        ops_skipped: list[Path] = []
        for clone in (dev, master):
            linked, _note = _link_ops(clone, install_data_dir, slug)
            (ops_linked if linked else ops_skipped).append(clone / "ops")
            _seed_memory(clone, slug)  # T-0502 shared git-ignored memory dir
            _seed_claude(clone, install_data_dir)  # T-0501 project-level Claude settings + hooks

        toml_text = render_per_project_toml(slug, dev, master, mother_dir)
        _atomic_write_text(mother_dir / ".bot-squad.toml", toml_text)
    except Exception:
        if rename_done:
            try:
                if renamed.exists() and not existing.exists():
                    os.rename(renamed, existing)
            except OSError:
                # Best-effort: if even the rollback fails the user can
                # still recover by hand from the rollback message the API
                # echoed before the destructive move was attempted.
                pass
        shutil.rmtree(mother_dir, ignore_errors=True)
        raise

    return ScaffoldResult(
        repo_path=dev,
        repo_master=master,
        repo_workspace=mother_dir,
        ops_linked=tuple(ops_linked),
        ops_skipped=tuple(ops_skipped),
    )
