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
    body = r.json()
    assert body["username"] == "testuser"
    # Phase 2: /me must expose linux_user + is_admin. The test fixture
    # configures testuser → linux_user=almdudleer, is_admin=true so the
    # rest of the suite can use either-user SIDs without ownership 403s.
    assert body["linux_user"] == "almdudleer"
    assert body["is_admin"] is True


def test_me_defaults_when_no_user_meta(tmp_bot_squad: Path, monkeypatch) -> None:
    """When auth.toml has NO [user_meta.<name>], defaults: linux_user==name, admin=false."""
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[session]\nttl = "7d"\n'
    )
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["linux_user"] == "testuser"
    assert body["is_admin"] is False


def test_require_admin_rejects_non_admin(tmp_bot_squad: Path, monkeypatch) -> None:
    """require_admin returns 403 for authenticated but non-admin users."""
    _set_env(monkeypatch, tmp_bot_squad)
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        r = client.get("/api/users")
    assert r.status_code == 403


def test_me_returns_user_meta_when_present(tmp_bot_squad: Path, monkeypatch) -> None:
    """When auth.toml has [user_meta.<name>], /me reflects those values."""
    _set_env(monkeypatch, tmp_bot_squad)
    # Overwrite auth.toml to include a meta block.
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "someoneelse"\n'
        'is_admin = true\n'
        '[session]\nttl = "7d"\n'
    )
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["linux_user"] == "someoneelse"
    assert body["is_admin"] is True
