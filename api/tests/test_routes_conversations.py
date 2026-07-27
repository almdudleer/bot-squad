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


# ---------------------------------------------------------------------------
# T-0660: a PASSIVE, non-actionable "fyi" append — a session wrote directly to
# the stakeholder (author=session:<sid>), or the stakeholder replied directly
# to a session (author=user, bypassing this thread's own attendant). Must
# record durably for context, but MUST NOT relay (would echo the FYI note
# right back to the user) and MUST NOT wake the attendant (not its own inbox
# item). Non-FYI appends must be completely unaffected.
# ---------------------------------------------------------------------------


def test_fyi_session_append_records_but_does_not_relay(tmp_bot_squad: Path, monkeypatch):
    """T-0660 requirement (c): a session's direct-write already went out via
    its own channel — relaying the FYI record again would echo the 'no reply
    needed' note straight back to the user. Must NOT happen."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV,
        json={"author": "session:S-dev-p9", "text": "[FYI — ответ не требуется] ...",
              "fyi": True},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fyi"] is True
    assert body["relayed"] is False
    assert calls == []  # tg_notify never called — no echo back to the user

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert out["messages"][-1]["fyi"] is True  # still durably recorded


def test_fyi_user_append_records_but_does_not_wake_attendant(tmp_bot_squad: Path, monkeypatch):
    """T-0660 requirement (b): the stakeholder replied directly to ANOTHER
    session (not this project's attendant) — recorded for context, but this
    isn't the attendant's own inbox item, so its auto-wake must be skipped."""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV,
        json={"author": "user", "text": "[FYI — ответ не требуется] ...", "fyi": True},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fyi"] is True
    assert body["ensured"] == {"ok": True, "skipped": "fyi"}
    assert calls == []  # ensure_user_conversation never called — no wake

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert out["messages"][-1]["fyi"] is True  # still durably recorded


def test_fyi_omitted_defaults_to_normal_behavior(tmp_bot_squad: Path, monkeypatch):
    """No `fyi` key at all -> unchanged pre-T-0660 behavior (back-compat)."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "a normal reply"},
        headers=_worker_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fyi"] is False
    assert body["relayed"] is True
    assert [name for name, _ in calls] == ["tg_notify"]


def test_non_fyi_user_append_still_wakes_attendant(tmp_bot_squad: Path, monkeypatch):
    """Regression guard: a NORMAL user-authored append (the common case) must
    still trigger the attendant wake exactly as before T-0660."""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(CONV, json={"author": "user", "text": "hi"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["fyi"] is False
    assert [name for name, _ in calls] == ["ensure_user_conversation"]


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


# ---------------------------------------------------------------------------
# T-0667: outgoing relay follows the conversation LOCUS (worker-recorded
# last-seen chat_id/thread_id per (slug,gid)) ahead of the tg_user_id DM and
# the project's static tg_chat — so a reply lands where the user last wrote,
# not always the old default DM (live stakeholder-reported split-conversation
# gap).
# ---------------------------------------------------------------------------


def _write_locus(tmp_bot_squad: Path, slug: str, gid: str, chat_id: str, thread_id) -> None:
    """Write the worker-owned conversation_locus.json directly — mirrors what
    ``bot_squad_worker.conversation_locus.set_locus`` persists, exercised here
    without spinning up the worker (API reads this file directly, T-0667)."""
    import json
    p = tmp_bot_squad / "data" / "_worker" / "conversation_locus.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.is_file() else {}
    existing[f"{slug}:{gid}"] = {"chat_id": chat_id, "thread_id": thread_id, "at": "2026-07-24T15:00:00Z"}
    p.write_text(json.dumps(existing))


def test_session_append_relay_prefers_locus_over_tg_user_id(tmp_bot_squad: Path, monkeypatch):
    """A locus recorded for (slug,gid) wins over the GlobalUser's DM chat_id —
    the reply must follow where the user actually last wrote, in-topic."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")  # old DM chat_id
    _write_locus(tmp_bot_squad, "test-project", "gu_abc", "-1003761939853", 42)

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is True
    assert calls[0][1]["chat_id"] == "-1003761939853"
    assert calls[0][1]["topic_id"] == 42


def test_session_append_relay_locus_dm_thread_id_none_omits_topic_id(tmp_bot_squad: Path, monkeypatch):
    """A locus with thread_id=None (a DM/general feed) must not send a bogus
    topic_id param."""
    _write_locus(tmp_bot_squad, "test-project", "gu_abc", "444", None)

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "444"
    assert "topic_id" not in calls[0][1]


def test_session_append_relay_falls_back_to_tg_user_id_when_no_locus(tmp_bot_squad: Path, monkeypatch):
    """No locus recorded (fresh/never-topic-bound user) -> unchanged pre-T-0667
    behavior: the GlobalUser's DM chat_id."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "555222111"
    assert "topic_id" not in calls[0][1]


def test_session_append_relay_locus_scoped_to_gid(tmp_bot_squad: Path, monkeypatch):
    """A locus recorded for a DIFFERENT global_user_id must not leak into this
    user's relay target."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")
    _write_locus(tmp_bot_squad, "test-project", "gu_someone_else", "999", 1)

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "555222111"


def test_session_append_relay_locus_scoped_to_slug(tmp_bot_squad: Path, monkeypatch):
    """A locus recorded for a DIFFERENT project must not leak into this one."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")
    _write_locus(tmp_bot_squad, "some-other-project", "gu_abc", "999", 1)

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "555222111"


def test_session_append_relay_locus_corrupt_file_falls_back(tmp_bot_squad: Path, monkeypatch):
    """An unreadable conversation_locus.json must never break the relay —
    falls back the same as no locus at all."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "555222111")
    p = tmp_bot_squad / "data" / "_worker" / "conversation_locus.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is True
    assert calls[0][1]["chat_id"] == "555222111"


# ---------------------------------------------------------------------------
# T-0676 items 3/6: optional thread_id — a bound forum topic's history +
# outbound relay are isolated from the project's mixed (slug, gid) thread.
# Absent thread_id must behave byte-identically to every test above.
# ---------------------------------------------------------------------------


def _write_threaded_locus(tmp_bot_squad: Path, slug: str, gid: str, thread_id, chat_id: str) -> None:
    """Write a locus entry under the THREAD-SCOPED key (mirrors
    ``conversation_locus._key`` with a thread_id) — distinct from
    ``_write_locus``'s bare ``slug:gid`` key."""
    import json
    p = tmp_bot_squad / "data" / "_worker" / "conversation_locus.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.is_file() else {}
    existing[f"{slug}:{gid}:{thread_id}"] = {
        "chat_id": chat_id, "thread_id": thread_id, "at": "2026-07-25T15:00:00Z",
    }
    p.write_text(json.dumps(existing))


def test_append_with_thread_id_isolates_from_bare_thread(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    client.post(CONV, json={"author": "user", "text": "dm message"}, headers=_worker_auth())
    client.post(CONV, json={"author": "user", "text": "topic message", "thread_id": 7},
                headers=_worker_auth())

    dm = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    topic = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc", thread_id=7)
    assert [m["text"] for m in dm["messages"]] == ["dm message"]
    assert [m["text"] for m in topic["messages"]] == ["topic message"]


def test_worker_append_forwards_general_feed_flag(tmp_bot_squad: Path, monkeypatch):
    """T-0693 Finding B: the append endpoint forwards `general_feed` from the
    request body to the store, so a message routed via an explicit
    tg_bindings General-feed binding is distinguishable, on the stored
    record, from a genuine DM (both have no thread_id)."""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    client.post(CONV, json={"author": "user", "text": "general feed msg", "general_feed": True},
                headers=_worker_auth())
    client.post(CONV, json={"author": "user", "text": "dm msg"}, headers=_worker_auth())

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert [(m["text"], m["general_feed"]) for m in out["messages"]] == [
        ("general feed msg", True), ("dm msg", False),
    ]


def test_worker_list_with_thread_id_reads_isolated_thread(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data"
    CS.append(d, "test-project", "gu_abc", author="user", text="dm msg")
    CS.append(d, "test-project", "gu_abc", author="user", text="topic msg", thread_id=7)

    r = client.get(CONV, headers=_worker_auth())
    assert [m["text"] for m in r.json()["messages"]] == ["dm msg"]

    r = client.get(CONV, params={"thread_id": "7"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert [m["text"] for m in r.json()["messages"]] == ["topic msg"]


def test_session_authed_list_with_thread_id_reads_isolated_thread(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data"
    CS.append(d, "test-project", "gu_abc", author="user", text="dm msg")
    CS.append(d, "test-project", "gu_abc", author="user", text="topic msg", thread_id=7)
    _login(client)

    r = client.get(AUTH_CONV, params={"thread_id": "7"})
    assert r.status_code == 200, r.text
    assert [m["text"] for m in r.json()["messages"]] == ["topic msg"]


def test_session_append_with_thread_id_relays_to_that_topics_locus(tmp_bot_squad: Path, monkeypatch):
    """The relay for a threaded reply must resolve THAT topic's own locus —
    not the bare (slug,gid) one, and not a DIFFERENT topic's locus — closing
    the item-3 misrouted-reply / item-6 bleed pair."""
    _write_locus(tmp_bot_squad, "test-project", "gu_abc", "111", None)  # bare/DM locus
    _write_threaded_locus(tmp_bot_squad, "test-project", "gu_abc", 7, "-100777")
    _write_threaded_locus(tmp_bot_squad, "test-project", "gu_abc", 9, "-100999")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer", "thread_id": 7},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is True
    assert calls[0][1]["chat_id"] == "-100777"
    assert calls[0][1]["topic_id"] == 7


def test_session_append_without_thread_id_still_uses_bare_locus(tmp_bot_squad: Path, monkeypatch):
    """Back-compat: omitting thread_id resolves the bare (slug,gid) locus
    exactly as before this change, ignoring any threaded entries."""
    _write_locus(tmp_bot_squad, "test-project", "gu_abc", "111", None)
    _write_threaded_locus(tmp_bot_squad, "test-project", "gu_abc", 7, "-100777")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "111"


def test_user_authored_append_with_thread_id_passes_thread_id_to_ensure(
    tmp_bot_squad: Path, monkeypatch,
):
    """The attendant-wake for a topic-bound message carries the thread_id
    through, so the SAME (slug,gid) attendant knows which topic's isolated
    thread the new message belongs to (T-0676 items 3/6 design)."""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch, result={"ok": True, "sid": "S-x-p1", "spawned": False})

    r = client.post(
        CONV, json={"author": "user", "text": "hi", "thread_id": 7}, headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][0] == "ensure_user_conversation"
    assert calls[0][1]["thread_id"] == 7


# ---------------------------------------------------------------------------
# T-0740: a threaded reply whose topic has NO locus yet must still be
# delivered into that topic — not the user's private DM.
#
# The live bug: a session created a fresh task topic and posted the first
# message into it. By construction nobody had written there, so the
# thread-scoped locus key didn't exist; the relay dropped the thread it had
# been handed and fell through to the GlobalUser's tg_user_id, i.e. the
# private DM ("some messages still land in the private DM with the bot
# instead of the group topic"). The topic's chat is knowable without a locus
# — it's in the worker's tg_bindings store — so the thread is now honoured.
# ---------------------------------------------------------------------------


def _write_binding(tmp_bot_squad: Path, chat_id: str, thread_id, slug: str) -> None:
    """Write the worker-owned tg_bindings.json directly — mirrors what
    ``bot_squad_worker.tg_bindings.set_binding`` persists (key
    ``"<chat_id>:<thread_id>"``), exercised without spinning up the worker
    (the API reads this file directly, T-0740)."""
    import json
    p = tmp_bot_squad / "data" / "_worker" / "tg_bindings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.is_file() else {}
    existing[f"{chat_id}:{'' if thread_id is None else thread_id}"] = {
        "slug": slug, "ticket_id": None, "session_id": None, "pinned_message_id": None,
    }
    p.write_text(json.dumps(existing))


def test_session_append_threaded_reply_with_no_locus_goes_to_topic_not_dm(
    tmp_bot_squad: Path, monkeypatch,
):
    """THE reported bug, minimally: a reply naming thread 278, a binding for
    it, no locus for it (the user has never written there), and a linked DM
    chat_id sitting in rung 3. Before the fix this relayed to the DM."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "404580642")  # the private DM
    _write_binding(tmp_bot_squad, "-1003761939853", 278, "test-project")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer", "thread_id": 278},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is True
    assert calls[0][1]["chat_id"] == "-1003761939853"
    assert calls[0][1]["topic_id"] == 278


def test_session_append_threaded_reply_prefers_locus_over_binding(
    tmp_bot_squad: Path, monkeypatch,
):
    """Rung 1 still beats the new rung 2: when the topic HAS a locus, it is
    used, so T-0667/T-0676 behaviour is untouched where it already worked."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "404580642")
    _write_threaded_locus(tmp_bot_squad, "test-project", "gu_abc", 7, "-100777")
    _write_binding(tmp_bot_squad, "-100999", 7, "test-project")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer", "thread_id": 7},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "-100777"


def test_session_append_threaded_reply_ignores_another_projects_binding(
    tmp_bot_squad: Path, monkeypatch,
):
    """A thread bound to a DIFFERENT project is not this project's
    destination — sending there would leak one project's reply into
    another's topic. Falls through to the DM as before."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "404580642")
    _write_binding(tmp_bot_squad, "-1003761939853", 278, "some-other-project")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer", "thread_id": 278},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "404580642"
    assert "topic_id" not in calls[0][1]


def test_session_append_threaded_reply_unbound_thread_falls_back_unchanged(
    tmp_bot_squad: Path, monkeypatch,
):
    """No locus AND no binding for the thread — nothing to resolve a chat
    from, so the pre-T-0740 fallback chain applies exactly as before."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "404580642")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer", "thread_id": 278},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "404580642"


def test_session_append_thread_less_reply_never_uses_a_binding(
    tmp_bot_squad: Path, monkeypatch,
):
    """A reply with NO thread must not acquire one from the binding store —
    a thread-less relay behaves byte-identically to before this change."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "404580642")
    _write_binding(tmp_bot_squad, "-1003761939853", 278, "test-project")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "404580642"
    assert "topic_id" not in calls[0][1]


def test_session_append_threaded_reply_ignores_general_feed_binding(
    tmp_bot_squad: Path, monkeypatch,
):
    """The General-feed binding key is ``"<chat>:"`` (empty thread segment).
    It must never match a real numeric thread id."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "404580642")
    _write_binding(tmp_bot_squad, "-1003761939853", None, "test-project")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer", "thread_id": 278},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][1]["chat_id"] == "404580642"


def test_session_append_threaded_reply_corrupt_bindings_file_falls_back(
    tmp_bot_squad: Path, monkeypatch,
):
    """An unreadable tg_bindings.json must never break the relay — same
    best-effort contract as the locus read."""
    _seed_tg_linked_user(tmp_bot_squad, "gu_abc", "404580642")
    p = tmp_bot_squad / "data" / "_worker" / "tg_bindings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")

    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "answer", "thread_id": 278},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["relayed"] is True
    assert calls[0][1]["chat_id"] == "404580642"


def test_worker_list_search_filters(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    d = tmp_bot_squad / "data"
    CS.append(d, "test-project", "gu_abc", author="user", text="deploy now")
    CS.append(d, "test-project", "gu_abc", author="user", text="something else")
    CS.append(d, "test-project", "gu_abc", author="user", text="DEPLOY tomorrow")
    r = client.get(CONV, params={"q": "deploy"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert [m["text"] for m in r.json()["messages"]] == ["deploy now", "DEPLOY tomorrow"]
