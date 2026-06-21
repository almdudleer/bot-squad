"""T-0393 (audit item 21 / theme-2): GET /m/users must be a PURE read.

``list_global_users`` called ``_seed_local_users`` on EVERY GET — a read that
mutated the store (upserting GlobalUsers + self-attachments from local
auth.toml). The closed-loops doctrine names a side-effecting GET a leak. Move the
backfill to the mothership router's STARTUP lifespan so the read is pure.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app
from app.mothership_users_store import MothershipUsersStore


_BCRYPT_TEST = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        "[users]\n"
        f'testuser = "{_BCRYPT_TEST}"\n'
        "[user_meta.testuser]\n"
        'linux_user = "almdudleer"\n'
        "is_admin = true\n"
        '[session]\nttl = "7d"\n'
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    return TestClient(build_app())


def _users_root(tmp_bot_squad: Path) -> Path:
    return tmp_bot_squad / "data" / "_mothership"


def test_seed_runs_at_startup_not_on_get(tmp_bot_squad: Path, monkeypatch):
    """The local auth.toml operator is in the registry after STARTUP, without
    any GET /m/users ever being called."""
    with _client(tmp_bot_squad, monkeypatch):
        store = MothershipUsersStore(_users_root(tmp_bot_squad))
        assert store.user_by_username("testuser") is not None, (
            "startup lifespan did not seed the local auth.toml operator"
        )


def test_get_users_is_a_pure_read(tmp_bot_squad: Path, monkeypatch):
    """GET /m/users must NOT re-create the registry it reads. After startup
    seeds users.json, deleting it and hitting the GET must leave it absent (a
    pure read), not silently re-seed on read."""
    users_json = _users_root(tmp_bot_squad) / "users.json"
    with _client(tmp_bot_squad, monkeypatch) as client:
        client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
        assert users_json.exists(), "startup seed should have written users.json"
        users_json.unlink()

        r = client.get("/api/m/users")
        assert r.status_code == 200, r.text
        # A pure read neither re-seeds the registry file ...
        if users_json.exists():
            assert json.loads(users_json.read_text()).get("users") in ([], None), (
                "GET /m/users re-seeded the store on read (side-effecting GET)"
            )
        # ... nor conjures the seeded operator back into the response.
        assert all(u["username"] != "testuser" for u in r.json()), (
            "GET /m/users re-seeded testuser on read"
        )
