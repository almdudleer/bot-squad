"""T-0369: GET /openapi.json (and Swagger) must render — FastAPI schema
generation was 500'ing on a public, unauthenticated path."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _env(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(root / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")


def test_openapi_json_renders(tmp_bot_squad: Path, monkeypatch) -> None:
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/openapi.json")
    assert r.status_code == 200, r.text
    schema = r.json()
    assert schema.get("openapi")
    assert "paths" in schema and schema["paths"]
