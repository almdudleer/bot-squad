"""Tests for /api/projects/{slug}/sessions endpoints."""
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

_SAMPLE_SESSION = {
    "sid": "S-almdudleer-spec5-p2",
    "status": "active",
    "window": "spec5",
    "cwd": "/home/almdudleer/signal_tracker_mgmt",
    "started_at": None,
    "last_prompt_at": None,
    "claude_uuid": "9d7b3153-0000-0000-0000-000000000001",
    "linked_tasks": [],
}


@pytest.fixture
def fake_worker_sessions(tmp_bot_squad: Path):
    """Fake worker that handles session management actions."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()

    @fake.post("/actions/noop")
    def noop(params: dict | None = None) -> dict:
        return {"ok": True, "ts": 99}

    @fake.post("/actions/list_sessions")
    def list_sessions(params: dict | None = None) -> dict:
        return {"sessions": [_SAMPLE_SESSION]}

    @fake.post("/actions/pause_session")
    def pause_session(params: dict | None = None) -> dict:
        return {"ok": True, "paused": True}

    @fake.post("/actions/resume_session")
    def resume_session(params: dict | None = None) -> dict:
        return {"ok": True, "sid": "S-almdudleer-spec5-p3"}

    @fake.post("/actions/spawn_session")
    def spawn_session(params: dict | None = None) -> dict:
        return {"ok": True, "sid": "S-almdudleer-new-window-p4"}

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
# GET /api/projects/{slug}/sessions
# ---------------------------------------------------------------------------

def test_list_sessions_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["sid"] == "S-almdudleer-spec5-p2"
    assert data[0]["status"] == "active"


def test_list_sessions_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 401


def test_list_sessions_unknown_project_404(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.get("/api/projects/no-such-project/sessions")
    assert r.status_code == 404


def test_list_sessions_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    # Point at a non-existent socket to trigger a WorkerError → 502.
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.get("/api/projects/test-project/sessions")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/pause
# ---------------------------------------------------------------------------

def test_pause_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pause")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "paused": True}


def test_pause_session_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pause")
    assert r.status_code == 401


def test_pause_session_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/pause")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/resume
# ---------------------------------------------------------------------------

def test_resume_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "sid" in data


def test_resume_session_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume")
    assert r.status_code == 401


def test_resume_session_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post("/api/projects/test-project/sessions/S-almdudleer-spec5-p2/resume")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions  (spawn)
# ---------------------------------------------------------------------------

def test_spawn_session_success(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "sid" in data


def test_spawn_session_with_prompt(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window", "initial_prompt": "Hello Claude"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_spawn_session_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window"},
        )
    assert r.status_code == 401


def test_spawn_session_empty_window_400(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_sessions) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "   "},
        )
    assert r.status_code == 400


def test_spawn_session_worker_502(tmp_bot_squad: Path, monkeypatch, fake_worker_sessions: Path):
    broken_sock = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken_sock) as client:
        r = client.post(
            "/api/projects/test-project/sessions",
            json={"window": "new-window"},
        )
    assert r.status_code == 502
