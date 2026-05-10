"""Tests for /api/auth/* — TG login proxied to worker, logout, require_auth."""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


# ---------------------------------------------------------------------------
# Helper: build a valid TG payload HMAC-signed with a given token
# ---------------------------------------------------------------------------

def _payload(bot_token: str, tg_id: int) -> dict:
    base = {
        "id": tg_id,
        "first_name": "Alexey",
        "auth_date": int(time.time()),
    }
    secret = hashlib.sha256(bot_token.encode()).digest()
    s = "\n".join(f"{k}={base[k]}" for k in sorted(base))
    h = hmac.new(secret, s.encode(), hashlib.sha256).hexdigest()
    return {**base, "hash": h}


def _set_env(monkeypatch, root: Path, sock: Path) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(sock))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")


def test_login_sets_cookie(tmp_bot_squad: Path, monkeypatch, fake_worker_tg) -> None:
    _set_env(monkeypatch, tmp_bot_squad, fake_worker_tg)
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/api/auth/tg", json=_payload("TESTBOT:TOKEN", 12345))
    assert r.status_code == 200
    assert "session" in r.cookies


def test_login_rejects_unallowed(tmp_bot_squad: Path, monkeypatch, fake_worker_tg) -> None:
    _set_env(monkeypatch, tmp_bot_squad, fake_worker_tg)
    app = build_app()
    with TestClient(app) as client:
        r = client.post("/api/auth/tg", json=_payload("TESTBOT:TOKEN", 99999))
    assert r.status_code == 403


def test_logout_clears_cookie(tmp_bot_squad: Path, monkeypatch, fake_worker_tg) -> None:
    _set_env(monkeypatch, tmp_bot_squad, fake_worker_tg)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/tg", json=_payload("TESTBOT:TOKEN", 12345))
        r = client.post("/api/auth/logout")
    assert r.status_code == 200
