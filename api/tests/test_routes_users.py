"""Tests for /api/users — admin-only CRUD over auth.toml."""
from __future__ import annotations

import tomllib
from pathlib import Path

import bcrypt
from fastapi.testclient import TestClient

from app.main import build_app


def _set_env(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(root / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")


def _login(client: TestClient, username: str = "testuser", password: str = "test") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _login_nonadmin(tmp: Path, client: TestClient) -> None:
    """Write a fresh auth.toml with a non-admin user, then login."""
    (tmp / "config" / "auth.toml").write_text(
        '[users]\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        'admin1 = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[user_meta.admin1]\n'
        'linux_user = "admin1"\n'
        'is_admin = true\n'
        '[session]\nttl = "7d"\n'
    )


def test_list_users_requires_admin(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    _login_nonadmin(tmp_bot_squad, None)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        r = client.get("/api/users")
    assert r.status_code == 403


def test_list_users_unauthenticated(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.get("/api/users")
    assert r.status_code == 401


def test_list_users_success(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.get("/api/users")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    names = [u["username"] for u in body]
    assert "testuser" in names
    me = [u for u in body if u["username"] == "testuser"][0]
    assert me["linux_user"] == "almdudleer"
    assert me["is_admin"] is True


def test_create_user(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/api/users",
            json={"username": "bob", "password": "s3cret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["username"] == "bob"
        assert body["linux_user"] == "bob"
        assert body["is_admin"] is False

        # Verify written to disk
        raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
        assert "bob" in raw["users"]
        hashed = raw["users"]["bob"]
        assert bcrypt.checkpw(b"s3cret", hashed.encode())


def test_create_user_duplicate(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/api/users",
            json={"username": "testuser", "password": "x"},
        )
    assert r.status_code == 400


def test_create_user_empty(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/users", json={"username": "", "password": "x"})
        assert r.status_code == 400
        r = client.post("/api/users", json={"username": "x", "password": ""})
        assert r.status_code == 400


def test_patch_user_is_admin_false_string_does_not_grant(tmp_bot_squad: Path, monkeypatch) -> None:
    """Footgun: bool("false") is True, so PATCH is_admin="false" used to GRANT
    admin (privilege escalation via a stringy payload). It must set False."""
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "x"})
        r = client.patch("/api/users/bob", json={"is_admin": "false"})
        assert r.status_code == 200, r.text
        assert r.json()["is_admin"] is False
    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert raw["user_meta"]["bob"]["is_admin"] is False


def test_patch_user_is_admin_true_string_grants(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "x"})
        r = client.patch("/api/users/bob", json={"is_admin": "true"})
        assert r.status_code == 200, r.text
        assert r.json()["is_admin"] is True


def test_patch_user_is_admin_bogus_400(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "x"})
        r = client.patch("/api/users/bob", json={"is_admin": "maybe"})
        assert r.status_code == 400, r.text


def test_create_user_is_admin_false_string(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post("/api/users", json={"username": "carol", "password": "x", "is_admin": "false"})
        assert r.status_code == 200, r.text
        assert r.json()["is_admin"] is False


def test_create_user_with_linux_user_and_admin(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/api/users",
            json={"username": "alice", "password": "p", "linux_user": "alice-os", "is_admin": True},
        )
    assert r.status_code == 200
    assert r.json() == {"username": "alice", "linux_user": "alice-os", "is_admin": True}


def test_patch_user_linux_and_admin(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        # First create a second user so we can flip is_admin freely
        client.post("/api/users", json={"username": "bob", "password": "x"})
        r = client.patch(
            "/api/users/bob",
            json={"linux_user": "bob2", "is_admin": True},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"username": "bob", "linux_user": "bob2", "is_admin": True}


def test_patch_user_404(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.patch("/api/users/nobody", json={"is_admin": True})
    assert r.status_code == 404


def test_patch_user_last_admin_protection(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        # Try to demote the only admin (testuser).
        r = client.patch("/api/users/testuser", json={"is_admin": False})
    assert r.status_code == 400


def test_reset_password(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "old"})
        r = client.put("/api/users/bob/password", json={"password": "new!"})
        assert r.status_code == 200
        # bob can login with new password
        client.post("/api/auth/logout")
        r2 = client.post("/api/auth/login", json={"username": "bob", "password": "new!"})
    assert r2.status_code == 200


def test_reset_password_404(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.put("/api/users/nobody/password", json={"password": "x"})
    assert r.status_code == 404


def test_delete_user(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/users", json={"username": "bob", "password": "x"})
        r = client.delete("/api/users/bob")
        assert r.status_code == 200
        raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
        assert "bob" not in raw["users"]


def test_delete_user_404(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        r = client.delete("/api/users/nobody")
    assert r.status_code == 404


def test_delete_last_admin_blocked(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        # testuser is the only admin in the fixture; deleting must be blocked.
        r = client.delete("/api/users/testuser")
    assert r.status_code == 400


def test_non_admin_gets_403_on_all_endpoints(tmp_bot_squad: Path, monkeypatch) -> None:
    _set_env(monkeypatch, tmp_bot_squad)
    _login_nonadmin(tmp_bot_squad, None)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        for method, path, body in [
            ("GET", "/api/users", None),
            ("POST", "/api/users", {"username": "z", "password": "z"}),
            ("PATCH", "/api/users/plain", {"is_admin": True}),
            ("PUT", "/api/users/plain/password", {"password": "x"}),
            ("DELETE", "/api/users/plain", None),
        ]:
            r = client.request(method, path, json=body)
            assert r.status_code == 403, (method, path, r.status_code, r.text)
