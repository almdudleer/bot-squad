from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app


def _client(tmp_bot_squad: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    app = build_app()
    return TestClient(app)


def _login(client) -> None:
    import hashlib, hmac, time
    bot_token = "TESTBOT:TOKEN"
    base = {"id": 12345, "first_name": "Alexey", "auth_date": int(time.time())}
    secret = hashlib.sha256(bot_token.encode()).digest()
    s = "\n".join(f"{k}={base[k]}" for k in sorted(base))
    base["hash"] = hmac.new(secret, s.encode(), hashlib.sha256).hexdigest()
    client.post("/api/auth/tg", json=base)


def test_projects_list_requires_auth(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        r = client.get("/api/projects")
    assert r.status_code == 401


def test_projects_list_after_login(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body == [{"slug": "test-project", "display_name": "Test Project"}]


def test_project_get(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/test-project")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "test-project"
    assert "counts" in body


def test_project_get_unknown_404(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get("/api/projects/nope")
    assert r.status_code == 404
