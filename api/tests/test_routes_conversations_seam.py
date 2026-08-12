"""T-0631: channel-generic user-mail intake seam.

The worker-token append endpoint (``POST /api/m/worker/conversations/{slug}/
{gid}/messages``) is the channel-agnostic intake seam itself: any caller that
lands a user-authored message through it (TG's tg_listener today; a direct
MCP/API caller; email/MAX once those transports exist) gets the same
attendant-wake (``ensure_user_conversation``) applied — not just TG. This
mirrors ``test_routes_conversations.py``'s T-0569 relay tests but for the
``_ensure_attendant`` half of ``append_message``.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import conversation_store as CS
from app.worker_client import WorkerClient

from test_routes_conversations import CONV, _client, _mock_call_action, _worker_auth


def test_user_authored_append_triggers_ensure_user_conversation(
    tmp_bot_squad: Path, monkeypatch,
):
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(
        monkeypatch, result={"ok": True, "sid": "S-x-p1", "spawned": True},
    )

    r = client.post(
        CONV,
        json={"author": "user", "text": "hello", "timestamp": "2026-07-18T00:00:00Z"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ensured"] == {"ok": True, "sid": "S-x-p1", "spawned": True}

    assert len(calls) == 1
    name, params = calls[0]
    assert name == "ensure_user_conversation"
    assert params == {
        "slug": "test-project",
        "global_user_id": "gu_abc",
        "message_ref": "2026-07-18T00:00:00Z",
    }


def test_attached_global_user_wake_uses_strict_per_user_worker(
    tmp_bot_squad: Path, monkeypatch,
):
    auth = tmp_bot_squad / "config" / "auth.toml"
    auth.write_text(
        auth.read_text().replace(
            'linux_user = "almdudleer"',
            'linux_user = "flomaster"\nattached_to_global_user = "gu_abc"',
        )
    )
    user_sock = tmp_bot_squad / "data" / "_sock" / "user-flomaster.sock"
    user_sock.touch()
    seen = []

    async def fake_call_action(self, name, params, timeout=None):
        seen.append((self.sock_path, name, params))
        return {"ok": True, "sid": "S-flomaster-x-p1", "spawned": True}

    monkeypatch.setattr(WorkerClient, "call_action", fake_call_action)
    client = _client(tmp_bot_squad, monkeypatch)
    response = client.post(
        CONV,
        json={"author": "user", "text": "hello"},
        headers=_worker_auth(),
    )
    assert response.status_code == 200
    assert seen[0][0] == user_sock


def test_transport_managed_append_skips_generic_api_wake(
    tmp_bot_squad: Path, monkeypatch,
):
    calls = _mock_call_action(monkeypatch)
    client = _client(tmp_bot_squad, monkeypatch)
    response = client.post(
        CONV,
        json={
            "author": "user",
            "text": "from telegram",
            "ensure_attendant": False,
        },
        headers=_worker_auth(),
    )
    assert response.status_code == 200
    assert response.json()["ensured"] == {
        "ok": True,
        "skipped": "transport_managed",
    }
    assert calls == []


def test_ensure_failure_does_not_fail_append(tmp_bot_squad: Path, monkeypatch):
    """A worker-unreachable / rejected ensure call must never fail the
    append — the message is already durably recorded."""
    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch, raise_error=True)

    r = client.post(CONV, json={"author": "user", "text": "hi"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["ensured"] == {"ok": False}

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert out["messages"][-1]["text"] == "hi"


def test_ensure_backoff_surfaces_parked(tmp_bot_squad: Path, monkeypatch):
    """A saturation/backoff refusal (mirrors tg_listener's own ``"backoff" in
    str(e)`` detection) is distinguishable from any other failure via
    ``parked: True`` — so a caller can tell the user their message is queued,
    not dropped."""
    from app.worker_client import WorkerClient, WorkerError

    async def fake_call_action(self, name, params, timeout=None):
        raise WorkerError(f"worker rejected {name}: spawn backoff active")

    monkeypatch.setattr(WorkerClient, "call_action", fake_call_action)

    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(CONV, json={"author": "user", "text": "hi"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["ensured"] == {"ok": False, "parked": True}


def test_session_authored_append_does_not_trigger_ensure(tmp_bot_squad: Path, monkeypatch):
    """A session writeback already HAS an attending session — waking one
    would be circular, so only user-authored appends carry ``ensured``."""
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "session:S-x-p1", "text": "reply"}, headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert "ensured" not in r.json()
    assert "ensure_user_conversation" not in [name for name, _ in calls]


def test_channel_defaults_to_tg(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch)

    r = client.post(CONV, json={"author": "user", "text": "hi"}, headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "tg"

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert out["messages"][0]["channel"] == "tg"


def test_channel_explicit_value_persisted(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch)

    r = client.post(
        CONV, json={"author": "user", "text": "hi", "channel": "mcp"}, headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "mcp"

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert out["messages"][0]["channel"] == "mcp"
