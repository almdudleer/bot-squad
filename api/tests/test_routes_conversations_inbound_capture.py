"""T-0769: a conversation read says whether it CONTAINS the user's inbound.

The defect, in one sentence: a forum topic pinned to a session records our
outbound and routes the user's inbound straight to that session, so the read
comes back as a ONE-SIDED conversation — and nothing in the response said so.
On 2026-07-28 two independent sessions read that off watchrobot topics 220/517,
concluded the inbound wiring was structurally broken, escalated message loss to
the stakeholder as urgent, and posted an apology into his topic for a fault that
had not occurred.

The routing is deliberate (T-0660/T-0667) and is NOT what these tests pin. They
pin that the RESPONSE now states which case it is, that the statement is right
for each case, and — the part that carries the whole ticket — that a healthy
session-routed topic and a genuinely dead one are no longer the same bytes.

The risk of this change is OVER-labelling: a marker that excuses every empty
page excuses a real fault too. So the plain-topic, empty-topic, unbound and
UNKNOWN cases are pinned just as hard as the session-routed one.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import conversation_store as CS

from test_routes_conversations import (
    AUTH_CONV,
    WORKER_TOKEN,
    _client,
    _login,
    _worker_auth,
)

SLUG = "test-project"
GID = "gu_abc"
SID = "S-almdudleer-gu_abc-user-conversation-p70"
CHAT = "-1003761939853"
WORKER_CONV = f"/api/m/worker/conversations/{SLUG}/{GID}/messages"


def _bindings(tmp_bot_squad: Path, mapping: dict) -> None:
    """Write the worker-owned binding store the API reads directly."""
    path = tmp_bot_squad / "data" / "_worker" / "tg_bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping))


def _session_bound(tmp_bot_squad: Path, thread_id: int = 517) -> None:
    """The state the incident happened in: a per-task topic pinned to a session."""
    _bindings(tmp_bot_squad, {
        f"{CHAT}:{thread_id}": {
            "slug": SLUG, "ticket_id": "T-0314",
            "session_id": SID, "pinned_message_id": None,
        },
    })


def _plain_topic(tmp_bot_squad: Path, thread_id: int = 278) -> None:
    """The control that was sitting on the same disk all night, capturing
    normally — a bound topic with no session pinned."""
    _bindings(tmp_bot_squad, {
        f"{CHAT}:{thread_id}": {
            "slug": SLUG, "ticket_id": "T-0321",
            "session_id": None, "pinned_message_id": None,
        },
    })


def _get(client: TestClient, **params) -> dict:
    r = client.get(AUTH_CONV, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _capture(client: TestClient, **params) -> dict:
    return _get(client, **params)["inbound_capture"]


# --- the incident itself ----------------------------------------------------


def test_session_routed_topic_says_so_and_names_the_session(
    tmp_bot_squad: Path, monkeypatch,
):
    """The read that misled two sessions now carries its own reason."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)
    CS.append(tmp_bot_squad / "data", SLUG, GID, author=f"session:{SID}",
              text="our answer into the topic", thread_id=517)

    body = _get(client, thread_id="517")
    # The one-sided page is still one-sided — the records are correct.
    assert body["total"] == 1
    assert body["messages"][0]["author"] == f"session:{SID}"

    cap = body["inbound_capture"]
    assert cap["scope"] == "session_routed"
    assert cap["records_inbound_here"] is False
    assert cap["bound_session_id"] == SID
    assert cap["bound_ticket_id"] == "T-0314"
    assert cap["thread_id"] == "517"


def test_session_routed_explain_points_at_a_reachable_project_thread(
    tmp_bot_squad: Path, monkeypatch,
):
    """The pointer has to RESOLVE, not just read well.

    A marker naming a place with nothing in it would be a new well-formed lie —
    the same failure one level along. So follow the pointer over HTTP and
    require the user's words to actually be there, in the fyi shape
    ``_handle_topic_bound`` writes them (author ``system:direct-reply``).
    """
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)
    # What the listener really appends for a direct reply (T-0660 mechanic #3).
    CS.append(tmp_bot_squad / "data", SLUG, GID, author="system:direct-reply",
              text=f"Пользователь ответил сессии {SID} напрямую: прием-прием",
              fyi=True)

    cap = _capture(client, thread_id="517")
    pointer = cap["inbound_recorded_in"]
    assert pointer, "a session-routed topic must say where the inbound went"
    # The pointer is a GET of THIS surface with no thread_id — follow it.
    assert pointer.startswith(f"GET {AUTH_CONV}")
    assert "no thread_id" in pointer

    followed = _get(client, q="прием-прием")
    assert followed["total"] == 1, "the pointer must lead to his actual words"
    assert followed["messages"][0]["author"] == "system:direct-reply"
    assert followed["messages"][0]["fyi"] is True


