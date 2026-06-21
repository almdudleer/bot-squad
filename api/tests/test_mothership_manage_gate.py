"""T-0390 (audit Fork-3): mothership write-authz SSOT.

One ``_can_manage`` predicate + one server-resolving ``require_manage`` FastAPI
dependency gate every server-MANAGEMENT write route (invites, grants, server
delete). The READ/enter tier (``_can_access`` / ``_require_server_access``) is
UNCHANGED — a grantee may still see + proxy into a server, but may NOT manage it.

D3 (operator 2026-06-21): management is OWNER-ONLY. This TIGHTENS invite-mint
from owner-OR-grantee to owner-only — grantee invite-mint is dropped.

Flaw-watch (assert by identity + path-param name match): the enumeration guard
asserts the management routes carry ``require_manage`` BY IDENTITY, and the
behavioural tests assert the gate actually FIRES (a 403, not a silent 404 from a
mis-named path param).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.install_tokens import hash_token, mint_server_bearer
from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore
from app.roles import GlobalRole


_BCRYPT_TEST = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"


# ---- helpers ----------------------------------------------------------------


def _write_auth(tmp_bot_squad: Path) -> None:
    """owner ``testuser`` + non-owner ``grantee`` + non-owner ``intruder``,
    all sharing the "test" password; none is a global admin."""
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        "[users]\n"
        f'testuser = "{_BCRYPT_TEST}"\n'
        f'grantee = "{_BCRYPT_TEST}"\n'
        f'intruder = "{_BCRYPT_TEST}"\n'
        "[user_meta.testuser]\n"
        'linux_user = "almdudleer"\n'
        "is_admin = false\n"
        "[user_meta.grantee]\n"
        'linux_user = "grantee"\n'
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
    is_self: bool = False,
    grants: list[dict] | None = None,
) -> AttachedServer:
    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    bearer = mint_server_bearer()
    entry = AttachedServer(
        id=server_id,
        display_name="Target Test",
        base_url="https://target.example.com",
        owner_user=owner_user,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        install_state="connected",
        server_bearer_hash=hash_token(bearer),
        is_self=is_self,
        grants=grants or [],
    )
    store.write([entry])
    return entry


def _active_grant(username: str, by: str = "testuser") -> dict:
    return {
        "username": username,
        "granted_by": by,
        "granted_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "revoked_at": None,
    }


# ---- unit: the _can_manage predicate ----------------------------------------


def _user(username: str, *, admin: bool = False) -> dict:
    return {
        "username": username,
        "is_admin": admin,
        "global_role": (GlobalRole.GLOBAL_ADMIN if admin else GlobalRole.GLOBAL_MEMBER).value,
    }


def test_can_manage_owner_true():
    from app.routes_mothership import _can_manage

    server = AttachedServer(
        id="srv_x", display_name="x", base_url="https://x", owner_user="alice",
        created_at="2026-01-01T00:00:00Z", install_state="connected",
    )
    assert _can_manage(server, _user("alice")) is True


def test_can_manage_non_owner_false():
    from app.routes_mothership import _can_manage

    server = AttachedServer(
        id="srv_x", display_name="x", base_url="https://x", owner_user="alice",
        created_at="2026-01-01T00:00:00Z", install_state="connected",
    )
    assert _can_manage(server, _user("bob")) is False


def test_can_manage_grantee_false():
    """D3: an active grantee may ENTER but NOT manage."""
    from app.routes_mothership import _can_manage

    server = AttachedServer(
        id="srv_x", display_name="x", base_url="https://x", owner_user="alice",
        created_at="2026-01-01T00:00:00Z", install_state="connected",
        grants=[_active_grant("bob", by="alice")],
    )
    assert _can_manage(server, _user("bob")) is False


def test_can_manage_is_self_requires_global_admin():
    from app.routes_mothership import _can_manage

    self_server = AttachedServer(
        id="srv_self", display_name="self", base_url="https://self", owner_user="system",
        created_at="2026-01-01T00:00:00Z", install_state="ready", is_self=True,
    )
    assert _can_manage(self_server, _user("anyone", admin=True)) is True
    assert _can_manage(self_server, _user("anyone", admin=False)) is False


# ---- behaviour: the gate FIRES (403, not a silent 404) ----------------------


def test_grantee_cannot_create_invite_403(tmp_bot_squad: Path, monkeypatch):
    """D3 tighten: a grantee (who can ENTER) cannot mint an invite."""
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser", grants=[_active_grant("grantee")])
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "grantee")
        r = client.post(
            "/api/m/servers/srv_test01/invites",
            json={"target_username": "x", "role": "non-admin"},
        )
    assert r.status_code == 403, r.text


def test_owner_can_create_invite_200(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.post(
            "/api/m/servers/srv_test01/invites",
            json={"target_username": "newuser", "role": "non-admin"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["target_username"] == "newuser"


def test_non_owner_cannot_create_grant_403(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser")
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "intruder")
        r = client.post("/api/m/servers/srv_test01/grants", json={"username": "x"})
    assert r.status_code == 403, r.text


def test_owner_can_list_grants_200(tmp_bot_squad: Path, monkeypatch):
    _write_auth(tmp_bot_squad)
    _seed_server(tmp_bot_squad, owner_user="testuser", grants=[_active_grant("grantee")])
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client, "testuser")
        r = client.get("/api/m/servers/srv_test01/grants")
    assert r.status_code == 200, r.text
    assert [g["username"] for g in r.json()["grants"]] == ["grantee"]


# ---- enumeration guard: management routes carry require_manage BY IDENTITY ---

# Cookie-auth server-scoped write routes that are deliberately the ACCESS tier
# (a grantee may invoke them) — NOT management. Documented exceptions. Paths are
# router-RELATIVE (the mothership router is mounted under /api/m).
_ACCESS_TIER_ALLOWLIST = {
    # proxy passthrough into the server's own API (the server's own auth re-applies)
    "/servers/{server_id}/api/{rest:path}",
    # refresh the projects cache — a viewer's read-through, not management
    "/servers/{server_id}/projects/refresh",
}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _all_api_routes(app) -> list[APIRoute]:
    """Collect every APIRoute, descending FastAPI's ``_IncludedRouter`` lazy
    include wrappers (this FastAPI version stores included routers as opaque
    placeholders in ``app.routes`` rather than flattening them — a naive
    ``isinstance(r, APIRoute)`` sweep over ``app.routes`` would see ZERO routed
    endpoints and pass vacuously). Paths are router-relative.
    """
    seen: set[int] = set()
    out: list[APIRoute] = []

    def walk(router) -> None:
        if id(router) in seen:
            return
        seen.add(id(router))
        for r in getattr(router, "routes", []):
            if type(r).__name__ == "_IncludedRouter":
                walk(r.original_router)
            elif isinstance(r, APIRoute):
                out.append(r)

    walk(app.router)
    return out


def _dep_callables(route: APIRoute) -> set:
    out: set = set()

    def walk(dep) -> None:
        if dep.call is not None:
            out.add(dep.call)
        for sub in dep.dependencies:
            walk(sub)

    walk(route.dependant)
    return out


def test_every_server_management_write_route_is_gated(tmp_bot_squad, monkeypatch):
    """No cookie-auth server-scoped write route is require_auth-only: it must
    carry ``require_manage`` (owner SSOT) or ``_require_super_admin``, else be an
    allowlisted access-tier exception. A new management route that forgets the
    gate goes RED here."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MOTHERSHIP", "1")
    from app.routes_mothership import require_manage, _require_super_admin

    app = build_app()
    routes = _all_api_routes(app)
    # guard against a future FastAPI that flattens — make sure we actually saw routes
    assert any(r.path.startswith("/servers/{server_id}") for r in routes), "walker found no server routes"
    ungated = []
    for route in routes:
        if not (route.methods & _WRITE_METHODS):
            continue
        if not route.path.startswith("/servers/{server_id}"):
            continue
        if route.path in _ACCESS_TIER_ALLOWLIST:
            continue
        deps = _dep_callables(route)
        if require_manage not in deps and _require_super_admin not in deps:
            for m in sorted(route.methods & _WRITE_METHODS):
                ungated.append(f"{m} {route.path}")
    assert not ungated, "server-management write routes missing require_manage:\n" + "\n".join(ungated)


def test_management_routes_carry_require_manage_by_identity(tmp_bot_squad, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MOTHERSHIP", "1")
    from app.routes_mothership import require_manage

    # router-relative paths (the mothership router mounts under /api/m)
    expected = {
        ("POST", "/servers/{server_id}/invites"),
        ("GET", "/servers/{server_id}/grants"),
        ("POST", "/servers/{server_id}/grants"),
        ("DELETE", "/servers/{server_id}/grants/{username}"),
    }
    app = build_app()
    seen = set()
    for route in _all_api_routes(app):
        for m in route.methods:
            if (m, route.path) in expected:
                assert require_manage in _dep_callables(route), f"{m} {route.path} not gated by require_manage"
                seen.add((m, route.path))
    assert seen == expected, f"missing management routes: {expected - seen}"
