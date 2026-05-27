"""T-0051 — direct unit tests for project_scaffold helpers.

These don't spin up the FastAPI app — they exercise the scaffolders
against a tmp filesystem so the route tests can focus on the
HTTP-layer contract and not the on-disk choreography.
"""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from app.project_scaffold import (
    ScaffoldError,
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
