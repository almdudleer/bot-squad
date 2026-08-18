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

Flaw-watch 2 (T-0900): the enumeration must not depend on the FastAPI route
SHAPE. ``fastapi>=0.110`` is unpinned, and the shapes disagree — 0.136 (what the
install's api venv runs) flattens included routers, 0.139+ (what the api image
runs) hides them behind ``_IncludedRouter``. Reading one shape only enumerates
NOTHING on the other, which is why this file was red on the install's fastapi
from the day it was written. ``_all_api_routes`` handles both, and
``test_enumeration_flags_an_ungated_management_route`` is the negative control
that keeps a green here from meaning "the walker saw nothing".
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.install_tokens import hash_token, mint_server_bearer
from app.main import build_app
from app.mothership_store import AttachedServer, MothershipStore
from app.roles import GlobalRole
from app.routes_auth import require_auth


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

# The mothership router mounts under ``/api/m``; every path below is the FULL
# mounted path, because the router-RELATIVE form is not stable across the
# FastAPI versions this repo runs (see ``_all_api_routes``).
_MANAGE_SCOPE = "/api/m/servers/{server_id}"

# Cookie-auth server-scoped write routes that are deliberately the ACCESS tier
# (a grantee may invoke them) — NOT management. Documented exceptions.
_ACCESS_TIER_ALLOWLIST = {
    # proxy passthrough into the server's own API (the server's own auth re-applies)
    _MANAGE_SCOPE + "/api/{rest:path}",
    # refresh the projects cache — a viewer's read-through, not management
    _MANAGE_SCOPE + "/projects/refresh",
}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _all_api_routes(app) -> list[tuple[APIRoute, str]]:
    """Every ``APIRoute`` paired with its FULL mounted path.

    TWO FastAPI route shapes are live at once — ``api/pyproject.toml`` pins only
    ``fastapi>=0.110``, so the install's api venv (0.136) and the api docker
    image / a fresh CI install (0.141) disagree — and reading only one of them
    fails SILENTLY, with an empty list, i.e. a vacuous pass in the suite that
    guards write authz (T-0900, and this is exactly what went red):

    * fastapi 0.136 FLATTENS on include — ``app.routes`` holds every ``APIRoute``
      already carrying its full path.
    * fastapi 0.141 appends opaque ``_IncludedRouter`` placeholders instead; the
      routes sit on ``original_router`` with router-RELATIVE paths and the mount
      prefix lives on ``include_context.prefix``.

    This walker descends the placeholders while ACCUMULATING that prefix, so both
    shapes yield the same full paths. Callers assert the result is non-empty, and
    ``test_enumeration_flags_an_ungated_management_route`` keeps the whole
    instrument from passing dead.
    """
    seen: set[tuple[int, str]] = set()
    out: list[tuple[APIRoute, str]] = []

    def walk(router, prefix: str) -> None:
        key = (id(router), prefix)
        if key in seen:
            return
        seen.add(key)
        for r in getattr(router, "routes", []):
            if type(r).__name__ == "_IncludedRouter":
                sub = getattr(getattr(r, "include_context", None), "prefix", "") or ""
                walk(r.original_router, prefix + sub)
            elif isinstance(r, APIRoute):
                out.append((r, prefix + r.path))

    walk(app.router, "")
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


def _gate_env(tmp_bot_squad: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("MOTHERSHIP", "1")


def _ungated_management_writes(app) -> list[str]:
    """``["<METHOD> <full path>", ...]`` for every cookie-auth server-scoped
    write route carrying neither ``require_manage`` (owner SSOT) nor
    ``_require_super_admin``, minus the allowlisted access-tier exceptions.

    Shared by the guard and by its negative control so the control exercises the
    SAME instrument, not a lookalike.
    """
    from app.routes_mothership import require_manage, _require_super_admin

    routes = _all_api_routes(app)
    # Vacuity guard: an enumeration that sees no server routes at all reports
    # "nothing ungated" and is indistinguishable from a clean gate.
    assert any(p.startswith(_MANAGE_SCOPE) for _, p in routes), (
        f"walker found no {_MANAGE_SCOPE} routes among {len(routes)} routes — "
        "the FastAPI route shape moved again; the enumeration is vacuous"
    )
    ungated: list[str] = []
    for route, path in routes:
        if not (route.methods & _WRITE_METHODS):
            continue
        if not path.startswith(_MANAGE_SCOPE):
            continue
        if path in _ACCESS_TIER_ALLOWLIST:
            continue
        deps = _dep_callables(route)
        if require_manage not in deps and _require_super_admin not in deps:
            for m in sorted(route.methods & _WRITE_METHODS):
                ungated.append(f"{m} {path}")
    return ungated


def test_every_server_management_write_route_is_gated(tmp_bot_squad: Path, monkeypatch):
    """No cookie-auth server-scoped write route is require_auth-only: it must
    carry ``require_manage`` (owner SSOT) or ``_require_super_admin``, else be an
    allowlisted access-tier exception. A new management route that forgets the
    gate goes RED here."""
    _gate_env(tmp_bot_squad, monkeypatch)
    ungated = _ungated_management_writes(build_app())
    assert not ungated, "server-management write routes missing require_manage:\n" + "\n".join(ungated)


def test_enumeration_flags_an_ungated_management_route(tmp_bot_squad: Path, monkeypatch):
    """Negative control for the guard above (T-0900).

    Plant a require_auth-only write route inside the management scope and assert
    the SAME instrument names it. Without this, the green above is worth nothing:
    the walker it runs on enumerated ZERO routes on the install's fastapi for
    weeks, and a dead check looks exactly like a clean gate.
    """
    _gate_env(tmp_bot_squad, monkeypatch)
    app = build_app()
    planted = APIRouter()

    @planted.post("/servers/{server_id}/t0900-negative-control")
    def _ungated_route(server_id: str, user: dict = Depends(require_auth)) -> dict:
        return {"server_id": server_id}

    app.include_router(planted, prefix="/api/m")
    assert (
        "POST " + _MANAGE_SCOPE + "/t0900-negative-control"
        in _ungated_management_writes(app)
    )


def test_management_routes_carry_require_manage_by_identity(tmp_bot_squad: Path, monkeypatch):
    _gate_env(tmp_bot_squad, monkeypatch)
    from app.routes_mothership import require_manage

    expected = {
        ("POST", _MANAGE_SCOPE + "/invites"),
        ("GET", _MANAGE_SCOPE + "/grants"),
        ("POST", _MANAGE_SCOPE + "/grants"),
        ("DELETE", _MANAGE_SCOPE + "/grants/{username}"),
        ("POST", _MANAGE_SCOPE + "/hold"),
        ("POST", _MANAGE_SCOPE + "/unhold"),
    }
    app = build_app()
    seen = set()
    for route, path in _all_api_routes(app):
        for m in route.methods:
            if (m, path) in expected:
                assert require_manage in _dep_callables(route), f"{m} {path} not gated by require_manage"
                seen.add((m, path))
    assert seen == expected, f"missing management routes: {expected - seen}"
