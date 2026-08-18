"""T-0878 — registering a project must survive the deploy that follows it.

The defect, found on 2026-08-11 after the install had been undeployable for
five days: ``config/projects.toml`` was BOTH the live registry the install
mutates on every project registration AND a git-tracked file, while the
bot-squad staging recipe syncs the install with ``git merge --ff-only
origin/<branch>`` straight into the install dir. From the moment a project was
registered there were exactly two outcomes, and both were bad:

1. the recipe's dirty-install guard fired (``exit 8``) and every deploy was
   refused — measured live at 2026-08-06 08:27 and 2026-08-11 14:23; or
2. without that guard, the fast-forward would overwrite the file with the
   repo's version and **silently de-register the project**.

What is pinned here is the PROPERTY the ticket names, not the file layout:
*register a project, run the deploy's ff-merge against a newer commit, and the
project is still registered afterwards.* The layout could change again; this
must not.

Every test drives real artifacts:

- the repo's OWN ``.gitignore`` and its OWN tracked ``config/`` file list, read
  off disk — so re-tracking the live registry, or dropping the ignore line,
  turns these red without anyone editing this file;
- the real writer, ``routes_projects._write_projects_toml`` — the single
  function both ``POST /api/projects`` and ``PUT /api/projects/{slug}/tg``
  write through;
- the real reader, ``ApiConfig.load``;
- real git, running the exact guard and merge commands the recipe runs.

``test_NEGATIVE_CONTROL_*`` rebuilds the pre-fix arrangement — the same driver,
the same writer, the same reader, with the registry tracked — and asserts the
property FAILS there. Without it a green suite would only prove the driver
runs, not that it can tell the two arrangements apart.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import registry  # noqa: E402
from app.config import ApiConfig  # noqa: E402
from app.routes_projects import _write_projects_toml  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Real git plumbing — a bare origin plus an "install dir" clone of it
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


BRANCH = "bot_squad/dev"


def _project_block(slug: str, repo_path: str = "/tmp/x") -> dict:
    """A registry entry in the shape the real serializer emits."""
    return {
        "slug": slug,
        "display_name": slug.title(),
        "repo_path": repo_path,
        "deploy_branch": BRANCH,
        "master_branch": "master",
        "prod_url": "",
        "staging_url": "",
        "dev_url": "",
        "deploy_targets": ["staging"],
        "tg_chat": "-100123",
    }


def _seed_content() -> str:
    """The registry body a fresh install starts from, in the real serializer's
    format. Content is irrelevant to the property; the shape is not."""
    from app.routes_projects import _serialize_projects_toml
    return _serialize_projects_toml({"bot-squad": _project_block("bot-squad")})


def _make_install(tmp_path: Path, *, tracked_registry: bool) -> tuple[Path, Path, Path]:
    """A bare origin + an install clone at commit 1.

    ``tracked_registry=False`` is the arrangement this repo actually ships:
    ``config/projects.toml`` git-ignored, ``config/projects.default.toml``
    tracked as the seed. ``True`` reproduces the pre-T-0878 arrangement.

    Returns ``(origin, install, config_dir)``.
    """
    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    (src / "api" / "app").mkdir(parents=True)
    _git_init(src)

    # The repo's REAL ignore rules, so the assertion tracks the shipped file
    # rather than a restatement of it.
    (src / ".gitignore").write_text((REPO / ".gitignore").read_text())
    (src / "api" / "app" / "code.py").write_text("VERSION = 1\n")

    if tracked_registry:
        # Pre-fix: the live registry itself is the tracked file. Neutralise the
        # repo's ignore line so this arrangement is reproducible at all.
        (src / ".gitignore").write_text(
            (REPO / ".gitignore").read_text().replace(
                "config/projects.toml", "# (un-ignored for the negative control)"
            )
        )
        (src / "config" / "projects.toml").write_text(_seed_content())
    else:
        (src / "config" / registry.DEFAULT_NAME).write_text(_seed_content())

    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "commit 1")
    _git(src, "branch", "-M", BRANCH)

    origin = tmp_path / "origin.git"
    _git(src, "clone", "--bare", "-q", str(src), str(origin))

    install = tmp_path / "install"
    subprocess.run(["git", "clone", "-q", "-b", BRANCH, str(origin), str(install)],
                   check=True, capture_output=True, text=True)
    _git_identity(install)
    return origin, install, install / "config"


def _git_init(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git_identity(repo)


def _git_identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "t0878@example.com")
    _git(repo, "config", "user.name", "T-0878")


def _push_newer_commit(tmp_path: Path, origin: Path) -> None:
    """A second commit on origin touching only tracked CODE — what any ordinary
    release is, from the registry's point of view."""
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", "-b", BRANCH, str(origin), str(work)],
                   check=True, capture_output=True, text=True)
    _git_identity(work)
    (work / "api" / "app" / "code.py").write_text("VERSION = 2\n")
    _git(work, "commit", "-q", "-am", "commit 2")
    _git(work, "push", "-q", "origin", BRANCH)


