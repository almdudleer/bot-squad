"""Tests for the API→worker proxy route (/api/worker/*)."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


async def test_worker_noop_proxy(tmp_bot_squad: Path, monkeypatch, fake_worker):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.post("/api/worker/noop")
    assert r.status_code == 200
    assert r.json()["ok"] is True
