"""GlobalUser surface — /api/m/users + /api/m/users/verify (T-0066).

Exercises the super-admin GET, the server-bearer-auth verify endpoint,
and confirms the rotation rule (install_token is rejected for verify).
"""
from __future__ import annotations

from pathlib import Path

import bcrypt
from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_store import MothershipStore
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
    names = set(u["username"] for u in body)
    # The minted globals are listed; T-0313 also lazily seeds the local
    # auth.toml operator (``testuser``) so the directory reflects reality.
    assert {"alice", "bob"} <= names
    assert "testuser" in names
    # password_hash is NEVER serialised to the FE.
    for u in body:
        assert "password_hash" not in u
        assert u["id"].startswith("gu_")


def test_list_global_users_attached_servers_counts(tmp_bot_squad: Path, monkeypatch):
    """T-0129: each row carries an ``attached_servers`` int derived from
    ``MothershipUsersStore.list_attachments_for_user``.

    Three cases in one shot so the projection contract is locked:
    - alice has 0 attachments → 0
    - bob has 1 attachment → 1
    - carol has 2 attachments → 2
    """
    alice_id = _mint_global(tmp_bot_squad, username="alice", password="x")
    bob_id = _mint_global(tmp_bot_squad, username="bob", password="x")
    carol_id = _mint_global(tmp_bot_squad, username="carol", password="x")
    # Need actual server rows for the attachment server_id keys — the
    # attachments dir is global-user-keyed, so technically we could use
    # any server_id string here, but real-world attachments always
    # reference a real srv_ id. Mint via the store directly.
    srv_store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    s1, _ = srv_store.register_server(
        display_name="s1", base_url="https://s1.example.com", owner_user="testuser"
    )
    s2, _ = srv_store.register_server(
        display_name="s2", base_url="https://s2.example.com", owner_user="testuser"
    )
    users_store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    users_store.upsert_attachment(
        global_user_id=bob_id, server_id=s1.id, server_username="bob"
    )
    users_store.upsert_attachment(
        global_user_id=carol_id, server_id=s1.id, server_username="carol"
    )
    users_store.upsert_attachment(
        global_user_id=carol_id, server_id=s2.id, server_username="carol"
    )

    with _client(tmp_bot_squad, monkeypatch, mothership=True) as client:
        _login(client)
        r = client.get("/api/m/users")
    assert r.status_code == 200, r.text
    by_name = {u["username"]: u for u in r.json()}
    assert by_name["alice"]["attached_servers"] == 0
    assert by_name["bob"]["attached_servers"] == 1
    assert by_name["carol"]["attached_servers"] == 2
    # Self-check: the keys are the real global-user ids we minted, not
    # accidentally swapped between rows.
    assert by_name["alice"]["id"] == alice_id


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
