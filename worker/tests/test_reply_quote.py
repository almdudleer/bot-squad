"""T-0780: the quoted original of a Telegram reply — extraction and rendering.

These pin ``reply_quote`` itself. The paths that CARRY it (the two envelopes,
the store payload) are pinned in ``test_tg_reply_quote_paths.py``.

Every test here is RED against HEAD: the module does not exist there.
"""
from __future__ import annotations

import pytest

from bot_squad_worker import reply_quote as RQ


BOT = {"id": 8123456789, "is_bot": True, "first_name": "bot-squad", "username": "bs_bot"}
HUMAN = {"id": 555123, "is_bot": False, "first_name": "Alexey", "last_name": "Serdyukov"}
QUESTION = "Вариант 1 — переписать extract. Вариант 2 — добавить поле. Какой берём?"


def _reply(answer: str = "второй вариант", **quoted) -> dict:
    q = {"message_id": 900, "from": BOT, "text": QUESTION}
    q.update(quoted)
    return {"message_id": 901, "text": answer, "from": HUMAN, "reply_to_message": q}


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def test_the_defect_this_module_exists_for():
    """THE ticket, in one assertion: a reply's quoted original is recoverable.

    The answer «второй вариант» is meaningless on its own — the question it
    answers was, before T-0780, read once for the SID regex and discarded."""
    q = RQ.extract(_reply())
    assert q["text"] == QUESTION
    assert "Вариант 2" in q["text"]


def test_not_a_reply_is_none():
    assert RQ.extract({"text": "hello"}) is None


def test_non_dict_inputs_are_none_not_a_crash():
    """This runs on the inbound path of every TG message; a shape surprise must
    cost the quote, never the routing."""
    assert RQ.extract(None) is None
    assert RQ.extract("not a message") is None
    assert RQ.extract({"text": "hi", "reply_to_message": "garbage"}) is None


def test_quoted_message_with_no_text_is_none():
    """A service message — the ``forum_topic_created`` some clients attach as
    the ``reply_to_message`` of a topic's own messages — must NOT become an
    empty quote on every message in that topic."""
    msg = _reply()
    msg["reply_to_message"] = {"message_id": 3, "from": BOT,
                               "forum_topic_created": {"name": "T-0780"}}
    assert RQ.extract(msg) is None


def test_media_caption_counts_as_the_quoted_text():
    msg = _reply()
    msg["reply_to_message"] = {"message_id": 900, "from": BOT,
                               "photo": [{"file_id": "x"}], "caption": "вот график"}
    assert RQ.extract(msg)["text"] == "вот график"


def test_message_id_rides_along_as_the_locator():
    assert RQ.extract(_reply())["message_id"] == 900


@pytest.mark.parametrize("sender,expected_author,expected_name", [
    (BOT, "bot:8123456789", "bot-squad"),
    (HUMAN, "user:555123", "Alexey Serdyukov"),
])
def test_author_of_the_quoted_message_is_named(sender, expected_author, expected_name):
    """WHO wrote the quoted text is the T-0746 half of this ticket: it is
    somebody else's words, so a reader must never have to infer whose."""
    q = RQ.extract(_reply(**{"from": sender}))
    assert q["author"] == expected_author
    assert q["author_name"] == expected_name


def test_channel_post_is_attributed_to_the_chat():
    q = RQ.extract(_reply(sender_chat={"id": -1001234, "title": "Новости"}))
    assert q["author"] == "chat:-1001234"
    assert q["author_name"] == "Новости"


def test_anonymous_quote_states_no_author_rather_than_guessing_one():
    """An absent author is recorded as absent. Defaulting it to 'the bot' would
    be T-0746 in miniature — attributing words to someone on no evidence."""
    msg = _reply()
    msg["reply_to_message"] = {"message_id": 900, "text": QUESTION}
    q = RQ.extract(msg)
    assert "author" not in q and "author_name" not in q
    assert q["text"] == QUESTION


def test_extract_does_not_cap(monkeypatch):
    """The RENDER cap is the render's business. The durable record keeps the
    quote whole (bounded by TG's own 4096 limit and the store's defensive cap),
    so extraction must not be where text is lost."""
    long_q = "я" * 3000
    assert len(RQ.extract(_reply(text=long_q))["text"]) == 3000


def test_manual_quote_fragment_is_kept_when_telegram_supplies_one():
    """Bot API 7.0+: he highlighted PART of the message before replying. That
    span is the most precise possible answer to 'what was he answering'."""
    msg = _reply()
    msg["quote"] = {"text": "Вариант 2 — добавить поле", "position": 30}
    q = RQ.extract(msg)
    assert q["fragment"] == "Вариант 2 — добавить поле"
    assert q["text"] == QUESTION          # the whole message is still carried


def test_a_highlighted_fragment_alone_still_yields_a_quote():
    """An external reply can carry the highlighted span without the source
    message's own text. That is still more than nothing."""
    msg = {"text": "да", "reply_to_message": {"message_id": 7, "from": BOT},
           "quote": {"text": "деплой в 18:00"}}
    q = RQ.extract(msg)
    assert q["fragment"] == "деплой в 18:00"


# ---------------------------------------------------------------------------
# render_block
# ---------------------------------------------------------------------------

def test_no_quote_renders_nothing():
    """An empty list is what keeps every quote-less envelope byte-identical to
    its pre-T-0780 form."""
    assert RQ.render_block(None) == []
    assert RQ.render_block({}) == []
    assert RQ.render_block({"text": ""}) == []


def test_rendered_block_says_the_words_are_not_the_senders():
    block = "\n".join(RQ.render_block(RQ.extract(_reply())))
    assert "NOT their words" in block
    assert "bot-squad" in block and "bot:8123456789" in block
    assert "message 900" in block


def test_every_quoted_line_carries_the_boundary_prefix():
    """The label alone is one line a reader can skim past; the prefix travels
    down the whole block, which is what makes the attribution survive."""
    q = {"text": "первая строка\nвторая строка\nтретья"}
    lines = RQ.render_block(q)
    quoted = [ln for ln in lines if "строка" in ln or "третья" in ln]
    assert len(quoted) == 3
    assert all(ln.startswith(RQ.LINE_PREFIX) for ln in quoted)


def test_the_quote_fence_is_not_the_human_words_fence():
    """The envelopes fence HIS words with '--- 8< ---'. Two blocks in one
    message, one his and one not, must not look alike."""
    block = "\n".join(RQ.render_block({"text": "чужие слова"}))
    assert "--- 8< ---" not in block


def test_long_quote_is_capped_and_states_how_much_is_missing():
    lines = RQ.render_block({"text": "я" * (RQ.QUOTE_CAP + 500)})
    block = "\n".join(lines)
    assert "[+500 chars]" in block
    assert block.count("я") == RQ.QUOTE_CAP


def test_a_quote_at_the_cap_is_not_marked_truncated():
    block = "\n".join(RQ.render_block({"text": "я" * RQ.QUOTE_CAP}))
    assert "chars]" not in block


def test_unattributed_quote_says_so_in_the_label():
    block = "\n".join(RQ.render_block({"text": "чей-то текст"}))
    assert "author not stated by Telegram" in block


def test_highlighted_fragment_is_rendered_separately_from_the_whole():
    block = "\n".join(RQ.render_block(
        {"text": "весь длинный вопрос", "fragment": "вторая часть"}))
    assert "весь длинный вопрос" in block
    assert "highlighted THIS part" in block
    assert "вторая часть" in block
