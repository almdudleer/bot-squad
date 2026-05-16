"""Smoke test for data/bot-squad/deploy/prod.sh (T-0081).

Exercises the real recipe end-to-end against a throwaway git repo + install
dir. Skipped if the recipe isn't present on the host (e.g. a dev checkout
without the install symlink).

The recipe lives in the install's ops dir (`$BOT_SQUAD/data/bot-squad/
deploy/prod.sh`), not in the source tree — same pattern as staging.sh.
We resolve it by env-var, defaulting to the standard install path.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest


RECIPE_PATH = Path(
    os.environ.get(
        "BOTSQUAD_PROD_RECIPE",
        "/home/www/bot-squad/data/bot-squad/deploy/prod.sh",
    )
)


pytestmark = pytest.mark.skipif(
    not RECIPE_PATH.exists(),
    reason=f"prod.sh recipe not found at {RECIPE_PATH} (set BOTSQUAD_PROD_RECIPE to override)",
)


# ---------------------------------------------------------------------------
# Fixture: tmp install dir + tmp bare origin + tmp master clone
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _bootstrap_env(tmp_path: Path, mothership_flag: str = "true") -> dict:
    """Build a throwaway install + master clone and return everything needed
    to invoke the recipe."""
    install_dir = tmp_path / "install"
    (install_dir / "config").mkdir(parents=True)
    (install_dir / "data" / "bot-squad" / "releases").mkdir(parents=True)

    # Minimal projects.toml — just enough for the mothership self-check.
    projects_toml = install_dir / "config" / "projects.toml"
    mothership_line = (
        f"mothership = {mothership_flag}\n" if mothership_flag in ("true", "false") else ""
    )
    projects_toml.write_text(
        "[projects.bot-squad]\n"
        'slug = "bot-squad"\n'
        + mothership_line
    )

    # Bare origin so `git push origin <tag>` would succeed if exercised.
    # (Smoke test sets SKIP_PUSH=1 to avoid network, but we still want a
    # plausible remote configured so git doesn't error on resolve.)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    # Master clone with one commit on `master`.
    master = tmp_path / "master"
    master.mkdir()
    _git(master, "init", "-q", "-b", "master")
    _git(master, "config", "user.email", "test@example.com")
    _git(master, "config", "user.name", "Test")
    _git(master, "remote", "add", "origin", str(origin))
    (master / "README.md").write_text("hi\n")
    _git(master, "add", "README.md")
    _git(master, "commit", "-q", "-m", "initial")

    return {
        "install_dir": install_dir,
        "master": master,
        "projects_toml": projects_toml,
        "releases_dir": install_dir / "data" / "bot-squad" / "releases",
        "manifest": install_dir / "data" / "bot-squad" / "releases" / "index.json",
    }


def _run_recipe(env_ctx: dict, version: str | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "BOT_SQUAD": str(env_ctx["install_dir"]),
        "BOTSQUAD_PROD_SKIP_PUSH": "1",
    }
    if version is not None:
        env["BOTSQUAD_PROD_VERSION"] = version
    return subprocess.run(
        ["bash", str(RECIPE_PATH)],
        cwd=str(env_ctx["master"]),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_prod_cuts_release_and_writes_manifest(tmp_path: Path) -> None:
    ctx = _bootstrap_env(tmp_path)

    result = _run_recipe(ctx, version="v0.0.0-test")
    assert result.returncode == 0, (
        f"recipe failed rc={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    # Tarball exists at the manifest-declared relative path.
    tarball = ctx["install_dir"] / "data" / "bot-squad" / "releases" / "v0.0.0-test.tar.gz"
    assert tarball.exists(), "tarball not written"

    # Manifest round-trips JSON parse and matches the canonical schema.
    manifest = json.loads(ctx["manifest"].read_text())
    assert manifest["current"] == "v0.0.0-test"
    assert len(manifest["releases"]) == 1
    entry = manifest["releases"][0]
    assert entry["version"] == "v0.0.0-test"
    assert entry["tarball_path"] == "data/bot-squad/releases/v0.0.0-test.tar.gz"
    assert len(entry["git_sha"]) == 40
    assert entry["created_at"].endswith("Z")
    assert entry["notes"] == ""
    # sha256 in the manifest matches the actual file digest.
    assert entry["sha256"] == hashlib.sha256(tarball.read_bytes()).hexdigest()

    # Tag exists locally on the master clone.
    tags = _git(ctx["master"], "tag", "-l", "v0.0.0-test").stdout.strip().splitlines()
    assert tags == ["v0.0.0-test"]


def test_prod_second_invocation_bumps_counter_without_overwriting(tmp_path: Path) -> None:
    """The same-day counter increments and prior entries stay intact."""
    ctx = _bootstrap_env(tmp_path)

    # Two computed-version cuts (no BOTSQUAD_PROD_VERSION override). The
    # date portion is today's UTC date; counter goes .1 then .2.
    r1 = _run_recipe(ctx)
    assert r1.returncode == 0, f"first cut failed:\n{r1.stdout}\n{r1.stderr}"
    r2 = _run_recipe(ctx)
    assert r2.returncode == 0, f"second cut failed:\n{r2.stdout}\n{r2.stderr}"

    manifest = json.loads(ctx["manifest"].read_text())
    versions = [e["version"] for e in manifest["releases"]]
    assert len(versions) == 2
    # Both share the YYYY.MM.DD prefix; suffixes are .1 then .2.
    assert versions[0].endswith(".1")
    assert versions[1].endswith(".2")
    assert versions[0][:-2] == versions[1][:-2]
    assert manifest["current"] == versions[1]

    # Both tarballs still present (the .2 cut did not overwrite the .1).
    for v in versions:
        tarball = ctx["releases_dir"] / f"{v}.tar.gz"
        assert tarball.exists(), f"expected tarball missing: {tarball}"


def test_prod_rejects_when_mothership_flag_absent(tmp_path: Path) -> None:
    ctx = _bootstrap_env(tmp_path, mothership_flag="absent")
    result = _run_recipe(ctx, version="v0.0.0-test")
    assert result.returncode == 2
    assert "T-0086" in result.stderr or "mothership" in result.stderr.lower()


def test_prod_rejects_when_mothership_flag_false(tmp_path: Path) -> None:
    ctx = _bootstrap_env(tmp_path, mothership_flag="false")
    result = _run_recipe(ctx, version="v0.0.0-test")
    assert result.returncode == 2
    assert "consumer" in result.stderr.lower() or "not the release source" in result.stderr.lower()


def test_prod_rejects_dirty_tree(tmp_path: Path) -> None:
    ctx = _bootstrap_env(tmp_path)
    (ctx["master"] / "dirty.txt").write_text("uncommitted change")
    result = _run_recipe(ctx, version="v0.0.0-test")
    assert result.returncode == 4
    assert "dirty" in result.stderr.lower()


def test_prod_rejects_existing_tag(tmp_path: Path) -> None:
    ctx = _bootstrap_env(tmp_path)
    # First cut succeeds.
    r1 = _run_recipe(ctx, version="v0.0.0-test")
    assert r1.returncode == 0
    # Second cut at the same version is rejected before any state change.
    r2 = _run_recipe(ctx, version="v0.0.0-test")
    assert r2.returncode == 5
    # Manifest still has exactly one entry.
    manifest = json.loads(ctx["manifest"].read_text())
    assert len(manifest["releases"]) == 1


def test_prod_picks_up_release_notes(tmp_path: Path) -> None:
    ctx = _bootstrap_env(tmp_path)
    notes_path = ctx["releases_dir"] / "v0.0.0-test.md"
    notes_body = "## v0.0.0-test\n\n- smoke test entry\n"
    notes_path.write_text(notes_body)

    result = _run_recipe(ctx, version="v0.0.0-test")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    manifest = json.loads(ctx["manifest"].read_text())
    assert manifest["releases"][0]["notes"] == notes_body
