"""Tests for /api/welcome/operator — T-0013 post-install handoff support."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _login(client: TestClient) -> None:
    client.post(
        "/api/auth/login", json={"username": "testuser", "password": "test"}
    )


def _envs(tmp_bot_squad: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")


def test_operator_default(tmp_bot_squad: Path, monkeypatch) -> None:
    _envs(tmp_bot_squad, monkeypatch)
    monkeypatch.delenv("BOTSQUAD_OPERATOR_SESSION", raising=False)
    with TestClient(build_app()) as client:
        _login(client)
        r = client.get("/api/welcome/operator")
    assert r.status_code == 200
    assert r.json() == {"session": "bot-squad-operator"}


def test_operator_env_override(tmp_bot_squad: Path, monkeypatch) -> None:
    _envs(tmp_bot_squad, monkeypatch)
    monkeypatch.setenv("BOTSQUAD_OPERATOR_SESSION", "ops-prime")
    with TestClient(build_app()) as client:
        _login(client)
        r = client.get("/api/welcome/operator")
    assert r.status_code == 200
    assert r.json() == {"session": "ops-prime"}


def test_operator_empty_env_falls_back_to_default(
    tmp_bot_squad: Path, monkeypatch,
) -> None:
    _envs(tmp_bot_squad, monkeypatch)
    monkeypatch.setenv("BOTSQUAD_OPERATOR_SESSION", "   ")
    with TestClient(build_app()) as client:
        _login(client)
        r = client.get("/api/welcome/operator")
    assert r.status_code == 200
    assert r.json() == {"session": "bot-squad-operator"}


def test_operator_requires_auth(tmp_bot_squad: Path, monkeypatch) -> None:
    _envs(tmp_bot_squad, monkeypatch)
    with TestClient(build_app()) as client:
        r = client.get("/api/welcome/operator")
    assert r.status_code == 401
