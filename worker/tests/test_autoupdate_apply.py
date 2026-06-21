"""Tests for the consumer apply pipeline + rollback (T-0084).

All HTTP, docker, and the smoke step are mocked. The snapshot/restore
round-trip is exercised against real tmp dirs (rsync is cheap and the
correctness of the data/-exclusion is the highest-risk corner). All
filesystem state is contained under pytest's ``tmp_path`` — the real
``/home/www/bot-squad`` is never touched.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import types
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from bot_squad_worker import autoupdate as poller
from bot_squad_worker import autoupdate_apply as apply_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip env vars that could change apply behavior under test."""
    for key in (
        "BOT_SQUAD_MOTHERSHIP_URL",
        "BOTSQUAD_MOTHERSHIP_URL",
        "BOT_SQUAD_SELF_URL",
        "BOT_SQUAD_INSTALL_ROOT",
        "BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES",
    ):
        monkeypatch.delenv(key, raising=False)
    # T-0107 — disable worker-restart scheduling by default so the apply
    # happy-path tests don't actually fork systemctl. Tests that exercise
    # the restart helper itself flip this explicitly.
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_RESTART_CMD", "off")
    # Default to consumer (not mothership); individual tests can flip.
    monkeypatch.setattr(apply_mod, "is_mothership", lambda *a, **kw: False)
    monkeypatch.setattr(poller, "is_mothership", lambda *a, **kw: False)


@pytest.fixture
def install_ctx(tmp_path: Path, monkeypatch) -> dict:
    """Build a throwaway install tree + data dir.

    Layout::

        tmp_path/
          install/                ← install_root
            docker-compose.yml    (stub content; never actually run)
            api/                  (a couple of source files)
            data/                 ← cfg.data_dir (excluded from snapshot)
              _worker/
              user_state.json
    """
    install = tmp_path / "install"
    (install / "api").mkdir(parents=True)
    (install / "api" / "main.py").write_text("# initial main\n")
    (install / "docker-compose.yml").write_text("version: '3'\n")
    (install / "VERSION").write_text("v2026.05.16.1\n")

    data = install / "data"
    (data / "_worker").mkdir(parents=True)
    (data / "user_state.json").write_text('{"user": "important"}\n')

    cfg = types.SimpleNamespace(
        data_dir=data,
        projects={
            "bot-squad": types.SimpleNamespace(prod_url="https://consumer.example"),
        },
    )

    monkeypatch.setenv("BOT_SQUAD_INSTALL_ROOT", str(install))
    monkeypatch.setenv("BOT_SQUAD_SELF_URL", "https://consumer.example")

    return {"install": install, "data": data, "cfg": cfg}


