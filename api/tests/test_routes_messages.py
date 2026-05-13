"""Tests for /api/projects/{slug}/sessions/{claude_uuid}/messages endpoint.

All tests use fixture .jsonl files. No real ~/.claude/ directory is read.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_logged_in(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    client = TestClient(build_app())
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    return client


def _anon_client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_messages_success(tmp_bot_squad: Path, monkeypatch):
    """Endpoint returns parsed messages from fixture jsonl."""
    import app.routes_messages as RM

    monkeypatch.setattr(
        RM, "_resolve_jsonl",
        lambda uuid: FIXTURES / "sample_session.jsonl",
    )

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/sessions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/messages")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) > 0
    roles = {m["role"] for m in data}
    assert "user" in roles
    assert "assistant" in roles


def test_get_messages_returns_expected_fields(tmp_bot_squad: Path, monkeypatch):
    """Each message has role, ts, text fields."""
    import app.routes_messages as RM

    monkeypatch.setattr(
        RM, "_resolve_jsonl",
        lambda uuid: FIXTURES / "sample_session.jsonl",
    )

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/sessions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/messages")
    assert r.status_code == 200
    for msg in r.json():
        assert "role" in msg
        assert "ts" in msg
        assert "text" in msg


def test_get_messages_not_found_404(tmp_bot_squad: Path, monkeypatch):
    """Missing .jsonl file → 404 with helpful message."""
    import app.routes_messages as RM

    monkeypatch.setattr(
        RM, "_resolve_jsonl",
        lambda uuid: None,
    )

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/sessions/11111111-2222-3333-4444-555555555555/messages")
    assert r.status_code == 404
    assert "session log not found" in r.json()["detail"]


def test_get_messages_requires_auth(tmp_bot_squad: Path, monkeypatch):
    """Unauthenticated request → 401."""
    import app.routes_messages as RM

    monkeypatch.setattr(
        RM, "_resolve_jsonl",
        lambda uuid: FIXTURES / "sample_session.jsonl",
    )

    with _anon_client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/sessions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/messages")
    assert r.status_code == 401


def test_get_messages_unknown_project_404(tmp_bot_squad: Path, monkeypatch):
    """Unknown project slug → 404."""
    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/no-such-project/sessions/uuid/messages")
    assert r.status_code == 404


def test_get_messages_limit(tmp_bot_squad: Path, monkeypatch):
    """?limit=1 returns at most 1 message."""
    import app.routes_messages as RM

    monkeypatch.setattr(
        RM, "_resolve_jsonl",
        lambda uuid: FIXTURES / "sample_session.jsonl",
    )

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects/test-project/sessions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/messages?limit=1")
    assert r.status_code == 200
    assert len(r.json()) <= 1


def test_get_messages_offset(tmp_bot_squad: Path, monkeypatch):
    """?offset=2 skips first 2 messages."""
    import app.routes_messages as RM

    monkeypatch.setattr(
        RM, "_resolve_jsonl",
        lambda uuid: FIXTURES / "sample_session.jsonl",
    )

    with _client_logged_in(tmp_bot_squad, monkeypatch) as client:
        r0 = client.get("/api/projects/test-project/sessions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/messages")
        r2 = client.get("/api/projects/test-project/sessions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/messages?offset=2")
    assert r0.status_code == 200
    assert r2.status_code == 200
    assert len(r2.json()) < len(r0.json())
