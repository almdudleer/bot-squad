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

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ScaffoldError(Exception):
    """Pre-condition or step failure during project scaffolding."""


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

        toml_text = render_per_project_toml(
            slug, repo_path, repo_master, mother_dir
        )
        _atomic_write_text(mother_dir / ".bot-squad.toml", toml_text)
    except Exception:
        # Best-effort rollback: remove anything we touched in mother_dir.
        # We deliberately do NOT unlink ops symlinks on the user's
        # existing clones — leaving them planted is harmless and avoids a
        # second failure during cleanup.
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

        toml_text = render_per_project_toml(slug, dev, master, mother_dir)
        _atomic_write_text(mother_dir / ".bot-squad.toml", toml_text)
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