def _make_tarball(version: str, *, contents: dict[str, str] | None = None) -> bytes:
    """Build an in-memory .tar.gz the apply pipeline can extract.

    The default payload mimics a git-archive output: source files at the
    repo root. ``data/`` is intentionally *not* included — git-archive
    won't ship runtime data, and the apply pipeline must not stomp on it
    anyway.
    """
    payload = contents or {
        "api/main.py": f"# updated main for {version}\n",
        "api/new_module.py": f"# new in {version}\n",
        "docker-compose.yml": "version: '3'\n",
        "VERSION": f"{version}\n",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in payload.items():
            data = body.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_entry(version: str, sha256: str, url: str = "https://mothership/api/releases/_files/foo.tar.gz") -> dict:
    return {
        "version": version,
        "git_sha": "abc123def" * 4 + "abcd",  # 40 chars
        "created_at": "2026-05-16T16:35:00Z",
        "tarball_path": f"data/bot-squad/releases/{version}.tar.gz",
        "tarball_url": url,
        "sha256": sha256,
        "notes": "",
    }


def _pre_stamp(cfg, version: str) -> None:
    """Pre-stamp installed_version (apply assumes the poller has run at least once)."""
    poller.save_state(cfg, {
        "installed_version": version,
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "oldsha",
    })


def _install_fake_download(monkeypatch, tarball_bytes: bytes) -> None:
    """Replace _download with one that just writes ``tarball_bytes`` to dest."""
    def fake_download(url, dest, *, timeout=300.0):  # noqa: ARG001
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(tarball_bytes)
    monkeypatch.setattr(apply_mod, "_download", fake_download)


# ---------------------------------------------------------------------------
# self_url / tarball_url helpers
# ---------------------------------------------------------------------------


def test_self_url_prefers_env_override(install_ctx, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_SELF_URL", "https://override.example/")
    assert apply_mod._self_url(install_ctx["cfg"]) == "https://override.example"


def test_self_url_falls_back_to_projects_config(install_ctx, monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_SELF_URL", raising=False)
    assert apply_mod._self_url(install_ctx["cfg"]) == "https://consumer.example"


def test_tarball_url_prefers_full_url():
    entry = {"tarball_url": "https://m/files/v1.tar.gz", "tarball_path": "data/.../v1.tar.gz"}
    assert apply_mod._tarball_url(entry) == "https://m/files/v1.tar.gz"


def test_tarball_url_falls_back_to_path(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example/")
    entry = {"tarball_path": "data/bot-squad/releases/v1.tar.gz"}
    assert (
        apply_mod._tarball_url(entry)
        == "https://mom.example/data/bot-squad/releases/v1.tar.gz"
    )


def test_tarball_url_missing_returns_none():
    assert apply_mod._tarball_url({}) is None


# ---------------------------------------------------------------------------
# Snapshot / restore round-trip — the highest-stakes correctness corner
# ---------------------------------------------------------------------------


def test_snapshot_excludes_data_dir(install_ctx):
    cfg = install_ctx["cfg"]
    snap = apply_mod._snapshot(cfg, "v2026.05.16.1")
    try:
        assert snap.exists()
        # Source files copied:
        assert (snap / "api" / "main.py").exists()
        assert (snap / "docker-compose.yml").exists()
        # data/ MUST NOT be in the snapshot — that's user state.
        assert not (snap / "data").exists()
    finally:
        if snap.exists():
            import shutil
            shutil.rmtree(snap)


def test_snapshot_restore_round_trip_preserves_data(install_ctx):
    """Mutate install + data, restore from snapshot: install reverts, data persists."""
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    data = install_ctx["data"]

    # Snapshot pristine state.
    snap = apply_mod._snapshot(cfg, "v2026.05.16.1")

    try:
        # Mutate the install tree (simulates a botched apply).
        (install / "api" / "main.py").write_text("# CORRUPTED\n")
        (install / "api" / "new_file.py").write_text("# stray from bad release\n")
        (install / "VERSION").write_text("v2026.05.16.99\n")

        # Mutate the data tree (simulates concurrent user activity during apply).
        (data / "user_state.json").write_text('{"user": "MUTATED DURING DEPLOY"}\n')
        (data / "_worker" / "new_runtime_file.json").write_text('{}\n')

        # Restore.
        apply_mod._restore(cfg, snap)

        # Install reverted:
        assert (install / "api" / "main.py").read_text() == "# initial main\n"
        assert not (install / "api" / "new_file.py").exists(), \
            "stray file from bad release should be wiped on restore (rsync --delete)"
        assert (install / "VERSION").read_text() == "v2026.05.16.1\n"

        # Data preserved (rsync's --exclude=data/ keeps it untouched):
        assert (data / "user_state.json").read_text() == '{"user": "MUTATED DURING DEPLOY"}\n'
        assert (data / "_worker" / "new_runtime_file.json").exists()
    finally:
        if snap.exists():
            import shutil
            shutil.rmtree(snap)


# ---------------------------------------------------------------------------
# BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES (T-0112 — coverage for the env-driven
# extra-excludes path proven by the T-0090 dogfood)
# ---------------------------------------------------------------------------


def test_extra_excludes_empty_by_default(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES", raising=False)
    assert apply_mod._extra_rsync_excludes() == ()


def test_extra_excludes_parses_comma_separated(monkeypatch):
    monkeypatch.setenv(
        "BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES",
        "docker-compose.yml,.env,config/",
    )
    assert apply_mod._extra_rsync_excludes() == (
        "docker-compose.yml",
        ".env",
        "config/",
    )


def test_extra_excludes_strips_whitespace_and_drops_blanks(monkeypatch):
    monkeypatch.setenv(
        "BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES",
        " docker-compose.yml , , .env ",
    )
    assert apply_mod._extra_rsync_excludes() == ("docker-compose.yml", ".env")


def test_sync_excludes_always_prefixes_data_dir(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES", raising=False)
    assert apply_mod._sync_excludes() == ("data/",)


def test_sync_excludes_appends_extras_after_data(monkeypatch):
    monkeypatch.setenv(
        "BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES",
        "docker-compose.yml,.env",
    )
    assert apply_mod._sync_excludes() == ("data/", "docker-compose.yml", ".env")


def test_extra_excludes_preserves_site_local_file_through_restore(install_ctx, monkeypatch):
    """End-to-end: a file matching EXTRA_EXCLUDES survives a snapshot+restore cycle
    even when the snapshot doesn't contain it. Proves the exclude pattern reaches
    the rsync invocation (both --exclude prevents copying it INTO the snapshot AND
    prevents --delete from wiping it on restore)."""
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]

    # Site-local file the consumer wants preserved across apply.
    site_local = install / "docker-compose.yml"
    site_local.write_text("# SITE-LOCAL override — must survive apply\n")

    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES", "docker-compose.yml")

    # Snapshot the install (docker-compose.yml excluded from snapshot).
    snap = apply_mod._snapshot(cfg, "v2026.05.16.1")
    try:
        assert not (snap / "docker-compose.yml").exists(), \
            "docker-compose.yml should NOT be in the snapshot when EXTRA_EXCLUDES lists it"

        # Mutate the live install to simulate a bad apply that touched the file.
        site_local.write_text("# CORRUPTED by bad apply\n")

        # Restore from snapshot — without the exclude, --delete would wipe
        # docker-compose.yml (it's not in the snapshot); WITH the exclude,
        # rsync leaves the live copy untouched.
        apply_mod._restore(cfg, snap)

        assert site_local.exists(), "site-local file was wiped despite EXTRA_EXCLUDES"
        assert site_local.read_text() == "# CORRUPTED by bad apply\n", \
            "site-local file content was overwritten despite EXTRA_EXCLUDES"
    finally:
        if snap.exists():
            import shutil
            shutil.rmtree(snap)


# ---------------------------------------------------------------------------
# Worker self-restart on successful apply (T-0107)
# ---------------------------------------------------------------------------


def _capture_popen(monkeypatch) -> list[tuple[list[str], dict]]:
    """Replace subprocess.Popen with a no-op that records its invocations."""
    calls: list[tuple[list[str], dict]] = []
    def fake_popen(argv, **kwargs):
        calls.append((list(argv), kwargs))
        # Return a fake handle with the minimal surface Popen callers touch.
        return types.SimpleNamespace(pid=12345, wait=lambda: 0)
    monkeypatch.setattr(apply_mod.subprocess, "Popen", fake_popen)
    return calls


def test_schedule_worker_restart_default_command(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_RESTART_CMD", "")  # cleared below
    monkeypatch.delenv("BOT_SQUAD_AUTOUPDATE_RESTART_CMD", raising=False)
    calls = _capture_popen(monkeypatch)
    apply_mod._schedule_worker_restart(delay_sec=2)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["nohup", "sh", "-c"]
    assert argv[3] == "sleep 2 && systemctl --user restart bot-squad-worker"
    assert kwargs.get("start_new_session") is True


def test_schedule_worker_restart_env_override(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_RESTART_CMD", "echo restart-hook")
    calls = _capture_popen(monkeypatch)
    apply_mod._schedule_worker_restart(delay_sec=3)
    assert len(calls) == 1
    argv, _ = calls[0]
    assert argv[3] == "sleep 3 && echo restart-hook"


@pytest.mark.parametrize("value", ["off", "none", "skip", "", "  off  ", "OFF"])
def test_schedule_worker_restart_disabled_values_skip(monkeypatch, value):
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_RESTART_CMD", value)
    calls = _capture_popen(monkeypatch)
    apply_mod._schedule_worker_restart()
    assert calls == [], f"value={value!r} should suppress the restart, but Popen was called"


def test_schedule_worker_restart_swallows_subprocess_errors(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_RESTART_CMD", "systemctl --user restart bot-squad-worker")
    def boom(*a, **kw):
        raise OSError("nohup not on PATH")
    monkeypatch.setattr(apply_mod.subprocess, "Popen", boom)
    # Must not propagate — a missing restart shouldn't fail the apply.
    apply_mod._schedule_worker_restart()


# ---------------------------------------------------------------------------
# Apply happy path
# ---------------------------------------------------------------------------


def test_apply_happy_path_updates_install_and_stamps_state(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)

    _install_fake_download(monkeypatch, tar_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")
    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)
    monkeypatch.setattr(apply_mod, "_running_git_sha", lambda cfg: entry["git_sha"])

    result = apply_mod.apply(cfg, entry)

    assert result.ok is True
    assert result.failed_step is None
    assert result.version == "v2026.05.16.2"

    # New release contents replaced the install:
    assert (install / "api" / "main.py").read_text() == "# updated main for v2026.05.16.2\n"
    assert (install / "api" / "new_module.py").exists()
    assert (install / "VERSION").read_text() == "v2026.05.16.2\n"

    # data/ still present and untouched:
    assert (install_ctx["data"] / "user_state.json").read_text() == '{"user": "important"}\n'

    # State updated:
    state = poller.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.2"
    assert state["last_apply_outcome"] == "success"
    assert state["last_apply_at"] is not None

    # Snapshot cleaned up:
    assert not apply_mod.snapshot_path(cfg, "v2026.05.16.1").exists()

    # No alert on success:
    assert not apply_mod.alert_path(cfg).exists()


def test_apply_happy_path_schedules_worker_restart(install_ctx, monkeypatch):
    """T-0107 — apply success must call _schedule_worker_restart so the
    running worker picks up the new install tree without manual intervention.
    """
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)

    _install_fake_download(monkeypatch, tar_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")
    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)
    monkeypatch.setattr(apply_mod, "_running_git_sha", lambda cfg: entry["git_sha"])

    # Spy on the helper directly — replacing subprocess.Popen would also
    # neuter the rsync/extract subprocess calls inside apply.
    restart_calls: list[int] = []
    monkeypatch.setattr(
        apply_mod, "_schedule_worker_restart",
        lambda *a, **kw: restart_calls.append(1),
    )

    result = apply_mod.apply(cfg, entry)

    assert result.ok is True
    assert restart_calls == [1], "apply success must schedule exactly one worker restart"


# ---------------------------------------------------------------------------
# Failure paths — each step rolls back to prior state
# ---------------------------------------------------------------------------


def test_sha_mismatch_aborts_without_touching_install_tree(install_ctx, monkeypatch):
    """sha mismatch: install must be untouched; no snapshot created."""
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    entry = _make_entry("v2026.05.16.2", "deadbeef" * 8)  # wrong sha
    _install_fake_download(monkeypatch, tar_bytes)

    # docker / smoke would crash the test if they ever got called — leave
    # them at the real implementations as a tripwire.

    result = apply_mod.apply(cfg, entry)

    assert result.ok is False
    assert result.failed_step == "sha_mismatch"

    # Install pristine:
    assert (install / "api" / "main.py").read_text() == "# initial main\n"
    assert not (install / "api" / "new_module.py").exists()

    # Snapshot was NOT created (sha check runs before snapshot step):
    assert not apply_mod.snapshot_path(cfg, "v2026.05.16.1").exists()

    # installed_version unchanged; last_apply_outcome reflects failure:
    state = poller.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.1"
    assert state["last_apply_outcome"] == "failed:sha_mismatch"

    # Alert written:
    assert apply_mod.alert_path(cfg).exists()
    alert = json.loads(apply_mod.alert_path(cfg).read_text())
    assert alert["step"] == "sha_mismatch"
    assert alert["version"] == "v2026.05.16.2"


def test_download_failure_marks_failed_and_does_not_touch_install(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    _pre_stamp(cfg, "v2026.05.16.1")
    entry = _make_entry("v2026.05.16.2", "irrelevant")

    def boom_download(url, dest, *, timeout=300.0):  # noqa: ARG001
        raise httpx.ConnectError("dns fail", request=httpx.Request("GET", url))
    monkeypatch.setattr(apply_mod, "_download", boom_download)

    result = apply_mod.apply(cfg, entry)

    assert result.ok is False
    assert result.failed_step == "download"
    assert (install / "api" / "main.py").read_text() == "# initial main\n"
    state = poller.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.1"
    assert state["last_apply_outcome"] == "failed:download"


def test_extract_failure_rolls_back_via_snapshot(install_ctx, monkeypatch):
    """If extract/rsync corrupts the install, the snapshot is restored."""
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)
    _install_fake_download(monkeypatch, tar_bytes)

    # Inject failure between snapshot and docker by making _rsync fail
    # on its SECOND call (first call is the snapshot step, second is the
    # extract→install copy).
    real_rsync = apply_mod._rsync
    calls = {"n": 0}

    def flaky_rsync(src, dest, *, delete, exclude=()):
        calls["n"] += 1
        if calls["n"] == 2:
            # Corrupt the install before raising, to prove rollback restores.
            (install / "api" / "main.py").write_text("# CORRUPTED\n")
            raise RuntimeError("simulated extract rsync failure")
        return real_rsync(src, dest, delete=delete, exclude=exclude)

    monkeypatch.setattr(apply_mod, "_rsync", flaky_rsync)

    result = apply_mod.apply(cfg, entry)

    assert result.ok is False
    assert result.failed_step == "extract"

    # Rollback restored the pristine main.py:
    assert (install / "api" / "main.py").read_text() == "# initial main\n"

    state = poller.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.1"
    assert state["last_apply_outcome"] == "failed:extract"
    assert apply_mod.alert_path(cfg).exists()


def test_build_failure_rolls_back_and_brings_prior_containers_back(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)
    _install_fake_download(monkeypatch, tar_bytes)

    def boom_build(cfg, git_sha=None):  # noqa: ARG001
        raise RuntimeError("simulated docker build failure")
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", boom_build)

    no_build_calls: list[Any] = []
    def fake_no_build(cfg):  # noqa: ARG001
        no_build_calls.append(1)
        return "restored prior containers"
    monkeypatch.setattr(apply_mod, "_docker_compose_up_no_build", fake_no_build)

    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)

    result = apply_mod.apply(cfg, entry)

    assert result.ok is False
    assert result.failed_step == "build"
    # Rollback restored install:
    assert (install / "api" / "main.py").read_text() == "# initial main\n"
    assert not (install / "api" / "new_module.py").exists()
    # `docker compose up -d` (no-build) was invoked to bring prior containers back:
    assert len(no_build_calls) == 1


def test_smoke_failure_rolls_back_and_brings_prior_containers_back(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)
    _install_fake_download(monkeypatch, tar_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")

    def boom_smoke(url):  # noqa: ARG001
        raise RuntimeError("simulated smoke failure")
    monkeypatch.setattr(apply_mod, "_smoke", boom_smoke)

    no_build_calls: list[Any] = []
    monkeypatch.setattr(
        apply_mod, "_docker_compose_up_no_build",
        lambda cfg: no_build_calls.append(1) or "ok",
    )

    # T-0107 — also assert failure path does NOT schedule a restart.
    # (Restarting after a failure would respawn the worker into the
    # restored-snapshot tree, which is the prior version — pointless churn.)
    restart_calls: list[int] = []
    monkeypatch.setattr(
        apply_mod, "_schedule_worker_restart",
        lambda *a, **kw: restart_calls.append(1),
    )

    result = apply_mod.apply(cfg, entry)

    assert result.ok is False
    assert result.failed_step == "smoke"
    assert (install / "api" / "main.py").read_text() == "# initial main\n"
    assert len(no_build_calls) == 1
    state = poller.load_state(cfg)
    assert state["last_apply_outcome"] == "failed:smoke"
    assert restart_calls == [], "apply failure must NOT schedule a worker restart"


# ---------------------------------------------------------------------------
# T-0379: release-apply stamps + asserts the released git sha (consumer path
# mirror of staging.sh's running==deployed gate).
# ---------------------------------------------------------------------------


def test_docker_compose_up_build_exports_git_sha(install_ctx, monkeypatch):
    """The build passes GIT_SHA so the image bakes BOT_SQUAD_GIT_SHA."""
    cfg = install_ctx["cfg"]
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "built"
        stderr = ""

    def fake_run(args, **kw):  # noqa: ANN001
        captured["env"] = kw.get("env", {})
        return _Proc()

    monkeypatch.setattr(apply_mod.subprocess, "run", fake_run)
    apply_mod._docker_compose_up_build(cfg, git_sha="deadbeefcafe")
    assert captured["env"].get("GIT_SHA") == "deadbeefcafe"


def test_apply_git_sha_mismatch_rolls_back(install_ctx, monkeypatch):
    """If the running container's sha != the released sha, apply must FAIL
    (step git_sha_verify), restore the snapshot + bring prior containers back —
    a build that didn't pick up the released source can't report success."""
    cfg = install_ctx["cfg"]
    install = install_ctx["install"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)  # entry["git_sha"] is 40 chars
    _install_fake_download(monkeypatch, tar_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")
    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)
    # Running container reports a DIFFERENT sha than the release.
    monkeypatch.setattr(apply_mod, "_running_git_sha", lambda cfg: "wrongshawrongsha")

    no_build_calls: list[Any] = []
    monkeypatch.setattr(
        apply_mod, "_docker_compose_up_no_build",
        lambda cfg: no_build_calls.append(1) or "ok",
    )

    result = apply_mod.apply(cfg, entry)

    assert result.ok is False
    assert result.failed_step == "git_sha_verify"
    # Rolled back to the prior install:
    assert (install / "api" / "main.py").read_text() == "# initial main\n"
    assert len(no_build_calls) == 1
    assert poller.load_state(cfg)["last_apply_outcome"] == "failed:git_sha_verify"


def test_apply_git_sha_match_succeeds(install_ctx, monkeypatch):
    """Running sha == released sha → apply succeeds normally."""
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")
    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)
    _install_fake_download(monkeypatch, tar_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")
    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)
    monkeypatch.setattr(apply_mod, "_running_git_sha", lambda cfg: entry["git_sha"])
    monkeypatch.setattr(apply_mod, "_schedule_worker_restart", lambda *a, **kw: None)

    result = apply_mod.apply(cfg, entry)
    assert result.ok is True
    assert poller.load_state(cfg)["last_apply_outcome"] == "success"


# ---------------------------------------------------------------------------
# Smoke retry/backoff
# ---------------------------------------------------------------------------


def test_smoke_retries_with_backoff_then_succeeds(monkeypatch):
    """Per T-0079 lesson: retry with backoff before declaring smoke failure."""
    # Compress sleeps so the test isn't slow.
    monkeypatch.setattr(apply_mod.time, "sleep", lambda s: None)

    calls = {"n": 0}
    def fake_get(url, timeout):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("not ready", request=httpx.Request("GET", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)

    apply_mod._smoke("https://consumer.example")
    assert calls["n"] == 3


def test_smoke_gives_up_after_full_backoff(monkeypatch):
    monkeypatch.setattr(apply_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}
    def fake_get(url, timeout):  # noqa: ARG001
        calls["n"] += 1
        raise httpx.ConnectError("never ready", request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(RuntimeError, match="smoke failed"):
        apply_mod._smoke("https://consumer.example")
    assert calls["n"] == len(apply_mod.SMOKE_BACKOFF_SECONDS)


# ---------------------------------------------------------------------------
# Alert / handoff
# ---------------------------------------------------------------------------


def test_failure_writes_structured_alert(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")
    entry = _make_entry("v2026.05.16.2", "deadbeef" * 8)  # bad sha
    _install_fake_download(monkeypatch, _make_tarball("v2026.05.16.2"))

    apply_mod.apply(cfg, entry)

    payload = json.loads(apply_mod.alert_path(cfg).read_text())
    assert payload["step"] == "sha_mismatch"
    assert payload["version"] == "v2026.05.16.2"
    assert "expected=" in payload["log_tail"]
    assert "occurred_at" in payload
    assert "retry_command" in payload  # T-0085 hook
    assert "force_command" in payload


def test_success_clears_prior_alert(install_ctx, monkeypatch):
    """A leftover alert from a prior failure is cleared on the next success."""
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")

    # Pre-seed an alert as if a prior apply failed.
    apply_mod._write_alert(cfg, version="v2026.05.16.1.5", step="smoke", log_tail="stale")
    assert apply_mod.alert_path(cfg).exists()

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)
    _install_fake_download(monkeypatch, tar_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")
    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)
    monkeypatch.setattr(apply_mod, "_running_git_sha", lambda cfg: entry["git_sha"])

    result = apply_mod.apply(cfg, entry)

    assert result.ok is True
    assert not apply_mod.alert_path(cfg).exists()


def test_notify_failure_seam_called_on_failure(install_ctx, monkeypatch):
    """T-0085 will fill in _notify_failure; verify the seam fires."""
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")

    seen: list[dict] = []
    monkeypatch.setattr(
        apply_mod, "_notify_failure",
        lambda cfg, *, version, step, log_tail: seen.append(
            {"version": version, "step": step}
        ),
    )

    entry = _make_entry("v2026.05.16.2", "deadbeef" * 8)
    _install_fake_download(monkeypatch, _make_tarball("v2026.05.16.2"))
    apply_mod.apply(cfg, entry)

    assert seen == [{"version": "v2026.05.16.2", "step": "sha_mismatch"}]


# ---------------------------------------------------------------------------
# Mothership self-exclusion
# ---------------------------------------------------------------------------


def test_apply_short_circuits_on_mothership(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")
    monkeypatch.setattr(apply_mod, "is_mothership", lambda *a, **kw: True)

    # Tripwires: if any pipeline step ran, the test would explode.
    def boom(*a, **kw):
        raise AssertionError("mothership apply should be a no-op")
    monkeypatch.setattr(apply_mod, "_download", boom)
    monkeypatch.setattr(apply_mod, "_snapshot", boom)

    entry = _make_entry("v2026.05.16.2", "deadbeef" * 8)
    result = apply_mod.apply(cfg, entry)

    assert result.ok is True
    assert result.skipped is True


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


def test_drain_one_processes_oldest_and_removes_queue_file(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")

    tar_bytes = _make_tarball("v2026.05.16.2")
    sha = hashlib.sha256(tar_bytes).hexdigest()
    entry = _make_entry("v2026.05.16.2", sha)

    # Enqueue two jobs; drain_one must process only the first.
    qdir = apply_mod.queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    f1 = qdir / "00000000000000000000000000000001.json"
    f1.write_text(json.dumps(entry))
    # Touch f1 older than f2 explicitly.
    import os as _os
    _os.utime(f1, (1000, 1000))
    entry2 = dict(entry)
    entry2["version"] = "v2026.05.16.3"
    f2 = qdir / "00000000000000000000000000000002.json"
    f2.write_text(json.dumps(entry2))
    _os.utime(f2, (2000, 2000))

    _install_fake_download(monkeypatch, tar_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")
    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)
    monkeypatch.setattr(apply_mod, "_running_git_sha", lambda cfg: entry["git_sha"])

    result = apply_mod.drain_one(cfg)

    assert result is not None and result.ok is True
    assert result.version == "v2026.05.16.2"
    assert not f1.exists(), "successful drain should remove the queue file"
    assert f2.exists(), "drain_one must process only one file per call"


def test_drain_one_parks_failed_job_under_failed_dir(install_ctx, monkeypatch):
    """A failing job is moved to autoupdate_queue_failed/ so the loop doesn't
    infinite-retry. T-0085's autoupdate_retry action moves it back."""
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")

    entry = _make_entry("v2026.05.16.2", "deadbeef" * 8)  # bad sha → fail
    qdir = apply_mod.queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    qf = qdir / "deadbeef.json"
    qf.write_text(json.dumps(entry))

    _install_fake_download(monkeypatch, _make_tarball("v2026.05.16.2"))

    result = apply_mod.drain_one(cfg)

    assert result is not None and result.ok is False
    assert not qf.exists()
    parked = apply_mod.failed_queue_dir(cfg) / "deadbeef.json"
    assert parked.exists()


def test_drain_one_empty_queue_returns_none(install_ctx):
    assert apply_mod.drain_one(install_ctx["cfg"]) is None


def test_drain_one_mothership_is_noop(install_ctx, monkeypatch):
    monkeypatch.setattr(apply_mod, "is_mothership", lambda *a, **kw: True)
    cfg = install_ctx["cfg"]
    qdir = apply_mod.queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "shouldnt-be-touched.json").write_text("{}")
    assert apply_mod.drain_one(cfg) is None
    # Queue file still present — drain didn't even look:
    assert (qdir / "shouldnt-be-touched.json").exists()


def test_drain_one_bad_json_parks_without_apply(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    qdir = apply_mod.queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    qf = qdir / "garbage.json"
    qf.write_text("not-json{")

    # Tripwire: apply must not be called on a junk queue file.
    monkeypatch.setattr(apply_mod, "apply", lambda *a, **kw: pytest.fail("apply called on junk"))

    result = apply_mod.drain_one(cfg)
    assert result is None
    assert not qf.exists()
    assert (apply_mod.failed_queue_dir(cfg) / "garbage.json").exists()
