"""Tests for T-0085 operator handoff on autoupdate failure.

Covers:

* ``_notify_failure`` wires the structured failure into ``tg_notify``
  (TgClient mocked — no real network).
* The failure-alert file roundtrip — written on apply failure, cleared on
  the next successful apply (verifies the T-0084 success path doesn't
  leave the banner stale).
* ``autoupdate_retry`` action — moves the most-recent failed manifest
  back into the live queue (idempotent when failed-queue is empty).
* ``autoupdate_force`` action — fetches a manifest entry from the
  mothership ``/api/releases/<version>`` endpoint and enqueues it
  (httpx mocked).
* Registry + mode tag wiring for both new actions.

All filesystem state is contained under pytest's ``tmp_path``; the real
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
from typing import Any

import httpx
import pytest

from bot_squad_worker import autoupdate as poller
from bot_squad_worker import autoupdate_apply as apply_mod
from bot_squad_worker import actions as A
from bot_squad_worker.actions import (
    ACTION_MODES,
    ACTION_REGISTRY,
    ActionError,
    dispatch,
    set_mode,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Same isolation as test_autoupdate_apply: no leaky env, consumer mode."""
    for key in (
        "BOT_SQUAD_MOTHERSHIP_URL",
        "BOTSQUAD_MOTHERSHIP_URL",
        "BOT_SQUAD_SELF_URL",
        "BOT_SQUAD_INSTALL_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(apply_mod, "is_mothership", lambda *a, **kw: False)
    monkeypatch.setattr(poller, "is_mothership", lambda *a, **kw: False)
    # Coordinator mode so the new actions are dispatchable.
    set_mode("coordinator")
    yield
    set_mode("coordinator")


@pytest.fixture
def install_ctx(tmp_path: Path, monkeypatch) -> dict:
    """Throwaway install tree + data dir, mirroring test_autoupdate_apply."""
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
            "bot-squad": types.SimpleNamespace(
                prod_url="https://consumer.example",
                tg_chat="424242",
            ),
        },
    )

    monkeypatch.setenv("BOT_SQUAD_INSTALL_ROOT", str(install))
    monkeypatch.setenv("BOT_SQUAD_SELF_URL", "https://consumer.example")

    return {"install": install, "data": data, "cfg": cfg}


def _make_tarball(version: str) -> bytes:
    payload = {
        "api/main.py": f"# updated main for {version}\n",
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


def _make_entry(version: str, sha256: str) -> dict:
    return {
        "version": version,
        "git_sha": "abc123def" * 4 + "abcd",
        "created_at": "2026-05-16T16:35:00Z",
        "tarball_path": f"data/bot-squad/releases/{version}.tar.gz",
        "tarball_url": f"https://mothership.example/api/releases/_files/{version}.tar.gz",
        "sha256": sha256,
        "notes": "",
    }


def _pre_stamp(cfg, version: str) -> None:
    poller.save_state(cfg, {
        "installed_version": version,
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "oldsha",
    })


def _install_fake_download(monkeypatch, tarball_bytes: bytes) -> None:
    def fake_download(url, dest, *, timeout=300.0):  # noqa: ARG001
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(tarball_bytes)
    monkeypatch.setattr(apply_mod, "_download", fake_download)


class _FakeTgClient:
    """Records send() calls. Mirrors the real signature."""
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, chat_id, text, sid="", user="", urgent=False, topic_id=None, debounce=True) -> bool:
        self.calls.append({
            "chat_id": chat_id, "text": text,
            "sid": sid, "user": user, "urgent": urgent, "topic_id": topic_id,
        })
        return True


# ---------------------------------------------------------------------------
# _notify_failure — tg_notify wiring
# ---------------------------------------------------------------------------


def test_notify_failure_sends_tg_with_structured_body(install_ctx, monkeypatch):
    """Failure handoff produces a TG message with the required fields."""
    cfg = install_ctx["cfg"]
    fake = _FakeTgClient()

    # T-0394: _notify_failure now pages via the _send_stakeholder_dm SSOT; with
    # MAX unconfigured it takes the TG path. Mock the TG client at that seam.
    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda c: fake)

    apply_mod._notify_failure(
        cfg,
        version="v2026.05.16.99",
        step="build",
        log_tail="docker compose up exited 1\nstderr: image build failed",
    )

    assert len(fake.calls) == 1, "exactly one TG message per failure"
    call = fake.calls[0]
    assert call["chat_id"] == "424242", "uses projects['bot-squad'].tg_chat"
    assert call["urgent"] is True, "apply failure bypasses quiet hours"

    body = call["text"]
    assert "FAILED" in body
    assert "v2026.05.16.99" in body, "version surfaced in body"
    assert "build" in body, "failed step surfaced in body"
    assert "docker compose up exited 1" in body, "log tail included verbatim"
    assert "https://consumer.example" in body, "install identifier surfaced"
    assert "autoupdate retry" in body, "retry command exposed to operator"
    assert "autoupdate force v2026.05.16.99" in body, "force command exposed"


