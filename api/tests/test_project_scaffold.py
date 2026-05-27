"""T-0051 / T-0122 — direct unit tests for project_scaffold helpers.

These don't spin up the FastAPI app — they exercise the scaffolders
against a tmp filesystem so the route tests can focus on the
HTTP-layer contract and not the on-disk choreography.
"""
from __future__ import annotations

import errno
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from app.project_scaffold import (
    ScaffoldError,
    scaffold_attach_destructive,
    scaffold_new_from_scratch,
    scaffold_paths_as_they_are,
)


def _git_init(repo: Path) -> None:
    """Create a minimal git repo at `repo` (no symlink targets, no
    network) for use as a Mode-3 attach target."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "--quiet", "-m", "init"],
        cwd=repo, check=True,
    )


# ---------------------------------------------------------------------------
# Mode 3 — paths_as_they_are
# ---------------------------------------------------------------------------


def test_paths_as_they_are_happy_path(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    (install_data / "myproj").mkdir(parents=True)

    existing_dev = tmp_path / "elsewhere" / "myproj-dev"
    existing_master = tmp_path / "elsewhere" / "myproj-master"
    _git_init(existing_dev)
    _git_init(existing_master)

    mother = tmp_path / "home" / "myproj"
    result = scaffold_paths_as_they_are(
        slug="myproj",
        mother_dir=mother,
        repo_path=existing_dev,
        repo_master=existing_master,
        install_data_dir=install_data,
    )

    assert mother.is_dir()
    assert result.repo_path == existing_dev
    assert result.repo_master == existing_master
    assert result.repo_workspace == mother

    # Ops symlinks planted on both clones, pointing at the per-slug
    # dir under the install's data root.
    for clone in (existing_dev, existing_master):
        link = clone / "ops"
        assert link.is_symlink(), f"{link} not a symlink"
        assert link.resolve() == (install_data / "myproj").resolve()
    assert set(result.ops_linked) == {existing_dev / "ops", existing_master / "ops"}
    assert result.ops_skipped == ()

    # Per-project config written, atomic-rename leaves no .tmp.
    cfg_path = mother / ".bot-squad.toml"
    assert cfg_path.is_file()
    assert not (mother / ".bot-squad.toml.tmp").exists()
    raw = tomllib.loads(cfg_path.read_text())
    assert raw["slug"] == "myproj"
    assert raw["repo_path"] == str(existing_dev)
    assert raw["repo_master"] == str(existing_master)
    assert raw["repo_workspace"] == str(mother)


def test_paths_as_they_are_mother_exists_aborts(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    (install_data / "myproj").mkdir(parents=True)
    existing_dev = tmp_path / "dev"
    existing_master = tmp_path / "master"
    _git_init(existing_dev)
    _git_init(existing_master)

    mother = tmp_path / "home" / "myproj"
    mother.mkdir(parents=True)

    with pytest.raises(ScaffoldError, match="mother dir already exists"):
        scaffold_paths_as_they_are(
            slug="myproj",
            mother_dir=mother,
            repo_path=existing_dev,
            repo_master=existing_master,
            install_data_dir=install_data,
        )


def test_paths_as_they_are_repo_path_missing_aborts(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    existing_master = tmp_path / "master"
    _git_init(existing_master)

    mother = tmp_path / "home" / "myproj"
    with pytest.raises(ScaffoldError, match="repo_path does not exist"):
        scaffold_paths_as_they_are(
            slug="myproj",
            mother_dir=mother,
            repo_path=tmp_path / "no-such-dir",
            repo_master=existing_master,
            install_data_dir=install_data,
        )
    # Mother dir must NOT exist after a pre-condition failure.
    assert not mother.exists()


def test_paths_as_they_are_existing_ops_dir_is_preserved(tmp_path: Path) -> None:
    """signal-tracker's clone has a real, tracked `ops/` dir. The
    scaffold must NOT clobber it — just record the skip and move on."""
    install_data = tmp_path / "install" / "data"
    (install_data / "myproj").mkdir(parents=True)

    existing_dev = tmp_path / "elsewhere" / "dev"
    existing_master = tmp_path / "elsewhere" / "master"
    _git_init(existing_dev)
    _git_init(existing_master)
    (existing_dev / "ops").mkdir()
    (existing_dev / "ops" / "marker.txt").write_text("user content")

    mother = tmp_path / "home" / "myproj"
    result = scaffold_paths_as_they_are(
        slug="myproj",
        mother_dir=mother,
        repo_path=existing_dev,
        repo_master=existing_master,
        install_data_dir=install_data,
    )

    # The user's tracked ops/ dir is untouched (still a dir, file kept).
    assert (existing_dev / "ops").is_dir()
    assert not (existing_dev / "ops").is_symlink()
    assert (existing_dev / "ops" / "marker.txt").read_text() == "user content"
    assert (existing_dev / "ops") in result.ops_skipped

    # The master side, with no prior ops, gets the symlink.
    assert (existing_master / "ops").is_symlink()
    assert (existing_master / "ops") in result.ops_linked


# ---------------------------------------------------------------------------
# Mode 1 — new_from_scratch
# ---------------------------------------------------------------------------


def test_new_from_scratch_local_init(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    (install_data / "fresh").mkdir(parents=True)
    mother = tmp_path / "home" / "fresh"

    result = scaffold_new_from_scratch(
        slug="fresh",
        mother_dir=mother,
        git_remote=None,
        install_data_dir=install_data,
    )

    dev = mother / "dev"
    master = mother / "master"
    assert dev.is_dir() and (dev / ".git").exists()
    assert master.is_dir() and (master / ".git").exists()

    # Both clones got ops symlinks (fresh clones have no prior ops/).
    for clone in (dev, master):
        link = clone / "ops"
        assert link.is_symlink()
        assert link.resolve() == (install_data / "fresh").resolve()
    assert set(result.ops_linked) == {dev / "ops", master / "ops"}
    assert result.ops_skipped == ()

    # The per-project config records what we just created.
    raw = tomllib.loads((mother / ".bot-squad.toml").read_text())
    assert raw["repo_path"] == str(dev)
    assert raw["repo_master"] == str(master)
    assert raw["repo_workspace"] == str(mother)

    # master is a real clone of dev, so it has a remote pointing at dev.
    remotes = subprocess.run(
        ["git", "-C", str(master), "remote", "-v"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert str(dev) in remotes


def test_new_from_scratch_mother_exists_aborts(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    (install_data / "fresh").mkdir(parents=True)
    mother = tmp_path / "home" / "fresh"
    mother.mkdir(parents=True)

    with pytest.raises(ScaffoldError, match="mother dir already exists"):
        scaffold_new_from_scratch(
            slug="fresh",
            mother_dir=mother,
            git_remote=None,
            install_data_dir=install_data,
        )


# ---------------------------------------------------------------------------
# Mode 2 — attach_destructive (T-0122)
# ---------------------------------------------------------------------------


def test_attach_destructive_existing_becomes_dev_cuts_master(tmp_path: Path) -> None:
    """Happy path: the user's repo becomes <mother>/dev, master is cloned
    from it. Both clones land with ops symlinks + a per-project toml."""
    install_data = tmp_path / "install" / "data"
    (install_data / "proj").mkdir(parents=True)

    existing = tmp_path / "elsewhere" / "proj-source"
    _git_init(existing)
    mother = tmp_path / "home" / "proj"

    result = scaffold_attach_destructive(
        slug="proj",
        mother_dir=mother,
        existing=existing,
        existing_becomes="dev",
        install_data_dir=install_data,
    )

    dev = mother / "dev"
    master = mother / "master"
    assert dev.is_dir() and (dev / ".git").exists()
    assert master.is_dir() and (master / ".git").exists()
    # The original existing path is gone — the rename is destructive.
    assert not existing.exists()

    assert result.repo_path == dev
    assert result.repo_master == master
    assert result.repo_workspace == mother

    for clone in (dev, master):
        link = clone / "ops"
        assert link.is_symlink()
        assert link.resolve() == (install_data / "proj").resolve()
    assert set(result.ops_linked) == {dev / "ops", master / "ops"}

    raw = tomllib.loads((mother / ".bot-squad.toml").read_text())
    assert raw["repo_path"] == str(dev)
    assert raw["repo_master"] == str(master)
    assert raw["repo_workspace"] == str(mother)

    # master was cloned from dev → it has a remote pointing at dev.
    remotes = subprocess.run(
        ["git", "-C", str(master), "remote", "-v"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert str(dev) in remotes


def test_attach_destructive_existing_becomes_master_cuts_dev(tmp_path: Path) -> None:
    """Symmetric branch: the user's repo becomes <mother>/master, dev is
    cloned from it."""
    install_data = tmp_path / "install" / "data"
    (install_data / "proj").mkdir(parents=True)

    existing = tmp_path / "elsewhere" / "proj-source"
    _git_init(existing)
    mother = tmp_path / "home" / "proj"

    result = scaffold_attach_destructive(
        slug="proj",
        mother_dir=mother,
        existing=existing,
        existing_becomes="master",
        install_data_dir=install_data,
    )

    dev = mother / "dev"
    master = mother / "master"
    assert dev.is_dir() and (dev / ".git").exists()
    assert master.is_dir() and (master / ".git").exists()
    assert not existing.exists()

    # dev was cloned from master → its remote points at master.
    remotes = subprocess.run(
        ["git", "-C", str(dev), "remote", "-v"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert str(master) in remotes

    assert result.repo_path == dev
    assert result.repo_master == master


def test_attach_destructive_mother_exists_aborts(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    existing = tmp_path / "src"
    _git_init(existing)
    mother = tmp_path / "home" / "proj"
    mother.mkdir(parents=True)

    with pytest.raises(ScaffoldError, match="mother dir already exists"):
        scaffold_attach_destructive(
            slug="proj",
            mother_dir=mother,
            existing=existing,
            existing_becomes="dev",
            install_data_dir=install_data,
        )
    # Existing repo untouched.
    assert existing.is_dir()


def test_attach_destructive_existing_missing_aborts(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    mother = tmp_path / "home" / "proj"

    with pytest.raises(ScaffoldError, match="existing path does not exist"):
        scaffold_attach_destructive(
            slug="proj",
            mother_dir=mother,
            existing=tmp_path / "no-such-dir",
            existing_becomes="dev",
            install_data_dir=install_data,
        )
    # Pre-condition failure must not leave a half-built mother.
    assert not mother.exists()


def test_attach_destructive_bad_existing_becomes_aborts(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    existing = tmp_path / "src"
    _git_init(existing)
    mother = tmp_path / "home" / "proj"

    with pytest.raises(ScaffoldError, match="existing_becomes"):
        scaffold_attach_destructive(
            slug="proj",
            mother_dir=mother,
            existing=existing,
            existing_becomes="trunk",  # not dev/master
            install_data_dir=install_data,
        )
    assert existing.is_dir()
    assert not mother.exists()


def test_attach_destructive_cross_fs_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.rename`` raises EXDEV when src/dst are on different devices.
    Mode 2 only makes sense as a true atomic move — we refuse with a
    clear pointer at Mode 3 instead of falling back to copy+delete."""
    install_data = tmp_path / "install" / "data"
    existing = tmp_path / "elsewhere" / "src"
    _git_init(existing)
    mother = tmp_path / "home" / "proj"

    real_rename = os.rename

    def fake_rename(src, dst):
        # Only the destructive move fails EXDEV — anything else (e.g. the
        # atomic .toml writer's tmp→final rename, which is never reached
        # here anyway) defers to the real impl.
        if str(src) == str(existing):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr("app.project_scaffold.os.rename", fake_rename)

    with pytest.raises(ScaffoldError) as exc:
        scaffold_attach_destructive(
            slug="proj",
            mother_dir=mother,
            existing=existing,
            existing_becomes="dev",
            install_data_dir=install_data,
        )
    msg = str(exc.value)
    assert "cross-filesystem" in msg
    assert "paths_as_they_are" in msg  # points at Mode 3 as the alternative
    # Rollback contract: existing repo still there, mother dir cleaned up.
    assert existing.is_dir()
    assert not mother.exists()


