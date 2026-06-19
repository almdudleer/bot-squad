"""T-0169: per-server access control on the mothership routes.

Closes the pivot where ANY authenticated mothership user could proxy into
ANY registered server via the stored ``server_bearer``. The guard
``_require_server_access`` allows a user to act on a server only if they own
it (``owner_user``), are super-admin (``is_admin``), or the server is the
mothership's own ``is_self`` entry.

These tests rewrite ``auth.toml`` to introduce a NON-owner NON-admin user
(``intruder``) and assert that the 403 fires BEFORE any upstream call /
bearer read / side effect — reusing the ``MockTransport`` recorder pattern
from ``test_routes_mothership_proxy.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import routes_mothership
from app.install_tokens import hash_token, mint_server_bearer
from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore


# bcrypt hash of "test" (rounds=12) — same one the conftest seeds.
_BCRYPT_TEST = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"


# ---- helpers ----------------------------------------------------------------


def _write_auth(
    tmp_bot_squad: Path,
    *,
    testuser_is_admin: bool,
    intruder_is_admin: bool,
) -> None:
    """Overwrite the conftest-seeded auth.toml with two users sharing the
    "test" password: ``testuser`` (the server owner) and ``intruder`` (a
    distinct user). Admin flags are per-test so we can exercise owner /
    non-owner × admin / non-admin combinations.
    """
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        "[users]\n"
        f'testuser = "{_BCRYPT_TEST}"\n'
        f'intruder = "{_BCRYPT_TEST}"\n'
        "[user_meta.testuser]\n"
        'linux_user = "almdudleer"\n'
        f"is_admin = {'true' if testuser_is_admin else 'false'}\n"
        "[user_meta.intruder]\n"
        'linux_user = "intruder"\n'
        f"is_admin = {'true' if intruder_is_admin else 'false'}\n"
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


def _seed_connected_server(
    tmp_bot_squad: Path,
    *,
    server_id: str = "srv_test01",
    base_url: str = "https://target.example.com",
    owner_user: str = "testuser",
    is_self: bool = False,
    projects_cache: list[dict] | None = None,
    grants: list[dict] | None = None,
) -> tuple[AttachedServer, str]:
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    bearer = mint_server_bearer()
    entry = AttachedServer(
        id=server_id,
        display_name="Target Test",
        base_url=base_url,
        owner_user=owner_user,
        created_at=datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        install_state="connected",
        server_bearer_hash=hash_token(bearer),
        projects_cache=projects_cache or [],
        is_self=is_self,
        grants=grants or [],
    )
    store.write([entry])
    bearers_dir = tmp_bot_squad / "data" / "_mothership" / "bearers"
    bearers_dir.mkdir(parents=True, exist_ok=True)
    bearer_path = bearers_dir / server_id
    bearer_path.write_text(bearer, encoding="utf-8")
    bearer_path.chmod(0o600)
    return entry, bearer


class _Recorder:
    def __init__(self, responder):
        self.calls: list[httpx.Request] = []
        self._responder = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._responder(request)


def _install_mock_upstream(monkeypatch, responder) -> _Recorder:
    recorder = _Recorder(responder)

    def _factory(base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler),
            base_url=base_url,
        )

    monkeypatch.setattr(routes_mothership, "_proxy_client", _factory)
    return recorder


def _no_upstream(monkeypatch) -> _Recorder:
    return _install_mock_upstream(
        monkeypatch,
        lambda req: pytest.fail(f"unexpected upstream call: {req.url}"),
    )


# ---- intruder (non-owner, non-admin) → 403, no upstream call ----------------


def test_intruder_proxy_get_forbidden_no_upstream(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    recorder = _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.get("/api/m/servers/srv_test01/api/health")

    assert r.status_code == 403, r.text
    assert "not authorized" in r.text.lower()
    assert recorder.calls == []


def test_intruder_proxy_post_forbidden_no_upstream(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    recorder = _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post(
            "/api/m/servers/srv_test01/api/projects/foo/backlog",
            json={"title": "x"},
        )

    assert r.status_code == 403, r.text
    assert recorder.calls == []


def test_intruder_refresh_forbidden_no_upstream(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    recorder = _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post("/api/m/servers/srv_test01/projects/refresh")

    assert r.status_code == 403, r.text
    assert recorder.calls == []


def test_intruder_list_projects_forbidden(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        owner_user="testuser",
        projects_cache=[{"slug": "alpha", "display_name": "Alpha", "status": "idle"}],
    )
    recorder = _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.get("/api/m/servers/srv_test01/projects")

    assert r.status_code == 403, r.text
    assert recorder.calls == []


def test_intruder_checkpoints_forbidden(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.get("/api/m/servers/srv_test01/checkpoints")

    assert r.status_code == 403, r.text


def test_intruder_create_invite_forbidden(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post(
            "/api/m/servers/srv_test01/invites",
            json={"target_username": "bob", "role": "non-admin"},
        )

    assert r.status_code == 403, r.text


# ---- owner (non-admin) → allowed --------------------------------------------


def test_owner_nonadmin_proxy_allowed(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _, bearer = _seed_connected_server(
        tmp_bot_squad, server_id="srv_test01", owner_user="testuser"
    )

    def responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True}, headers={"content-type": "application/json"}
        )

    recorder = _install_mock_upstream(monkeypatch, responder)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.get("/api/m/servers/srv_test01/api/health")

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert len(recorder.calls) == 1
    assert recorder.calls[0].headers.get("authorization") == f"Bearer {bearer}"


# ---- admin non-owner → 403 (god-mode REMOVED, T-0221 D2) --------------------
# This is the INTENTIONAL flip vs T-0169's conservative kept-god-mode stopgap:
# a global admin who neither owns nor holds a grant for the server is now denied
# entry, with zero upstream calls (deny BEFORE the proxy hop / bearer read).


def test_admin_nonowner_proxy_forbidden_no_upstream(tmp_bot_squad: Path, monkeypatch):
    # intruder is admin; server is owned by testuser; intruder holds no grant.
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=True)
    _seed_connected_server(
        tmp_bot_squad, server_id="srv_test01", owner_user="testuser"
    )
    recorder = _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.get("/api/m/servers/srv_test01/api/health")

    assert r.status_code == 403, r.text
    assert "not authorized" in r.text.lower()
    assert recorder.calls == []


# ---- active grantee (non-owner, non-admin) → allowed ------------------------


def _active_grant(username: str, granted_by: str = "testuser") -> dict:
    return {
        "username": username,
        "granted_by": granted_by,
        "granted_at": "2026-06-19T00:00:00Z",
        "revoked_at": None,
    }


def test_active_grantee_proxy_allowed(tmp_bot_squad: Path, monkeypatch):
    # intruder is neither owner nor admin, but holds an active grant.
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _, bearer = _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        owner_user="testuser",
        grants=[_active_grant("intruder")],
    )

    def responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True}, headers={"content-type": "application/json"}
        )

    recorder = _install_mock_upstream(monkeypatch, responder)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.get("/api/m/servers/srv_test01/api/health")

    assert r.status_code == 200, r.text
    assert len(recorder.calls) == 1
    assert recorder.calls[0].headers.get("authorization") == f"Bearer {bearer}"


def test_revoked_grantee_proxy_forbidden_no_upstream(tmp_bot_squad: Path, monkeypatch):
    # A revoked grant (revoked_at set) does NOT grant access.
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    revoked = {
        "username": "intruder",
        "granted_by": "testuser",
        "granted_at": "2026-06-19T00:00:00Z",
        "revoked_at": "2026-06-19T01:00:00Z",
    }
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        owner_user="testuser",
        grants=[revoked],
    )
    recorder = _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.get("/api/m/servers/srv_test01/api/health")

    assert r.status_code == 403, r.text
    assert recorder.calls == []


# ---- is_self server → allowed regardless of owner ---------------------------


def test_is_self_allowed_for_nonowner_nonadmin(tmp_bot_squad: Path, monkeypatch):
    """A non-owner non-admin may enter the mothership's own (is_self) server:
    list_server_projects fans into the LOCAL api whose own auth applies.
    Owner is deliberately someone else to prove is_self bypasses the owner
    check.
    """
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_self",
        owner_user="someone_else",
        is_self=True,
    )
    # No upstream call expected — is_self list fans into the local handler.
    recorder = _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.get("/api/m/servers/srv_self/projects")

    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
    assert recorder.calls == []


# ---- grant lifecycle API (owner-only) — T-0221 ------------------------------


def _ok_upstream(monkeypatch) -> _Recorder:
    return _install_mock_upstream(
        monkeypatch,
        lambda req: httpx.Response(
            200, json={"ok": True}, headers={"content-type": "application/json"}
        ),
    )


def test_owner_post_grant_then_grantee_gains_access(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    _ok_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        # Before the grant, intruder is denied.
        _login(client, "intruder")
        assert client.get("/api/m/servers/srv_test01/api/health").status_code == 403

        # Owner grants intruder access.
        _login(client, "testuser")
        r = client.post(
            "/api/m/servers/srv_test01/grants", json={"username": "intruder"}
        )
        assert r.status_code == 200, r.text
        assert any(g["username"] == "intruder" for g in r.json()["grants"])

        # Now intruder is allowed.
        _login(client, "intruder")
        assert client.get("/api/m/servers/srv_test01/api/health").status_code == 200


def test_owner_post_grant_idempotent(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        client.post("/api/m/servers/srv_test01/grants", json={"username": "intruder"})
        r = client.post(
            "/api/m/servers/srv_test01/grants", json={"username": "intruder"}
        )
        assert r.status_code == 200, r.text
        active = [g for g in r.json()["grants"] if g["username"] == "intruder"]
        assert len(active) == 1  # no duplicate active rows


def test_nonowner_cannot_post_grant(tmp_bot_squad: Path, monkeypatch):
    # intruder is a plain non-owner non-admin.
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post(
            "/api/m/servers/srv_test01/grants", json={"username": "bob"}
        )
    assert r.status_code == 403, r.text


def test_admin_nonowner_cannot_post_grant(tmp_bot_squad: Path, monkeypatch):
    # A global admin who is not the owner cannot manage grants (no god-mode).
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=True)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post(
            "/api/m/servers/srv_test01/grants", json={"username": "bob"}
        )
    assert r.status_code == 403, r.text


def test_grantee_cannot_regrant(tmp_bot_squad: Path, monkeypatch):
    # A grantee has access but must NOT be able to re-grant to a third user.
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        owner_user="testuser",
        grants=[_active_grant("intruder")],
    )
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post(
            "/api/m/servers/srv_test01/grants", json={"username": "bob"}
        )
    assert r.status_code == 403, r.text


def test_owner_delete_grant_revokes_access(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        owner_user="testuser",
        grants=[_active_grant("intruder")],
    )
    _ok_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        # Grantee has access first.
        _login(client, "intruder")
        assert client.get("/api/m/servers/srv_test01/api/health").status_code == 200

        # Owner revokes.
        _login(client, "testuser")
        r = client.delete("/api/m/servers/srv_test01/grants/intruder")
        assert r.status_code == 200, r.text
        assert all(g["username"] != "intruder" for g in r.json()["grants"])

        # Access is gone.
        _login(client, "intruder")
        assert client.get("/api/m/servers/srv_test01/api/health").status_code == 403


def test_nonowner_cannot_delete_grant(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=True, intruder_is_admin=False)
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        owner_user="someone_else",
        grants=[_active_grant("intruder", granted_by="someone_else")],
    )
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        # testuser is admin but NOT the owner — denied.
        _login(client, "testuser")
        r = client.delete("/api/m/servers/srv_test01/grants/intruder")
    assert r.status_code == 403, r.text


def test_get_grants_owner_only(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=True)
    _seed_connected_server(
        tmp_bot_squad,
        server_id="srv_test01",
        owner_user="testuser",
        grants=[_active_grant("intruder")],
    )
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        # Owner sees the grants.
        _login(client, "testuser")
        r = client.get("/api/m/servers/srv_test01/grants")
        assert r.status_code == 200, r.text
        assert [g["username"] for g in r.json()["grants"]] == ["intruder"]

        # Admin non-owner (also the grantee here) cannot read the grant list.
        _login(client, "intruder")
        assert client.get("/api/m/servers/srv_test01/grants").status_code == 403


def test_post_grant_unknown_server_404(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=True, intruder_is_admin=False)
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.post("/api/m/servers/srv_missing/grants", json={"username": "bob"})
    assert r.status_code == 404, r.text


def test_post_grant_missing_username_400(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_connected_server(tmp_bot_squad, server_id="srv_test01", owner_user="testuser")
    _no_upstream(monkeypatch)

    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.post("/api/m/servers/srv_test01/grants", json={})
    assert r.status_code == 400, r.text


# ---- list scoping (T-0219 D3): owner/grant-scoped, no global all-servers ----


def _make_server(
    server_id: str,
    *,
    owner_user: str,
    is_self: bool = False,
    grants: list[dict] | None = None,
) -> AttachedServer:
    return AttachedServer(
        id=server_id,
        display_name=server_id,
        base_url=f"https://{server_id}.example.com",
        owner_user=owner_user,
        created_at="2026-06-19T00:00:00Z",
        install_state="connected",
        is_self=is_self,
        grants=grants or [],
    )


def _seed_servers(tmp_bot_squad: Path, servers: list[AttachedServer]) -> None:
    MothershipStore(tmp_bot_squad / "data" / "_mothership").write(servers)


def _list_ids(client: TestClient, *, include_self: bool = True) -> set[str]:
    # The app auto-registers its own ``is_self`` entry on startup (always
    # visible per ``_can_access``); ``include_self=False`` filters it out so a
    # test can assert the exact set of PEER (attached) servers it seeded.
    r = client.get("/api/m/servers")
    assert r.status_code == 200, r.text
    return {s["id"] for s in r.json() if include_self or not s.get("is_self")}


def test_list_servers_owner_sees_only_own(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_servers(
        tmp_bot_squad,
        [
            _make_server("srv_mine", owner_user="testuser"),
            _make_server("srv_theirs", owner_user="someone_else"),
        ],
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        assert _list_ids(client, include_self=False) == {"srv_mine"}


def test_list_servers_admin_does_not_see_others(tmp_bot_squad: Path, monkeypatch):
    # Global admin, but god-mode is gone — they don't own srv_theirs, so it
    # does not appear in their list.
    _write_auth(tmp_bot_squad, testuser_is_admin=True, intruder_is_admin=False)
    _seed_servers(
        tmp_bot_squad,
        [
            _make_server("srv_mine", owner_user="testuser"),
            _make_server("srv_theirs", owner_user="someone_else"),
        ],
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        assert _list_ids(client, include_self=False) == {"srv_mine"}


def test_list_servers_grantee_sees_granted(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_servers(
        tmp_bot_squad,
        [
            _make_server("srv_owned_by_other", owner_user="someone_else"),
            _make_server(
                "srv_granted",
                owner_user="someone_else",
                grants=[_active_grant("intruder", granted_by="someone_else")],
            ),
        ],
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        assert _list_ids(client, include_self=False) == {"srv_granted"}


def test_list_servers_revoke_removes_from_list(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_servers(
        tmp_bot_squad,
        [
            _make_server(
                "srv_granted",
                owner_user="testuser",
                grants=[_active_grant("intruder")],
            ),
        ],
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        assert _list_ids(client, include_self=False) == {"srv_granted"}  # grantee sees it

        _login(client, "testuser")
        client.delete("/api/m/servers/srv_granted/grants/intruder")

        _login(client, "intruder")
        assert _list_ids(client, include_self=False) == set()  # revoked → gone


def test_list_servers_includes_is_self_for_anyone(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_servers(
        tmp_bot_squad,
        [
            _make_server("srv_self", owner_user="system", is_self=True),
            _make_server("srv_theirs", owner_user="someone_else"),
        ],
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        ids = _list_ids(client)
        assert "srv_self" in ids          # seeded is_self always visible
        assert "srv_theirs" not in ids    # peer owned by someone else is not


def test_list_servers_strips_grants_from_public(tmp_bot_squad: Path, monkeypatch):
    # The grants list must not leak through the public list projection.
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    _seed_servers(
        tmp_bot_squad,
        [
            _make_server(
                "srv_mine",
                owner_user="testuser",
                grants=[_active_grant("intruder")],
            ),
        ],
    )
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.get("/api/m/servers")
        row = next(s for s in r.json() if s["id"] == "srv_mine")
        assert "grants" not in row


# ---- compat: old (pre-v2) servers.json without a grants key loads -----------


def test_old_servers_json_without_grants_loads(tmp_bot_squad: Path, monkeypatch):
    # Simulate a v1 registry written before T-0221: a server row with NO
    # ``grants`` key (and the old version stamp). It must deserialise with an
    # empty grants list and not crash the list endpoint.
    import json

    mship = tmp_bot_squad / "data" / "_mothership"
    mship.mkdir(parents=True, exist_ok=True)
    (mship / "servers.json").write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {
                        "id": "srv_legacy",
                        "display_name": "Legacy",
                        "base_url": "https://legacy.example.com",
                        "owner_user": "testuser",
                        "created_at": "2026-06-01T00:00:00Z",
                        "install_state": "connected",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = MothershipStore(mship)
    loaded = store.list_servers()
    assert len(loaded) == 1
    assert loaded[0].grants == []

    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=False)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        assert _list_ids(client, include_self=False) == {"srv_legacy"}
