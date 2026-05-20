"""Tests for the consumer autoupdate status/pause/check-now router (T-0089).

Covers the DoD cases from
``backlog/T-0089-per-consumer-autoupdate-status-pill-and-pause.md``:

* GET /status returns a composite of autoupdate.json + autoupdate_alert.json
  + the pause-flag presence.
* POST /pause flips the on-disk flag and is idempotent both ways.
* POST /check_now invokes the worker ``autoupdate_check_now`` action and
  surfaces its response.
* All three endpoints 404 on the mothership build (MOTHERSHIP=1).
* Auth is required (no session cookie → 401).

The worker bridge is faked via the existing ``fake_worker`` conftest
fixture pattern so ``check_now`` doesn't need a real APScheduler.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _client(
    tmp_bot_squad: Path, monkeypatch, *, mothership: bool = False
) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv(
        "WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock")
    )
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    # Same SPA-avoidance trick used in the telemetry tests so /api/* 404s
    # come from the router, not the SPA catch-all.
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)
    return TestClient(build_app())


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def _worker_dir(tmp_bot_squad: Path) -> Path:
    return tmp_bot_squad / "data" / "_worker"


def _seed_state(tmp_bot_squad: Path, state: dict) -> None:
    p = _worker_dir(tmp_bot_squad) / "autoupdate.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))


def _seed_alert(tmp_bot_squad: Path, alert: dict) -> None:
    p = _worker_dir(tmp_bot_squad) / "autoupdate_alert.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(alert))


def _seed_queued_job(tmp_bot_squad: Path, version: str, *, name: str = "job1.json") -> None:
    qdir = _worker_dir(tmp_bot_squad) / "autoupdate_queue"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / name).write_text(json.dumps({"version": version, "sha256": "deadbeef" * 8}))


# ---------------------------------------------------------------------------
# Worker fake — only mounts ``autoupdate_check_now`` because that's all
# this router proxies. Captures the call so a single test can assert
# the bridge was invoked correctly.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_autoupdate_worker(tmp_bot_squad: Path):
    """Minimal fake worker that responds to ``/actions/autoupdate_check_now``.

    Records each call body in ``calls`` so tests can verify the API
    forwarded the action without mutating params on the way through.
    """
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()
    calls: list[dict] = []

    @fake.post("/actions/autoupdate_check_now")
    def check_now(params: dict | None = None) -> dict:
        calls.append(params or {})
        return {
            "ok": True,
            "scheduled": True,
            "next_run": "2026-05-20T00:00:00+00:00",
        }

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)
    try:
        yield calls
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


def test_status_returns_composite_state(tmp_bot_squad: Path, monkeypatch):
    """Happy path: state + alert + pause flag all surface in the response."""
    _seed_state(
        tmp_bot_squad,
        {
            "installed_version": "v2026.05.16.1",
            "last_check_at": "2026-05-16T17:00:00+00:00",
            "last_apply_at": "2026-05-16T16:30:00+00:00",
            "last_apply_outcome": "success",
            "current_git_sha": "abc123def456",
        },
    )
    monkeypatch.setenv("BOT_SQUAD_MOTHERSHIP_URL", "https://mothership.test/")

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["installed_version"] == "v2026.05.16.1"
    assert body["last_check_at"] == "2026-05-16T17:00:00+00:00"
    assert body["last_apply_outcome"] == "success"
    assert body["paused"] is False
    assert body["alert"] is None
    assert body["pending_apply_version"] is None
    # mothership_url is stripped of trailing slash
    assert body["mothership_url"] == "https://mothership.test"
    # next_check_at = last_check_at + interval; default interval is 900s
    assert body["next_check_at"] == "2026-05-16T17:15:00+00:00"
    assert body["poll_interval_seconds"] == 900


def test_status_surfaces_pending_apply_version(tmp_bot_squad: Path, monkeypatch):
    """A queued apply job populates pending_apply_version so the UI can
    show 'applying v...'. With multiple queued, the oldest (by mtime) wins —
    that's the one T-0084's drain loop will pick up next."""
    _seed_state(
        tmp_bot_squad,
        {"installed_version": "v2026.05.16.1", "last_apply_outcome": "success"},
    )
    _seed_queued_job(tmp_bot_squad, "v2026.05.16.2", name="oldest.json")

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 200
    assert r.json()["pending_apply_version"] == "v2026.05.16.2"


def test_status_pending_apply_is_none_when_queue_unreadable(
    tmp_bot_squad: Path, monkeypatch
):
    """A corrupt queue file (or non-dict body) doesn't crash the endpoint."""
    qdir = _worker_dir(tmp_bot_squad) / "autoupdate_queue"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "broken.json").write_text("{not json")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 200
    assert r.json()["pending_apply_version"] is None


