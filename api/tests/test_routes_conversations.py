"""T-0489: TG conversation history endpoints.

- POST /api/m/conversations/{slug}/{gid}/messages — worker append (token-gated
  by the shared-secret WORKER_API_TOKEN, the same path T-0488 established;
  fails closed). The worker records each inbound user message here.
- GET  /api/m/conversations/{slug}/{gid}/messages — session-auth list/search
  (paginated). The durable lookup surface for an attending session.

Mounted only on the MOTHERSHIP build (the user-communication module is
centralized on the mothership, voice-04).
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app
from app import conversation_store as CS

WORKER_TOKEN = "worker-secret-token-xyz"


def _client(tmp_bot_squad: Path, monkeypatch, *, worker_token: str | None = WORKER_TOKEN) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    repo_bundle = Path(__file__).resolve().parents[2] / "scripts" / "install"
    monkeypatch.setenv("INSTALL_BUNDLE_DIR", str(repo_bundle))
    monkeypatch.setenv("MOTHERSHIP", "1")
    if worker_token is None:
        monkeypatch.delenv("WORKER_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WORKER_API_TOKEN", worker_token)
    return TestClient(build_app())


def _worker_auth(token: str = WORKER_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "test"})
    assert r.status_code == 200, r.text


CONV = "/api/m/worker/conversations/test-project/gu_abc/messages"  # T-0529 worker POST
AUTH_CONV = "/api/m/conversations/test-project/gu_abc/messages"  # session-auth GET (stays public)


def test_worker_append_persists_message(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(
        CONV,
        json={"author": "user", "text": "hello brain", "timestamp": "2026-06-27T00:00:00Z"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["text"] == "hello brain"
    assert rec["author"] == "user"

    # Persisted in the store under (slug, global_user_id).
    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert out["total"] == 1
    assert out["messages"][0]["text"] == "hello brain"


def test_worker_append_missing_bearer_is_401(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(CONV, json={"author": "user", "text": "x"})
    assert r.status_code == 401


def test_worker_append_wrong_bearer_is_401(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(CONV, json={"author": "user", "text": "x"}, headers=_worker_auth("nope"))
    assert r.status_code == 401


def test_worker_append_unconfigured_token_fails_closed(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch, worker_token=None)
    r = client.post(CONV, json={"author": "user", "text": "x"}, headers=_worker_auth("anything"))
    assert r.status_code == 401


def test_worker_append_missing_text_is_400(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(CONV, json={"author": "user"}, headers=_worker_auth())
    assert r.status_code == 400


def test_list_requires_auth(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.get(AUTH_CONV)
    assert r.status_code == 401


def test_list_returns_paginated_thread(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    for i in range(5):
        CS.append(tmp_bot_squad / "data", "test-project", "gu_abc",
                  author="user", text=f"m{i}")
    _login(client)
    r = client.get(AUTH_CONV, params={"limit": 2, "offset": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 5
    assert [m["text"] for m in body["messages"]] == ["m1", "m2"]


def _make_nonadmin(tmp_bot_squad: Path) -> None:
    """Rewrite auth.toml so ``testuser`` is a project-LIMITED (non-admin) user.

    Today the only privilege tier modelled at project scope is the global
    admin/member role (no per-project membership store yet — see
    ``app.project_authz``), so a non-admin IS the "project-limited user" of
    voice-04: they have access to no project's private conversation content."""
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )


def test_list_nonadmin_denied_cross_project_content(tmp_bot_squad: Path, monkeypatch):
    """T-0493 / voice-04 privacy: a project-limited (non-admin) user must NOT
    be able to read another project's conversation thread. require_auth-only
    leaked ANY project's content to ANY authed user; the read is now
    project-access-gated -> 403 (no content served)."""
    _make_nonadmin(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    # Seed content the limited user must NOT see.
    CS.append(tmp_bot_squad / "data", "test-project", "gu_abc", author="user", text="secret thread")
    _login(client)
    r = client.get(AUTH_CONV)
    assert r.status_code == 403, r.text
    # The private content must not leak in the denied response.
    assert "secret thread" not in r.text


def test_list_admin_allowed(tmp_bot_squad: Path, monkeypatch):
    """The admin (conftest default) passes the project-access gate and reads
    the thread — gating must not break the legitimate read path."""
    client = _client(tmp_bot_squad, monkeypatch)
    CS.append(tmp_bot_squad / "data", "test-project", "gu_abc", author="user", text="hello")
    _login(client)
    r = client.get(AUTH_CONV)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


def test_list_search_filters(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data"
    CS.append(d, "test-project", "gu_abc", author="user", text="deploy now")
    CS.append(d, "test-project", "gu_abc", author="user", text="something else")
    CS.append(d, "test-project", "gu_abc", author="user", text="DEPLOY tomorrow")
    _login(client)
    r = client.get(AUTH_CONV, params={"q": "deploy"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert [m["text"] for m in body["messages"]] == ["deploy now", "DEPLOY tomorrow"]


# ---------------------------------------------------------------------------
# T-0492: per-(user, server) current-project routing endpoints (worker-token).
# The worker reads/sets the user's sticky project through the API (single-writer
# = API, pins_store). Slug is validated against the project registry.
# ---------------------------------------------------------------------------

ROUTING = "/api/m/worker/routing/gu_abc/current-project"  # T-0529 worker-token


def test_worker_set_current_project_persists(tmp_bot_squad: Path, monkeypatch):
    from app import pins_store
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(ROUTING, json={"slug": "test-project"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "test-project"
    assert pins_store.get_current_project(tmp_bot_squad / "data", "gu_abc") == "test-project"


def test_worker_set_current_project_unknown_slug_is_400(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(ROUTING, json={"slug": "no-such-project"}, headers=_worker_auth())
    assert r.status_code == 400


def test_worker_set_current_project_missing_slug_is_400(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(ROUTING, json={}, headers=_worker_auth())
    assert r.status_code == 400


def test_worker_set_current_project_requires_token(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(ROUTING, json={"slug": "test-project"})
    assert r.status_code == 401


def test_worker_get_current_project_unset_is_null(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.get(ROUTING, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["slug"] is None


def test_worker_get_current_project_after_set(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    client.post(ROUTING, json={"slug": "test-project"}, headers=_worker_auth())
    r = client.get(ROUTING, headers=_worker_auth())
    assert r.status_code == 200
    assert r.json()["slug"] == "test-project"


def test_worker_get_current_project_requires_token(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.get(ROUTING)
    assert r.status_code == 401


def test_endpoints_absent_when_not_mothership(tmp_bot_squad: Path, monkeypatch):
    """Detach build (MOTHERSHIP unset): the conversation routes never mount."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("WORKER_API_TOKEN", WORKER_TOKEN)
    monkeypatch.delenv("MOTHERSHIP", raising=False)
    client = TestClient(build_app())
    r = client.post(CONV, json={"author": "user", "text": "x"}, headers=_worker_auth())
    assert r.status_code == 404
