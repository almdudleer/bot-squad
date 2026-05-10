import asyncio
import hashlib
import hmac
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import build_app


@pytest.fixture
def fake_worker(tmp_bot_squad: Path):
    """Start a fake worker in a background thread (not the test event loop)."""
    import uvicorn

    sock = tmp_bot_squad / "data" / "_sock" / "worker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    fake = FastAPI()

    @fake.post("/actions/noop")
    def noop(params: dict | None = None) -> dict:
        return {"ok": True, "ts": 99}

    config = uvicorn.Config(fake, uds=str(sock), log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for socket to bind.
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)

    yield sock

    server.should_exit = True
    thread.join(timeout=5)


async def test_worker_noop_proxy(tmp_bot_squad: Path, monkeypatch, fake_worker):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(fake_worker))
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
