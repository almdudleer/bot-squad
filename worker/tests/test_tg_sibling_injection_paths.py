"""T-0773 — the two sibling injection paths of the T-0770 branch.

Three surfaces put a human's words into a live session's composer. T-0770 fixed
one of them; these tests cover the other two:

* **PATH 2** — an explicit ``[<sid>]`` reply to a bot message (the
  stall-escalation answer path, ``handle_update`` → ``_handle_reply``);
* **PATH 3** — ``/say <sid> <text>`` (``_handle_slash``).

WHAT IS PINNED, AND WHY EACH HALF IS HERE
-----------------------------------------
1. **ONE composer submission, not one per line.** The defect that makes this
   ticket exist, and it needed no product input: ``inject_input``'s transport
   sends one Enter PER LINE, so a multi-line human message arrived as N separate
   submissions and the session began answering line 1 while 2..N were still
   landing. Measured at a real tmux pane before any code changed: a 3-line reply
   → 3 submissions on both paths, and the same 3 lines → 1 submission after.
   ``tests/test_actions.py::test_inject_input_multiline`` still pins the OLD
   behaviour and MUST keep passing — it describes that transport, which is
   unchanged and still correct for the single-line nudges it exists for.

2. **Light provenance.** The session is told a human wrote this and over what
   channel (operator ruling on T-0773: "a session which knows a human is on the
   other end behaves differently from one handed an anonymous string").

3. **NO answer-owed ledger — the negative half, and the reason this file has as
   many negative tests as positive ones.** The named risk on the ticket is that
   someone folds T-0770's machinery in here for symmetry. The ruling refuses it
   on both paths: a ``[<sid>]`` reply is him ANSWERING a question the session
   asked, and the correct response to «да» is to go do the thing, not to post
   «да, понял» into his thread; ``/say`` is one-way by construction. So every
   path here asserts ``tg_direct_reply.load(cfg) == {}``.

   An empty ledger proves nothing on its own — a probe that never records
   anything reports exactly that too (T-0740). So
   ``test_control_the_topic_path_still_records_its_debt`` drives the T-0770 path
   through the same helpers and requires a debt to appear. If that control ever
   goes green-with-an-empty-ledger, the negatives above are measuring nothing.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import tg_listener as TL
from bot_squad_worker import tg_direct_reply as TDR


THREE_LINES = "line1\nline2\nline3"
SID = "S-alice-spec5-p3"


def _cfg(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        tg_bot_token="TESTBOT:TOKEN", data_dir=data_dir,
        projects={"test-project": types.SimpleNamespace(tg_chat="12345")},
        tg_proxy_url="", voice_enabled=False,
    )


def _from() -> dict:
    return {"id": 555123, "first_name": "Alexey", "last_name": "S", "username": "alx"}


def _reply_msg(text: str, *, sid: str = SID, chat_id: int = 12345,
               thread_id: int | None = None) -> dict:
    msg = {
        "message_id": 200, "chat": {"id": chat_id, "type": "private"},
        "from": _from(), "text": text,
        "reply_to_message": {"message_id": 100,
                             "text": f"[{sid}] needs your input — waiting"},
    }
    if thread_id is not None:
        msg["message_thread_id"] = thread_id
    return msg


def _say_msg(text: str, *, sid: str = SID, chat_id: int = 12345) -> dict:
    return {"message_id": 201, "chat": {"id": chat_id, "type": "private"},
            "from": _from(), "text": f"/say {sid} {text}"}


@pytest.fixture()
def injected(monkeypatch):
    """Capture (verb, params) instead of touching a pane."""
    import bot_squad_worker.actions as A
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(A, "dispatch",
                        lambda name, params: calls.append((name, params)) or {"ok": True})
    monkeypatch.setattr(TL, "_notify", lambda *a, **k: None)
    return calls


# ---------------------------------------------------------------------------
# 1. One submission, not one per line
# ---------------------------------------------------------------------------

def test_reply_path_delivers_a_multiline_message_as_ONE_block(tmp_path, injected):
    """★ The defect. Three lines used to be three composer submissions."""
    cfg = _cfg(tmp_path)
    TL.handle_update(cfg, {"update_id": 1, "message": _reply_msg(THREE_LINES)})

    assert len(injected) == 1
    verb, params = injected[0]
    assert verb == "inject_prompt"
    assert params["sid"] == SID
    # All three lines in ONE payload, in order, unaltered.
    assert "line1\nline2\nline3" in params["text"]


def test_say_path_delivers_a_multiline_message_as_ONE_block(tmp_path, injected):
    cfg = _cfg(tmp_path)
    TL.handle_update(cfg, {"update_id": 2, "message": _say_msg(THREE_LINES)})

    assert len(injected) == 1
    verb, params = injected[0]
    assert verb == "inject_prompt"
    assert params["sid"] == SID
    assert "line1\nline2\nline3" in params["text"]


# ---------------------------------------------------------------------------
# 2. Provenance
# ---------------------------------------------------------------------------

def test_reply_path_says_a_human_wrote_it_and_over_what_channel(tmp_path, injected):
    cfg = _cfg(tmp_path)
    TL.handle_update(cfg, {"update_id": 3, "message": _reply_msg("да")})

    text = injected[0][1]["text"]
    assert "TELEGRAM" in text
    assert "a human" in text
    assert "Alexey S" in text          # from the update, not assumed
    # T-0795: the chat id is HIGHLIGHTED, and a DM's absent topic is named as
    # prose rather than emitted as an empty field. Exact form pinned in
    # test_ping_ids_highlight.py.
    assert "▶ CHAT ID: 12345   (no topic id — not a forum topic)" in text
    assert "да" in text                # his words, verbatim, still present
    assert SID in text


def test_say_path_says_a_human_wrote_it_and_over_what_channel(tmp_path, injected):
    cfg = _cfg(tmp_path)
    TL.handle_update(cfg, {"update_id": 4, "message": _say_msg("почини тест")})

    text = injected[0][1]["text"]
    assert "TELEGRAM" in text
    assert "a human" in text
    assert "Alexey S" in text
    assert "`/say`" in text            # the channel, named
    assert "почини тест" in text


def test_a_reply_in_a_forum_topic_names_that_topic_and_its_answer_route(
        tmp_path, injected, monkeypatch):
    """A stall-escalation reply can arrive in a BOUND topic, not only a DM
    (T-0684 measured exactly that). The destination has to be spelled out —
    ``bsq tg ping`` resolves by lookup and answers in the wrong topic when a
    session holds two (the T-0770 measurement)."""
    from bot_squad_worker import tg_bindings
    cfg = _cfg(tmp_path)
    tg_bindings.set_binding(cfg, "999888", 42, "test-project")
    msg = _reply_msg("ну и?", chat_id=999888, thread_id=42)

    TL.handle_update(cfg, {"update_id": 5, "message": msg})

    text = injected[0][1]["text"]
    # T-0795: both ids highlighted instead of buried in the prose `Where` row.
    assert "▶ CHAT ID: 999888   ▶ TOPIC ID: 42" in text
    assert 'bsq topic say --chat 999888 --topic 42 "<your answer>"' in text


def test_a_dm_reply_is_never_handed_a_topic_command_that_cannot_run(tmp_path, injected):
    """``bsq topic say`` REFUSES ``--chat`` without ``--topic`` ("a thread id is
    only meaningful inside its own chat"), so a DM must be given the DM route.
    A command that dies on arrival is a wrong answer one step later — the
    T-0775 lesson, in a prompt string again."""
    cfg = _cfg(tmp_path)
    TL.handle_update(cfg, {"update_id": 6, "message": _reply_msg("ок")})

    text = injected[0][1]["text"]
    assert 'bsq tg ping "<your answer>"' in text
    assert "--topic" not in text


# ---------------------------------------------------------------------------
# 3. NO answer-owed ledger — the negatives, plus the control that gives them
#    meaning
# ---------------------------------------------------------------------------

def _bound_topic(cfg):
    """A forum topic both paths can arrive in — and the ONLY shape in which a
    debt could be created at all.

    This is load-bearing, and a weak first version of these two tests is why it
    is spelled out: written against a DM they stayed GREEN with the ledger
    deliberately folded back in, because ``record_owed`` no-ops on a thread-less
    message (``thread_id is None`` → ``None``, pinned in test_tg_direct_reply).
    A negative asserted where the mechanism cannot fire measures nothing."""
    from bot_squad_worker import tg_bindings
    tg_bindings.set_binding(cfg, "999888", 42, "test-project")
    return {"chat_id": 999888, "thread_id": 42}


def test_reply_path_creates_no_answer_owed_debt(tmp_path, injected):
    cfg = _cfg(tmp_path)
    where = _bound_topic(cfg)
    TL.handle_update(cfg, {"update_id": 7, "message": _reply_msg(THREE_LINES, **where)})

    assert TDR.load(cfg) == {}
    assert not TDR.owed_path(cfg).exists()


def test_say_path_creates_no_answer_owed_debt(tmp_path, injected):
    cfg = _cfg(tmp_path)
    where = _bound_topic(cfg)
    msg = _say_msg(THREE_LINES, chat_id=where["chat_id"])
    msg["message_thread_id"] = where["thread_id"]
    TL.handle_update(cfg, {"update_id": 8, "message": msg})

    assert TDR.load(cfg) == {}
    assert not TDR.owed_path(cfg).exists()


def test_neither_path_tells_the_session_an_answer_is_OWED(tmp_path, injected):
    """The ledger is not the only way to nag. A session told "AN ANSWER IS OWED"
    posts an acknowledgement into his thread whether or not anything is
    recorded, which is the outcome the ruling refuses — the ticket's harm
    inverted. Both phrasings that carry that instruction are excluded."""
    cfg = _cfg(tmp_path)
    TL.handle_update(cfg, {"update_id": 9, "message": _reply_msg("да")})
    TL.handle_update(cfg, {"update_id": 10, "message": _say_msg("сделай")})

    for _verb, params in injected:
        text = params["text"]
        assert "AN ANSWER IS OWED" not in text
        assert "the system will" not in text     # …remind you, and then answer for you
        assert "Send it now" not in text


def test_control_the_topic_path_still_records_its_debt(tmp_path, monkeypatch, injected):
    """★ POSITIVE CONTROL for the four negatives above (T-0740). They read an
    empty ledger; a helper set that never reaches the recording code reads the
    same empty ledger on a path that DOES record. This drives T-0770's own path
    with the same cfg/fixtures and requires the debt to appear."""
    from bot_squad_worker import tg_bindings
    cfg = _cfg(tmp_path)
    tg_bindings.set_binding(cfg, "111", 42, "test-project",
                            ticket_id="T-0314", session_id=SID)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: None)
    msg = {"message_id": 202, "chat": {"id": 111, "type": "supergroup", "is_forum": True},
           "message_thread_id": 42, "from": _from(), "text": "ну и что тут?"}

    result = TL.handle_update(cfg, {"update_id": 11, "message": msg})

    assert result["action"] == "task_topic_inject"
    assert TDR.load(cfg)[f"111:42:{SID}"]["text"] == "ну и что тут?"


# ---------------------------------------------------------------------------
# 4. What must NOT change
# ---------------------------------------------------------------------------

def test_an_empty_reply_is_still_refused_rather_than_enveloped(tmp_path, monkeypatch):
    """An envelope is never empty. Wrapping unconditionally would turn this
    loud refusal into a header delivered with no body — a silent success where
    a failure used to be reported to the sender."""
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path)
    dispatched: list[tuple[str, dict]] = []

    def _dispatch(name, params):
        dispatched.append((name, params))
        raise A.ActionError("inject_input: empty text")

    monkeypatch.setattr(A, "dispatch", _dispatch)
    notices: list[str] = []
    monkeypatch.setattr(TL, "_notify",
                        lambda cfg_, chat_id, text, **k: notices.append(text))

    result = TL.handle_update(cfg, {"update_id": 12, "message": _reply_msg("   ")})

    assert dispatched and dispatched[0][0] == "inject_input"   # the OLD path
    assert result["ok"] is False and result["fallback"] == "impossible"
    assert result["reason"] == "нечего передавать (пустой текст)"
    assert notices and "НЕ доставлено" in notices[0]


@pytest.mark.parametrize("kind", ["reply", "say"])
def test_a_dead_session_still_falls_back_and_stores_HIS_words(
        tmp_path, monkeypatch, kind):
    """T-0746 must survive the transport swap on BOTH paths. ``inject_prompt``
    raises the same ``ActionError`` as ``inject_input`` when there is no pane,
    which is why the fallback still fires — and what lands in the store is his
    text, never our envelope around it (system prose must not enter the record
    as his words)."""
    import bot_squad_worker.actions as A
    cfg = _cfg(tmp_path)
    dead = "S-almdudleer-gone-p999"
    (cfg.data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "test-project" / "sessions" / f"{dead}.md").write_text(
        f"---\nsid: {dead}\n---\n", encoding="utf-8")

    def _raise(name, params):
        raise A.ActionError("no live pane")

    monkeypatch.setattr(A, "dispatch", _raise)
    monkeypatch.setattr(TL, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    posts: list[dict] = []
    monkeypatch.setattr(TL, "_post_conversation",
                        lambda c, s, g, payload: posts.append(payload) or True)

    msg = (_reply_msg("мультилайн\nвторая", sid=dead) if kind == "reply"
           else _say_msg("мультилайн\nвторая", sid=dead))
    result = TL.handle_update(cfg, {"update_id": 13, "message": msg})

    assert result["fallback"] == "routed"
    assert result["action"] == ("inject_fallback" if kind == "reply" else "say_fallback")
    assert posts[-1]["author"] == "user"
    assert posts[-1]["text"] == "мультилайн\nвторая"
    assert "TELEGRAM" not in posts[-1]["text"]


def test_the_reply_path_still_clears_the_stall_marker(tmp_path, injected):
    """T-0155: answering via TG cancels the pending stall escalation. The
    transport changed underneath it; the behaviour must not."""
    from bot_squad_worker import tg_stall as TS
    cfg = _cfg(tmp_path)
    TS.mark_blocked(cfg, "test-project", SID, "need a call")
    assert TS._marker_path(cfg, "test-project", SID).exists()

    TL.handle_update(cfg, {"update_id": 14, "message": _reply_msg("да, поехали")})

    assert not TS._marker_path(cfg, "test-project", SID).exists()
