"""T-0228: super-admin gate resolves the session user's stored
GlobalUser.global_role (Fork-1 A: migrated → stored role; un-migrated → the
transitional build-flag fallback, so the bootstrap sole-admin is never locked
out). The behavior change: a MIGRATED mothership server-admin whose
global_role==GLOBAL_MEMBER loses the mothership admin surface."""
from __future__ import annotations

from pathlib import Path

import bcrypt
from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_users_store import MothershipUsersStore


def _env(monkeypatch, tmp: Path, *, mothership: bool = True) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    if mothership:
        monkeypatch.setenv("MOTHERSHIP", "1")
    else:
        monkeypatch.delenv("MOTHERSHIP", raising=False)


def _mint_global(tmp: Path, *, username: str, password: str, super_admin: bool) -> str:
    store = MothershipUsersStore(tmp / "data" / "_mothership")
    pwh = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    u = store.create_user(username=username, password_hash=pwh, is_super_admin=super_admin)
    return u.id


def _set_unmigrated_admin(tmp: Path, *, is_admin: bool, attached: str | None = None) -> None:
    """Rewrite testuser's auth.toml row (password stays 'test')."""
    block = (
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        f'is_admin = {"true" if is_admin else "false"}\n'
    )
    if attached:
        block += f'attached_to_global_user = "{attached}"\n'
    block += '[session]\nttl = "7d"\n'
    (tmp / "config" / "auth.toml").write_text(block)


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def _attach(client: TestClient, *, gu_username: str, gu_password: str) -> None:
    r = client.post("/api/auth/attach",
                    json={"global_username": gu_username, "global_password": gu_password})
    assert r.status_code == 200, r.text


def test_migrated_server_admin_with_member_global_role_loses_surface(tmp_bot_squad, monkeypatch):
    # THE FIX: testuser is a server admin, but their GlobalUser is global_member.
    _mint_global(tmp_bot_squad, username="g_member", password="pw", super_admin=False)
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        _attach(client, gu_username="g_member", gu_password="pw")
        me = client.get("/api/auth/me").json()
        assert me["global_role"] == "global_member"
        assert me["is_super_admin"] is False
        # server scope is untouched — still a server admin
        assert me["is_admin"] is True
        assert client.get("/api/m/users").status_code == 403


def test_migrated_global_admin_keeps_surface(tmp_bot_squad, monkeypatch):
    _mint_global(tmp_bot_squad, username="g_admin", password="pw", super_admin=True)
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        _attach(client, gu_username="g_admin", gu_password="pw")
        me = client.get("/api/auth/me").json()
        assert me["global_role"] == "global_admin"
        assert me["is_super_admin"] is True
        assert client.get("/api/m/users").status_code == 200


def test_unmigrated_server_admin_falls_back_to_bridge(tmp_bot_squad, monkeypatch):
    # bootstrap sole-admin (alexey-like): no GlobalUser → fallback keeps them in
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        me = client.get("/api/auth/me").json()
        assert me["attached_to_global_user"] is None
        assert me["is_super_admin"] is True
        assert client.get("/api/m/users").status_code == 200


def test_unmigrated_member_is_not_super_admin(tmp_bot_squad, monkeypatch):
    _set_unmigrated_admin(tmp_bot_squad, is_admin=False)
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        me = client.get("/api/auth/me").json()
        assert me["is_super_admin"] is False
        assert client.get("/api/m/users").status_code == 403


def test_dangling_attachment_falls_back_to_bridge(tmp_bot_squad, monkeypatch):
    # attached_to_global_user points at a GlobalUser that doesn't exist →
    # fallback (don't lock out), don't 500
    _set_unmigrated_admin(tmp_bot_squad, is_admin=True, attached="gu_does_not_exist")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        _login(client)
        me = client.get("/api/auth/me").json()
        assert me["is_super_admin"] is True
        assert client.get("/api/m/users").status_code == 200