def test_new_from_scratch_bad_remote_rolls_back(tmp_path: Path) -> None:
    """A git clone failure (bogus remote URL) must clean up the
    mother dir so a retry starts from scratch."""
    install_data = tmp_path / "install" / "data"
    (install_data / "fresh").mkdir(parents=True)
    mother = tmp_path / "home" / "fresh"

    bogus = tmp_path / "no-such-remote"
    with pytest.raises(ScaffoldError, match="git clone"):
        scaffold_new_from_scratch(
            slug="fresh",
            mother_dir=mother,
            git_remote=str(bogus),
            install_data_dir=install_data,
        )
    assert not mother.exists()


# ---------------------------------------------------------------------------
# T-0123 — vision/roles seeding
# ---------------------------------------------------------------------------

# Mirrors `_VISION_ROLE_FILENAMES` in app.project_scaffold. Kept inline so a
# drift between the bundled set and the assertions is a test-edit, not a
# silent skip.
_EXPECTED_ROLES = ("operator.md", "teamlead.md", "dev.md", "prod-teamlead.md")


def _assert_roles_seeded(install_data: Path, slug: str) -> None:
    """The four canonical role mds land under
    ``<install_data>/<slug>/vision/roles/`` with non-empty content.
    qa.md is intentionally NOT seeded (tracked by T-0124 until the
    canonical md lands)."""
    from app.project_scaffold import _RESOURCES_DIR

    roles_dir = install_data / slug / "vision" / "roles"
    assert roles_dir.is_dir(), f"{roles_dir} not seeded"
    for name in _EXPECTED_ROLES:
        seeded = roles_dir / name
        assert seeded.is_file(), f"{name} missing from seeded vision/roles/"
        bundled = _RESOURCES_DIR / "roles" / name
        assert seeded.read_text() == bundled.read_text(), (
            f"{name} seeded content drifted from bundled source"
        )
    assert not (roles_dir / "qa.md").exists(), (
        "qa.md must NOT be seeded yet (T-0124 dependency)"
    )


def test_paths_as_they_are_seeds_vision_roles(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    (install_data / "myproj").mkdir(parents=True)

    existing_dev = tmp_path / "elsewhere" / "dev"
    existing_master = tmp_path / "elsewhere" / "master"
    _git_init(existing_dev)
    _git_init(existing_master)

    mother = tmp_path / "home" / "myproj"
    scaffold_paths_as_they_are(
        slug="myproj",
        mother_dir=mother,
        repo_path=existing_dev,
        repo_master=existing_master,
        install_data_dir=install_data,
    )

    _assert_roles_seeded(install_data, "myproj")


def test_new_from_scratch_seeds_vision_roles(tmp_path: Path) -> None:
    install_data = tmp_path / "install" / "data"
    (install_data / "fresh").mkdir(parents=True)
    mother = tmp_path / "home" / "fresh"

    scaffold_new_from_scratch(
        slug="fresh",
        mother_dir=mother,
        git_remote=None,
        install_data_dir=install_data,
    )

    _assert_roles_seeded(install_data, "fresh")
