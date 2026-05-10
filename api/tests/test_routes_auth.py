"""Tests for /api/auth/* — username/password login, logout, me."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _set_env(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(root / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")


def test_login_sets_cookie(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["username"] == "testuser"
    assert "session" in r.cookies


def test_login_bad_password(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_username(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": "nobody", "password": "test"})
    assert r.status_code == 401


def test_login_missing_fields(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": "testuser"})
    assert r.status_code == 400


def test_logout_clears_cookie(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.post("/api/auth/logout")
    assert r.status_code == 200


def test_me_without_cookie(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_with_cookie(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "testuser"
