from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def test_health_no_worker(tmp_bot_squad: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["worker"]["alive"] is False    # heartbeat file doesn't exist


def test_health_worker_fresh(tmp_bot_squad: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    hb = tmp_bot_squad / "data" / "_worker" / "heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.touch()
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.json()["worker"]["alive"] is True
