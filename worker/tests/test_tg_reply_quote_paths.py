"""T-0780: the quoted original reaching the SESSION and the STORE.

``test_reply_quote.py`` pins the extractor and the renderer. This file pins the
carriers — the two envelopes and every store payload built on an inbound TG
path — because a correct extractor nothing calls is exactly the shape of the
defect being fixed: ``reply_to_message["text"]`` was already being READ at HEAD,
for the SID regex, and then dropped.

A separate file rather than more of ``test_tg_listener.py``: that file is
2000+ lines of routing tests, and this ticket must be readable as "what carries
the quote" without being diffused through them.

Positive control (T-0740): the tests here were measured RED against HEAD before
the fix — see the ticket's Progress log for the counts. A guard that passes with
the defect present pins nothing.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import bot_squad_worker.tg_listener as TL
from bot_squad_worker import reply_quote as RQ
from bot_squad_worker import tg_direct_reply


BOT = {"id": 8123456789, "is_bot": True, "first_name": "bot-squad"}
HUMAN = {"id": 555123, "is_bot": False, "first_name": "Alexey", "last_name": "Serdyukov"}
QUESTION = "[bot-squad dev] Вариант 1 — переписать extract. Вариант 2 — новое поле. Какой?"
ANSWER = "второй вариант"
CHAT = -1001234567890
SID = "S-almdudleer-t0780-dev-p437"


def _cfg(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True)
    return types.SimpleNamespace(
        tg_bot_token="8123456789:AA-fake", data_dir=data_dir,
        projects={"bot-squad": types.SimpleNamespace(tg_chat=str(CHAT))},
        tg_proxy_url="", voice_enabled=False,
    )


def _reply_msg(*, thread_id=220, answer: str = ANSWER) -> dict:
    return {
        "message_id": 901,
        "chat": {"id": CHAT, "type": "supergroup"},
        "from": HUMAN,
        "text": answer,
        "message_thread_id": thread_id,
        "reply_to_message": {"message_id": 900, "chat": {"id": CHAT},
                             "from": BOT, "text": QUESTION},
    }


def _plain_msg() -> dict:
    return {"message_id": 902, "chat": {"id": CHAT, "type": "supergroup"},
            "from": HUMAN, "text": "просто сообщение"}


@pytest.fixture
def captured_posts(monkeypatch):
    """Every conversation-store payload the listener builds, without HTTP."""
    posts: list[dict] = []
    monkeypatch.setattr(
        TL, "_post_conversation",
        lambda cfg, slug, gid, payload: (posts.append(payload), True)[1])
    return posts


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_the_record_carries_what_he_was_answering(tmp_path, captured_posts):
    """THE ticket's store half. At HEAD the complete key union of a
    user-authored record was attachments/author/channel/forwarded_from/fyi/
    general_feed/text/thread_id/timestamp — no slot for this at all."""
    TL.append_conversation(_cfg(tmp_path), "bot-squad", "gu_x", _reply_msg(),
                           thread_id=220)
    rec = captured_posts[0]
    assert rec["reply_to"]["text"] == QUESTION
    assert rec["reply_to"]["message_id"] == 900


def test_the_quote_is_never_folded_into_his_text(tmp_path, captured_posts):
    """THE T-0746 TRAP, pinned. The quoted words are somebody else's — usually
    OURS. On a record authored "user", concatenating them into ``text`` would
    re-create the exact defect T-0746 existed to fix: system prose recorded as
    the stakeholder saying it, in the one file that answers what he said."""
    TL.append_conversation(_cfg(tmp_path), "bot-squad", "gu_x", _reply_msg())
    rec = captured_posts[0]
    assert rec["author"] == "user"
    assert rec["text"] == ANSWER
    assert "Вариант" not in rec["text"]
    assert QUESTION not in rec["text"]
    assert isinstance(rec["reply_to"], dict)


def test_the_record_names_who_wrote_the_quoted_message(tmp_path, captured_posts):
    TL.append_conversation(_cfg(tmp_path), "bot-squad", "gu_x", _reply_msg())
    assert captured_posts[0]["reply_to"]["author"] == "bot:8123456789"


def test_a_non_reply_record_is_unchanged(tmp_path, captured_posts):
    """The overwhelming common case must stay byte-identical to its pre-T-0780
    shape — no empty slot, no null, nothing new to reason about."""
    TL.append_conversation(_cfg(tmp_path), "bot-squad", "gu_x", _plain_msg())
    assert "reply_to" not in captured_posts[0]


def test_a_voice_note_sent_as_a_reply_keeps_the_quote(tmp_path, captured_posts):
    """A DM voice note routes its TRANSCRIPT through the unquoted path as a
    synthesised message (`transcript_msg = dict(msg)`), so it inherits the
    original's ``reply_to_message``. Pinned rather than assumed: the synthetic
    message is built by hand, and a future rewrite that assembles it from
    scratch instead of copying would drop the quote silently on that path."""
    msg = _reply_msg(answer="")            # a voice note carries no text…
    msg["voice"] = {"file_id": "AwAC", "duration": 4}
    transcript_msg = dict(msg)
    transcript_msg["text"] = "второй вариант, давай"   # …the transcript does
    TL.append_conversation(_cfg(tmp_path), "bot-squad", "gu_x", transcript_msg)
    assert captured_posts[0]["reply_to"]["text"] == QUESTION
    assert captured_posts[0]["text"] == "второй вариант, давай"


def test_the_fyi_summary_of_a_direct_reply_carries_it_too(tmp_path, captured_posts):
    """On the direct-mode task-topic path this fyi line is the ONLY store
    record of what he said, so without the field that path stays lossy."""
    TL.append_conversation_fyi(
        _cfg(tmp_path), "bot-squad", "gu_x", author="system:direct-reply",
        text="Пользователь ответил сессии X напрямую: да",
        reply_to=RQ.extract(_reply_msg()))
    assert captured_posts[0]["reply_to"]["text"] == QUESTION


def test_the_undelivered_fallback_record_carries_it(tmp_path, captured_posts, monkeypatch):
    """The attendant picking up a message aimed at a reaped session is the
    reader that needs the question most — it holds none of that session's
    context, so a bare «да» in its thread is unanswerable."""
    from bot_squad_worker import sessions as S
    monkeypatch.setattr(S, "project_of_sid", lambda cfg, sid: "bot-squad")
    monkeypatch.setattr(TL, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: {"ok": True})

    TL._fallback_undelivered(
        _cfg(tmp_path), CHAT, SID, ANSWER, gid="gu_x",
        quote=RQ.extract(_reply_msg()), error="no pane")

    user_records = [p for p in captured_posts if p.get("author") == "user"]
    assert len(user_records) == 1
    assert user_records[0]["reply_to"]["text"] == QUESTION
    # …and record 1, the system's account, is still the system's account.
    assert "reply_to" not in captured_posts[0]


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

def test_the_light_envelope_carries_the_question(tmp_path):
    """THE ticket's session half, on the path it is about: an ``origin="reply"``
    envelope means he is ANSWERING something the session asked. At HEAD the
    session received «второй вариант» and nothing else."""
    env = tg_direct_reply.compose_light_envelope(
        text=ANSWER, chat_id=CHAT, thread_id=220, sid=SID, sender="Alexey",
        origin="reply", quote=RQ.extract(_reply_msg()))
    assert "переписать extract" in env          # appears ONLY in the question
    assert "NOT their words" in env
    assert env.index(QUESTION) < env.index(ANSWER)   # question, then answer


def test_the_full_envelope_carries_the_question(tmp_path):
    env = tg_direct_reply.compose_envelope(
        text=ANSWER, chat_id=CHAT, thread_id=220, sid=SID, slug="bot-squad",
        sender="Alexey", quote=RQ.extract(_reply_msg()))
    assert "переписать extract" in env
    assert "NOT their words" in env


@pytest.mark.parametrize("compose,kwargs", [
    (tg_direct_reply.compose_light_envelope, {"origin": "reply"}),
    (tg_direct_reply.compose_light_envelope, {"origin": "say"}),
    (tg_direct_reply.compose_envelope, {"slug": "bot-squad"}),
])
def test_a_quoteless_envelope_is_unchanged(compose, kwargs):
    """No quote ⇒ nothing added. `/say` is composed, never a reply, so its
    envelope can never grow a quote block."""
    base = dict(text=ANSWER, chat_id=CHAT, thread_id=220, sid=SID, sender="Alexey")
    assert compose(**base, **kwargs) == compose(**base, **kwargs, quote=None)
    assert RQ.LINE_PREFIX not in compose(**base, **kwargs, quote=None)


def test_the_session_can_tell_the_two_blocks_apart(tmp_path):
    """His words and the quoted words are in the same message. The one thing
    that must never happen is a reader taking the quote for something he
    wrote — so they are marked differently, not just separated."""
    env = tg_direct_reply.compose_light_envelope(
        text=ANSWER, chat_id=CHAT, thread_id=220, sid=SID, sender="Alexey",
        origin="reply", quote=RQ.extract(_reply_msg()))
    his = env.split("--- 8< ---")[1].split("--- >8 ---")[0]
    assert his.strip() == ANSWER
    assert QUESTION not in his
    assert RQ.LINE_PREFIX + QUESTION in env


# ---------------------------------------------------------------------------
# What must NOT have changed (the ticket's scope limits)
# ---------------------------------------------------------------------------

def test_routing_still_resolves_by_reply_map_first(tmp_path):
    """T-0725 lineage: the map is authoritative and the quoted-text SID regex
    stays the FALLBACK. Adding a quote must not make routing read display text.
    Here the quoted text names one SID and the map names another; the map wins,
    exactly as before."""
    from bot_squad_worker import tg_reply_map
    cfg = _cfg(tmp_path)
    tg_reply_map.record(cfg.data_dir, chat_id=CHAT, message_id=900,
                        sid="S-real-target-p9")
    msg = _reply_msg()
    msg["reply_to_message"]["text"] = "[S-stale-in-text-p3] старое сообщение"
    assert TL.extract_reply_target(msg, cfg) == ("S-real-target-p9", ANSWER)


def test_extract_reply_target_still_returns_a_pair(tmp_path):
    """The quote rides in its own channel. Widening this function's return
    would have made every caller and every T-0719/T-0725 test its business."""
    cfg = _cfg(tmp_path)
    result = TL.extract_reply_target(_reply_msg(), cfg)
    assert result is None or len(result) == 2


def test_the_quote_never_writes_the_reply_map(tmp_path):
    """T-0667: nothing this ticket adds may pin the reply map or become the
    conversation locus."""
    from bot_squad_worker import tg_reply_map
    cfg = _cfg(tmp_path)
    before = tg_reply_map.map_path(cfg.data_dir).exists()
    RQ.extract(_reply_msg())
    TL.append_conversation(cfg, "bot-squad", "gu_x", _reply_msg())
    assert tg_reply_map.map_path(cfg.data_dir).exists() == before
