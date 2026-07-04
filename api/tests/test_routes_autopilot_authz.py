"""T-0374: global orchestration controls are admin-gated — pinned per endpoint.

OPERATOR DECISION (2026-07-04, T-0374): non-admins must NOT control global
orchestration — gate autonomous enable/disable + autopilot start/stop like the
other admin surfaces (/api/users, /api/system-settings). State of the world
when this landed:

- ``routes_autonomous.py`` (POST /enable, /disable) was DELETED in T-0403
  (autonomous orchestrator cut — autopilot covers the project case). Those
  endpoints are pinned 404 here; re-adding them is a deliberate act and the
  enumeration guard in ``test_project_write_authz.py`` forces the new route to
  carry ``require_project_member``/``require_admin`` or an explicit allowlist
  entry.
- ``POST /autopilot/start`` + ``/stop`` are gated by ``require_project_member``
  (T-0381) — the project-write SSOT, admin-only until per-project roles land
  (T-0216). Behaviorally identical to ``require_admin`` today; kept as the SSOT
  dependency on purpose (see ``app/project_authz.py`` docstring: do not scatter
  the gate).

This module pins the DECIDED policy endpoint-by-endpoint: non-admin JWT
(is_admin=false) → 403 BEFORE any worker side effect; admin passes the authz
gate. Manually walked 2026-07-04 (non-admin 403/403 + 404/404, admin 502/502
at the worker proxy with no socket — gate fires first) before automation.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app

_P = "/api/projects/test-project"

_MUTATING = [
    ("post", f"{_P}/autopilot/start", {"kind": "project", "prompt": "x"}),
    ("post", f"{_P}/autopilot/stop", {"key": "x"}),
]
# Removed in T-0403 — pinned gone so a revival is a conscious, gated decision.
_REMOVED = [
    ("post", f"{_P}/autonomous/enable", {}),
    ("post", f"{_P}/autonomous/disable", {}),
]


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


def test_nonadmin_403_on_autopilot_start_and_stop(tmp_bot_squad, monkeypatch):
    _make_nonadmin(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        for method, path, body in _MUTATING:
            r = getattr(client, method)(path, json=body)
            assert r.status_code == 403, (
                f"{method.upper()} {path} -> {r.status_code} (want 403 for non-admin): {r.text}"
            )


def test_admin_passes_autopilot_authz_gate(tmp_bot_squad, monkeypatch):
    # conftest testuser is admin. No worker socket in this fixture, so passing
    # the gate surfaces as 502 at the worker proxy — the assertion is only
    # that authz (401/403) does NOT fire for an admin.
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        for method, path, body in _MUTATING:
            r = getattr(client, method)(path, json=body)
            assert r.status_code not in (401, 403), (
                f"{method.upper()} {path} -> {r.status_code} for admin: {r.text}"
            )


def test_nonadmin_can_still_read_autopilot_status(tmp_bot_squad, monkeypatch):
    # Reads stay broad (T-0381 model): the gate is on WRITES only. Non-admin
    # GET must not be rejected by authz (502 at the worker proxy is fine).
    _make_nonadmin(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        r = client.get(f"{_P}/autopilot")
        assert r.status_code not in (401, 403), f"GET autopilot -> {r.status_code}: {r.text}"


def test_autonomous_endpoints_stay_removed(tmp_bot_squad, monkeypatch):
    # T-0403 cut routes_autonomous.py — nobody, regardless of privilege, can
    # flip the deleted orchestrator via the API. Gone = 404, or 405 when
    # web/dist exists: the GET-only SPA catch-all (main.py "/{full_path:path}")
    # then matches the path, so an unrouted POST is method-not-allowed.
    _GONE = (404, 405)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)  # admin
        for method, path, body in _REMOVED:
            r = getattr(client, method)(path, json=body)
            assert r.status_code in _GONE, f"[admin] {method.upper()} {path} -> {r.status_code}"
    _make_nonadmin(tmp_bot_squad)
    with _client(tmp_bot_squad, monkeypatch) as client:
        _login(client)
        for method, path, body in _REMOVED:
            r = getattr(client, method)(path, json=body)
            assert r.status_code in _GONE, f"[non-admin] {method.upper()} {path} -> {r.status_code}"
