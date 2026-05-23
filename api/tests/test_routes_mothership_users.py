"""GlobalUser surface — /api/m/users + /api/m/users/verify (T-0066).

Exercises the super-admin GET, the server-bearer-auth verify endpoint,
and confirms the rotation rule (install_token is rejected for verify).
"""
from __future__ import annotations

from pathlib import Path

import bcrypt
from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_users_store import MothershipUsersStore


def _client(tmp_bot_squad: Path, monkeypatch, *, mothership: bool):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)
    return TestClient(build_app())


def _login(client, username: str = "testuser", password: str = "test") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _mint_global(tmp_bot_squad: Path, *, username: str, password: str) -> str:
    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    pwh = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    u = store.create_user(username=username, password_hash=pwh)
    return u.id


def test_list_global_users_requires_super_admin(tmp_bot_squad: Path, monkeypatch):
    """Non-admin → 403 (super-admin = admin on MOTHERSHIP=1 builds)."""
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        client.post("/api/auth/login", json={"username": "plain", "password": "test"})
        r = client.get("/api/m/users")
    assert r.status_code == 403


def test_list_global_users_returns_public_projection(tmp_bot_squad: Path, monkeypatch):
    _mint_global(tmp_bot_squad, username="alice", password="pw1")
    _mint_global(tmp_bot_squad, username="bob", password="pw2")
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/m/users")
    assert r.status_code == 200, r.text
    body = r.json()
    names = sorted(u["username"] for u in body)
    assert names == ["alice", "bob"]
    # password_hash is NEVER serialised to the FE.
    for u in body:
        assert "password_hash" not in u
        assert u["id"].startswith("gu_")


def test_list_global_users_unauthenticated(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.get("/api/m/users")
    assert r.status_code == 401


def test_list_global_users_404_in_detach_build(tmp_bot_squad: Path, monkeypatch):
    """Single-install builds MUST NOT mount /api/m/* at all."""
    with _client(tmp_bot_squad, monkeypatch, mothership=False) as client:
        _login(client)
        r = client.get("/api/m/users")
    assert r.status_code == 404


def test_verify_global_user_succeeds_with_server_bearer(tmp_bot_squad: Path, monkeypatch):
    """A server with a valid server_bearer can POST /users/verify and get a GlobalUser."""
    _mint_global(tmp_bot_squad, username="alice", password="hunter2")
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        # Use the same /connect dance as the install bundle does.
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        client.cookies.clear()
        bearer = client.post(
            "/api/m/installer/connect",
            json={"token": token, "server_meta": {"hostname": "h"}},
        ).json()["server_bearer"]

        r = client.post(
            "/api/m/users/verify",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"username": "alice", "password": "hunter2"},
        )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["username"] == "alice"
    assert "password_hash" not in out


def test_verify_global_user_bad_password_401(tmp_bot_squad: Path, monkeypatch):
    _mint_global(tmp_bot_squad, username="alice", password="hunter2")
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        client.cookies.clear()
        bearer = client.post(
            "/api/m/installer/connect",
            json={"token": token, "server_meta": {"hostname": "h"}},
        ).json()["server_bearer"]
        r = client.post(
            "/api/m/users/verify",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"username": "alice", "password": "wrong"},
        )
    assert r.status_code == 401


def test_verify_global_user_unknown_404(tmp_bot_squad: Path, monkeypatch):
    """Unknown global username → 404 (distinct from bad-password 401)."""
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        client.cookies.clear()
        bearer = client.post(
            "/api/m/installer/connect",
            json={"token": token, "server_meta": {"hostname": "h"}},
        ).json()["server_bearer"]
        r = client.post(
            "/api/m/users/verify",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"username": "nobody", "password": "x"},
        )
    assert r.status_code == 404


def test_verify_global_user_install_token_rejected(tmp_bot_squad: Path, monkeypatch):
    """Rotation rule: install_token MUST NOT authenticate /users/verify.

    Pre-/connect installers have no business validating user creds; we
    keep the rule explicit here so a future loosening of
    _authenticate_installer doesn't quietly widen this surface.
    """
    _mint_global(tmp_bot_squad, username="alice", password="hunter2")
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        body = client.post(
            "/api/m/servers",
            json={"display_name": "S", "base_url": "https://s.example.com"},
        ).json()
        token = body["install_token"]
        client.cookies.clear()
        r = client.post(
            "/api/m/users/verify",
            headers={"Authorization": f"Bearer {token}"},
            json={"username": "alice", "password": "hunter2"},
        )
    assert r.status_code == 403


def test_verify_global_user_no_bearer_401(tmp_bot_squad: Path, monkeypatch):
    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        r = client.post(
            "/api/m/users/verify",
            json={"username": "alice", "password": "x"},
        )
    assert r.status_code == 401