def test_the_explain_is_readable_without_the_worker_source(
    tmp_bot_squad: Path, monkeypatch,
):
    """The ticket's test is a reader who has never opened
    ``_handle_topic_bound``. The enum alone would just be a new thing to look
    up, so the prose has to carry the three facts that reader is missing: the
    inbound goes to a session, our-side-only is the DESIGNED state, and this is
    not message loss."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)

    explain = _capture(client, thread_id="517")["explain"]
    assert SID in explain
    assert "DESIGNED" in explain
    assert "not evidence" in explain.lower()
    assert "system:direct-reply" in explain


# --- the pair the whole ticket is about -------------------------------------


def test_a_dead_topic_and_a_session_routed_one_are_no_longer_the_same_read(
    tmp_bot_squad: Path, monkeypatch,
):
    """THE GATE. Both pages show no inbound from the user. One is healthy and
    one would be a real fault, and at HEAD they differed only by a record count
    a reader has no baseline for. They must now differ in a field that answers
    the question directly."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _bindings(tmp_bot_squad, {
        f"{CHAT}:517": {"slug": SLUG, "ticket_id": "T-0314",
                        "session_id": SID, "pinned_message_id": None},
        f"{CHAT}:901": {"slug": SLUG, "ticket_id": "T-0999",
                        "session_id": None, "pinned_message_id": None},
    })

    healthy = _capture(client, thread_id="517")
    dead = _capture(client, thread_id="901")

    assert healthy["records_inbound_here"] is False
    assert dead["records_inbound_here"] is True
    assert healthy["scope"] != dead["scope"]
    # And the dead one is not excused: it says the absence is real.
    assert "real absence" in dead["explain"]


def test_plain_bound_topic_is_not_labelled_session_routed(
    tmp_bot_squad: Path, monkeypatch,
):
    """The over-labelling guard. 9 of the 11 live bindings look like this;
    labelling them session-routed would excuse a genuinely broken topic."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _plain_topic(tmp_bot_squad)
    CS.append(tmp_bot_squad / "data", SLUG, GID, author="user",
              text="his message, captured normally", thread_id=278)

    cap = _capture(client, thread_id="278")
    assert cap["scope"] == "topic"
    assert cap["records_inbound_here"] is True
    assert cap["bound_session_id"] == ""
    assert cap["bound_ticket_id"] == "T-0321"
    assert cap["inbound_recorded_in"] == ""


def test_project_thread_read_describes_itself(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)

    cap = _capture(client)
    assert cap["scope"] == "project"
    assert cap["records_inbound_here"] is True
    assert cap["thread_id"] == ""


# --- UNKNOWN degrades to the vaguer TRUE statement (T-0759/T-0761/T-0772) ----


def test_missing_binding_store_is_unknown_not_a_claim(
    tmp_bot_squad: Path, monkeypatch,
):
    """No store file at all. The tempting default — "no binding found, so this
    topic must record inbound" — is a specific falsehood produced exactly when
    the instrument is broken."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)

    cap = _capture(client, thread_id="517")
    assert cap["scope"] == "unknown"
    assert cap["records_inbound_here"] is None
    assert "UNKNOWN" in cap["explain"]


def test_corrupt_binding_store_is_unknown(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    path = tmp_bot_squad / "data" / "_worker" / "tg_bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"broken": ')

    cap = _capture(client, thread_id="517")
    assert cap["scope"] == "unknown"
    assert cap["records_inbound_here"] is None


def test_non_dict_binding_store_is_unknown(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    path = tmp_bot_squad / "data" / "_worker" / "tg_bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["not", "a", "map"]')

    cap = _capture(client, thread_id="517")
    assert cap["scope"] == "unknown"
    assert cap["records_inbound_here"] is None


def test_unknown_is_caused_by_unreadability_not_by_the_fixture(
    tmp_bot_squad: Path, monkeypatch,
):
    """Positive control on the three tests above (T-0740). A marker that says
    UNKNOWN for every read would pass them all and pin nothing, so drive the
    SAME request against a readable store and require a definite answer."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    path = tmp_bot_squad / "data" / "_worker" / "tg_bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"broken": ')
    assert _capture(client, thread_id="517")["scope"] == "unknown"

    _session_bound(tmp_bot_squad)  # same path, now valid JSON
    assert _capture(client, thread_id="517")["scope"] == "session_routed"


# --- the other two ways an empty page gets misread ---------------------------


def test_unbound_thread_says_nothing_routes_there(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)

    cap = _capture(client, thread_id="4242")
    assert cap["scope"] == "unbound"
    assert cap["records_inbound_here"] is False
    assert "4242" in cap["explain"]


def test_a_same_numbered_topic_elsewhere_does_not_shadow_ours(
    tmp_bot_squad: Path, monkeypatch,
):
    """A thread id is unique only INSIDE its chat, so two chats can carry the
    same number. Matching on the number alone and checking the slug afterwards
    would let another install's topic 517 answer for ours — turning a correct
    session-routed page into a "wrong project" explanation, which is a fresh
    way to mislead the same reader."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _bindings(tmp_bot_squad, {
        # The foreign one is written FIRST, so a first-match scan hits it.
        f"-100999:517": {"slug": "some-other-project", "ticket_id": None,
                         "session_id": None, "pinned_message_id": None},
        f"{CHAT}:517": {"slug": SLUG, "ticket_id": "T-0314",
                        "session_id": SID, "pinned_message_id": None},
    })

    cap = _capture(client, thread_id="517")
    assert cap["scope"] == "session_routed"
    assert cap["bound_session_id"] == SID


