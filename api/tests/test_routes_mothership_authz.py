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


# ---- admin non-owner → allowed (status-quo god-mode preserved) --------------


def test_admin_nonowner_proxy_allowed(tmp_bot_squad: Path, monkeypatch):
    # intruder is admin; server is owned by testuser.
    _write_auth(tmp_bot_squad, testuser_is_admin=False, intruder_is_admin=True)
    _, bearer = _seed_connected_server(
        tmp_bot_squad, server_id="srv_test01", owner_user="testuser"
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