def test_status_with_alert_surfaces_alert_block(tmp_bot_squad: Path, monkeypatch):
    """A persisted alert is round-tripped verbatim for the UI banner link."""
    _seed_state(
        tmp_bot_squad,
        {
            "installed_version": "v2026.05.16.1",
            "last_check_at": "2026-05-16T17:00:00+00:00",
            "last_apply_at": "2026-05-16T17:01:00+00:00",
            "last_apply_outcome": "failed:smoke",
        },
    )
    alert = {
        "version": "v2026.05.16.2",
        "step": "smoke",
        "log_tail": "smoke failed after 4 attempts",
        "occurred_at": "2026-05-16T17:01:00+00:00",
        "retry_command": "bot-squad-cli autoupdate retry",
        "force_command": "bot-squad-cli autoupdate force v2026.05.16.2",
    }
    _seed_alert(tmp_bot_squad, alert)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 200
    body = r.json()
    assert body["last_apply_outcome"] == "failed:smoke"
    assert body["alert"] == alert


def test_status_fresh_install_no_state_returns_skeleton(
    tmp_bot_squad: Path, monkeypatch
):
    """Brand-new install: autoupdate.json doesn't exist yet → graceful nulls.

    Mirrors the worker's load_state skeleton — last_apply_outcome defaults
    to "never" so the UI has a non-null discriminator to render against.
    """
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 200
    body = r.json()
    assert body["installed_version"] is None
    assert body["last_check_at"] is None
    assert body["next_check_at"] is None
    assert body["last_apply_outcome"] == "never"
    assert body["paused"] is False
    assert body["alert"] is None


def test_status_paused_flag_reflected_in_response(
    tmp_bot_squad: Path, monkeypatch
):
    """Operator-paused installs report paused=True without needing other state."""
    flag = _worker_dir(tmp_bot_squad) / "autoupdate_paused.flag"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("{}")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 200
    assert r.json()["paused"] is True


def test_status_corrupt_state_file_does_not_500(tmp_bot_squad: Path, monkeypatch):
    """A partially-written autoupdate.json never crashes the pill — the
    UI degrades to "checked never" rather than a 500."""
    p = _worker_dir(tmp_bot_squad) / "autoupdate.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json {[")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 200
    body = r.json()
    assert body["installed_version"] is None
    assert body["last_apply_outcome"] == "never"


def test_status_custom_interval_env_propagates(tmp_bot_squad: Path, monkeypatch):
    """``BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS`` is reflected in the response
    so the UI countdown stays in sync with the worker."""
    _seed_state(
        tmp_bot_squad,
        {
            "installed_version": "v2026.05.16.1",
            "last_check_at": "2026-05-16T17:00:00+00:00",
            "last_apply_outcome": "success",
        },
    )
    monkeypatch.setenv("BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS", "300")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    body = r.json()
    assert body["poll_interval_seconds"] == 300
    assert body["next_check_at"] == "2026-05-16T17:05:00+00:00"


# ---------------------------------------------------------------------------
# POST /pause
# ---------------------------------------------------------------------------