def test_notify_failure_swallows_tg_errors(install_ctx, monkeypatch, caplog):
    """If TG send blows up, we log and move on — banner is the recovery surface."""
    cfg = install_ctx["cfg"]

    class _BoomClient:
        def send(self, **kw):
            raise RuntimeError("TG API exploded")

    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda c: _BoomClient())

    # Must not raise.
    apply_mod._notify_failure(cfg, version="v1", step="smoke", log_tail="x")


def test_notify_failure_no_chat_id_is_banner_only(install_ctx, monkeypatch, caplog):
    """Missing tg_chat → log + bail; alert file is still the recovery path."""
    cfg = install_ctx["cfg"]
    # Strip the chat — simulates an install whose projects.toml hasn't wired it.
    cfg.projects["bot-squad"] = types.SimpleNamespace(
        prod_url="https://consumer.example",
        tg_chat="",
    )

    called = {"n": 0}

    class _ShouldNotBeUsed:
        def send(self, **kw):
            called["n"] += 1
            return True

    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda c: _ShouldNotBeUsed())
    monkeypatch.setattr(A, "_get_max_client", lambda c: _ShouldNotBeUsed())

    apply_mod._notify_failure(cfg, version="v1", step="smoke", log_tail="x")
    assert called["n"] == 0, "no chat → no send attempt"


def test_notify_failure_tg_primary_single_delivery(install_ctx, monkeypatch):
    """T-0394 → T-0610 inversion: with MAX configured, the apply-failure page
    goes via TG (primary) into #team-queries — ONE delivery, MAX reserve only."""
    cfg = install_ctx["cfg"]
    cfg.max_default_chat_id = "MAXID"
    cfg.max_recipient_kind = "chat_id"
    from bot_squad_worker import tg_topics
    tg_topics.save(cfg, "bot-squad", {"team_queries": 808})

    import bot_squad_worker.actions as A
    max_calls, tg_calls = [], []
    monkeypatch.setattr(A, "_get_max_client", lambda c: types.SimpleNamespace(
        send=lambda **k: (max_calls.append(k) or True)))
    monkeypatch.setattr(A, "_get_tg_client", lambda c: types.SimpleNamespace(
        send=lambda **k: (tg_calls.append(k) or True)))

    apply_mod._notify_failure(cfg, version="v9", step="build", log_tail="boom")
    assert len(tg_calls) == 1 and tg_calls[0]["topic_id"] == 808
    assert len(max_calls) == 0  # one page = one delivery (T-0610)


