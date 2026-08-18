"""T-0878 DoD 3 — the one-shot, per-install migration off the tracked registry.

Untracking ``config/projects.toml`` is not free on an install that already has
it: the deploy that ships the change carries a commit which DELETES that path,
so the very fast-forward doing the fix would take the live registry with it —
the failure this ticket exists to prevent, caused by the fix for it. The
bot-squad staging recipe therefore moves the live content aside, restores the
tracked copy so the tree is clean, lets the sync remove the now-untracked path,
and puts the live content back.

These tests execute the SHIPPED BYTES. The two blocks are extracted from
``deploy-recipes/bot-squad/staging.sh`` between the ``T-0878-MIGRATION-*``
markers and run against a real git install clone — a paraphrase of the shell in
this file would pass forever while the recipe rotted. If the markers disappear,
the extractor raises rather than skipping: a migration nobody is testing must
not look tested.

The environment around the blocks is the recipe's own — the same variables, the
same dirty guard, the same ``merge --ff-only`` — so what is being asserted is
that this migration works where it runs, not that a similar one could.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "deploy-recipes" / "bot-squad" / "staging.sh"
BRANCH = "bot_squad/dev"


def _extract(marker: str) -> str:
    text = RECIPE.read_text()
    m = re.search(
        rf"^# >>> {re.escape(marker)}.*?\n(.*?)^# <<< {re.escape(marker)}\s*$",
        text, flags=re.S | re.M,
    )
    assert m, (
        f"marker {marker} not found in {RECIPE} — the migration block was "
        "renamed or deleted, so this test would silently stop testing it"
    )
    body = m.group(1)
    assert body.strip(), f"marker {marker} wraps an empty block"
    return body


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


LIVE_REGISTRY = '''# bot-squad project registry.
[projects.bot-squad]
slug = "bot-squad"
repo_path = "/home/almdudleer/bot-squad/dev"

[projects.guestent]
slug = "guestent"
repo_path = "/home/flomaster/guestent/dev"
tg_chat = "-1004321"
'''

REPO_REGISTRY = '''# bot-squad project registry.
[projects.bot-squad]
slug = "bot-squad"
repo_path = "/home/almdudleer/bot-squad/dev"
'''


@pytest.fixture
def install(tmp_path: Path) -> Path:
    """An install dir in the PRE-fix state: config/projects.toml tracked, and
    carrying a project (`guestent`) that exists only on the install — the exact
    shape found on 2026-08-11."""
    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    (src / "worker").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(src)], check=True,
                   capture_output=True, text=True)
    _git(src, "config", "user.email", "t@t.com")
    _git(src, "config", "user.name", "T")
    _git(src, "checkout", "-q", "-b", BRANCH)
    (src / ".gitignore").write_text("data/\n")
    (src / "config" / "projects.toml").write_text(REPO_REGISTRY)
    (src / "worker" / "code.py").write_text("V = 1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "pre-fix: registry is tracked")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(origin)],
                   check=True, capture_output=True, text=True)

    inst = tmp_path / "install"
    subprocess.run(["git", "clone", "-q", "-b", BRANCH, str(origin), str(inst)],
                   check=True, capture_output=True, text=True)
    _git(inst, "config", "user.email", "t@t.com")
    _git(inst, "config", "user.name", "T")
    (inst / "data").mkdir()
    # The install registered guestent — live state, only here.
    (inst / "config" / "projects.toml").write_text(LIVE_REGISTRY)

    # The release being deployed is the one that untracks the registry.
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", "-b", BRANCH, str(origin), str(work)],
                   check=True, capture_output=True, text=True)
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "T")
    _git(work, "rm", "-q", "--cached", "config/projects.toml")
    (work / "config" / "projects.default.toml").write_text(REPO_REGISTRY)
    (work / ".gitignore").write_text("data/\nconfig/projects.toml\n")
    (work / "worker" / "code.py").write_text("V = 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "T-0878: untrack the live registry")
    _git(work, "push", "-q", "origin", BRANCH)
    return inst


def _driver(install: Path, tmp_path: Path, *, extra: str = "") -> str:
    """The recipe's own frame around the two extracted blocks."""
    return "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f'INSTALL_DIR={install}',
        f'DEPLOY_BRANCH="{BRANCH}"',
        'INSTALL_BRANCH=$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)',
        'git -C "$INSTALL_DIR" fetch -q origin',
        _extract("T-0878-MIGRATION-STASH"),
        # The recipe's dirty guard, verbatim from staging.sh step 2.
        "DIRTY=$(git -C \"$INSTALL_DIR\" status --porcelain | grep -v '^?? data/' || true)",
        'if [ -n "$DIRTY" ]; then',
        '    echo "DIRTY-GUARD-TRIPPED:"; echo "$DIRTY"; exit 8',
        'fi',
        extra,
        'git -C "$INSTALL_DIR" merge --ff-only "origin/$DEPLOY_BRANCH" >/dev/null',
        _extract("T-0878-MIGRATION-RESTORE"),
        'echo MIGRATION-OK',
    ])


