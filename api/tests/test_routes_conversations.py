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


# ---- T-0542: worker-token READ of a thread (attendant reads its own thread) --
# A user-conversation ATTENDANT runs in worker context (it holds the
# WORKER_API_TOKEN, not a user JWT), so it cannot use the session-auth GET to
# review its own thread — before T-0542 it had to reach into the store JSONL
# directly. This worker-token GET is the supported read path.


def test_worker_list_returns_thread(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data"
    CS.append(d, "test-project", "gu_abc", author="user", text="m1")
    CS.append(d, "test-project", "gu_abc", author="session:S-x", text="m2")
    r = client.get(CONV, headers=_worker_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["text"] for m in body["messages"]] == ["m1", "m2"]


def test_worker_list_missing_bearer_is_401(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.get(CONV)
    assert r.status_code == 401


def test_worker_list_wrong_bearer_is_401(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.get(CONV, headers=_worker_auth("nope"))
    assert r.status_code == 401


def test_worker_list_unconfigured_token_fails_closed(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch, worker_token=None)
    r = client.get(CONV, headers=_worker_auth("anything"))
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# T-0569: writeback relay — a session-authored append is best-effort relayed
# to the user's Telegram chat via the worker's tg_notify action, so the user
# actually sees the reply. worker_client is mocked (no real socket / TG).
# ---------------------------------------------------------------------------


def _mock_call_action(monkeypatch, *, result=None, raise_error: bool = False):
    """Patch WorkerClient.call_action so append_message's relay hits a fake
    instead of a real unix socket. Returns a list capturing (name, params)."""
    from app.worker_client import WorkerClient, WorkerError

    calls: list[tuple[str, dict]] = []

    async def fake_call_action(self, name, params, timeout=None):
        calls.append((name, params))
        if raise_error:
            raise WorkerError("boom")
        return result if result is not None else {"ok": True, "sent": True, "channel": "tg"}

    monkeypatch.setattr(WorkerClient, "call_action", fake_call_action)
    return calls


def _seed_tg_linked_user(tmp_bot_squad: Path, global_user_id: str, tg_user_id: str) -> None:
    """Write a GlobalUser with a specific id (matching the (slug, gid) path
    segment used by CONV) and tg_user_id — bypassing the normal mint-a-random-
    id creation flow, since the relay lookup keys on the exact gid in the URL."""
    from datetime import datetime, timezone
    from app.mothership_users_store import GlobalUser, MothershipUsersStore

    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    user = GlobalUser(
        id=global_user_id,
        username=f"tg:{tg_user_id}",
        password_hash="",
        created_at=datetime.now(timezone.utc).isoformat(),
        tg_user_id=tg_user_id,
    )
    store._write_users([user])


def test_session_append_relays_to_telegram(tmp_bot_squad: Path, monkeypatch):
    """A session-authored append (author='session:<sid>') triggers the relay:
    tg_notify is called with the resolved chat_id + the reply text, urgent +
    undebounced (interactive reply), and the response carries relayed=True."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV,
        json={"author": f"session:S-x-p1", "text": "here's your answer"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["relayed"] is True

    assert len(calls) == 1
    name, params = calls[0]
    assert name == "tg_notify"
    assert params["chat_id"] == "555222111"
    assert params["message"] == "here's your answer"
    assert params["urgent"] is True
    assert params["debounce"] is False


def test_user_authored_append_never_relays(tmp_bot_squad: Path, monkeypatch):
    """A regular user-authored append (author='user') must NOT trigger a
    relay — it would just echo the user's own message back to themselves. (It
    DOES trigger the T-0631 attendant-wake — see test_routes_conversations_seam.py
    — so calls is no longer expected to be empty here.)"""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(CONV, json={"author": "user", "text": "hi"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is False
    assert [name for name, _ in calls] == ["ensure_user_conversation"]


def test_session_append_empty_text_never_relays(tmp_bot_squad: Path, monkeypatch):
    """An empty-text session append (e.g. a bookkeeping/no-op record) has
    nothing to relay."""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(CONV, json={"author": "session:S-x-p1", "text": ""}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is False
    assert calls == []


def test_session_append_relay_failure_does_not_fail_append(tmp_bot_squad: Path, monkeypatch):
    """A relay failure (worker unreachable) must NEVER fail the append — the
    message is already durably recorded; relayed just reports False."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "777")

    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch, raise_error=True)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is False
    # The message IS durably recorded despite the relay failure.
    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert out["messages"][-1]["text"] == "answer"


def test_session_append_no_resolvable_chat_is_relayed_false(tmp_bot_squad: Path, monkeypatch):
    """No linked GlobalUser tg_user_id and the project's tg_chat is empty (the
    conftest fixture sets tg_chat='0', so use a project without one) — nothing
    resolvable, relayed False, no call attempted."""
    (tmp_bot_squad / "config" / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        'repo_path = "/tmp/test-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = ""\n'
        'created_at = 2026-05-10\n'
    )
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is False
    assert calls == []


def test_session_append_falls_back_to_project_tg_chat(tmp_bot_squad: Path, monkeypatch):
    """No linked GlobalUser (gu_abc doesn't exist in the users store) — falls
    back to the project's configured tg_chat (conftest sets it to '0')."""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is True
    assert calls[0][1]["chat_id"] == "0"


def test_worker_list_search_filters(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data"
    CS.append(d, "test-project", "gu_abc", author="user", text="deploy now")
    CS.append(d, "test-project", "gu_abc", author="user", text="something else")
    CS.append(d, "test-project", "gu_abc", author="user", text="DEPLOY tomorrow")
    r = client.get(CONV, params={"q": "deploy"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert [m["text"] for m in r.json()["messages"]] == ["deploy now", "DEPLOY tomorrow"]
