"""T-0606: a session reply whose thread is UNKNOWN must not be delivered
anywhere — least of all into whatever topic the collapsed locus key happens to
still name.

The live defect (2026-08-12, the stakeholder's only channel)
------------------------------------------------------------
``data/_worker/conversation_locus.json`` held BOTH of these for the same user::

    "watchrobot:gu_dc8262…"    -> {"thread_id": 23, "at": "2026-07-25T16:08:30Z"}
    "watchrobot:gu_dc8262…:11" -> {"thread_id": 11, "at": "2026-08-12T09:37:14Z"}

The first key is the pre-T-0676 COLLAPSED key — one locus per (slug, gid),
whatever topic the user last wrote in. T-0676 moved every new record to the
thread-scoped key, so that entry froze on 2026-07-25 pointing at topic 23 — a
topic that has since been closed. Nothing rewrites it and nothing expires it.

Any path that loses ``thread_id`` therefore resolved the collapsed key and
delivered into topic 23, reporting ``relayed: true``. That is how it survived
three weeks: **success and miss were indistinguishable.** For three weeks the
stakeholder got replies to questions he had not asked, in a closed topic, and
never saw the answers to the ones he had.

What this file pins
-------------------
1. A thread-less relay for a user who converses IN TOPICS is refused, not
   delivered — ``relayed`` is false and ``relay_error`` names why (DoD 1).
2. The collapsed key is no longer a silent fallback for such a user (DoD 2).
   It stays fully in play for a user with no thread-scoped locus at all, which
   is the entire pre-T-0676 world, so nothing that works today stops working.
3. A collapsed record whose OWN ``thread_id`` is null — a genuine DM or an
   explicit General-feed binding (T-0693) — is unambiguous and still delivers.
   That is the live shape of ``bot-squad:gu_dc8262…`` and
   ``guestent:gu_882fd0…``; over-fixing this would take those channels down.
4. ``thread_id`` in the BODY still delivers to the named topic (DoD 4) — the
   workaround p151 is talking to the stakeholder through right now.
5. ``?thread_id=`` in the QUERY is read instead of being silently dropped
   (DoD 5). ``append_message`` had no query parameters at all, so the
   ``?thread_id=11`` that p151 sent was accepted and forgotten — the request
   that started this ticket.

The fixtures are hand-written on purpose: a collapsed key NAMING a topic is a
pre-T-0676 fossil that today's ``conversation_locus.set_locus`` can no longer
produce (it would key that record ``slug:gid:23``). It can only be read off
disk, which is exactly what makes it a trap — see
``test_relay_destination_differential.py`` for the writer-driven differential
that covers the shapes the worker CAN still write.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app

WORKER_TOKEN = "worker-secret-token-xyz"
CONV = "/api/m/worker/conversations/test-project/gu_abc/messages"

TOPIC_CHAT = "-1003761939853"  # the forum supergroup topics 11/23 live in
LIVE_TOPIC = 11                # where he actually writes
DEAD_TOPIC = 23                # closed since 2026-08-07; the collapsed key's fossil
DM_CHAT = "555222111"          # the GlobalUser's tg_user_id — the ladder's rung 3


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("WORKER_API_TOKEN", WORKER_TOKEN)
    return TestClient(build_app())


def _worker_auth() -> dict:
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}


def _mock_call_action(monkeypatch) -> list[tuple[str, dict]]:
    """Capture what the relay asks the worker to send, instead of a real
    socket. An empty list is the assertion that matters most here: a REFUSED
    relay must never have reached the transport at all."""
    from app.worker_client import WorkerClient

    calls: list[tuple[str, dict]] = []

    async def fake_call_action(self, name, params, timeout=None):
        calls.append((name, params))
        return {"ok": True, "sent": True, "channel": "tg"}

    monkeypatch.setattr(WorkerClient, "call_action", fake_call_action)
    return calls


def _write_locus_entries(tmp_bot_squad: Path, entries: dict[str, dict]) -> None:
    """Write raw ``conversation_locus.json`` keys — including shapes today's
    worker can no longer write (see the module docstring)."""
    p = tmp_bot_squad / "data" / "_worker" / "conversation_locus.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.is_file() else {}
    existing.update(entries)
    p.write_text(json.dumps(existing))


def _seed_tg_linked_user(tmp_bot_squad: Path) -> None:
    """A linked DM makes every assertion below non-degenerate: a resolver that
    merely fell through answers ``DM_CHAT``, which is a different wrong answer
    from topic 23 and is asserted separately."""
    from app.mothership_users_store import GlobalUser, MothershipUsersStore

    store = MothershipUsersStore(tmp_bot_squad / "data" / "_mothership")
    store._write_users([
        GlobalUser(
            id="gu_abc",
            username=f"tg:{DM_CHAT}",
            password_hash="",
            created_at=datetime.now(timezone.utc).isoformat(),
            tg_user_id=DM_CHAT,
        )
    ])


def _seed_live_shape(tmp_bot_squad: Path) -> None:
    """The exact live shape on 2026-08-12: a frozen collapsed key naming the
    dead topic, and a fresh thread-scoped key for the live one."""
    _seed_tg_linked_user(tmp_bot_squad)
    _write_locus_entries(tmp_bot_squad, {
        "test-project:gu_abc": {
            "chat_id": TOPIC_CHAT, "thread_id": DEAD_TOPIC, "at": "2026-07-25T16:08:30Z",
        },
        f"test-project:gu_abc:{LIVE_TOPIC}": {
            "chat_id": TOPIC_CHAT, "thread_id": LIVE_TOPIC, "at": "2026-08-12T09:37:14Z",
        },
    })


def _append_session_reply(client: TestClient, **body) -> dict:
    payload = {"author": "session:S-x-p1", "text": "answer", **body}
    r = client.post(CONV, json=payload, headers=_worker_auth())
    assert r.status_code == 200, r.text
    return r.json()


# --- DoD 1 + 2: the miss is honest, and the collapsed key stops deciding -----


def test_threadless_relay_is_refused_instead_of_delivered_to_the_stale_topic(
    tmp_bot_squad: Path, monkeypatch,
):
    """THE defect, at its own size. A session reply that lost its thread must
    not be delivered — not to the collapsed key's dead topic, and not to the DM
    either. ``relayed`` false plus a named reason is the whole fix: a caller
    that can see the miss fixes it in one turn, which is what three weeks of
    ``relayed: true`` prevented."""
    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    body = _append_session_reply(client)

    assert calls == [], (
        f"a thread-less reply reached the transport: {calls!r} — if topic_id is "
        f"{DEAD_TOPIC} this is the live defect verbatim, the collapsed locus key "
        "deciding a destination the request never named"
    )
    assert body["relayed"] is False, (
        "an undelivered reply reported relayed:true — success and miss must not "
        "look the same, that indistinguishability is what let this run 3 weeks"
    )
    assert body.get("relay_error", {}).get("reason") == "thread_undetermined", (
        f"the refusal did not say why: {body.get('relay_error')!r}"
    )


def test_the_record_is_still_appended_when_the_relay_is_refused(
    tmp_bot_squad: Path, monkeypatch,
):
    """A refused relay is not a failed append. The reply is durably recorded
    exactly as before — the relay was always best-effort and must stay so, or
    this fix trades a routing bug for a data-loss one."""
    from app import conversation_store as CS

    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch)

    _append_session_reply(client, text="recorded even though undeliverable")

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert [m["text"] for m in out["messages"]] == ["recorded even though undeliverable"]


def test_collapsed_key_still_serves_a_user_with_no_thread_scoped_locus(
    tmp_bot_squad: Path, monkeypatch,
):
    """DoD 2's boundary. For a user who has never written in a bound topic
    there is no ambiguity to protect against and no newer signal to prefer —
    the collapsed key is the only record, and it keeps deciding, exactly as it
    did before T-0676. The entire pre-T-0676 world lives here."""
    _seed_tg_linked_user(tmp_bot_squad)
    _write_locus_entries(tmp_bot_squad, {
        "test-project:gu_abc": {
            "chat_id": TOPIC_CHAT, "thread_id": 42, "at": "2026-07-25T16:08:30Z",
        },
    })
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    body = _append_session_reply(client)

    assert body["relayed"] is True
    assert calls[0][1]["chat_id"] == TOPIC_CHAT
    assert calls[0][1]["topic_id"] == 42


def test_a_null_thread_collapsed_locus_is_unambiguous_and_still_delivers(
    tmp_bot_squad: Path, monkeypatch,
):
    """The live ``bot-squad:gu_dc8262…`` / ``guestent:gu_882fd0…`` shape: a
    collapsed record whose own ``thread_id`` is null — a DM or an explicit
    General-feed binding (T-0693). "No topic" is a real answer, not a lost one,
    so it delivers even though this user also has thread-scoped entries.
    Refusing here would silence two live channels to fix a third."""
    _seed_tg_linked_user(tmp_bot_squad)
    _write_locus_entries(tmp_bot_squad, {
        "test-project:gu_abc": {
            "chat_id": TOPIC_CHAT, "thread_id": None,
            "at": "2026-08-12T09:53:51Z", "general_feed": True,
        },
        f"test-project:gu_abc:{LIVE_TOPIC}": {
            "chat_id": TOPIC_CHAT, "thread_id": LIVE_TOPIC, "at": "2026-08-12T09:37:14Z",
        },
    })
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    body = _append_session_reply(client)

    assert body["relayed"] is True
    assert calls[0][1]["chat_id"] == TOPIC_CHAT
    assert "topic_id" not in calls[0][1], (
        f"a null-thread locus sent a topic_id: {calls[0][1].get('topic_id')!r}"
    )


def test_threadless_relay_without_any_locus_still_reaches_the_dm(
    tmp_bot_squad: Path, monkeypatch,
):
    """Rung 3, untouched: a user with no locus at all has no topic to be
    ambiguous about, so the DM stays the right default (T-0667's own reading of
    its ladder). This is the fresh-user path and must not become a refusal."""
    _seed_tg_linked_user(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    body = _append_session_reply(client)

    assert body["relayed"] is True
    assert calls[0][1]["chat_id"] == DM_CHAT
    assert "topic_id" not in calls[0][1]


# --- DoD 4: the workaround the stakeholder is being answered through today ---


def test_thread_id_in_the_body_still_delivers_to_that_topic(
    tmp_bot_squad: Path, monkeypatch,
):
    """The live workaround, pinned against this very change: p151 puts
    ``thread_id`` in the BODY and reaches topic 11. It is the only channel to
    the stakeholder, and the fix above must not cost it."""
    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    body = _append_session_reply(client, thread_id=LIVE_TOPIC)

    assert body["relayed"] is True
    assert calls[0][1]["chat_id"] == TOPIC_CHAT
    assert calls[0][1]["topic_id"] == LIVE_TOPIC
    assert "relay_error" not in body


# --- DoD 5: the query parameter is read, never accepted-and-forgotten -------


def test_thread_id_in_the_query_is_not_silently_ignored(
    tmp_bot_squad: Path, monkeypatch,
):
    """``append_message`` declared no query parameters, so ``?thread_id=11``
    was accepted with a 200 and dropped — the request that opened this ticket.
    It now resolves the same topic the body form does."""
    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, params={"thread_id": str(LIVE_TOPIC)},
        json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["relayed"] is True
    assert calls[0][1]["topic_id"] == LIVE_TOPIC, (
        f"?thread_id={LIVE_TOPIC} did not reach the resolver "
        f"(topic_id={calls[0][1].get('topic_id')!r}) — silently ignoring it is "
        "what this ticket forbids"
    )


def test_query_thread_id_is_recorded_on_the_message_as_an_int(
    tmp_bot_squad: Path, monkeypatch,
):
    """The query form must produce the SAME record as the body form — a
    thread_id stored as ``"11"`` would key the same locus string but read back
    as a different type to every JSON consumer."""
    from app import conversation_store as CS

    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch)

    client.post(
        CONV, params={"thread_id": str(LIVE_TOPIC)},
        json={"author": "session:S-x-p1", "text": "in the topic"}, headers=_worker_auth(),
    )

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc",
                           thread_id=LIVE_TOPIC)
    assert [m["text"] for m in out["messages"]] == ["in the topic"]
    assert out["messages"][0]["thread_id"] == LIVE_TOPIC


def test_conflicting_body_and_query_thread_id_is_a_400(
    tmp_bot_squad: Path, monkeypatch,
):
    """Two different answers to "which topic" is not a preference question.
    Picking one silently is the same class of defect this ticket exists for."""
    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, params={"thread_id": str(DEAD_TOPIC)},
        json={"author": "session:S-x-p1", "text": "answer", "thread_id": LIVE_TOPIC},
        headers=_worker_auth(),
    )
    assert r.status_code == 400, r.text
    assert "thread_id" in r.json()["detail"]
    assert calls == [], "a rejected append still relayed"


def test_matching_body_and_query_thread_id_is_accepted(
    tmp_bot_squad: Path, monkeypatch,
):
    """Agreement is not a conflict — a caller that belts-and-braces both forms
    must not be punished for it."""
    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    calls = _mock_call_action(monkeypatch)

    r = client.post(
        CONV, params={"thread_id": str(LIVE_TOPIC)},
        json={"author": "session:S-x-p1", "text": "answer", "thread_id": LIVE_TOPIC},
        headers=_worker_auth(),
    )
    assert r.status_code == 200, r.text
    assert calls[0][1]["topic_id"] == LIVE_TOPIC


def test_non_numeric_query_thread_id_is_a_400(tmp_bot_squad: Path, monkeypatch):
    """A Telegram ``message_thread_id`` is an integer. A query value that
    cannot be one names no topic that exists, so it is rejected at the edge
    rather than carried into the store and the locus key."""
    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch)

    r = client.post(
        CONV, params={"thread_id": "general"},
        json={"author": "session:S-x-p1", "text": "answer"}, headers=_worker_auth(),
    )
    assert r.status_code == 400, r.text


# --- the user-authored path is unaffected ------------------------------------


def test_a_user_authored_append_is_not_refused(tmp_bot_squad: Path, monkeypatch):
    """Only the session WRITEBACK relay resolves a destination. An inbound
    user message is recorded and wakes its attendant regardless of what the
    locus store says — a refusal here would drop the stakeholder's own words."""
    from app import conversation_store as CS

    _seed_live_shape(tmp_bot_squad)
    client = _client(tmp_bot_squad, monkeypatch)
    _mock_call_action(monkeypatch)

    r = client.post(CONV, json={"author": "user", "text": "his message"},
                    headers=_worker_auth())
    assert r.status_code == 200, r.text
    assert "relay_error" not in r.json()

    out = CS.list_messages(tmp_bot_squad / "data", "test-project", "gu_abc")
    assert [m["text"] for m in out["messages"]] == ["his message"]
