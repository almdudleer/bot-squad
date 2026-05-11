"""Tests for GET /api/scheduler endpoint."""
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
# Sample scheduler state returned by the fake worker
# ---------------------------------------------------------------------------

_SAMPLE_SCHEDULER_STATE = {
    "jobs": [
        {"id": "heartbeat", "next_run": "2026-05-11T12:01:00+00:00", "trigger": "interval[60s]"},
        {"id": "deploy_monitor", "next_run": "2026-05-11T12:01:00+00:00", "trigger": "interval[60s]"},
        {"id": "kick_stuck", "next_run": "2026-05-11T11:59:00+00:00", "trigger": "cron[11:59 UTC]"},
        {"id": "oauth_refresh", "next_run": "2026-05-11T18:00:00+00:00", "trigger": "interval[6h]"},
    ],
    "worker_started_at": "2026-05-11T10:00:00+00:00",
    "last_heartbeat_age_seconds": 23.4,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_worker_scheduler(tmp_bot_squad: Path):
    """Fake worker that handles the scheduler_state action."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()

    @fake.post("/actions/scheduler_state")
    def scheduler_state(params: dict | None = None) -> dict:
        return _SAMPLE_SCHEDULER_STATE

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


def _client_logged_in(tmp_bot_squad: Path, monkeypatch, sock_path: Path) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


def _anon_client(tmp_bot_squad: Path, monkeypatch, sock_path: Path) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_scheduler_state_success(tmp_bot_squad: Path, monkeypatch, fake_worker_scheduler: Path):
    """Returns scheduler state with jobs list and metadata."""
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_scheduler) as client:
        r = client.get("/api/scheduler")
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data
    assert len(data["jobs"]) == 4
    assert "worker_started_at" in data
    assert "last_heartbeat_age_seconds" in data


def test_get_scheduler_state_jobs_shape(tmp_bot_squad: Path, monkeypatch, fake_worker_scheduler: Path):
    """Each job has id, next_run, trigger fields."""
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_scheduler) as client:
        r = client.get("/api/scheduler")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    job_ids = {j["id"] for j in jobs}
    assert "heartbeat" in job_ids
    assert "deploy_monitor" in job_ids
    for job in jobs:
        assert "id" in job
        assert "next_run" in job
        assert "trigger" in job


def test_get_scheduler_state_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_scheduler: Path):
    """Unauthenticated request → 401."""
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_scheduler) as client:
        r = client.get("/api/scheduler")
    assert r.status_code == 401


def test_get_scheduler_state_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_scheduler: Path):
    """Worker unreachable → 502."""
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.get("/api/scheduler")
    assert r.status_code == 502