def test_pause_creates_flag_file(tmp_bot_squad: Path, monkeypatch):
    flag = _worker_dir(tmp_bot_squad) / "autoupdate_paused.flag"
    assert not flag.exists()
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post("/api/autoupdate/pause", json={"paused": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "paused": True}
    assert flag.exists()


def test_unpause_removes_flag_file(tmp_bot_squad: Path, monkeypatch):
    flag = _worker_dir(tmp_bot_squad) / "autoupdate_paused.flag"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("{}")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post("/api/autoupdate/pause", json={"paused": False})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "paused": False}
    assert not flag.exists()


def test_pause_is_idempotent_both_ways(tmp_bot_squad: Path, monkeypatch):
    """Re-pausing or re-unpausing both succeed without flapping."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        # double pause
        r1 = client.post("/api/autoupdate/pause", json={"paused": True})
        r2 = client.post("/api/autoupdate/pause", json={"paused": True})
        assert r1.status_code == 200 and r1.json()["paused"] is True
        assert r2.status_code == 200 and r2.json()["paused"] is True
        # double unpause
        r3 = client.post("/api/autoupdate/pause", json={"paused": False})
        r4 = client.post("/api/autoupdate/pause", json={"paused": False})
        assert r3.status_code == 200 and r3.json()["paused"] is False
        assert r4.status_code == 200 and r4.json()["paused"] is False


def test_pause_missing_field_400s(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post("/api/autoupdate/pause", json={})
    assert r.status_code == 400


def test_pause_wrong_type_400s(tmp_bot_squad: Path, monkeypatch):
    """Non-boolean ``paused`` is rejected — refuses to coerce "true" / 1 /
    null so the contract stays narrow."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        for bad in ["true", 1, 0, None, "yes", []]:
            r = client.post("/api/autoupdate/pause", json={"paused": bad})
            assert r.status_code == 400, (bad, r.text)


def test_pause_status_reflects_change(tmp_bot_squad: Path, monkeypatch):
    """End-to-end through both endpoints: pause, then GET /status sees it."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        client.post("/api/autoupdate/pause", json={"paused": True})
        r = client.get("/api/autoupdate/status")
        assert r.json()["paused"] is True
        client.post("/api/autoupdate/pause", json={"paused": False})
        r = client.get("/api/autoupdate/status")
        assert r.json()["paused"] is False


# ---------------------------------------------------------------------------
# POST /check_now
# ---------------------------------------------------------------------------


def test_check_now_proxies_to_worker(
    tmp_bot_squad: Path, monkeypatch, fake_autoupdate_worker
):
    """Happy path: API forwards the action and returns the worker payload."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post("/api/autoupdate/check_now")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scheduled"] is True
    assert body["next_run"] == "2026-05-20T00:00:00+00:00"
    # The action takes no params; verify we didn't smuggle anything through.
    assert fake_autoupdate_worker == [{}]


def test_check_now_502_when_worker_unreachable(tmp_bot_squad: Path, monkeypatch):
    """No fake worker = no socket. API surfaces a 502 rather than 500
    so the UI can show a "worker down" hint instead of an opaque error."""
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.post("/api/autoupdate/check_now")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# Mothership-gate: all three endpoints 404 when MOTHERSHIP=1
# ---------------------------------------------------------------------------


def test_status_404s_on_mothership(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 404


def test_pause_404s_on_mothership(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post("/api/autoupdate/pause", json={"paused": True})
    assert r.status_code == 404


def test_check_now_404s_on_mothership(
    tmp_bot_squad: Path, monkeypatch, fake_autoupdate_worker
):
    """Even with a worker reachable, mothership returns 404 — the gate is
    install-role-based, not worker-availability-based."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.post("/api/autoupdate/check_now")
    assert r.status_code == 404
    # The worker MUST NOT have been called.
    assert fake_autoupdate_worker == []


# ---------------------------------------------------------------------------
# Auth gate: anonymous → 401 on every endpoint
# ---------------------------------------------------------------------------


def test_status_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/autoupdate/status")
    assert r.status_code == 401


def test_pause_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.post("/api/autoupdate/pause", json={"paused": True})
    assert r.status_code == 401


def test_check_now_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.post("/api/autoupdate/check_now")
    assert r.status_code == 401
