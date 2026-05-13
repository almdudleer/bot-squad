"""Tests for /api/projects/{slug}/peer/* endpoints."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


@pytest.fixture
def fake_worker_peer(tmp_bot_squad: Path):
    """Minimal fake worker that handles peer_* actions."""
    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)

    fake = FastAPI()

    @fake.post("/actions/peer_send")
    def peer_send(params: dict | None = None) -> dict:
        return {"ok": True, "delivered_to": [(params or {}).get("to", "")]}

    @fake.post("/actions/peer_inbox_read")
    def peer_inbox_read(params: dict | None = None) -> dict:
        return {"ok": True, "messages": ["2026-05-12T00:00:00Z\t[from S-x]\thi"], "count": 1}

    @fake.post("/actions/peer_inbox_wait")
    def peer_inbox_wait(params: dict | None = None) -> dict:
        # Tests use a short timeout so this returns immediately.
        return {"ok": True, "ready": False, "elapsed_sec": float((params or {}).get("timeout", 0))}

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
# /peer/send
# ---------------------------------------------------------------------------

def test_peer_send_success(tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post(
            "/api/projects/test-project/peer/send",
            json={"from_sid": "S-testuser-foo-p0", "to": "S-other", "text": "hi"},
        )
    assert r.status_code == 200
    assert r.json()["delivered_to"] == ["S-other"]


def test_peer_send_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post(
            "/api/projects/test-project/peer/send",
            json={"from_sid": "S-testuser-foo-p0", "to": "S-other", "text": "hi"},
        )
    assert r.status_code == 401


def test_peer_send_worker_502(tmp_bot_squad: Path, monkeypatch):
    broken = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken) as client:
        r = client.post(
            "/api/projects/test-project/peer/send",
            json={"from_sid": "S-testuser-foo-p0", "to": "S-other", "text": "hi"},
        )
    assert r.status_code == 502


def test_peer_send_stakeholder_allowed(tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path):
    """`stakeholder` is a recognised from-sid user even from another login."""
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post(
            "/api/projects/test-project/peer/send",
            json={"from_sid": "S-stakeholder-ui-p0", "to": "S-other", "text": "hi"},
        )
    assert r.status_code == 200


def test_peer_send_rejects_cross_user_impersonation(
    tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path,
):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post(
            "/api/projects/test-project/peer/send",
            json={"from_sid": "S-someoneelse-x-p0", "to": "S-other", "text": "hi"},
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /peer/{sid}/read
# ---------------------------------------------------------------------------

def test_peer_inbox_read_success(tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post("/api/projects/test-project/peer/S-x-p0/read")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert len(data["messages"]) == 1


def test_peer_inbox_read_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post("/api/projects/test-project/peer/S-x-p0/read")
    assert r.status_code == 401


def test_peer_inbox_read_worker_502(tmp_bot_squad: Path, monkeypatch):
    broken = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken) as client:
        r = client.post("/api/projects/test-project/peer/S-x-p0/read")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# /peer/{sid}/wait
# ---------------------------------------------------------------------------

def test_peer_inbox_wait_success(tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path):
    with _client_logged_in(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post(
            "/api/projects/test-project/peer/S-x-p0/wait",
            json={"timeout": 1},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "ready" in data


def test_peer_inbox_wait_requires_auth(tmp_bot_squad: Path, monkeypatch, fake_worker_peer: Path):
    with _anon_client(tmp_bot_squad, monkeypatch, fake_worker_peer) as client:
        r = client.post(
            "/api/projects/test-project/peer/S-x-p0/wait",
            json={"timeout": 1},
        )
    assert r.status_code == 401


def test_peer_inbox_wait_worker_502(tmp_bot_squad: Path, monkeypatch):
    broken = tmp_bot_squad / "data" / "_sock" / "broken.sock"
    with _client_logged_in(tmp_bot_squad, monkeypatch, broken) as client:
        r = client.post(
            "/api/projects/test-project/peer/S-x-p0/wait",
            json={"timeout": 1},
        )
    assert r.status_code == 502
