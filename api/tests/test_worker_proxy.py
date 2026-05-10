"""Tests for the API→worker proxy route (/api/worker/*)."""
import hashlib
import hmac
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


async def test_worker_noop_proxy(tmp_bot_squad: Path, monkeypatch, fake_worker_tg):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker_tg))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    app = build_app()
    with TestClient(app) as client:
        base = {"id": 12345, "first_name": "Alexey", "auth_date": int(time.time())}
        secret = hashlib.sha256("TESTBOT:TOKEN".encode()).digest()
        s = "\n".join(f"{k}={base[k]}" for k in sorted(base))
        base["hash"] = hmac.new(secret, s.encode(), hashlib.sha256).hexdigest()
        client.post("/api/auth/tg", json=base)

        r = client.post("/api/worker/noop")
    assert r.status_code == 200
    assert r.json()["ok"] is True