def _run(script: str, tmp_path: Path) -> subprocess.CompletedProcess:
    path = tmp_path / "driver.sh"
    path.write_text(script)
    path.chmod(0o755)
    return subprocess.run(["bash", str(path)], capture_output=True, text=True)


def _registered(install: Path) -> set[str]:
    import tomllib
    live = install / "config" / "projects.toml"
    seed = install / "config" / "projects.default.toml"
    path = live if live.exists() else seed
    return set(tomllib.loads(path.read_text()).get("projects", {}))


# ---------------------------------------------------------------------------

def test_migration_keeps_the_install_only_project(install, tmp_path) -> None:
    """guestent existed nowhere but this install. After the deploy that untracks
    the registry it is still registered, byte for byte."""
    cp = _run(_driver(install, tmp_path), tmp_path)
    assert "MIGRATION-OK" in cp.stdout, cp.stdout + cp.stderr
    assert cp.returncode == 0

    assert (install / "config" / "projects.toml").read_text() == LIVE_REGISTRY
    assert "guestent" in _registered(install)
    # …and the release actually landed.
    assert (install / "worker" / "code.py").read_text() == "V = 2\n"


def test_migration_leaves_the_install_clean_for_the_NEXT_deploy(
    install, tmp_path
) -> None:
    """The whole point: the second deploy must not see what the first preserved.
    A stash parked in config/, or a restored file git can still see, would show
    up here as the same exit-8 refusal."""
    _run(_driver(install, tmp_path), tmp_path)

    dirty = _git(install, "status", "--porcelain")
    assert dirty.strip() == "", f"install still dirty after migration:\n{dirty}"
    tracked = _git(install, "ls-files", "config/").split()
    assert "config/projects.toml" not in tracked
    assert "config/projects.default.toml" in tracked


def test_migration_is_idempotent(install, tmp_path) -> None:
    """Every later deploy runs these same lines against an already-migrated
    install. They must be a no-op, not a second migration."""
    _run(_driver(install, tmp_path), tmp_path)
    before = (install / "config" / "projects.toml").read_text()

    cp = _run(_driver(install, tmp_path), tmp_path)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (install / "config" / "projects.toml").read_text() == before
    assert "guestent" in _registered(install)


def test_an_early_exit_inside_the_window_restores_the_registry(
    install, tmp_path
) -> None:
    """The window between the checkout and the restore is the only moment the
    live registry is not at its live content. If a guard fires there — and the
    dirty guard is exactly the kind that does — the live content has to come
    back, or the migration becomes the de-registration.

    Simulated with a hand-edit to tracked CODE, which is a legitimate exit-8
    that has nothing to do with the registry.
    """
    (install / "worker" / "code.py").write_text("V = 1  # someone's hand-edit\n")

    cp = _run(_driver(install, tmp_path), tmp_path)
    assert cp.returncode == 8, cp.stdout + cp.stderr
    assert "DIRTY-GUARD-TRIPPED" in cp.stdout
    assert "worker/code.py" in cp.stdout

    assert (install / "config" / "projects.toml").read_text() == LIVE_REGISTRY, (
        "the refused deploy left the registry reverted to the repo's version — "
        "guestent would be gone and nobody would know"
    )
    assert not list((install / "data").rglob("*t0878-migration*")), \
        "the stash was left behind"


def test_a_fresh_install_is_seeded_from_the_tracked_default(
    install, tmp_path
) -> None:
    """An install that has never registered anything has no live registry at
    all. The shell readers (scripts/cli/*.sh, scripts/hooks/*.sh) know only
    ``$BOT_SQUAD/config/projects.toml``, so a deploy has to leave one there.

    Driven as the second deploy on an already-migrated install with its live
    file removed — the only state in which the seed branch is reachable, and
    the reason this is a separate test rather than an extra assertion above.
    """
    _run(_driver(install, tmp_path), tmp_path)          # deploy 1: migrate
    (install / "config" / "projects.toml").unlink()      # never-registered

    cp = _run(_driver(install, tmp_path), tmp_path)      # deploy 2

    assert cp.returncode == 0, cp.stdout + cp.stderr
    live = install / "config" / "projects.toml"
    assert live.exists(), "no live registry was seeded"
    assert live.read_text() == REPO_REGISTRY
    # The seed's content is distinguishable from the pre-migration live content,
    # so this cannot be a restore wearing the seed's name.
    assert REPO_REGISTRY != LIVE_REGISTRY
    assert _git(install, "status", "--porcelain").strip() == ""
