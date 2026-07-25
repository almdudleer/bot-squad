"""T-0653: deliberate-hold state for a non-terminal server install.

A pending install that's actually a live, stakeholder-gated hold (e.g. T-0328
awaiting explicit GO) previously read identically to a genuinely dead/stalled
install — the FE had no signal to tell them apart other than elapsed time.
This adds an explicit ``hold_reason``/``held_at``/``held_by`` on the registry
row, settable/clearable ONLY by the server owner (mirrors the grants gate),
so the FE state-derivation can treat a held row as distinct from stalled.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.install_tokens import hash_token, mint_server_bearer
from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore


_BCRYPT_TEST = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"


def _write_auth(tmp_bot_squad: Path) -> None:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        "[users]\n"
        f'testuser = "{_BCRYPT_TEST}"\n'
        f'intruder = "{_BCRYPT_TEST}"\n'
        "[user_meta.testuser]\n"
        'linux_user = "almdudleer"\n'
        "is_admin = false\n"
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


def _seed_server(
    tmp_bot_squad: Path,
    *,
    server_id: str = "srv_test01",
    owner_user: str = "testuser",
    install_state: str = "pending",
) -> AttachedServer:
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    bearer = mint_server_bearer()
    entry = AttachedServer(
        id=server_id,
        display_name="Target Test",
        base_url="https://target.example.com",
        owner_user=owner_user,
        created_at="2026-06-20T15:08:31Z",
        install_state=install_state,
        server_bearer_hash=hash_token(bearer),
    )
    store.write([entry])
    return entry


# ---- store: set_hold / clear_hold --------------------------------------------


def _srv(**overrides) -> AttachedServer:
    base = dict(
        id="srv_x",
        display_name="x",
        base_url="https://x",
        owner_user="u",
        created_at="2026-01-01T00:00:00Z",
        install_state="pending",
    )
    base.update(overrides)
    return AttachedServer(**base)


def test_set_hold_sets_reason_and_holder(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([_srv()])
    out = store.set_hold("srv_x", "awaiting stakeholder GO (T-0328)", held_by="alexey")
    assert out.hold_reason == "awaiting stakeholder GO (T-0328)"
    assert out.held_by == "alexey"
    assert out.held_at is not None
    reread = store.get_server("srv_x")
    assert reread.hold_reason == "awaiting stakeholder GO (T-0328)"


def test_set_hold_idempotent_overwrites_reason(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([_srv()])
    store.set_hold("srv_x", "first reason", held_by="alexey")
    out = store.set_hold("srv_x", "second reason", held_by="alexey")
    assert out.hold_reason == "second reason"


def test_set_hold_unknown_server_returns_none(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([])
    assert store.set_hold("nope", "reason", held_by="alexey") is None


def test_clear_hold_clears_all_three_fields(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([_srv()])
    store.set_hold("srv_x", "reason", held_by="alexey")
    out = store.clear_hold("srv_x")
    assert out.hold_reason is None
    assert out.held_at is None
    assert out.held_by is None


def test_clear_hold_noop_when_not_held(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([_srv()])
    out = store.clear_hold("srv_x")
    assert out.hold_reason is None


def test_clear_hold_unknown_server_returns_none(tmp_bot_squad: Path):
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.write([])
    assert store.clear_hold("nope") is None


def test_pre_t0653_row_without_hold_fields_deserialises(tmp_bot_squad: Path):
    """A servers.json row written before T-0653 (no hold_reason key at all)
    must still load cleanly with the field defaulting to None."""
    import json

    root = tmp_bot_squad / "data" / "_mothership"
    root.mkdir(parents=True, exist_ok=True)
    (root / "servers.json").write_text(
        json.dumps(
            {
                "version": 2,
                "servers": [
                    {
                        "id": "srv_old",
                        "display_name": "old",
                        "base_url": "https://old.example.com",
                        "owner_user": "u",
                        "created_at": "2026-01-01T00:00:00Z",
                        "install_state": "pending",
                    }
                ],
            }
        )
    )
    store = MothershipStore(root)
    server = store.get_server("srv_old")
    assert server.hold_reason is None
    assert server.held_at is None
    assert server.held_by is None


# ---- route: owner-gated hold/unhold ------------------------------------------


def test_owner_can_hold_server_200(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.post(
            "/api/m/servers/srv_test01/hold",
            json={"reason": "awaiting stakeholder GO"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hold_reason"] == "awaiting stakeholder GO"
    assert body["held_by"] == "testuser"
    assert body["held_at"] is not None


def test_hold_requires_nonempty_reason_400(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.post("/api/m/servers/srv_test01/hold", json={"reason": ""})
    assert r.status_code == 400, r.text


def test_non_owner_cannot_hold_server_403(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post(
            "/api/m/servers/srv_test01/hold", json={"reason": "nope"}
        )
    assert r.status_code == 403, r.text


def test_owner_can_unhold_server_200(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        client.post(
            "/api/m/servers/srv_test01/hold", json={"reason": "awaiting GO"}
        )
        r = client.post("/api/m/servers/srv_test01/unhold")
    assert r.status_code == 200, r.text
    assert r.json()["hold_reason"] is None


def test_non_owner_cannot_unhold_server_403(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        client.post(
            "/api/m/servers/srv_test01/hold", json={"reason": "awaiting GO"}
        )
        client.cookies.clear()
        _login(client, "intruder")
        r = client.post("/api/m/servers/srv_test01/unhold")
    assert r.status_code == 403, r.text


def test_hold_unknown_server_404(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.post(
            "/api/m/servers/srv_nonexistent/hold", json={"reason": "x"}
        )
    assert r.status_code == 404, r.text


def test_listed_server_includes_hold_reason_in_public_projection(
    tmp_bot_squad: Path, monkeypatch
):
    """hold_reason is NOT sensitive (unlike grants/invites) — any viewer who
    can see the row via GET /servers needs to see why it's held."""
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        client.post(
            "/api/m/servers/srv_test01/hold", json={"reason": "awaiting GO"}
        )
        r = client.get("/api/m/servers")
    assert r.status_code == 200, r.text
    rows = {s["id"]: s for s in r.json()}
    assert rows["srv_test01"]["hold_reason"] == "awaiting GO"
