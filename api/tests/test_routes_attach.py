"""Server-side /api/auth/attach (T-0066).

Exercised against the MOTHERSHIP=1 in-process short-circuit (no HTTP hop
to a separate mothership). Confirms the writeback to auth.toml + the
end-to-end login round-trip after attachment.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import bcrypt
from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_users_store import MothershipUsersStore


def _env(monkeypatch, tmp_bot_squad: Path, *, mothership: bool = True) -> None:
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


def _mint_global(tmp: Path, *, username: str, password: str) -> str:
    store = MothershipUsersStore(tmp / "data" / "_mothership")
    pwh = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    u = store.create_user(username=username, password_hash=pwh)
    return u.id


def test_attach_writes_global_user_id_to_auth_toml(tmp_bot_squad: Path, monkeypatch):
    gu_id = _mint_global(tmp_bot_squad, username="g_alice", password="global-pw")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "global-pw"},
        )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["server_username"] == "testuser"
    assert out["global_user_id"] == gu_id
    assert out["global_username"] == "g_alice"

    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    assert raw["user_meta"]["testuser"]["attached_to_global_user"] == gu_id


def test_attach_appears_on_me_endpoint(tmp_bot_squad: Path, monkeypatch):
    """After attach, /api/auth/me surfaces the attached uuid + is_super_admin."""
    gu_id = _mint_global(tmp_bot_squad, username="g_alice", password="global-pw")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "global-pw"},
        )
        me = client.get("/api/auth/me").json()
    # T-0228: once MIGRATED, the global role is read from the stored GlobalUser,
    # not the build-flag bridge. testuser is a server admin in the fixture, but
    # g_alice is a global_member → is_super_admin is now correctly False (the
    # fix for "the gate ignored its own field"). The attachment uuid surfaces.
    assert me["global_role"] == "global_member"
    assert me["is_super_admin"] is False
    assert me["is_admin"] is True  # server scope unchanged
    assert me["attached_to_global_user"] == gu_id


def test_attach_legacy_login_still_works_after_attach(tmp_bot_squad: Path, monkeypatch):
    """Back-compat: server-local password keeps working post-attach.

    Cross-server SSO (login via mothership-proxied creds) is filed as a
    follow-up; for now the local password remains the source of truth for
    THIS server's login surface so attaching can't lock anyone out.
    """
    _mint_global(tmp_bot_squad, username="g_alice", password="global-pw")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "global-pw"},
        )
        client.post("/api/auth/logout")
        r = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "test"},
        )
    assert r.status_code == 200


def test_attach_unmigrated_users_unchanged(tmp_bot_squad: Path, monkeypatch):
    """Login flow must be byte-identical for users with no attached_to_global_user.

    Defends against the regression where adding a new field changes how
    auth.toml is serialised for rows that didn't opt in.
    """
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/login", json={"username": "testuser", "password": "test"}
        )
    assert r.status_code == 200
    raw = tomllib.loads((tmp_bot_squad / "config" / "auth.toml").read_text())
    # No attached_to_global_user written to disk for the legacy fixture row.
    assert "attached_to_global_user" not in raw["user_meta"]["testuser"]


def test_attach_idempotent_same_global_user(tmp_bot_squad: Path, monkeypatch):
    """Re-attach to the same global user is a 200 noop (the writeback is a
    deterministic rewrite of the same uuid)."""
    gu_id = _mint_global(tmp_bot_squad, username="g_alice", password="global-pw")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r1 = client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "global-pw"},
        )
        r2 = client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "global-pw"},
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["global_user_id"] == gu_id
    assert r2.json()["global_user_id"] == gu_id


def test_attach_to_different_global_user_409(tmp_bot_squad: Path, monkeypatch):
    """Switching attachments must require an explicit detach (separate
    ticket); silent overwrite would let a misclick transfer ownership.
    """
    _mint_global(tmp_bot_squad, username="g_alice", password="pw1")
    _mint_global(tmp_bot_squad, username="g_bob", password="pw2")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "pw1"},
        )
        r = client.post(
            "/api/auth/attach",
            json={"global_username": "g_bob", "global_password": "pw2"},
        )
    assert r.status_code == 409


def test_attach_bad_global_password_401(tmp_bot_squad: Path, monkeypatch):
    _mint_global(tmp_bot_squad, username="g_alice", password="pw1")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "wrong"},
        )
    assert r.status_code == 401


def test_attach_unknown_global_user_401(tmp_bot_squad: Path, monkeypatch):
    """Unknown global username surfaces as 401 (bad credentials) on the
    server-side route — the 404 distinction lives only on the mothership
    /users/verify surface where the caller is another server, not a user."""
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.post(
            "/api/auth/attach",
            json={"global_username": "nobody", "global_password": "pw"},
        )
    assert r.status_code == 401


def test_attach_requires_auth(tmp_bot_squad: Path, monkeypatch):
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/attach",
            json={"global_username": "x", "global_password": "y"},
        )
    assert r.status_code == 401


def test_attach_missing_fields(tmp_bot_squad: Path, monkeypatch):
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.post("/api/auth/attach", json={"global_username": ""})
    assert r.status_code == 400


def test_attach_drops_enable_pending_marker(tmp_bot_squad: Path, monkeypatch):
    """Bundle D (T-0067) coordination: a successful attach leaves a
    ``_users/<linux_user>/enable-worker.pending`` marker that the per-user
    worker enable script consumes.
    """
    from app.attach_hooks import is_enable_pending

    _mint_global(tmp_bot_squad, username="g_alice", password="pw")
    _env(monkeypatch, tmp_bot_squad)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        r = client.post(
            "/api/auth/attach",
            json={"global_username": "g_alice", "global_password": "pw"},
        )
    assert r.status_code == 200
    # testuser → linux_user=almdudleer (fixture default).
    assert is_enable_pending(tmp_bot_squad / "data", "almdudleer") is True


def test_me_is_super_admin_false_on_detach_build(tmp_bot_squad: Path, monkeypatch):
    """Single-install builds: is_super_admin is always False (no MOTHERSHIP scope)."""
    _env(monkeypatch, tmp_bot_squad, mothership=False)
    app = build_app()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        me = client.get("/api/auth/me").json()
    assert me["is_admin"] is True
    assert me["is_super_admin"] is False
