"""Tests for the consumer-side autoupdate poller (T-0083).

All HTTP is mocked — no real network is ever touched. The mothership
self-exclusion path is exercised by patching ``is_mothership`` on the
autoupdate module.
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot_squad_worker import autoupdate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path) -> Any:
    """Minimal cfg stub: just needs ``data_dir``."""
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True)
    return types.SimpleNamespace(data_dir=data_dir)


def _entry(version: str = "v2026.05.16.2", git_sha: str = "abc123def") -> dict:
    return {
        "version": version,
        "git_sha": git_sha,
        "created_at": "2026-05-16T16:35:00Z",
        "tarball_path": f"data/bot-squad/releases/{version}.tar.gz",
        "sha256": "deadbeef" * 8,
        "notes": "",
    }


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip env vars that could change tick behavior under test."""
    for key in (
        "BOT_SQUAD_MOTHERSHIP_URL",
        "BOTSQUAD_MOTHERSHIP_URL",
        "BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS",
        "BOT_SQUAD_INSTALL_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    # Default to consumer; tests opt in to mothership mode.
    monkeypatch.setattr(autoupdate, "is_mothership", lambda *a, **kw: False)


# ---------------------------------------------------------------------------
# Env / config
# ---------------------------------------------------------------------------

def test_interval_seconds_default():
    assert autoupdate.interval_seconds() == 900


def test_interval_seconds_env_override(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS", "120")
    assert autoupdate.interval_seconds() == 120


def test_interval_seconds_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS", "not-a-number")
    assert autoupdate.interval_seconds() == 900


def test_mothership_url_primary_env(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example/")
    assert autoupdate.mothership_url() == "https://mothership.example"


def test_mothership_url_legacy_fallback(monkeypatch):
    monkeypatch.setenv("BOTSQUAD_MOTHERSHIP_URL", "https://legacy.example/")
    assert autoupdate.mothership_url() == "https://legacy.example"


def test_mothership_url_unset():
    assert autoupdate.mothership_url() is None


# ---------------------------------------------------------------------------
# Version compare
# ---------------------------------------------------------------------------

def test_is_newer_simple():
    assert autoupdate.is_newer("v2026.05.17.1", "v2026.05.16.1") is True
    assert autoupdate.is_newer("v2026.05.16.2", "v2026.05.16.1") is True
    assert autoupdate.is_newer("v2026.05.16.1", "v2026.05.16.1") is False
    assert autoupdate.is_newer("v2026.05.16.1", "v2026.05.16.2") is False


def test_is_newer_handles_double_digit_counter():
    """N>9 still sorts numerically (would fail under naive lex compare)."""
    assert autoupdate.is_newer("v2026.05.16.10", "v2026.05.16.2") is True


def test_is_newer_handles_month_rollover():
    assert autoupdate.is_newer("v2026.06.01.1", "v2026.05.31.9") is True


def test_is_newer_malformed_installed_loses():
    """If installed_version is garbage, any sane manifest wins (we'd rather try)."""
    assert autoupdate.is_newer("v2026.05.16.1", "garbage") is True


# ---------------------------------------------------------------------------
# State round-trip
# ---------------------------------------------------------------------------

def test_load_state_missing_returns_skeleton(tmp_path):
    cfg = _make_cfg(tmp_path)
    state = autoupdate.load_state(cfg)
    assert state["installed_version"] is None
    assert state["last_apply_outcome"] == "never"


def test_save_load_round_trip(tmp_path):
    cfg = _make_cfg(tmp_path)
    autoupdate.save_state(cfg, {
        "installed_version": "v2026.05.16.1",
        "last_check_at": "2026-05-16T17:00:00+00:00",
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "abc",
    })
    state = autoupdate.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.1"
    assert state["current_git_sha"] == "abc"


def test_load_state_corrupt_returns_skeleton(tmp_path):
    cfg = _make_cfg(tmp_path)
    autoupdate.state_path(cfg).write_text("{not-json")
    state = autoupdate.load_state(cfg)
    assert state["installed_version"] is None


# ---------------------------------------------------------------------------
# _fetch_latest
# ---------------------------------------------------------------------------

def test_fetch_latest_success(monkeypatch):
    entry = _entry()

    def fake_get(url, timeout):  # noqa: ARG001
        return httpx.Response(200, json=entry, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    result = autoupdate._fetch_latest("https://mothership.example")
    assert result == entry


def test_fetch_latest_retries_once_on_transient_error(monkeypatch):
    calls = {"n": 0}
    entry = _entry()

    def fake_get(url, timeout):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))
        return httpx.Response(200, json=entry, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    result = autoupdate._fetch_latest("https://mothership.example")
    assert calls["n"] == 2
    assert result == entry


def test_fetch_latest_gives_up_after_retry(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, timeout):  # noqa: ARG001
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    result = autoupdate._fetch_latest("https://mothership.example")
    assert calls["n"] == 2  # one retry, then give up
    assert result is None


def test_fetch_latest_non_2xx_returns_none(monkeypatch):
    def fake_get(url, timeout):  # noqa: ARG001
        return httpx.Response(503, text="upstream down", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert autoupdate._fetch_latest("https://mothership.example") is None


# ---------------------------------------------------------------------------
# tick — happy paths
# ---------------------------------------------------------------------------

def _install_fake_fetch(monkeypatch, entry: dict | None):
    def fake_fetch(base_url, *, timeout=10.0):  # noqa: ARG001
        return entry
    monkeypatch.setattr(autoupdate, "_fetch_latest", fake_fetch)


def test_tick_first_run_stamps_and_does_not_enqueue(tmp_path, monkeypatch):
    """First run with no autoupdate.json: stamp current latest, do NOT enqueue."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    entry = _entry(version="v2026.05.16.3", git_sha="deadbeef")
    _install_fake_fetch(monkeypatch, entry)

    autoupdate.tick(cfg)

    state = autoupdate.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.3"
    assert state["current_git_sha"] == "deadbeef"
    assert state["last_check_at"] is not None

    qdir = autoupdate.queue_dir(cfg)
    assert not qdir.exists() or list(qdir.glob("*.json")) == []


def test_tick_newer_version_enqueues_apply_job(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    # Pre-stamp installed_version older than what mothership returns.
    autoupdate.save_state(cfg, {
        "installed_version": "v2026.05.16.1",
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "old",
    })
    entry = _entry(version="v2026.05.16.2", git_sha="newsha")
    _install_fake_fetch(monkeypatch, entry)

    autoupdate.tick(cfg)

    queued = list(autoupdate.queue_dir(cfg).glob("*.json"))
    assert len(queued) == 1
    body = json.loads(queued[0].read_text())
    assert body["version"] == "v2026.05.16.2"
    assert body["git_sha"] == "newsha"

    # installed_version is NOT bumped here — that's T-0084's job after apply.
    state = autoupdate.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.1"
    assert state["last_check_at"] is not None


def test_tick_same_version_is_noop(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    autoupdate.save_state(cfg, {
        "installed_version": "v2026.05.16.2",
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "samesha",
    })
    _install_fake_fetch(monkeypatch, _entry(version="v2026.05.16.2"))

    autoupdate.tick(cfg)

    queued = list(autoupdate.queue_dir(cfg).glob("*.json")) if autoupdate.queue_dir(cfg).exists() else []
    assert queued == []
    state = autoupdate.load_state(cfg)
    assert state["installed_version"] == "v2026.05.16.2"
    assert state["last_check_at"] is not None


def test_tick_older_manifest_is_noop(tmp_path, monkeypatch):
    """Defensive: mothership reporting an older version doesn't trigger downgrade."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    autoupdate.save_state(cfg, {
        "installed_version": "v2026.05.17.1",
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "newer",
    })
    _install_fake_fetch(monkeypatch, _entry(version="v2026.05.16.1"))

    autoupdate.tick(cfg)

    queued = list(autoupdate.queue_dir(cfg).glob("*.json")) if autoupdate.queue_dir(cfg).exists() else []
    assert queued == []
    assert autoupdate.load_state(cfg)["installed_version"] == "v2026.05.17.1"


# ---------------------------------------------------------------------------
# tick — short-circuits
# ---------------------------------------------------------------------------

def test_tick_mothership_short_circuits(tmp_path, monkeypatch):
    """On the mothership itself, tick is a complete no-op (no HTTP, no state writes)."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    monkeypatch.setattr(autoupdate, "is_mothership", lambda *a, **kw: True)

    fetched = {"n": 0}
    def fake_fetch(*a, **kw):
        fetched["n"] += 1
        return _entry()
    monkeypatch.setattr(autoupdate, "_fetch_latest", fake_fetch)

    autoupdate.tick(cfg)

    assert fetched["n"] == 0
    # No autoupdate.json should have been created on the mothership.
    assert not autoupdate.state_path(cfg).exists()


def test_tick_missing_mothership_url_is_noop(tmp_path, monkeypatch):
    """Without BOT_SQUAD_MOTHERSHIP_URL, tick does nothing (and doesn't crash)."""
    cfg = _make_cfg(tmp_path)
    fetched = {"n": 0}
    def fake_fetch(*a, **kw):
        fetched["n"] += 1
        return None
    monkeypatch.setattr(autoupdate, "_fetch_latest", fake_fetch)

    autoupdate.tick(cfg)

    assert fetched["n"] == 0
    assert not autoupdate.state_path(cfg).exists()


def test_tick_stamps_last_check_at_even_on_fetch_failure(tmp_path, monkeypatch):
    """Operator-UI liveness: last_check_at advances even when mothership is down."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    _install_fake_fetch(monkeypatch, None)  # simulate fetch failure

    autoupdate.tick(cfg)

    state = autoupdate.load_state(cfg)
    assert state["last_check_at"] is not None
    assert state["installed_version"] is None  # still no stamp on failure


def test_tick_bad_entry_does_not_enqueue(tmp_path, monkeypatch):
    """Manifest missing 'version' is treated as a fetch failure (no enqueue, no stamp)."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    _install_fake_fetch(monkeypatch, {"git_sha": "abc"})  # no 'version' key

    autoupdate.tick(cfg)

    queued = list(autoupdate.queue_dir(cfg).glob("*.json")) if autoupdate.queue_dir(cfg).exists() else []
    assert queued == []
    state = autoupdate.load_state(cfg)
    assert state["installed_version"] is None


# ---------------------------------------------------------------------------
# Pause flag (T-0089)
# ---------------------------------------------------------------------------


def test_is_paused_reflects_flag_presence(tmp_path):
    """is_paused is presence-only — contents of the flag file don't matter."""
    cfg = _make_cfg(tmp_path)
    assert autoupdate.is_paused(cfg) is False
    flag = autoupdate.paused_flag_path(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("")  # empty is fine
    assert autoupdate.is_paused(cfg) is True
    flag.unlink()
    assert autoupdate.is_paused(cfg) is False


def test_tick_newer_version_does_not_enqueue_when_paused(tmp_path, monkeypatch):
    """T-0089: with the pause flag set, a newer manifest entry is logged
    but NOT enqueued. last_check_at still advances so the UI's liveness
    clock keeps moving."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    autoupdate.save_state(cfg, {
        "installed_version": "v2026.05.16.1",
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "old",
    })
    # Pause before the tick.
    flag = autoupdate.paused_flag_path(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("")
    _install_fake_fetch(monkeypatch, _entry(version="v2026.05.16.2"))

    autoupdate.tick(cfg)

    # No enqueue.
    qdir = autoupdate.queue_dir(cfg)
    assert not qdir.exists() or list(qdir.glob("*.json")) == []
    # Last-check-at still advanced.
    state = autoupdate.load_state(cfg)
    assert state["last_check_at"] is not None
    # installed_version unchanged.
    assert state["installed_version"] == "v2026.05.16.1"


def test_handle_latest_returns_paused_marker_when_paused(tmp_path, monkeypatch):
    """The internal status string is "paused" — useful for log analysis
    and any future test that wants to assert the codepath was taken
    rather than just observing absence."""
    cfg = _make_cfg(tmp_path)
    autoupdate.save_state(cfg, {
        "installed_version": "v2026.05.16.1",
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "old",
    })
    flag = autoupdate.paused_flag_path(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("")
    result = autoupdate._handle_latest(cfg, _entry(version="v2026.05.16.2"))
    assert result == "paused"


def test_first_run_still_stamps_when_paused(tmp_path):
    """First-run stamping is bookkeeping, not an apply — should run even
    when paused. Otherwise a paused fresh install would never learn what
    version it's on, breaking the UI pill's default state."""
    cfg = _make_cfg(tmp_path)
    # No prior state.
    flag = autoupdate.paused_flag_path(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("")
    result = autoupdate._handle_latest(cfg, _entry(version="v2026.05.16.2"))
    assert result == "first_run_stamped"
    assert autoupdate.load_state(cfg)["installed_version"] == "v2026.05.16.2"


# ---------------------------------------------------------------------------
# install_id resolution (T-0088)
# ---------------------------------------------------------------------------

def test_install_id_env_takes_precedence(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # File present AND env set — env wins.
    autoupdate.install_id_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    autoupdate.install_id_path(cfg).write_text("srv_from_file\n")
    monkeypatch.setenv("BOT_SQUAD_INSTALL_ID", "srv_from_env")
    assert autoupdate.install_id(cfg) == "srv_from_env"


def test_install_id_reads_from_file_when_env_unset(tmp_path):
    cfg = _make_cfg(tmp_path)
    autoupdate.install_id_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    autoupdate.install_id_path(cfg).write_text("srv_from_file\n")
    assert autoupdate.install_id(cfg) == "srv_from_file"


def test_install_id_missing_everywhere_returns_none(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert autoupdate.install_id(cfg) is None


def test_install_id_empty_file_returns_none(tmp_path):
    cfg = _make_cfg(tmp_path)
    autoupdate.install_id_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    autoupdate.install_id_path(cfg).write_text("   \n")
    assert autoupdate.install_id(cfg) is None


# ---------------------------------------------------------------------------
# Telemetry POST (T-0088)
# ---------------------------------------------------------------------------

def _capture_post(monkeypatch, *, status_code: int = 204):
    """Replace httpx.post with a capture stub; returns the calls list."""
    calls: list[dict] = []

    def fake_post(url, json, timeout):  # noqa: A002 — match httpx signature
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(status_code, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def _seed_install_id(cfg, value: str = "srv_test_consumer") -> None:
    autoupdate.install_id_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    autoupdate.install_id_path(cfg).write_text(value + "\n")


def test_post_telemetry_sends_full_snapshot(monkeypatch):
    calls = _capture_post(monkeypatch, status_code=204)
    state = {
        "installed_version": "v2026.05.16.2",
        "last_check_at": "2026-05-16T17:00:00+00:00",
        "last_apply_at": "2026-05-16T16:30:00+00:00",
        "last_apply_outcome": "success",
        "current_git_sha": "abc123",
    }
    ok = autoupdate._post_telemetry("https://mothership.example/", "srv_x", state)
    assert ok is True
    assert len(calls) == 1
    sent = calls[0]
    assert sent["url"] == "https://mothership.example/api/releases/_telemetry"
    assert sent["json"]["install_id"] == "srv_x"
    for k, v in state.items():
        assert sent["json"][k] == v


def test_post_telemetry_drops_unknown_state_keys(monkeypatch):
    """Only spec'd fields ride along — a future debug-only key in
    autoupdate.json must not leak over the wire."""
    calls = _capture_post(monkeypatch)
    state = {
        "installed_version": "v2026.05.16.2",
        "last_check_at": "2026-05-16T17:00:00+00:00",
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": None,
        "secret_debug_field": "should-not-leak",
    }
    autoupdate._post_telemetry("https://mothership.example", "srv_x", state)
    assert "secret_debug_field" not in calls[0]["json"]


def test_post_telemetry_returns_false_on_403(monkeypatch):
    _capture_post(monkeypatch, status_code=403)
    ok = autoupdate._post_telemetry(
        "https://mothership.example", "srv_unknown", {}
    )
    assert ok is False


def test_post_telemetry_returns_false_on_500(monkeypatch):
    _capture_post(monkeypatch, status_code=500)
    ok = autoupdate._post_telemetry("https://mothership.example", "srv_x", {})
    assert ok is False


def test_post_telemetry_returns_false_on_transport_error(monkeypatch):
    def boom(url, json, timeout):  # noqa: ARG001, A002
        raise httpx.ConnectError("nope", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", boom)
    ok = autoupdate._post_telemetry("https://mothership.example", "srv_x", {})
    assert ok is False


# ---------------------------------------------------------------------------
# tick() — telemetry piggy-back (T-0088)
# ---------------------------------------------------------------------------

def test_tick_piggybacks_telemetry_post_after_fetch(tmp_path, monkeypatch):
    """Happy path: tick fetches latest, then POSTs telemetry with the
    freshly-stamped state."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    _seed_install_id(cfg, "srv_consumer_one")
    _install_fake_fetch(monkeypatch, _entry(version="v2026.05.16.5", git_sha="sha5"))
    calls = _capture_post(monkeypatch)

    autoupdate.tick(cfg)

    assert len(calls) == 1
    sent = calls[0]["json"]
    assert sent["install_id"] == "srv_consumer_one"
    # First-run-stamped bumps installed_version BEFORE the telemetry POST
    # reloads state, so the mothership sees the post-handle snapshot.
    assert sent["installed_version"] == "v2026.05.16.5"
    assert sent["current_git_sha"] == "sha5"


def test_tick_skips_telemetry_when_install_id_missing(tmp_path, monkeypatch):
    """No install.id + no env → telemetry POST is silently skipped (the
    tick itself still updates state via _handle_latest)."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    _install_fake_fetch(monkeypatch, _entry())
    calls = _capture_post(monkeypatch)

    autoupdate.tick(cfg)

    assert calls == []  # never posted
    # but state was still stamped — tick wasn't blocked.
    assert autoupdate.load_state(cfg)["installed_version"] is not None


def test_tick_posts_telemetry_even_when_fetch_fails(tmp_path, monkeypatch):
    """Mothership read failure must NOT suppress the write-side ping —
    that's how the operator UI's "last seen" clock keeps moving while
    the read path is flaky."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    _seed_install_id(cfg, "srv_consumer_one")
    # Pre-stamp some state so the POST has interesting content.
    autoupdate.save_state(cfg, {
        "installed_version": "v2026.05.16.1",
        "last_check_at": None,
        "last_apply_at": None,
        "last_apply_outcome": "never",
        "current_git_sha": "old",
    })
    _install_fake_fetch(monkeypatch, None)  # fetch fails
    calls = _capture_post(monkeypatch)

    autoupdate.tick(cfg)

    assert len(calls) == 1
    assert calls[0]["json"]["install_id"] == "srv_consumer_one"
    assert calls[0]["json"]["installed_version"] == "v2026.05.16.1"
    # last_check_at was stamped pre-fetch and the POST reads fresh state.
    assert calls[0]["json"]["last_check_at"] is not None


def test_tick_telemetry_post_failure_does_not_break_tick(tmp_path, monkeypatch):
    """A 403 from the mothership is logged but does not raise — the tick
    completes normally and the next tick will retry."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    _seed_install_id(cfg, "srv_unknown")
    _install_fake_fetch(monkeypatch, _entry())
    _capture_post(monkeypatch, status_code=403)

    # Must not raise.
    autoupdate.tick(cfg)

    # State was still updated by _handle_latest's first-run path.
    assert autoupdate.load_state(cfg)["installed_version"] is not None


def test_tick_mothership_skips_telemetry_too(tmp_path, monkeypatch):
    """Mothership self-exclusion short-circuits before telemetry POST."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.example")
    monkeypatch.setattr(autoupdate, "is_mothership", lambda *a, **kw: True)
    _seed_install_id(cfg, "srv_does_not_matter")
    calls = _capture_post(monkeypatch)

    autoupdate.tick(cfg)

    assert calls == []