def test_thread_of_another_project_explains_the_empty_page(
    tmp_bot_squad: Path, monkeypatch,
):
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _bindings(tmp_bot_squad, {
        f"{CHAT}:517": {"slug": "some-other-project", "ticket_id": "T-0314",
                        "session_id": SID, "pinned_message_id": None},
    })

    cap = _capture(client, thread_id="517")
    assert cap["scope"] == "other_project"
    assert cap["records_inbound_here"] is False
    # The caller holds read access to the slug they ASKED for, not to the one
    # that owns the thread — so the other project is never named.
    assert "some-other-project" not in json.dumps(cap)
    assert cap["bound_session_id"] == ""


# --- every surface, every path ----------------------------------------------


def test_search_path_carries_the_same_marker(tmp_bot_squad: Path, monkeypatch):
    """Grepping a session-routed topic for the user's own words returns zero
    hits — the read MOST likely to be mistaken for loss, so ``q=`` must carry
    the explanation too."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)

    body = _get(client, thread_id="517", q="прием-прием")
    assert body["total"] == 0
    assert body["inbound_capture"]["scope"] == "session_routed"
    assert body["inbound_capture"]["records_inbound_here"] is False


def test_worker_token_surface_carries_the_marker_with_its_own_path(
    tmp_bot_squad: Path, monkeypatch,
):
    """The user-conversation ATTENDANT reads here (it holds the worker token,
    not a JWT), and T-0676's boot prompt points it at a topic-scoped read — so
    it is exactly a reader that gets handed a one-sided topic. The pointer must
    name ITS surface, not the session-auth one it cannot call."""
    client = _client(tmp_bot_squad, monkeypatch)
    _session_bound(tmp_bot_squad)

    r = client.get(WORKER_CONV, params={"thread_id": "517"},
                   headers=_worker_auth(WORKER_TOKEN))
    assert r.status_code == 200, r.text
    cap = r.json()["inbound_capture"]
    assert cap["scope"] == "session_routed"
    assert cap["inbound_recorded_in"].startswith(f"GET {WORKER_CONV}")


def test_marker_is_additive_and_leaves_the_page_untouched(
    tmp_bot_squad: Path, monkeypatch,
):
    """The pagination envelope is a contract other readers already depend on.
    Everything except the new key must be byte-identical to the store's own
    answer."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)
    CS.append(tmp_bot_squad / "data", SLUG, GID, author=f"session:{SID}",
              text="ours", thread_id=517)

    body = _get(client, thread_id="517")
    expected = CS.list_messages(tmp_bot_squad / "data", SLUG, GID, thread_id="517")
    assert {k: v for k, v in body.items() if k != "inbound_capture"} == expected


def test_every_read_carries_a_marker(tmp_bot_squad: Path, monkeypatch):
    """No 200 may come back without one — an absent marker is precisely the
    state that misled two sessions, and it must not survive as a silent
    fall-through on any parameter combination."""
    client = _client(tmp_bot_squad, monkeypatch)
    _login(client)
    _session_bound(tmp_bot_squad)

    for params in (
        {},
        {"thread_id": "517"},
        {"thread_id": "278"},
        {"q": "anything"},
        {"thread_id": "517", "q": "anything"},
        {"thread_id": "", "q": ""},
        {"limit": 1, "offset": 5},
    ):
        cap = _get(client, **params).get("inbound_capture")
        assert cap is not None, params
        assert cap["scope"] in {
            "project", "topic", "session_routed", "unbound",
            "other_project", "unknown",
        }, (params, cap)
        assert cap["explain"].strip(), params
