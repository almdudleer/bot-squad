"""T-0780: the conversation store's slot for a quoted original.

Before this ticket the store had none. A scan of all 1298 live records across
both projects returned a key union of ``attachments, author, channel, direction,
delivered, forwarded_from, fyi, general_feed, text, thread_id, timestamp`` — the
quote was not "sometimes unpopulated", it was dropped at ingestion into a schema
with nowhere to put it.

The writer half lives in the worker (``bot_squad_worker.reply_quote``, and
``worker/tests/test_tg_reply_quote_paths.py``). This file pins the store: the
field persists, it stays SEPARATE from the sender's own text, garbage is refused
at the one write path rather than absorbed, and an absent field is never read as
"this was not a reply".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app import conversation_store as CS

WORKER_TOKEN = "worker-secret-token-xyz"
CONV = "/api/m/worker/conversations/test-project/gu_abc/messages"
QUESTION = "Вариант 1 — переписать extract. Вариант 2 — добавить поле. Какой берём?"
QUOTE = {"text": QUESTION, "message_id": 900, "author": "bot:8123456789",
         "author_name": "bot-squad"}


def _client(tmp_bot_squad: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_bot_squad / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_bot_squad / "data"))
    monkeypatch.setenv("WORKER_SOCK", str(tmp_bot_squad / "data" / "_sock" / "worker.sock"))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("WEB_DIST", str(tmp_bot_squad / "nonexistent-web-dist"))
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mothership.test")
    monkeypatch.setenv("INSTALL_BUNDLE_DIR",
                       str(Path(__file__).resolve().parents[2] / "scripts" / "install"))
    monkeypatch.setenv("MOTHERSHIP", "1")
    monkeypatch.setenv("WORKER_API_TOKEN", WORKER_TOKEN)
    return TestClient(build_app())


def _auth() -> dict:
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}


# ---------------------------------------------------------------------------
# The store function
# ---------------------------------------------------------------------------

def test_reply_to_persists_on_the_record(tmp_path: Path):
    rec = CS.append(tmp_path, "p", "gu", author="user", text="второй вариант",
                    reply_to=dict(QUOTE))
    assert rec["reply_to"]["text"] == QUESTION
    assert rec["reply_to"]["message_id"] == 900
    assert rec["reply_to"]["author"] == "bot:8123456789"


def test_the_quote_stays_out_of_the_senders_own_text(tmp_path: Path):
    """THE T-0746 rule, at the store's own boundary: the quoted words belong to
    somebody else — usually to us — so a record authored "user" must not carry
    them as something he said."""
    rec = CS.append(tmp_path, "p", "gu", author="user", text="второй вариант",
                    reply_to=dict(QUOTE))
    assert rec["text"] == "второй вариант"
    assert QUESTION not in rec["text"]


def test_a_record_with_no_quote_is_byte_identical_to_its_old_shape(tmp_path: Path):
    """No empty slot and no null: every non-reply record — which is most of
    them — keeps exactly the shape it had before this ticket."""
    rec = CS.append(tmp_path, "p", "gu", author="user", text="привет")
    assert "reply_to" not in rec
    assert sorted(rec) == ["attachments", "author", "channel", "fyi", "text", "timestamp"]


def test_a_bare_string_quote_is_refused_not_flattened(tmp_path: Path):
    """A caller passing a string is about to fold somebody else's words into
    the record as one blob. This is the one write path into the store, so it
    fails loudly here — the same reasoning as the T-0755 author vocabulary."""
    with pytest.raises(ValueError, match="must be a dict"):
        CS.append(tmp_path, "p", "gu", author="user", text="да",
                  reply_to="Вариант 1 — переписать extract")


def test_unknown_keys_inside_the_quote_are_dropped(tmp_path: Path):
    """A nested blob a caller can put arbitrary keys into is a schema that has
    stopped being one."""
    rec = CS.append(tmp_path, "p", "gu", author="user", text="да",
                    reply_to={"text": "q", "author": "user:1",
                              "surprise": "payload", "attachments": ["x"]})
    assert sorted(rec["reply_to"]) == ["author", "text"]


def test_an_empty_quote_is_stored_as_no_quote(tmp_path: Path):
    """A quote with no text would read on every consumer as 'he replied to
    something blank' — which is a claim, and a false one."""
    assert "reply_to" not in CS.append(tmp_path, "p", "gu", author="user",
                                       text="да", reply_to={"message_id": 5})
    assert "reply_to" not in CS.append(tmp_path, "p", "gu", author="user",
                                       text="да", reply_to={})


def test_an_over_long_quote_is_capped_and_says_so(tmp_path: Path):
    """The store must not trust a client. This will essentially never fire for
    a TG record (Telegram's own limit is 4096) — it bounds the non-TG callers
    T-0631 made this seam serve."""
    rec = CS.append(tmp_path, "p", "gu", author="user", text="да",
                    reply_to={"text": "A" * (CS.REPLY_TO_TEXT_CAP + 100)})
    assert len(rec["reply_to"]["text"]) == CS.REPLY_TO_TEXT_CAP
    assert rec["reply_to"]["truncated"] is True


def test_a_quote_at_the_cap_is_not_marked_truncated(tmp_path: Path):
    rec = CS.append(tmp_path, "p", "gu", author="user", text="да",
                    reply_to={"text": "A" * CS.REPLY_TO_TEXT_CAP})
    assert "truncated" not in rec["reply_to"]


def test_an_absent_reply_to_is_never_defaulted_on_read(tmp_path: Path):
    """THE explicit-UNKNOWN rule, and the reason this field is deliberately
    NOT normalized the way ``forwarded_from`` and ``direction`` are: every
    record written before T-0780 lost its quote at ingestion, so defaulting the
    field would assert "this was not a reply" over 1298 historical records —
    some of which were. An absent field must not read as a real negative."""
    p = CS.conv_path(tmp_path, "p", "gu")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"timestamp": "2026-07-01T00:00:00Z", "author": "user",
                             "text": "да"}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    rec = CS.list_messages(tmp_path, "p", "gu")["messages"][0]
    assert "reply_to" not in rec
    assert rec["forwarded_from"] == ""     # the fields that ARE defaulted still are


def test_the_quote_survives_a_write_read_round_trip(tmp_path: Path):
    CS.append(tmp_path, "p", "gu", author="user", text="второй вариант",
              reply_to=dict(QUOTE))
    rec = CS.list_messages(tmp_path, "p", "gu")["messages"][0]
    assert rec["reply_to"] == QUOTE


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_the_append_endpoint_carries_the_quote_to_disk(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(CONV, json={"author": "user", "text": "второй вариант",
                                "reply_to": dict(QUOTE)}, headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["reply_to"]["text"] == QUESTION

    stored = json.loads(
        CS.conv_path(tmp_bot_squad / "data", "test-project", "gu_abc")
        .read_text(encoding="utf-8").splitlines()[0])
    assert stored["reply_to"]["text"] == QUESTION
    assert stored["text"] == "второй вариант"


def test_the_endpoint_rejects_a_non_dict_quote(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(CONV, json={"author": "user", "text": "да",
                                "reply_to": "Вариант 1"}, headers=_auth())
    assert r.status_code == 400
    assert "reply_to" in r.json()["detail"]


def test_an_append_without_a_quote_is_unaffected(tmp_bot_squad: Path, monkeypatch):
    client = _client(tmp_bot_squad, monkeypatch)
    r = client.post(CONV, json={"author": "user", "text": "привет"}, headers=_auth())
    assert r.status_code == 200, r.text
    assert "reply_to" not in r.json()
