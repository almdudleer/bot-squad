"""Tests for /api/projects/{slug}/autonomous endpoints."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_STATUS = {
    "ok": True,
    "slug": "test-project",
    "enabled": False,
    "status": "idle",
    "current_task_id": None,
    "current_pane_id": None,
    "current_started_at": None,
    "last_tick_at": None,
    "sleep_start_hour": 22,
    "sleep_end_hour": 8,
    "fail_counts": {},
    "tick_log": [],
}


@pytest.fixture
def fake_worker_autonomous(tmp_bot_squad: Path):
    """Fake worker that handles autonomous actions."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()

    @fake.post("/actions/noop")
    def noop(params: dict | None = None) -> dict:
        return {"ok": True, "ts": 99}

    @fake.post("/actions/autonomous_status")
    def autonomous_status(params: dict | None = None) -> dict:
        return dict(_SAMPLE_STATUS)

    @fake.post("/actions/autonomous_enable")
    def autonomous_enable(params: dict | None = None) -> dict:
        return {"ok": True, "slug": "test-project", "enabled": True}

    @fake.post("/actions/autonomous_disable")
    def autonomous_disable(params: dict | None = None) -> dict:
        return {"ok": True, "slug": "test-project", "enabled": False}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock

    server.should_exit = True
    thread.join(timeout=5)


def _client_logged_in(tmp_bot_squad: Path, monkeypatch, sock_path: Path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


def _anon_client(tmp_bot_squad: Path, monkeypatch, sock_path: Path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/autonomous
# ---------------------------------------------------------------------------

def test_get_autonomous_status_success(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.get("/api/projects/test-project/autonomous")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["enabled"] is False
    assert data["status"] == "idle"


def test_get_autonomous_status_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.get("/api/projects/test-project/autonomous")
    assert r.status_code == 401


def test_get_autonomous_status_unknown_project_404(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.get("/api/projects/no-such-project/autonomous")
    assert r.status_code == 404


def test_get_autonomous_status_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.get("/api/projects/test-project/autonomous")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/autonomous/enable
# ---------------------------------------------------------------------------

def test_enable_autonomous_success(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.post("/api/projects/test-project/autonomous/enable", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["enabled"] is True


def test_enable_autonomous_with_sleep_hours(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.post(
            "/api/projects/test-project/autonomous/enable",
            json={"sleep_start_hour": 23, "sleep_end_hour": 7},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_enable_autonomous_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.post("/api/projects/test-project/autonomous/enable", json={})
    assert r.status_code == 401


def test_enable_autonomous_unknown_project_404(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.post("/api/projects/no-such-project/autonomous/enable", json={})
    assert r.status_code == 404


def test_enable_autonomous_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post("/api/projects/test-project/autonomous/enable", json={})
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/autonomous/disable
# ---------------------------------------------------------------------------

def test_disable_autonomous_success(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.post("/api/projects/test-project/autonomous/disable")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["enabled"] is False


def test_disable_autonomous_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.post("/api/projects/test-project/autonomous/disable")
    assert r.status_code == 401


def test_disable_autonomous_unknown_project_404(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.post("/api/projects/no-such-project/autonomous/disable")
    assert r.status_code == 404


def test_disable_autonomous_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post("/api/projects/test-project/autonomous/disable")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/autonomous/log
# ---------------------------------------------------------------------------

def test_get_autonomous_log_returns_list(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.get("/api/projects/test-project/autonomous/log")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_get_autonomous_log_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.get("/api/projects/test-project/autonomous/log")
    assert r.status_code == 401


def test_get_autonomous_log_unknown_project_404(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_autonomous) as client:
        r = client.get("/api/projects/no-such-project/autonomous/log")
    assert r.status_code == 404


def test_get_autonomous_log_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_autonomous: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.get("/api/projects/test-project/autonomous/log")
    assert r.status_code == 502