def test_install_identifier_falls_back_to_env(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    # Remove the project so the env override is the only source.
    cfg.projects.pop("bot-squad")
    monkeypatch.setenv("BOT_SQUAD_SELF_URL", "https://override.example/")
    assert apply_mod._install_identifier(cfg) == "https://override.example"


def test_install_identifier_unknown_when_nothing_configured(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    cfg.projects.pop("bot-squad")
    monkeypatch.delenv("BOT_SQUAD_SELF_URL", raising=False)
    assert apply_mod._install_identifier(cfg) == "unknown-install"


# ---------------------------------------------------------------------------
# Alert file roundtrip — write on failure, clear on next success
# ---------------------------------------------------------------------------


def test_alert_file_roundtrip(install_ctx, monkeypatch):
    """Failed apply → alert.json written; next successful apply → cleared."""
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")

    # Round 1: apply fails (sha mismatch) → alert written.
    bad_entry = _make_entry("v2026.05.16.2", "deadbeef" * 8)
    _install_fake_download(monkeypatch, _make_tarball("v2026.05.16.2"))
    apply_mod.apply(cfg, bad_entry)

    alert_p = apply_mod.alert_path(cfg)
    assert alert_p.exists()
    alert = json.loads(alert_p.read_text())
    assert alert["step"] == "sha_mismatch"
    assert alert["version"] == "v2026.05.16.2"
    assert alert["retry_command"] == "bot-squad-cli autoupdate retry"
    assert alert["force_command"] == "bot-squad-cli autoupdate force v2026.05.16.2"

    # Round 2: same install, fresh apply succeeds → alert cleared.
    good_bytes = _make_tarball("v2026.05.16.3")
    good_sha = hashlib.sha256(good_bytes).hexdigest()
    good_entry = _make_entry("v2026.05.16.3", good_sha)

    _install_fake_download(monkeypatch, good_bytes)
    monkeypatch.setattr(apply_mod, "_docker_compose_up_build", lambda cfg, git_sha=None: "ok")
    monkeypatch.setattr(apply_mod, "_smoke", lambda url: None)
    # T-0379: success path now asserts running==released sha.
    monkeypatch.setattr(apply_mod, "_running_git_sha", lambda cfg: good_entry["git_sha"])

    result = apply_mod.apply(cfg, good_entry)
    assert result.ok is True
    assert not alert_p.exists(), "next successful apply must clear the banner"


# ---------------------------------------------------------------------------
# retry_last_failed / autoupdate_retry action
# ---------------------------------------------------------------------------


def test_retry_last_failed_moves_newest_back(install_ctx):
    cfg = install_ctx["cfg"]
    fdir = apply_mod.failed_queue_dir(cfg)
    fdir.mkdir(parents=True, exist_ok=True)
    qdir = apply_mod.queue_dir(cfg)

    # Two parked files; the second is newer.
    entry_old = _make_entry("v2026.05.16.2", "a" * 64)
    entry_new = _make_entry("v2026.05.16.3", "b" * 64)
    f_old = fdir / "old.json"
    f_old.write_text(json.dumps(entry_old))
    f_new = fdir / "new.json"
    f_new.write_text(json.dumps(entry_new))
    os.utime(f_old, (1000, 1000))
    os.utime(f_new, (2000, 2000))

    out = apply_mod.retry_last_failed(cfg)

    assert out["ok"] is True
    assert out["requeued"] is True
    assert out["version"] == "v2026.05.16.3"
    assert out["queue_file"] == "new.json"
    # Newest moved to live queue; older still parked.
    assert (qdir / "new.json").exists()
    assert not f_new.exists()
    assert f_old.exists()


def test_retry_last_failed_idempotent_when_empty(install_ctx):
    cfg = install_ctx["cfg"]
    out = apply_mod.retry_last_failed(cfg)
    assert out == {"ok": True, "requeued": False, "reason": "failed-queue empty"}


def test_retry_last_failed_idempotent_when_dir_missing(install_ctx):
    """If failed-queue dir was never created, retry must still be a clean no-op."""
    cfg = install_ctx["cfg"]
    assert not apply_mod.failed_queue_dir(cfg).exists()
    out = apply_mod.retry_last_failed(cfg)
    assert out["requeued"] is False


def test_autoupdate_retry_action_dispatches(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    # Give the project a slug so the action's slug-check passes.
    cfg.projects["bot-squad"].__dict__["slug"] = "bot-squad"
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    # Empty failed-queue → idempotent path.
    out = dispatch("autoupdate_retry", {"slug": "bot-squad"})
    assert out == {"ok": True, "requeued": False, "reason": "failed-queue empty"}


def test_autoupdate_retry_rejects_unknown_slug(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="unknown project slug"):
        dispatch("autoupdate_retry", {"slug": "no-such-thing"})


def test_autoupdate_retry_rejects_extra_params(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="unexpected params"):
        dispatch("autoupdate_retry", {"slug": "bot-squad", "evil": "x"})


def test_autoupdate_retry_requires_slug(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="missing required"):
        dispatch("autoupdate_retry", {})


# ---------------------------------------------------------------------------
# force_apply / autoupdate_force action
# ---------------------------------------------------------------------------


def test_force_apply_fetches_manifest_and_enqueues(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example")

    target_entry = _make_entry("v2026.05.16.7", "c" * 64)

    captured: dict = {}
    def fake_get(url, timeout):  # noqa: ARG001
        captured["url"] = url
        return httpx.Response(200, json=target_entry, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)

    out = apply_mod.force_apply(cfg, "v2026.05.16.7")

    assert out["ok"] is True
    assert out["version"] == "v2026.05.16.7"
    assert captured["url"] == "https://mom.example/api/releases/v2026.05.16.7"

    # Queue file present with the fetched entry.
    qdir = apply_mod.queue_dir(cfg)
    queued = list(qdir.glob("*.json"))
    assert len(queued) == 1
    on_disk = json.loads(queued[0].read_text())
    assert on_disk["version"] == "v2026.05.16.7"
    assert on_disk["sha256"] == target_entry["sha256"]


def test_force_apply_fetch_failure_raises(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example")

    def fake_get(url, timeout):  # noqa: ARG001
        raise httpx.ConnectError("unreachable", request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(RuntimeError, match="could not fetch manifest"):
        apply_mod.force_apply(cfg, "v2026.05.16.7")

    # No queue file created on failure.
    qdir = apply_mod.queue_dir(cfg)
    assert not qdir.exists() or not list(qdir.glob("*.json"))


def test_force_apply_no_mothership_raises(install_ctx, monkeypatch):
    """Without BOT_SQUAD_MOTHERSHIP_URL, fetch is impossible → loud error."""
    cfg = install_ctx["cfg"]
    monkeypatch.delenv("BOT_SQUAD_MOTHERSHIP_URL", raising=False)
    with pytest.raises(RuntimeError, match="could not fetch manifest"):
        apply_mod.force_apply(cfg, "v2026.05.16.7")


def test_force_apply_rejects_version_mismatch(install_ctx, monkeypatch):
    """Defensive: mothership returns a different version than asked for → error."""
    cfg = install_ctx["cfg"]
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example")

    wrong = _make_entry("v9999.12.31.9", "d" * 64)
    def fake_get(url, timeout):  # noqa: ARG001
        return httpx.Response(200, json=wrong, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(RuntimeError, match="returned version"):
        apply_mod.force_apply(cfg, "v2026.05.16.7")


def test_autoupdate_force_action_dispatches(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mom.example")

    target = _make_entry("v2026.05.17.1", "e" * 64)
    def fake_get(url, timeout):  # noqa: ARG001
        return httpx.Response(200, json=target, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)

    out = dispatch("autoupdate_force", {"slug": "bot-squad", "version": "v2026.05.17.1"})
    assert out["ok"] is True
    assert out["version"] == "v2026.05.17.1"
    assert "queue_file" in out


def test_autoupdate_force_rejects_unknown_slug(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="unknown project slug"):
        dispatch("autoupdate_force", {"slug": "ghost", "version": "v1"})


def test_autoupdate_force_rejects_empty_version(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="empty version"):
        dispatch("autoupdate_force", {"slug": "bot-squad", "version": "   "})


def test_autoupdate_force_rejects_extra_params(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="unexpected params"):
        dispatch("autoupdate_force", {"slug": "bot-squad", "version": "v1", "wat": 1})


def test_autoupdate_force_requires_version(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError, match="missing required"):
        dispatch("autoupdate_force", {"slug": "bot-squad"})


def test_autoupdate_force_wraps_fetch_failures(install_ctx, monkeypatch):
    """Underlying RuntimeError surfaces as ActionError with the cause attached."""
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.delenv("BOT_SQUAD_MOTHERSHIP_URL", raising=False)
    with pytest.raises(ActionError, match="could not fetch manifest"):
        dispatch("autoupdate_force", {"slug": "bot-squad", "version": "v1"})


# ---------------------------------------------------------------------------
# Registry + mode wiring sanity
# ---------------------------------------------------------------------------


def test_new_actions_registered_and_mode_tagged():
    assert "autoupdate_retry" in ACTION_REGISTRY
    assert "autoupdate_force" in ACTION_REGISTRY
    assert ACTION_MODES["autoupdate_retry"] == "coordinator_only"
    assert ACTION_MODES["autoupdate_force"] == "coordinator_only"


def test_new_actions_refused_in_user_worker_mode():
    set_mode("user-worker")
    with pytest.raises(ActionError, match="not available in user-worker"):
        dispatch("autoupdate_retry", {"slug": "bot-squad"})
    with pytest.raises(ActionError, match="not available in user-worker"):
        dispatch("autoupdate_force", {"slug": "bot-squad", "version": "v1"})


# ---------------------------------------------------------------------------
# End-to-end: failure → retry → re-attempt drains
# ---------------------------------------------------------------------------


def test_apply_failure_into_retry_into_drain(install_ctx, monkeypatch):
    """Drain a failing job → autoupdate_retry → drain again resurfaces same entry.

    Proves the operator's recovery levers actually pull a failed job back
    into the apply pipeline (not just shuffling files for show).
    """
    cfg = install_ctx["cfg"]
    _pre_stamp(cfg, "v2026.05.16.1")

    # Enqueue a bad job (sha mismatch).
    qdir = apply_mod.queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    bad_entry = _make_entry("v2026.05.16.2", "deadbeef" * 8)
    qf = qdir / "deadbeef.json"
    qf.write_text(json.dumps(bad_entry))

    _install_fake_download(monkeypatch, _make_tarball("v2026.05.16.2"))

    # Drain — fails, parks the file.
    result = apply_mod.drain_one(cfg)
    assert result is not None and result.ok is False
    parked = apply_mod.failed_queue_dir(cfg) / "deadbeef.json"
    assert parked.exists()
    assert not qf.exists()

    # Operator retries.
    out = apply_mod.retry_last_failed(cfg)
    assert out["requeued"] is True
    # Same uuid is back in the live queue.
    assert (qdir / "deadbeef.json").exists()
    assert not parked.exists()

    # Next drain re-attempts (and re-parks, since we didn't fix the sha).
    result2 = apply_mod.drain_one(cfg)
    assert result2 is not None and result2.ok is False
    assert (apply_mod.failed_queue_dir(cfg) / "deadbeef.json").exists()


# ---------------------------------------------------------------------------
# autoupdate_check_now action (T-0089)
# ---------------------------------------------------------------------------


def test_autoupdate_check_now_falls_back_to_inline_tick(install_ctx, monkeypatch):
    """No scheduler injected: the action runs autoupdate.tick inline so the
    operator's "force a check" lever still does something useful."""
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    # Make sure no scheduler is registered for this test (autouse fixture
    # doesn't set one).
    A.set_scheduler(None)

    calls = {"n": 0}
    def fake_tick(_cfg):
        calls["n"] += 1
    monkeypatch.setattr(poller, "tick", fake_tick)

    out = dispatch("autoupdate_check_now", {})
    assert out == {"ok": True, "scheduled": False, "ran_inline": True}
    assert calls["n"] == 1


def test_autoupdate_check_now_reschedules_when_scheduler_present(
    install_ctx, monkeypatch
):
    """With a scheduler injected the action modifies the autoupdate job's
    next_run_time and returns the new value. No inline tick runs."""
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    # Tripwire: inline tick must NOT run when the scheduler path is taken.
    def fake_tick(_cfg):
        pytest.fail("tick must not run inline when scheduler is present")
    monkeypatch.setattr(poller, "tick", fake_tick)

    from datetime import datetime, timezone
    seen: dict = {}

    class FakeJob:
        def __init__(self, t):
            self.next_run_time = t

    class FakeSched:
        def modify_job(self, job_id, *, next_run_time):
            seen["id"] = job_id
            seen["nrt"] = next_run_time
            return FakeJob(next_run_time)

    A.set_scheduler(FakeSched())
    try:
        out = dispatch("autoupdate_check_now", {})
    finally:
        A.set_scheduler(None)

    assert out["ok"] is True
    assert out["scheduled"] is True
    assert out["next_run"] is not None
    assert seen["id"] == "autoupdate"
    # next_run_time was set to ~now (UTC). Allow a generous skew window.
    now = datetime.now(timezone.utc)
    assert abs((now - seen["nrt"]).total_seconds()) < 5


def test_autoupdate_check_now_rejects_params(install_ctx, monkeypatch):
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    A.set_scheduler(None)
    with pytest.raises(ActionError, match="no params"):
        dispatch("autoupdate_check_now", {"slug": "bot-squad"})


def test_autoupdate_check_now_surfaces_scheduler_errors(install_ctx, monkeypatch):
    """A JobLookupError / scheduler-not-running becomes an ActionError so
    the API gets a usable 502 instead of a stack trace."""
    cfg = install_ctx["cfg"]
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    class ExplodingSched:
        def modify_job(self, *a, **kw):
            raise RuntimeError("scheduler not running")

    A.set_scheduler(ExplodingSched())
    try:
        with pytest.raises(ActionError, match="could not reschedule"):
            dispatch("autoupdate_check_now", {})
    finally:
        A.set_scheduler(None)


def test_autoupdate_check_now_registered_as_coordinator_only(install_ctx):
    """Mode tag wiring: the action must be coordinator-only, not tmux_only."""
    assert "autoupdate_check_now" in ACTION_REGISTRY
    assert ACTION_MODES["autoupdate_check_now"] == "coordinator_only"
