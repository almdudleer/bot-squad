"""Tests for worker FastAPI app on Unix socket."""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot_squad_worker.server import build_app


def test_health_endpoint():
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body
    assert "uptime" in body


def test_actions_noop():
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/actions/noop", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_actions_unknown_returns_400():
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/actions/wipe-disk", json={})
    assert r.status_code == 400
    assert "unknown action" in r.json()["detail"]


def test_actions_bad_params_returns_400():
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/actions/noop", json={"oops": 1})
    assert r.status_code == 400
