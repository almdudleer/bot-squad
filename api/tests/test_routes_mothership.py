"""Mothership build-flag seam.

Exercises only the detach build flag — single-install builds (MOTHERSHIP
unset) MUST NOT expose /api/m/*; mothership builds (MOTHERSHIP=1) mount
the placeholder router with an empty server registry. T-0023/T-0024/T-0025
own the behavioural surface.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client(tmp_bot_squad: Path, monkeypatch, *, mothership: bool):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)
    return TestClient(build_app())


def _login(client) -> None:
    client.post("/api/auth/login", json={"username": "testuser", "password": "test"})


def test_mothership_off_means_no_routes(tmp_bot_squad: Path, monkeypatch):
    """Single-install build — /api/m/* must not exist at all."""
    with _client(tmp_bot_squad, monkeypatch, mothership=False) as client:
        _login(client)
        r = client.get("/api/m/servers")
    assert r.status_code == 404


def test_mothership_on_mounts_servers_empty(tmp_bot_squad: Path, monkeypatch):
    """Mothership build — router mounted, empty registry returns []."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/m/servers")
    assert r.status_code == 200
    assert r.json() == []


def test_mothership_on_requires_auth(tmp_bot_squad: Path, monkeypatch):
    """Mothership routes inherit the same auth dependency as the rest."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.get("/api/m/servers")
    assert r.status_code == 401
