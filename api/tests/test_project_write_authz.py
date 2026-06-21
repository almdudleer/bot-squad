"""T-0381: PROJECT-WRITE routes require admin (require_project_member gate).

dogfood (T-0331, session p206) proved — live, as non-admin ``aqice`` — that
project-WRITE routes were ``Depends(require_auth)``-ONLY: any authenticated user
could trigger deploys, rewrite the fleet-wide AGENTS.md (prompt injection across
every agent), enable autonomous runs, spawn agents (cost abuse), delete tasks,
and edit docs/usecases/vision/feedback. Part of the privesc cluster with T-0376
(create_server), T-0377 (is_self bypass), T-0379 (stale-image deploy).

DECIDED MODEL (operator, 2026-06-21): project writes require owner/member; admin
always allowed. No per-project membership store exists yet, so the enforceable
gate today is admin-only via the shared ``require_project_member`` dependency
(``app.project_authz``) — the single extension point for project-roles (T-0216).
Reads stay broad. A non-admin must get 403 BEFORE the side effect.

Phase 1 (original, p67): the verified-dangerous set. Phase 2 (p210): content
CRUD (backlog/docs/usecases/vision/feedback). The enumeration guard at the end
asserts NO project-write route regresses to require_auth-only.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import build_app
from app.project_authz import require_project_member
from app.routes_auth import require_admin


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    return TestClient(build_app())


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


def _make_nonadmin(tmp_bot_squad: Path) -> None:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )


_P = "/api/projects/test-project"

# (method, path, json-body) for every gated project-WRITE route.
# Phase 1 — verified-dangerous (cost / destructive / fleet-config).
_PHASE1 = [
    ("post", f"{_P}/deploy", {"target": "staging", "reason": "x"}),
    ("put", f"{_P}/repo-agents-md", {"content": "x"}),          # fleet prompt-injection
    ("post", f"{_P}/autonomous/enable", {}),
    ("post", f"{_P}/autonomous/disable", {}),
    ("post", f"{_P}/autopilot/start", {"kind": "project", "prompt": "x"}),
    ("post", f"{_P}/autopilot/stop", {"key": "x"}),
    ("post", f"{_P}/sessions", {"window": "x"}),                # spawn agent
    ("post", f"{_P}/dev-spawn-request", {"tl_sid": "S-x", "instructions": "x"}),
    ("delete", f"{_P}/backlog/T-0001", None),                   # destructive
]
# Phase 2 — content CRUD (backlog / docs / usecases / vision / feedback).
_PHASE2 = [
    ("post", f"{_P}/backlog", {"title": "x"}),
    ("patch", f"{_P}/backlog/T-0001", {"status": "open"}),
    ("patch", f"{_P}/backlog/T-0001/priority", {"priority": 1}),
    ("post", f"{_P}/backlog/T-0001/comments", {"body": "x"}),
    ("post", f"{_P}/backlog/T-0001/progress", {"text": "x"}),
    ("post", f"{_P}/vision/active_initiatives/foo", {"body": "x"}),
    ("put", f"{_P}/vision/roles/dev", {"body": "x"}),
    ("post", f"{_P}/docs", {"title": "x", "body": "y"}),
    ("put", f"{_P}/docs/D-0001", {"body": "x"}),
    ("post", f"{_P}/use_cases", {"title": "x"}),
    ("post", f"{_P}/use_cases/UC-0001/run", {}),
    ("post", f"{_P}/feedback/F-0001/promote", {}),
]
_GATED = _PHASE1 + _PHASE2


def test_nonadmin_blocked_from_all_project_writes(tmp_bot_squad, monkeypatch):
    _make_nonadmin(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        failures = []
        for method, path, body in _GATED:
            r = getattr(client, method)(path, json=body) if body is not None else client.request(method, path)
            if r.status_code != 403:
                failures.append(f"{method.upper()} {path} -> {r.status_code} (want 403)")
    assert not failures, "non-admin reached project writes:\n" + "\n".join(failures)


def test_nonadmin_can_still_read(tmp_bot_squad, monkeypatch):
    # Reads stay broad — gating must not break GET.
    _make_nonadmin(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        assert client.get(f"{_P}/backlog").status_code == 200
        assert client.get(f"{_P}/docs").status_code == 200


def test_admin_passes_the_gate(tmp_bot_squad, monkeypatch):
    # conftest testuser is admin → must NOT be 403 (gate passed; deeper
    # 400/404/422/502 is fine). Probes one route per gated module.
    samples = [
        ("delete", f"{_P}/backlog/T-0001", None),
        ("put", f"{_P}/repo-agents-md", {"content": ""}),
        ("post", f"{_P}/docs", {"title": "", "body": ""}),
        ("post", f"{_P}/vision/active_initiatives/foo", {"body": ""}),
    ]
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        for method, path, body in samples:
            r = getattr(client, method)(path, json=body) if body is not None else client.request(method, path)
            assert r.status_code != 403, f"{method.upper()} {path} -> 403 for admin: {r.text}"


# ---------------------------------------------------------------------------
# Enumeration guard: NO project-write route may be require_auth-only.
# ---------------------------------------------------------------------------

# Project-scoped WRITE routes that are intentionally NOT admin-gated, with the
# reason each is safe. A new ungated project-write route NOT on this list trips
# the test below — forcing a deliberate gate-or-allowlist decision.
_ALLOWLIST = {
    # Session lifecycle: owner-scoped inside the handler via _check_sid_ownership
    # (a non-admin manages only their OWN sessions). T-0381 leaves these as-is.
    "/api/projects/{slug}/sessions/{sid}/pause",
    "/api/projects/{slug}/sessions/{sid}/suspend",
    "/api/projects/{slug}/sessions/{sid}/resume",
    "/api/projects/{slug}/sessions/{sid}/bind/task",
    "/api/projects/{slug}/sessions/{sid}/bind/initiative",
    "/api/projects/{slug}/sessions/{sid}/unbind/task",
    "/api/projects/{slug}/sessions/{sid}/unbind/initiative",
    "/api/projects/{slug}/sessions/{sid}/archive",
    "/api/projects/{slug}/sessions/{sid}/unarchive",
    # Peer message bus (routes_intersession, NOT in T-0381's file scope): the
    # cross-session messaging primitive — sessions of any user send/read here.
    # Flagged to operator for a separate session-scoped review.
    "/api/projects/{slug}/peer/send",
    "/api/projects/{slug}/peer/{sid}/read",
    "/api/projects/{slug}/peer/{sid}/wait",
}

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _dep_callables(route: APIRoute) -> set:
    """Every dependency callable in the route's resolved dependant tree."""
    out: set = set()

    def walk(dep) -> None:
        if dep.call is not None:
            out.add(dep.call)
        for sub in dep.dependencies:
            walk(sub)

    walk(route.dependant)
    return out


def test_no_project_write_route_is_require_auth_only(tmp_bot_squad, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    app = build_app()
    ungated = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not (route.methods & _WRITE_METHODS):
            continue
        if not route.path.startswith("/api/projects/{slug}"):
            continue
        if route.path in _ALLOWLIST:
            continue
        deps = _dep_callables(route)
        if require_project_member not in deps and require_admin not in deps:
            for m in sorted(route.methods & _WRITE_METHODS):
                ungated.append(f"{m} {route.path}")
    assert not ungated, (
        "project-write routes gated by require_auth only (gate with "
        "require_project_member or allowlist with a reason):\n" + "\n".join(sorted(ungated))
    )