# ---------------------------------------------------------------------------
# The recipe's own two steps, run verbatim
# ---------------------------------------------------------------------------

def _dirty_guard(install: Path) -> str:
    """``git status --porcelain | grep -v '^?? data/'`` from staging.sh step 2.
    Non-empty means the recipe exits 8."""
    out = subprocess.run(
        ["git", "-C", str(install), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    return "\n".join(
        ln for ln in out.splitlines() if ln and not ln.startswith("?? data/")
    )


def _ff_merge(install: Path) -> subprocess.CompletedProcess:
    subprocess.run(["git", "-C", str(install), "fetch", "-q", "origin"],
                   check=True, capture_output=True, text=True)
    return subprocess.run(
        ["git", "-C", str(install), "merge", "--ff-only", f"origin/{BRANCH}"],
        capture_output=True, text=True,
    )


def _registered(config_dir: Path) -> set[str]:
    """Whatever the API would serve as the registry, right now."""
    return set(ApiConfig.load(config_dir).projects)


def _register(config_dir: Path, slug: str) -> None:
    """Register a project the way the API does — the real writer, not a fixture."""
    from app.routes_projects import _read_projects_toml
    raw = _read_projects_toml(config_dir)
    raw[slug] = _project_block(slug, repo_path=f"/tmp/{slug}")
    _write_projects_toml(config_dir, raw)


# ---------------------------------------------------------------------------
# THE property
# ---------------------------------------------------------------------------

def test_registration_survives_the_deploys_ff_merge(tmp_path: Path) -> None:
    """Register, deploy, still registered. The whole ticket in one test."""
    origin, install, config_dir = _make_install(tmp_path, tracked_registry=False)

    _register(config_dir, "guestent")
    assert "guestent" in _registered(config_dir)

    _push_newer_commit(tmp_path, origin)

    assert _dirty_guard(install) == "", (
        "registering a project made the install dirty — the deploy would exit 8"
    )
    merge = _ff_merge(install)
    assert merge.returncode == 0, merge.stderr

    assert "guestent" in _registered(config_dir), (
        "the deploy's fast-forward de-registered a live project"
    )
    # And the release actually landed — otherwise a merge that did nothing
    # would pass this test.
    assert (install / "api" / "app" / "code.py").read_text() == "VERSION = 2\n"


def test_git_checkout_can_no_longer_delete_a_live_project(tmp_path: Path) -> None:
    """The property the ticket names in as many words: *`git checkout` on the
    install must stop being able to delete a live project.*

    Both blunt instruments are run — the whole-tree revert (the first instinct
    on reading the dirty guard's file list, and precisely the action that used
    to destroy the registration) and the hard reset to origin, which is what a
    human reaches for when the revert is not enough.
    """
    origin, install, config_dir = _make_install(tmp_path, tracked_registry=False)
    _register(config_dir, "guestent")
    _push_newer_commit(tmp_path, origin)

    _git(install, "checkout", "--", ".")
    assert "guestent" in _registered(config_dir), "a whole-tree revert wiped it"

    subprocess.run(["git", "-C", str(install), "fetch", "-q", "origin"],
                   check=True, capture_output=True, text=True)
    _git(install, "reset", "--hard", f"origin/{BRANCH}")
    assert "guestent" in _registered(config_dir), "a hard reset to origin wiped it"


def test_NEGATIVE_CONTROL_tracked_registry_loses_the_project(tmp_path: Path) -> None:
    """The same driver against the PRE-FIX arrangement must fail the property.

    Both historical outcomes count as a failure, and which one you get depends
    only on whether the guard is present: the tree goes dirty (recipe exit 8,
    no deploy for five days) or the fast-forward wins and the project is gone.
    Asserting "one of the two" rather than picking one is deliberate — pinning
    a single outcome would pin the guard, and the guard is not the thing under
    test.
    """
    origin, install, config_dir = _make_install(tmp_path, tracked_registry=True)

    _register(config_dir, "guestent")
    assert "guestent" in _registered(config_dir)

    _push_newer_commit(tmp_path, origin)

    # Outcome 1 — the registration itself makes the install dirty, so the
    # recipe's guard refuses. This is what actually happened, twice.
    dirty = _dirty_guard(install)
    assert f"config/{registry.LIVE_NAME}" in dirty, (
        "the pre-fix arrangement was expected to leave the install dirty; if it "
        "does not, this control no longer reproduces the defect and the tests "
        "above prove nothing"
    )

    # Outcome 2 — clearing that dirtiness the obvious way destroys the
    # registration. Same command as the positive test above, opposite result.
    _git(install, "checkout", "--", ".")
    assert "guestent" not in _registered(config_dir), (
        "the pre-fix arrangement was expected to lose the registration to a "
        "plain `git checkout`"
    )


def test_writer_never_touches_a_tracked_path(tmp_path: Path) -> None:
    """DoD 1, checked at the source rather than through its consequence: after a
    registration, git has nothing to say about the file that changed."""
    _origin, install, config_dir = _make_install(tmp_path, tracked_registry=False)

    _register(config_dir, "guestent")

    assert (config_dir / registry.LIVE_NAME).exists()
    tracked = subprocess.run(
        ["git", "-C", str(install), "ls-files", "config/"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert f"config/{registry.LIVE_NAME}" not in tracked
    assert f"config/{registry.DEFAULT_NAME}" in tracked
    assert _dirty_guard(install) == ""


def test_dirty_guard_still_fails_closed_on_tracked_code(tmp_path: Path) -> None:
    """DoD 4. The guard is the only thing that caught this, and the tempting
    wrong fix was to widen it to skip ``config/``. A hand-edit to tracked code —
    and to a tracked file inside ``config/`` — must still trip it."""
    _origin, install, _config_dir = _make_install(tmp_path, tracked_registry=False)

    (install / "api" / "app" / "code.py").write_text("VERSION = 1  # hand-edit\n")
    assert "api/app/code.py" in _dirty_guard(install)

    (install / "api" / "app" / "code.py").write_text("VERSION = 1\n")
    assert _dirty_guard(install) == ""

    (install / "config" / registry.DEFAULT_NAME).write_text("# hand-edited seed\n")
    assert f"config/{registry.DEFAULT_NAME}" in _dirty_guard(install)


# ---------------------------------------------------------------------------
# Seed behaviour — a fresh install, and the promise that seeding never loses
# ---------------------------------------------------------------------------

def test_fresh_install_reads_the_tracked_seed(tmp_path: Path) -> None:
    _origin, _install, config_dir = _make_install(tmp_path, tracked_registry=False)
    assert not (config_dir / registry.LIVE_NAME).exists()
    assert _registered(config_dir) == {"bot-squad"}


def test_load_materialises_the_live_file_for_the_shell_readers(tmp_path: Path) -> None:
    """``scripts/cli/*.sh`` and ``scripts/hooks/*.sh`` know only
    ``$BOT_SQUAD/config/projects.toml``. Loading seeds it so they keep working
    on an install that has never registered anything."""
    _origin, install, config_dir = _make_install(tmp_path, tracked_registry=False)

    ApiConfig.load(config_dir)

    live = config_dir / registry.LIVE_NAME
    assert live.exists()
    assert live.read_text() == (config_dir / registry.DEFAULT_NAME).read_text()
    # …and the seeded file is invisible to git, or we'd have re-created the bug.
    assert _dirty_guard(install) == ""


def test_first_registration_keeps_the_seeded_projects(tmp_path: Path) -> None:
    """Registering on a never-registered install must not drop the seed's
    projects — the live file is a REPLACEMENT for the seed, not a delta."""
    _origin, _install, config_dir = _make_install(tmp_path, tracked_registry=False)

    _register(config_dir, "guestent")

    assert _registered(config_dir) == {"bot-squad", "guestent"}


def test_live_file_wins_over_the_seed(tmp_path: Path) -> None:
    """Once an install owns a registry, the tracked seed is dead to it — a repo
    edit must not reach in and change a running install."""
    _origin, _install, config_dir = _make_install(tmp_path, tracked_registry=False)
    _register(config_dir, "guestent")

    from app.routes_projects import _serialize_projects_toml
    (config_dir / registry.DEFAULT_NAME).write_text(
        _serialize_projects_toml({"from-the-repo": _project_block("from-the-repo")})
    )

    assert _registered(config_dir) == {"bot-squad", "guestent"}


# ---------------------------------------------------------------------------
# The shipped repo itself
# ---------------------------------------------------------------------------

def test_this_repo_ignores_the_live_registry_and_tracks_the_seed() -> None:
    """The arrangement every test above assumes, asserted against the real repo.
    This is what goes red if someone re-tracks ``config/projects.toml``."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(
        # safe.directory: the suite runs in a container as a different uid than
        # the tree's owner, where git otherwise refuses EVERY command with
        # "dubious ownership" — and a returncode check would then read that as
        # "not a checkout" and SKIP, i.e. the one assertion pinning the shipped
        # arrangement would go quietly vacuous.
        ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
         "ls-files", "config/"],
        capture_output=True, text=True,
    )
    assert tracked.returncode == 0, tracked.stderr
    names = tracked.stdout.split()
    assert f"config/{registry.DEFAULT_NAME}" in names
    assert f"config/{registry.LIVE_NAME}" not in names

    ignored = subprocess.run(
        ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
         "check-ignore", "-q", f"config/{registry.LIVE_NAME}"],
        capture_output=True, text=True,
    )
    for tmp in (f"config/{registry.LIVE_NAME}.tmp",
                f"config/{registry.LIVE_NAME}.seed.1.tmp"):
        # The atomic-write temporaries live for microseconds, but the deploy's
        # dirty check runs once a minute and would call one somebody's stray
        # edit — the same exit-8 refusal, from the fix itself.
        r = subprocess.run(
            ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
             "check-ignore", "-q", tmp],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{tmp} is not git-ignored"
    # …and the tracked seed must NOT be caught by that wildcard.
    seed_ignored = subprocess.run(
        ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
         "check-ignore", "-q", f"config/{registry.DEFAULT_NAME}"],
        capture_output=True, text=True,
    )
    assert seed_ignored.returncode != 0, "the tracked seed is git-ignored"

    assert ignored.returncode == 0, (
        "config/projects.toml is not git-ignored — an install that registers a "
        "project would show it as an untracked file and trip the deploy's "
        "dirty guard all over again"
    )


def test_registry_mirror_is_byte_identical() -> None:
    """``api/app/registry.py`` is a MIRROR of
    ``worker/bot_squad_worker/registry.py`` — separate runtimes, no shared
    import, so byte-identity is the anti-drift contract (the ``task_search.py``
    / ``idalloc.py`` precedent). A resolver that answers differently on the two
    sides of the socket is exactly how a registry ends up in two places again.
    """
    api_copy = Path(__file__).resolve().parents[1] / "app" / "registry.py"
    candidates = [
        REPO / "worker" / "bot_squad_worker" / "registry.py",
        Path("/worker/bot_squad_worker/registry.py"),
    ]
    worker_copy = next((p for p in candidates if p.exists()), None)
    if worker_copy is None:
        pytest.skip("worker tree not available in this environment")
    assert worker_copy.read_bytes() == api_copy.read_bytes(), (
        "worker/bot_squad_worker/registry.py and api/app/registry.py have "
        "drifted — re-sync them (they must stay byte-identical)."
    )
