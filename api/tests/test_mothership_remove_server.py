"""T-0392 (audit Fork-6 DEREGISTER): server registration lifecycle close.

``register_server`` opens the loop; nothing closed it — orphan / wrong-host /
abandoned rows were immortal. This adds the owned close:

- store ``remove_server`` sweeps the registry row + the bearer sidecar + the
  per-server checkpoint log (every malloc names its free).
- ``DELETE /api/m/servers/{server_id}`` is owner-gated via the ``require_manage``
  SSOT (T-0390) and REFUSES the ``is_self`` row (it self-resurrects via
  ``register_self_if_missing`` on next boot — deleting it is pointless +
  confusing, and routing the destructive op through anything but the owner SSOT
  would be the next is_self privesc).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.install_tokens import hash_token, mint_server_bearer
from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore


_BCRYPT_TEST = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"


def _write_auth(tmp_bot_squad: Path, *, testuser_admin: bool = False) -> None:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        "[users]\n"
        f'testuser = "{_BCRYPT_TEST}"\n'
        f'intruder = "{_BCRYPT_TEST}"\n'
        "[user_meta.testuser]\n"
        'linux_user = "almdudleer"\n'
        f"is_admin = {'true' if testuser_admin else 'false'}\n"
        "[user_meta.intruder]\n"
        'linux_user = "intruder"\n'
        "is_admin = false\n"
        '[session]\nttl = "7d"\n'
    )


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    return TestClient(build_app())


def _login(client: TestClient, username: str) -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": "test"})
    assert r.status_code == 200, r.text


def _seed(
    tmp_bot_squad: Path,
    *,
    server_id: str = "srv_test01",
    owner_user: str = "testuser",
    is_self: bool = False,
) -> tuple[MothershipStore, str]:
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    bearer = mint_server_bearer()
    entry = AttachedServer(
        id=server_id, display_name="Target", base_url="https://target.example.com",
        owner_user=owner_user,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        install_state="connected", server_bearer_hash=hash_token(bearer), is_self=is_self,
    )
    store.write([entry])
    store.store_server_bearer(server_id, bearer)
    store.append_checkpoint(server_id, {"checkpoint": "x", "status": "done"})
    return store, bearer


# ---- store: remove_server sweeps row + bearer + checkpoints ------------------


def test_remove_server_sweeps_row_bearer_and_checkpoints(tmp_bot_squad: Path):
    store, _ = _seed(tmp_bot_squad, server_id="srv_kill")
    assert store.get_server("srv_kill") is not None
    assert store.read_server_bearer("srv_kill") is not None
    assert store.read_checkpoints("srv_kill")

    assert store.remove_server("srv_kill") is True

    assert store.get_server("srv_kill") is None
    assert store.read_server_bearer("srv_kill") is None
    assert store.read_checkpoints("srv_kill") == []


def test_remove_server_unknown_returns_false(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([])
    assert store.remove_server("srv_nope") is False


def test_remove_server_leaves_other_rows(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    _seed(tmp_bot_squad, server_id="srv_a")
    # second row in the same store
    b = mint_server_bearer()
    keep = AttachedServer(
        id="srv_b", display_name="Keep", base_url="https://b", owner_user="testuser",
        created_at="2026-01-01T00:00:00Z", install_state="connected", server_bearer_hash=hash_token(b),
    )
    store.write([*store.list_servers(), keep])
    assert store.remove_server("srv_a") is True
    assert [s.id for s in store.list_servers()] == ["srv_b"]


# ---- route: owner-gated DELETE, refuses is_self -----------------------------


def test_owner_can_delete_peer_server(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.delete("/api/m/servers/srv_test01")
        assert r.status_code == 200, r.text
        # gone from the owner's list
        listing = client.get("/api/m/servers").json()
    assert all(s["id"] != "srv_test01" for s in listing)


def test_non_owner_cannot_delete_server(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.delete("/api/m/servers/srv_test01")
    assert r.status_code == 403, r.text


def test_delete_unknown_server_404(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.delete("/api/m/servers/srv_missing")
    assert r.status_code == 404, r.text


def test_delete_is_self_refused_even_for_admin(tmp_bot_squad: Path, monkeypatch):
    """The is_self row self-resurrects on boot; deleting it is refused (409)."""
    _write_auth(tmp_bot_squad, testuser_admin=True)
    _seed(tmp_bot_squad, server_id="srv_self", owner_user="system", is_self=True)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.delete("/api/m/servers/srv_self")
        assert r.status_code == 409, r.text
        # still present
        store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    assert store.get_server("srv_self") is not None
