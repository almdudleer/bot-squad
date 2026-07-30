"""Tests for worker.tg_listener — all network + subprocess mocked."""
from __future__ import annotations

import logging
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bot_squad_worker.tg_listener as TL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(
    tmp_path: Path,
    *,
    bot_token: str = "TESTBOT:TOKEN",
    tg_chat: str = "12345",
    proxy_url: str = "",
    voice_enabled: bool = False,
):
    """Build a minimal config-like namespace for tg_listener tests."""
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True)
    proj = types.SimpleNamespace(tg_chat=tg_chat)
    return types.SimpleNamespace(
        tg_bot_token=bot_token,
        data_dir=data_dir,
        projects={"test-project": proj},
        tg_proxy_url=proxy_url,
        voice_enabled=voice_enabled,
    )


def _from(uid=555123, first="Alexey", last="S", username="alx") -> dict:
    return {"id": uid, "first_name": first, "last_name": last, "username": username}


def _reply_message(sid: str, reply_text: str, chat_id: int = 12345) -> dict:
    """Build a fake TG message that is a reply to a worker notification."""
    return {
        "message_id": 200,
        "chat": {"id": chat_id, "type": "private"},
        "text": reply_text,
        "reply_to_message": {
            "message_id": 100,
            "text": f"[{sid}] needs your input — waiting",
        },
    }


def _slash_message(text: str, chat_id: int = 12345) -> dict:
    """Build a fake TG message with a slash command."""
    return {
        "message_id": 201,
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }


# ---------------------------------------------------------------------------
# extract_reply_target
# ---------------------------------------------------------------------------


def test_extract_reply_target_well_formed():
    msg = _reply_message("S-alice-spec5-p3", "yes go ahead")
    result = TL.extract_reply_target(msg)
    assert result == ("S-alice-spec5-p3", "yes go ahead")


def test_extract_reply_target_no_reply():
    msg = {"text": "hello", "chat": {"id": 1}}
    assert TL.extract_reply_target(msg) is None


def test_extract_reply_target_reply_without_sid():
    msg = {
        "text": "hi",
        "reply_to_message": {"text": "some random message without SID"},
    }
    assert TL.extract_reply_target(msg) is None


def test_extract_reply_target_sid_not_at_start():
    # SID_RE uses match (from start), so mid-string SID should NOT match
    msg = {
        "text": "ok",
        "reply_to_message": {"text": "prefix text [S-alice-spec5-p3] in middle"},
    }
    # The regex is anchored at start via .match(), so this should return None
    assert TL.extract_reply_target(msg) is None


def test_extract_reply_target_strips_text_whitespace():
    msg = _reply_message("S-alice-spec5-p3", "  yes  ")
    sid, text = TL.extract_reply_target(msg)
    assert text == "yes"


# ---------------------------------------------------------------------------
# T-0719 REGRESSION: reply routing vs. the T-0676 item 5 COMPACT label
#
# T-0676 item 5 made the TG prefix compact ("<slug> <role>"), which removed the
# raw SID from the message text entirely. SID_RE — the only resolver at the
# time — could no longer match, so EVERY session's replies silently fell
# through to the user-conversation attendant. These tests pin routing against
# that exact label format, which is the gap that let the regression ship.
# ---------------------------------------------------------------------------

# What _prefix actually puts on the wire for a compact label (T-0676 item 5):
# sessions.sid_display_label(sid, slug, compact=True) -> "bot-squad operator",
# then tg._prefix -> "[bot-squad operator] <message>".
COMPACT_QUOTED = "[bot-squad operator] operator here — please confirm"


def _compact_reply_message(reply_text: str, *, chat_id: int = 12345,
                           quoted: str = COMPACT_QUOTED,
                           quoted_message_id: int = 900) -> dict:
    """A reply to a page whose prefix carries NO raw SID (the compact form)."""
    return {
        "message_id": 201,
        "chat": {"id": chat_id, "type": "private"},
        "from": _from(),
        "text": reply_text,
        "reply_to_message": {
            "message_id": quoted_message_id,
            "chat": {"id": chat_id, "type": "private"},
            "text": quoted,
        },
    }


def test_compact_label_is_genuinely_unparseable_by_the_regex():
    """The premise of the whole ticket, pinned: text-based resolution CANNOT
    work against the compact label. If this ever starts passing, the label
    changed shape and the rest of these tests are testing the wrong thing."""
    assert TL.SID_RE.match(COMPACT_QUOTED) is None
    assert TL.extract_reply_target(_compact_reply_message("yes")) is None


def test_extract_reply_target_resolves_compact_label_via_message_id(tmp_path):
    """THE regression: a reply to a compact-labelled page must still reach the
    session that sent it — resolved from reply_to_message.message_id, not from
    the (SID-less) quoted text."""
    from bot_squad_worker import tg_reply_map

    cfg = _make_cfg(tmp_path)
    sid = "S-almdudleer-operator-p241"
    tg_reply_map.record(cfg.data_dir, chat_id=12345, message_id=900, sid=sid)

    msg = _compact_reply_message("yes, go ahead")
    assert TL.extract_reply_target(msg, cfg) == (sid, "yes, go ahead")


@pytest.mark.parametrize("kind,sid", [
    ("dev", "S-almdudleer-t-0719-dev-p260"),
    ("teamlead", "S-almdudleer-gateway-routing-tl-p23"),
    ("operator", "S-almdudleer-operator-p241"),
    ("user-conversation", "S-almdudleer-gu_dc8262b6-user-conversation-p5"),
])
def test_compact_label_reply_resolves_for_every_sender_kind(tmp_path, kind, sid):
    """The break was SYSTEM-WIDE, not operator-specific — every sender kind
    that pages the stakeholder derives a role and so gets the compact label."""
    from bot_squad_worker import tg_reply_map

    cfg = _make_cfg(tmp_path)
    quoted = f"[bot-squad {kind}] paging you about T-0719"
    assert TL.SID_RE.match(quoted) is None          # compact ⇒ no raw SID
    tg_reply_map.record(cfg.data_dir, chat_id=12345, message_id=901, sid=sid)

    msg = _compact_reply_message("ack", quoted=quoted, quoted_message_id=901)
    assert TL.extract_reply_target(msg, cfg) == (sid, "ack")


def test_reply_falls_back_to_regex_when_no_map_entry(tmp_path):
    """Messages sent BEFORE the map existed must keep resolving — the regex
    path stays. (What that path does and does NOT cover is the table in
    `test_sid_re_resolves_exactly_the_wire_forms`; this docstring used to
    assert coverage of a form the pattern could not match.)"""
    cfg = _make_cfg(tmp_path)
    msg = _reply_message("S-alice-spec5-p3", "sure")
    assert TL.extract_reply_target(msg, cfg) == ("S-alice-spec5-p3", "sure")


def test_sid_re_resolves_exactly_the_wire_forms():
    """The fallback's coverage as a TABLE, built from the REAL renderers.

    T-0719 follow-up (operator p502 found the first half of this). Two
    docstrings — here, on `SID_RE`, and on `tg_reply_map` — promised that the
    "non-compact bracket form some callers still use" kept resolving. It did
    not: `tg._prefix` wraps its label in brackets, so a nested
    `sid_display_label` label reaches the wire DOUBLE-bracketed
    (`[[bot-squad] S-…-p1] текст`), and the old pattern required the SID
    immediately after the first `[`. Nobody noticed because no test built the
    string the renderer actually produces — they hand-typed the easy form.

    So the inputs here are COMPOSED by `sid_display_label` + `_prefix` rather
    than written as literals: if either renderer changes shape, this test
    follows it instead of quietly testing a string that no longer ships.
    """
    from bot_squad_worker import sessions as S
    from bot_squad_worker.tg import _prefix

    sid = "S-almdudleer-operator-p241"
    nested = S.sid_display_label(sid, "bot-squad")               # "[bot-squad] S-…"
    compact = S.sid_display_label(sid, "bot-squad", compact=True)  # "bot-squad operator"
    assert nested == f"[bot-squad] {sid}" and compact == "bot-squad operator"

    resolves = {
        "raw sid label": _prefix("текст", sid=sid, user=""),
        "raw sid + user": _prefix("текст", sid=sid, user="almdudleer"),
        # Reachable today via `sender_tag.compose`'s except-branch, which falls
        # back to `_prefix` with the caller's raw (possibly nested) label.
        "nested bracket label": _prefix("текст", sid=nested, user=""),
        "nested bracket + user": _prefix("текст", sid=nested, user="almdudleer"),
    }
    for name, wire in resolves.items():
        m = TL.SID_RE.match(wire)
        assert m is not None, f"{name} must resolve: {wire!r}"
        assert m.group(1) == sid, f"{name} resolved to {m.group(1)!r}: {wire!r}"

    never = {
        # THE regression. No SID exists in this text for any regex to find —
        # only the message-id map can route it, which is the whole ticket.
        "compact label (T-0676)": _prefix("текст", sid=compact, user=""),
        # Routing must never be steerable by quoting a SID mid-sentence.
        "sid in the body": f"[bot-squad operator] см. {sid} выше",
        "non-identity leading marker": f"[FYI — ответ не требуется] {sid}",
        "leading whitespace": " " + _prefix("текст", sid=sid, user=""),
    }
    for name, wire in never.items():
        assert TL.SID_RE.match(wire) is None, f"{name} must NOT resolve: {wire!r}"


def test_nested_bracket_label_reply_resolves_without_a_map_entry(tmp_path):
    """The table above, driven through the actual entry point: a pre-map (or
    evicted) message whose prefix carries the nested label still routes."""
    from bot_squad_worker.tg import _prefix

    cfg = _make_cfg(tmp_path)
    sid = "S-almdudleer-operator-p241"
    quoted = _prefix("подтвердите, пожалуйста", sid=f"[bot-squad] {sid}", user="")
    msg = _compact_reply_message("да", quoted=quoted, quoted_message_id=4242)

    assert TL.extract_reply_target(msg, cfg) == (sid, "да")


def test_map_wins_over_a_stale_sid_in_the_quoted_text(tmp_path):
    """Message-id resolution is authoritative: it is the presentation-
    independent key, so it must not be second-guessed by whatever the text
    happens to say."""
    from bot_squad_worker import tg_reply_map

    cfg = _make_cfg(tmp_path)
    tg_reply_map.record(
        cfg.data_dir, chat_id=12345, message_id=100, sid="S-real-target-p9")
    msg = _reply_message("S-alice-spec5-p3", "go")     # quoted says p3…
    assert TL.extract_reply_target(msg, cfg) == ("S-real-target-p9", "go")


def test_synthetic_sender_reply_still_falls_through(tmp_path):
    """`deploy_monitor` is not a session: nothing is recorded for it, and its
    replies keep falling through to the attendant path — unchanged behaviour."""
    cfg = _make_cfg(tmp_path)
    msg = _compact_reply_message(
        "ok", quoted="[[bot-squad] deploy_monitor] deploy finished",
        quoted_message_id=902)
    assert TL.extract_reply_target(msg, cfg) is None


def test_corrupt_reply_map_falls_back_to_regex(tmp_path):
    """A broken store must cost us the message-id path only, never the whole
    inbound route."""
    from bot_squad_worker import tg_reply_map

    cfg = _make_cfg(tmp_path)
    tg_reply_map.map_path(cfg.data_dir).write_text("{not json")
    msg = _reply_message("S-alice-spec5-p3", "sure")
    assert TL.extract_reply_target(msg, cfg) == ("S-alice-spec5-p3", "sure")


def test_handle_update_injects_compact_label_reply_into_originating_session(tmp_path):
    """End-to-end through handle_update: a reply to a compact-labelled page is
    dispatched to THAT session, not routed to the attendant.

    T-0773 updated the payload assertion, not the routing claim this test was
    written for: the verb is now ``inject_prompt`` (ONE composer submission —
    ``inject_input`` sent one Enter per line, so his multi-line answers arrived
    split) and his words ride inside a provenance envelope. What is asserted
    below is that his text survives verbatim and reaches THIS sid."""
    from bot_squad_worker import tg_reply_map

    cfg = _make_cfg(tmp_path)
    sid = "S-almdudleer-operator-p241"
    tg_reply_map.record(cfg.data_dir, chat_id=12345, message_id=900, sid=sid)

    calls: list[tuple[str, dict]] = []

    def _fake_dispatch(action, params):
        calls.append((action, params))
        return {"ok": True}

    import bot_squad_worker.actions as A
    with patch.object(A, "dispatch", _fake_dispatch):
        result = TL.handle_update(cfg, {"message": _compact_reply_message("do it")})

    assert result["action"] == "inject"
    assert result["sid"] == sid
    assert len(calls) == 1
    verb, params = calls[0]
    assert verb == "inject_prompt"
    assert params["sid"] == sid
    assert "do it" in params["text"]


# ---------------------------------------------------------------------------
# extract_slash_command
# ---------------------------------------------------------------------------


def test_extract_slash_command_sessions():
    msg = _slash_message("/sessions")
    assert TL.extract_slash_command(msg) == ("sessions", "")


def test_extract_slash_command_say():
    msg = _slash_message("/say S-alice-spec5-p3 hello world")
    assert TL.extract_slash_command(msg) == ("say", "S-alice-spec5-p3 hello world")


def test_extract_slash_command_say_with_botname():
    msg = _slash_message("/say@watchbot S-alice-spec5-p3 hi")
    assert TL.extract_slash_command(msg) == ("say", "S-alice-spec5-p3 hi")


def test_extract_slash_command_help():
    msg = _slash_message("/help")
    assert TL.extract_slash_command(msg) == ("help", "")


def test_extract_slash_command_state():
    msg = _slash_message("/state")
    assert TL.extract_slash_command(msg) == ("state", "")


def test_extract_slash_command_unknown_returns_none():
    msg = _slash_message("/garbage something")
    assert TL.extract_slash_command(msg) is None


def test_extract_slash_command_no_slash():
    msg = _slash_message("just normal text")
    assert TL.extract_slash_command(msg) is None


# ---------------------------------------------------------------------------
# handle_update
# ---------------------------------------------------------------------------


def test_handle_update_skips_non_message(tmp_path):
    cfg = _make_cfg(tmp_path)
    update = {"update_id": 1, "edited_message": {"text": "nope"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "skip"
    assert result["reason"] == "no message"


# ---------------------------------------------------------------------------
# T-0682 (T-0676 item 2): bot/service-message intake guard
# ---------------------------------------------------------------------------


def test_handle_update_skips_bot_authored_message(tmp_path):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 3,
        "message": {
            "chat": {"id": 12345, "type": "supergroup"},
            "from": {"id": 8206895402, "is_bot": True, "first_name": "Bot Squad"},
            "text": "hi",
        },
    }
    result = TL.handle_update(cfg, update)
    assert result == {"ok": True, "action": "skip", "reason": "bot or service message"}


def test_handle_update_skips_forum_topic_created_service_message(tmp_path):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 4,
        "message": {
            "chat": {"id": 12345, "type": "supergroup"},
            "message_thread_id": 45,
            "from": {"id": 8206895402, "is_bot": True, "first_name": "Bot Squad"},
            "forum_topic_created": {"name": "[watchrobot] T-0270", "icon_color": 0},
        },
    }
    result = TL.handle_update(cfg, update)
    assert result == {"ok": True, "action": "skip", "reason": "bot or service message"}


def test_handle_update_skips_sender_chat_message(tmp_path):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 5,
        "message": {
            "chat": {"id": 12345, "type": "supergroup"},
            "sender_chat": {"id": -100999, "type": "channel", "title": "Anon"},
            "text": "hi",
        },
    }
    result = TL.handle_update(cfg, update)
    assert result == {"ok": True, "action": "skip", "reason": "bot or service message"}


def test_handle_update_does_not_skip_ordinary_human_message(tmp_path):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 6,
        "message": {
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 555123, "is_bot": False, "first_name": "Alexey"},
            "text": "hi",
        },
    }
    result = TL.handle_update(cfg, update)
    assert result["action"] != "skip" or result.get("reason") != "bot or service message"


def test_is_ignorable_service_message_true_cases():
    assert TL._is_ignorable_service_message({"from": {"is_bot": True}})
    assert TL._is_ignorable_service_message({"sender_chat": {"id": -1}})
    assert TL._is_ignorable_service_message({"forum_topic_created": {"name": "x"}})
    assert TL._is_ignorable_service_message({"new_chat_members": [{"id": 1}]})
    assert TL._is_ignorable_service_message({"pinned_message": {"message_id": 1}})


def test_is_ignorable_service_message_false_for_plain_text():
    assert not TL._is_ignorable_service_message({"from": {"is_bot": False}, "text": "hi"})
    assert not TL._is_ignorable_service_message({"text": "hi"})


def test_is_ignorable_service_message_covers_real_member_triggerable_types():
    """T-0684 audit: these TG service-message types carry no `text` and can be
    triggered by an ordinary (non-bot) group member via normal client UI — a
    real human hitting one of these would otherwise slip past the T-0682 guard
    and re-trigger the same 'empty message wakes an attendant' bug via a
    different door."""
    assert TL._is_ignorable_service_message({"from": {"is_bot": False}, "message_auto_delete_timer_changed": {"message_auto_delete_time": 86400}})
    assert TL._is_ignorable_service_message({"from": {"is_bot": False}, "chat_background_set": {}})
    assert TL._is_ignorable_service_message({"from": {"is_bot": False}, "boost_added": {"boost_count": 1}})


def test_own_bot_id_derived_from_token():
    cfg = types.SimpleNamespace(tg_bot_token="8206895402:AAGSomeSecretHere")
    assert TL._own_bot_id(cfg) == "8206895402"


def test_own_bot_id_empty_without_token():
    cfg = types.SimpleNamespace(tg_bot_token="")
    assert TL._own_bot_id(cfg) == ""


def test_is_ignorable_service_message_matches_own_bot_id_even_without_is_bot():
    # Belt-and-suspenders: a payload that (hypothetically) omits is_bot but
    # carries our own bot's numeric id is still caught.
    cfg = types.SimpleNamespace(tg_bot_token="8206895402:AAGSomeSecretHere")
    msg = {"from": {"id": 8206895402}, "text": ""}
    assert TL._is_ignorable_service_message(msg, cfg)


def test_is_ignorable_service_message_does_not_match_other_sender_id():
    cfg = types.SimpleNamespace(tg_bot_token="8206895402:AAGSomeSecretHere")
    msg = {"from": {"id": 555123, "is_bot": False}, "text": "hi"}
    assert not TL._is_ignorable_service_message(msg, cfg)


def test_handle_update_skips_non_allowlisted_chat(tmp_path):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 2,
        "message": {"chat": {"id": 99999}, "text": "hi"},
    }
    result = TL.handle_update(cfg, update)
    assert result["action"] == "skip"
    assert "not allowlisted" in result["reason"]


# ---------------------------------------------------------------------------
# T-0664: unrecognized chat_id onboarding seam
# ---------------------------------------------------------------------------


def test_handle_update_unknown_chat_logs_warning(tmp_path, caplog):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 2,
        "message": {
            "chat": {"id": 99999, "type": "group", "title": "New Squad HQ"},
            "text": "hi",
        },
    }
    with caplog.at_level("WARNING"):
        TL.handle_update(cfg, update)
    assert any("99999" in r.message for r in caplog.records)


def test_handle_update_unknown_chat_writes_discovery_record(tmp_path):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 2,
        "message": {
            "chat": {"id": 99999, "type": "group", "title": "New Squad HQ"},
            "text": "hi",
        },
    }
    result = TL.handle_update(cfg, update)

    # T-0664: the skip return shape is unchanged — discovery is a side effect.
    assert result == {"ok": True, "action": "skip", "reason": "chat 99999 not allowlisted"}

    import json
    record = json.loads(TL._unknown_chats_path(cfg).read_text())
    assert record["99999"]["title"] == "New Squad HQ"
    assert record["99999"]["type"] == "group"
    assert record["99999"]["count"] == 1
    assert record["99999"]["first_seen_at"] == record["99999"]["last_seen_at"]


def test_handle_update_unknown_chat_second_message_updates_in_place(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 2,
        "message": {"chat": {"id": 99999, "type": "group", "title": "New Squad HQ"}, "text": "hi"},
    }
    TL.handle_update(cfg, update)

    times = iter(["2026-07-25T00:00:00Z", "2026-07-25T00:05:00Z"])
    monkeypatch.setattr(TL, "_now_iso", lambda: next(times))
    TL.handle_update(cfg, {**update, "update_id": 3})

    import json
    record = json.loads(TL._unknown_chats_path(cfg).read_text())
    assert len(record) == 1
    assert record["99999"]["count"] == 2


def test_handle_update_dispatches_reply_to_inject(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _reply_message("S-alice-spec5-p3", "go ahead", chat_id=12345)
    update = {"update_id": 3, "message": msg}

    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True, "pane_id": "%3", "lines_sent": 1})

    result = TL.handle_update(cfg, update)
    assert result["ok"] is True
    assert result["action"] == "inject"
    assert result["sid"] == "S-alice-spec5-p3"


def test_handle_update_reply_clears_stall_marker(tmp_path, monkeypatch):
    # T-0155: a TG reply unblocks the agent → its stall marker is removed.
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    sid = "S-alice-spec5-p3"

    import bot_squad_worker.actions as A
    import bot_squad_worker.tg_stall as TS
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True})

    TS.mark_blocked(cfg, "test-project", sid, "need a call")
    assert TS._marker_path(cfg, "test-project", sid).exists()

    update = {"update_id": 4, "message": _reply_message(sid, "ship it", chat_id=12345)}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "inject"
    assert not TS._marker_path(cfg, "test-project", sid).exists()


def test_handle_update_reply_in_bound_topic_clears_stall_marker(tmp_path, monkeypatch):
    """T-0684 audit: a stall-escalation reply can arrive via a T-0639 BOUND
    forum topic whose chat_id is NOT any project's static tg_chat (by design
    — see handle_update's own allowlist comment: "the stakeholder's forum
    supergroup is not any project's static tg_chat"). Before this fix,
    _clear_stall only matched a project's static tg_chat, so this reply could
    never find its marker — it would survive to a redundant re-escalation
    even though the stakeholder already answered."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    sid = "S-alice-spec5-p3"
    bound_chat_id = "999888"
    thread_id = 42

    import bot_squad_worker.actions as A
    import bot_squad_worker.tg_stall as TS
    from bot_squad_worker import tg_bindings
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True})

    tg_bindings.set_binding(cfg, bound_chat_id, thread_id, "test-project")
    TS.mark_blocked(cfg, "test-project", sid, "need a call")
    assert TS._marker_path(cfg, "test-project", sid).exists()

    msg = _reply_message(sid, "ship it", chat_id=int(bound_chat_id))
    msg["message_thread_id"] = thread_id
    update = {"update_id": 5, "message": msg}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "inject"
    assert not TS._marker_path(cfg, "test-project", sid).exists()


def test_handle_update_dispatches_sessions_slash(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _slash_message("/sessions", chat_id=12345)
    update = {"update_id": 4, "message": msg}

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "list_sessions", lambda _cfg, _slug: [
        {"sid": "S-alice-spec5-p3", "window": "spec5", "status": "active"}
    ])

    # Mock _notify to avoid real HTTP
    monkeypatch.setattr(TL, "_notify", lambda cfg, chat_id, text, **k: None)

    result = TL.handle_update(cfg, update)
    assert result["ok"] is True
    assert result["action"] == "sessions"
    assert result["count"] == 1


def test_handle_update_dispatches_say_slash(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _slash_message("/say S-alice-spec5-p3 hello", chat_id=12345)
    update = {"update_id": 5, "message": msg}

    import bot_squad_worker.actions as A
    dispatch_calls = []
    monkeypatch.setattr(A, "dispatch", lambda name, params: dispatch_calls.append((name, params)) or {"ok": True, "pane_id": "%3", "lines_sent": 1})

    result = TL.handle_update(cfg, update)
    assert result["ok"] is True
    assert result["action"] == "say"
    # T-0773: was `("inject_input", {..., "text": "hello"})`. That pin was the
    # defect written down — the bare text over the one-Enter-per-line transport.
    # `/say` now delivers ONE composer submission carrying a light provenance
    # envelope; see test_tg_sibling_injection_paths.py for what is in it.
    verb, params = dispatch_calls[0]
    assert verb == "inject_prompt"
    assert params["sid"] == "S-alice-spec5-p3"
    assert "hello" in params["text"]


def test_handle_update_inject_failed_notifies(tmp_path, monkeypatch):
    """T-0746 item (b): this message carries no ``from``, so there is no
    recognized sender and therefore no ``(slug, gid)`` thread to fall back
    into. The failure must then reach the SENDER as a failure — never silently,
    and (unlike the pre-T-0746 "message dropped" notice, which came back in and
    corrupted the record) never written to the store as content."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _reply_message("S-alice-spec5-p3", "text", chat_id=12345)
    update = {"update_id": 6, "message": msg}

    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "dispatch", MagicMock(side_effect=A.ActionError("no pane")))

    notify_calls = []
    monkeypatch.setattr(TL, "_notify", lambda cfg, chat_id, text, **k: notify_calls.append(text))
    posts = []
    monkeypatch.setattr(TL, "_post_conversation",
                        lambda *a, **k: posts.append(a) or True)

    result = TL.handle_update(cfg, update)
    assert result["ok"] is False
    assert result["action"] == "inject_failed"
    assert result["fallback"] == "impossible"
    assert len(notify_calls) == 1
    assert "не активна" in notify_calls[0] and "НЕ доставлено" in notify_calls[0]
    assert posts == []


def test_handle_update_skips_plain_message(tmp_path):
    """A plain (non-reply, non-slash) message in an allowlisted chat is skipped."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = {"chat": {"id": 12345}, "text": "just chatting"}
    update = {"update_id": 7, "message": msg}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "skip"


def test_handle_update_project_slash_not_appended_to_store(tmp_path, monkeypatch):
    """T-0659: a /project control command must NOT be append_conversation()'d
    into the statically-mapped project's store. Doing so spuriously wakes that
    project's user-conversation attendant (the append endpoint auto-wakes on
    any user-authored append, T-0631) with a contextless '/project' it can't
    interpret — the confused-clarification reply the stakeholder hit every time
    he used /project to switch."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _slash_message("/project other-project", chat_id=12345)
    update = {"update_id": 20, "message": msg}

    # Resolve a real gid so the `if gid` guard passes — the ONLY thing that must
    # now prevent the append is the T-0659 `not slash` condition.
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda *a, **k: {"global_user_id": "gu_test"})
    append_calls = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda cfg, slug, gid, m, **k: append_calls.append((slug, gid)))
    # _handle_project reads/sets the pin over HTTP — stub it out.
    monkeypatch.setattr(TL, "_handle_project",
                        lambda cfg, chat_id, gid, args, **k: {"ok": True, "action": "project", "slug": args})

    result = TL.handle_update(cfg, update)
    assert result["action"] == "project"
    assert append_calls == []  # no spurious append for a control/routing command


def test_handle_update_reply_still_appended_to_store(tmp_path, monkeypatch):
    """T-0659 guard: the fix skips the append for SLASH commands only — a reply
    (real project-directed content, T-0489) must still be recorded to the store."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _reply_message("S-alice-spec5-p3", "go ahead", chat_id=12345)
    update = {"update_id": 21, "message": msg}

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda *a, **k: {"global_user_id": "gu_test"})
    append_calls = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda cfg, slug, gid, m, **k: append_calls.append((slug, gid)))
    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True, "pane_id": "%3", "lines_sent": 1})

    result = TL.handle_update(cfg, update)
    assert result["action"] == "inject"
    assert append_calls == [("test-project", "gu_test")]  # content still recorded


# ---------------------------------------------------------------------------
# T-0488: TG sender -> mothership GlobalUser linkage (single bot user recognition)
# resolve_or_link_sender posts to the API (single-writer owns the registry write);
# env-gated + best-effort so inbound routing is never blocked.
# ---------------------------------------------------------------------------


def _link_env(monkeypatch, base="https://mship.test", token="WTOKEN"):
    # T-0527: clear the dedicated var so these tests exercise the
    # MOTHERSHIP_BASE_URL fallback path explicitly (precedence tested below).
    monkeypatch.delenv("WORKER_API_BASE_URL", raising=False)
    if base is None:
        monkeypatch.delenv("MOTHERSHIP_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("MOTHERSHIP_BASE_URL", base)
    if token is None:
        monkeypatch.delenv("WORKER_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WORKER_API_TOKEN", token)


def test_api_base_url_prefers_dedicated_worker_var(monkeypatch):
    """T-0527: WORKER_API_BASE_URL wins over MOTHERSHIP_BASE_URL (the latter is
    the API container's public self-URL and collides in the shared .env)."""
    from bot_squad_worker import tg_listener as TL
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://botsquad.dev")
    monkeypatch.setenv("WORKER_API_BASE_URL", "http://127.0.0.1:8099/")
    assert TL._api_base_url() == "http://127.0.0.1:8099"


def test_api_base_url_falls_back_to_mothership(monkeypatch):
    """T-0527: with no dedicated var, fall back to MOTHERSHIP_BASE_URL."""
    from bot_squad_worker import tg_listener as TL
    monkeypatch.delenv("WORKER_API_BASE_URL", raising=False)
    monkeypatch.setenv("MOTHERSHIP_BASE_URL", "https://mship.test/")
    assert TL._api_base_url() == "https://mship.test"


def test_api_base_url_empty_when_neither_set(monkeypatch):
    """T-0527: no var set → empty → linkage no-ops (inbound never blocked)."""
    from bot_squad_worker import tg_listener as TL
    monkeypatch.delenv("WORKER_API_BASE_URL", raising=False)
    monkeypatch.delenv("MOTHERSHIP_BASE_URL", raising=False)
    assert TL._api_base_url() == ""


def test_resolve_or_link_sender_first_contact_posts(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"global_user_id": "gu_abc", "created": True, "slug": "test-project"}
        return resp

    msg = {"from": _from(), "text": "hi", "chat": {"id": 12345}}
    with patch("httpx.post", side_effect=fake_post):
        identity = TL.resolve_or_link_sender(cfg, msg, "test-project")

    assert identity["global_user_id"] == "gu_abc"
    assert identity["created"] is True
    assert identity["slug"] == "test-project"
    assert captured["url"] == "https://mship.test/api/m/worker/tg/resolve-or-link"
    assert captured["json"]["tg_user_id"] == "555123"
    assert captured["json"]["slug"] == "test-project"
    assert captured["json"]["display_name"] == "Alexey S"
    assert captured["headers"]["Authorization"] == "Bearer WTOKEN"


def test_resolve_or_link_sender_recognized_subsequent(tmp_path, monkeypatch):
    """A returning sender resolves to the same GlobalUser, created=False."""
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)

    def fake_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"global_user_id": "gu_abc", "created": False, "slug": ""}
        return resp

    msg = {"from": _from(), "text": "again"}
    with patch("httpx.post", side_effect=fake_post):
        identity = TL.resolve_or_link_sender(cfg, msg, "")
    assert identity["global_user_id"] == "gu_abc"
    assert identity["created"] is False


def test_resolve_or_link_sender_noop_without_env(tmp_path, monkeypatch):
    """No API base / token configured => no-op (no HTTP), returns None."""
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch, base=None, token=None)
    msg = {"from": _from(), "text": "hi"}
    with patch("httpx.post", side_effect=AssertionError("must not POST")):
        assert TL.resolve_or_link_sender(cfg, msg, "test-project") is None


def test_resolve_or_link_sender_noop_without_sender(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    msg = {"text": "no from field"}
    with patch("httpx.post", side_effect=AssertionError("must not POST")):
        assert TL.resolve_or_link_sender(cfg, msg, "test-project") is None


def test_resolve_or_link_sender_swallows_http_error(tmp_path, monkeypatch):
    """A link failure must not propagate — inbound routing keeps working."""
    import httpx
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    msg = {"from": _from(), "text": "hi"}
    with patch("httpx.post", side_effect=httpx.ConnectError("down")):
        assert TL.resolve_or_link_sender(cfg, msg, "test-project") is None


def test_resolve_or_link_does_not_use_tg_proxy(tmp_path, monkeypatch):
    """The local-API call is NOT TG egress — it must not route via tg_proxy_url
    (a proxy kwarg would TypeError this signature)."""
    cfg = _make_cfg(tmp_path, proxy_url="socks5://10.0.0.1:1080")
    _link_env(monkeypatch)

    def fake_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"global_user_id": "gu_x", "created": True}
        return resp

    msg = {"from": _from(), "text": "hi"}
    with patch("httpx.post", side_effect=fake_post):
        identity = TL.resolve_or_link_sender(cfg, msg, "test-project")
    assert identity["global_user_id"] == "gu_x"


def test_handle_update_links_sender_and_records_identity(tmp_path, monkeypatch):
    """handle_update resolves the sender and surfaces (slug, global_user_id).

    T-0492: a recognized sender's unquoted message no longer just 'skips' — with
    no current project pinned it asks which project. The identity-surfacing
    invariant (global_user_id on the result) still holds."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    monkeypatch.setattr(
        TL, "resolve_or_link_sender",
        lambda c, m, slug: {"global_user_id": "gu_zzz", "created": True, "slug": slug},
    )
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: None)
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat, **k: None)
    update = {"update_id": 9, "message": {"chat": {"id": 12345}, "from": _from(), "text": "hello"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "ask_project"     # recognized + unpinned -> asks
    assert result["global_user_id"] == "gu_zzz"  # identity still surfaced


def test_handle_update_first_contact_affirms_free_form_steering(tmp_path, monkeypatch):
    """T-0634: a brand-new sender (created=True) gets a one-time welcome
    affirming that free-form text — no command — is the normal way to steer,
    so a stakeholder never has to wonder whether typing here "just works"."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    monkeypatch.setattr(
        TL, "resolve_or_link_sender",
        lambda c, m, slug: {"global_user_id": "gu_new", "created": True, "slug": slug},
    )
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: None)
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat, **k: None)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))
    update = {"update_id": 9, "message": {"chat": {"id": 12345}, "from": _from(), "text": "hello"}}
    TL.handle_update(cfg, update)
    assert any("no command" in t.lower() or "any request" in t.lower() for t in echoes)


def test_handle_update_returning_sender_no_welcome(tmp_path, monkeypatch):
    """T-0634: created=False (a returning sender) must NOT re-fire the welcome
    on every message."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    monkeypatch.setattr(
        TL, "resolve_or_link_sender",
        lambda c, m, slug: {"global_user_id": "gu_old", "created": False, "slug": slug},
    )
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: None)
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat, **k: None)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))
    update = {"update_id": 10, "message": {"chat": {"id": 12345}, "from": _from(), "text": "hello again"}}
    TL.handle_update(cfg, update)
    assert echoes == []


def test_handle_slash_help_affirms_free_form_steering(tmp_path, monkeypatch):
    """T-0634: /help must state that plain typed text (no command) is routed —
    the discoverability gap the stakeholder flagged."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))
    result = TL._handle_slash(cfg, "12345", "help", "")
    assert result["action"] == "help"
    assert any("no command" in t.lower() for t in echoes)


# ---------------------------------------------------------------------------
# T-0655 (Addendum 1): /state — drive on/off, quota target, lifecycle state.
# ---------------------------------------------------------------------------

def test_handle_slash_state_reports_drive_and_target(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))

    import bot_squad_worker.dispatch as D
    import bot_squad_worker.operator_redrive as ORD
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(D, "live_operator_sids", lambda c, slug: ["S-op-1"])
    monkeypatch.setattr(S, "_find_session_md", lambda sdir, sid, uuid: sdir / f"{sid}.md")
    monkeypatch.setattr(S, "_read_session_metadata", lambda p: {"drive": "off"})
    monkeypatch.setattr(S, "list_sessions", lambda c, slug: [
        {"sid": "S-op-1"}, {"sid": "S-dev-1"},
    ])
    monkeypatch.setattr(ORD, "pacing_status", lambda c, slug: {
        "weekly_target_pct": 20.0, "spend_pct": 4.5, "recommendation": "advisory",
    })

    result = TL._handle_slash(cfg, "12345", "state", "")
    assert result["ok"] is True
    assert result["action"] == "state"
    assert result["drive"] == "off"
    assert result["operator"] == "S-op-1"
    body = echoes[0]
    assert "S-op-1" in body
    assert "drive=off" in body
    assert "20%" in body
    assert "2" in body  # active session count


def test_handle_slash_state_no_live_operator(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))

    import bot_squad_worker.dispatch as D
    import bot_squad_worker.operator_redrive as ORD
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(D, "live_operator_sids", lambda c, slug: [])
    monkeypatch.setattr(S, "list_sessions", lambda c, slug: [])
    monkeypatch.setattr(ORD, "pacing_status", lambda c, slug: {
        "weekly_target_pct": None, "spend_pct": None, "recommendation": "ok",
    })

    result = TL._handle_slash(cfg, "12345", "state", "")
    assert result["ok"] is True
    assert result["operator"] is None
    assert result["drive"] == "n/a (no live operator)"
    assert "not set" in echoes[0]


def test_handle_slash_state_unlinked_chat(tmp_path, monkeypatch):
    """A chat with no project mapped to it (tg_chat mismatch) is refused,
    not a crash reading a nonexistent slug's session dir."""
    cfg = _make_cfg(tmp_path, tg_chat="99999")  # different from the call below
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))

    result = TL._handle_slash(cfg, "12345", "state", "")
    assert result["ok"] is False
    assert result["action"] == "state_no_project"


# ---------------------------------------------------------------------------
# T-0489: conversation history append. Each inbound TG user message is recorded
# to the API conversation store (single-writer = API), keyed by
# (slug, global_user_id). Env-gated + best-effort so inbound routing is never
# blocked by a record failure.
# ---------------------------------------------------------------------------


def test_append_conversation_posts(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    msg = {"from": _from(), "text": "deploy please", "date": 1750000000}
    with patch("httpx.post", side_effect=fake_post):
        ok = TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert ok is True
    assert captured["url"] == "https://mship.test/api/m/worker/conversations/test-project/gu_abc/messages"
    assert captured["json"]["author"] == "user"
    assert captured["json"]["text"] == "deploy please"
    assert captured["json"]["timestamp"]  # an ISO ts derived from the TG date
    assert captured["json"]["attachments"] == []
    assert captured["headers"]["Authorization"] == "Bearer WTOKEN"


def test_append_conversation_captures_voice_attachment(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    msg = {"from": _from(), "voice": {"file_id": "VID", "duration": 3}}
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert captured["json"]["attachments"] == [{"type": "voice", "file_id": "VID"}]


# ---------------------------------------------------------------------------
# T-0782: a photo must keep its HANDLE, not just its type. The pre-fix code
# recorded `{"type": "photo"}` six lines below the voice branch that keeps
# `file_id`, so the image was unreachable forever. These pin the handle AND
# the variant choice — TG sends an ARRAY of PhotoSize variants, and a silent
# `photo[0]` would store the thumbnail while passing any "a file_id is
# present" assertion.
# ---------------------------------------------------------------------------


def _photo_variants() -> list[dict]:
    """A real TG `photo` array: same image, thumbnail-first (ascending)."""
    return [
        {"file_id": "THUMB_90", "width": 90, "height": 67, "file_size": 1234},
        {"file_id": "MID_320", "width": 320, "height": 240, "file_size": 14567},
        {"file_id": "FULL_1280", "width": 1280, "height": 960, "file_size": 153021},
    ]


def test_append_conversation_captures_photo_file_id(tmp_path, monkeypatch):
    """The whole ticket, at the seam it actually ships through."""
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    msg = {"from": _from(), "photo": _photo_variants()}
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert captured["json"]["attachments"] == [
        {"type": "photo", "file_id": "FULL_1280"}
    ]


def test_photo_keeps_the_largest_variant_not_the_first():
    assert TL._msg_attachments({"photo": _photo_variants()}) == [
        {"type": "photo", "file_id": "FULL_1280"}
    ]


def test_photo_largest_is_measured_not_positional():
    """Same variants, DESCENDING. A "take the last one" shortcut passes the
    ascending case and stores the thumbnail here."""
    assert TL._msg_attachments({"photo": list(reversed(_photo_variants()))}) == [
        {"type": "photo", "file_id": "FULL_1280"}
    ]


def test_photo_without_dimensions_falls_back_to_file_size():
    photo = [
        {"file_id": "SMALL", "file_size": 900},
        {"file_id": "BIG", "file_size": 90000},
        {"file_id": "MEDIUM", "file_size": 9000},
    ]
    assert TL._msg_attachments({"photo": photo}) == [
        {"type": "photo", "file_id": "BIG"}
    ]


def test_photo_without_any_size_hint_degrades_to_last_not_thumbnail():
    """No width/height/file_size anywhere: TG's own ascending order is the only
    signal left, so the biggest is the LAST — never `photo[0]`."""
    photo = [{"file_id": "A"}, {"file_id": "B"}, {"file_id": "C"}]
    assert TL._msg_attachments({"photo": photo}) == [
        {"type": "photo", "file_id": "C"}
    ]


def test_no_photo_records_no_descriptor():
    """Absent records as absent — an empty array is not a photo whose id we
    lost, and must not produce a default-shaped descriptor."""
    assert TL._msg_attachments({"photo": []}) == []
    assert TL._msg_attachments({"text": "hi"}) == []


def test_photo_with_no_usable_file_id_states_why():
    """The marker survives (losing the FACT would be worse than the pre-fix
    behaviour) and says WHY it has no handle, so an id-less record can never
    be misread as "we dropped the id"."""
    assert TL._msg_attachments({"photo": [{"width": 90, "height": 67}]}) == [
        {"type": "photo", "file_id_unknown_reason": TL.PHOTO_UNKNOWN_NO_FILE_ID}
    ]
    assert TL._msg_attachments({"photo": "not-an-array"}) == [
        {"type": "photo", "file_id_unknown_reason": TL.PHOTO_UNKNOWN_NOT_A_LIST}
    ]


def test_photo_with_junk_dimensions_still_routes():
    """This runs on the inbound routing path, which does NOT wrap the call —
    so junk sizes must degrade the CHOICE, never raise and lose the message."""
    photo = [
        {"file_id": "A", "width": {"bad": 1}, "height": None},
        {"file_id": "B", "width": "not-a-number", "file_size": "12"},
    ]
    assert TL._msg_attachments({"photo": photo}) == [
        {"type": "photo", "file_id": "B"}
    ]


def test_photo_fix_leaves_the_voice_branch_alone():
    """The asymmetry is the bug; the voice side is the working half and his
    highest-value input. A media-group-ish message keeps BOTH handles."""
    msg = {
        "voice": {"file_id": "VID", "duration": 3},
        "document": {"file_id": "DID"},
        "photo": _photo_variants(),
    }
    assert TL._msg_attachments(msg) == [
        {"type": "voice", "file_id": "VID"},
        {"type": "document", "file_id": "DID"},
        {"type": "photo", "file_id": "FULL_1280"},
    ]


# ---------------------------------------------------------------------------
# T-0787 — the unfixed twin of T-0782/0785/0786. `_msg_attachments` knew three
# keys, so a video/animation/sticker/video_note/audio returned NO descriptor:
# the durable record said nothing arrived, and T-0785's ATTACHED section never
# fired, so an UNCAPTIONED one still woke a session with an empty fence.
#
# RED AT e243dae: every test in this block down to (and including)
# `test_several_media_keys_all_keep_their_handles`.
# GREEN AT e243dae (regression guards, not defect pins): the three after it —
# they pin what must NOT change.
# ---------------------------------------------------------------------------


def test_video_animation_sticker_video_note_and_audio_keep_their_handles():
    """The whole ticket at the function. Each of these returned `[]` before —
    not a lossy descriptor, NONE."""
    assert TL._msg_attachments({"video": {"file_id": "VID_H", "duration": 7}}) == [
        {"type": "video", "file_id": "VID_H"}]
    assert TL._msg_attachments({"animation": {"file_id": "ANIM_H"}}) == [
        {"type": "animation", "file_id": "ANIM_H"}]
    assert TL._msg_attachments({"sticker": {"file_id": "STK_H", "emoji": "🔥"}}) == [
        {"type": "sticker", "file_id": "STK_H"}]
    assert TL._msg_attachments({"video_note": {"file_id": "VN_H"}}) == [
        {"type": "video_note", "file_id": "VN_H"}]
    assert TL._msg_attachments({"audio": {"file_id": "AUD_H", "title": "t"}}) == [
        {"type": "audio", "file_id": "AUD_H"}]


def test_media_keeps_its_own_handle_not_the_thumbnail_one():
    """A video carries a nested `thumbnail` with its OWN file_id. Taking that
    would pass any "a file_id is present" assertion while recording a lossy
    substitute indistinguishable from the real thing — `_largest_photo`'s
    reason for refusing `photo[0]`, one media type over."""
    msg = {"video": {"file_id": "VIDEO_HANDLE",
                     "thumbnail": {"file_id": "VTHUMB"},
                     "thumb": {"file_id": "VTHUMB_LEGACY"}}}
    assert TL._msg_attachments(msg) == [{"type": "video", "file_id": "VIDEO_HANDLE"}]


def test_id_less_media_states_why_instead_of_reading_as_a_dropped_id():
    """The marker survives — losing the FACT would be worse than the bug being
    fixed — and names why it has no handle, in T-0782's `PHOTO_UNKNOWN_*`
    style. An absent handle must never read as a dropped one."""
    assert TL._msg_attachments({"video": {"duration": 7}}) == [
        {"type": "video", "file_id_unknown_reason": TL.MEDIA_UNKNOWN_NO_FILE_ID}]
    assert TL._msg_attachments({"sticker": "not-an-object"}) == [
        {"type": "sticker", "file_id_unknown_reason": TL.MEDIA_UNKNOWN_NOT_AN_OBJECT}]


def test_id_less_media_descriptor_renders_as_an_attached_line():
    """The reason has to survive the RENDERER too — T-0785 owns the wording and
    T-0787 invents no new mechanism, so an id-less video must read as "Telegram
    gave no usable file_id", never as a blank one."""
    from bot_squad_worker import tg_direct_reply as TDR
    lines = "\n".join(TDR.render_attachments(
        TL._msg_attachments({"video": {"duration": 7}})))
    assert "video" in lines and "no usable file_id" in lines
    assert TL.MEDIA_UNKNOWN_NO_FILE_ID in lines


def test_several_media_keys_all_keep_their_handles():
    """The voice+document+photo precedent, extended. Order is voice first, so a
    message that was already recorded correctly keeps its exact record."""
    msg = {
        "voice": {"file_id": "VOICE_H", "duration": 3},
        "document": {"file_id": "DOC_H"},
        "photo": _photo_variants(),
        "video": {"file_id": "VID_H"},
        "sticker": {"file_id": "STK_H"},
    }
    assert TL._msg_attachments(msg) == [
        {"type": "voice", "file_id": "VOICE_H"},
        {"type": "document", "file_id": "DOC_H"},
        {"type": "video", "file_id": "VID_H"},
        {"type": "sticker", "file_id": "STK_H"},
        {"type": "photo", "file_id": "FULL_1280"},
    ]


def test_a_real_voice_note_descriptor_is_unchanged():
    """GREEN AT e243dae. The voice path is the working half and his
    highest-value input; this ticket must not move it. Byte-identical, alone
    and beside the other two branches that already existed."""
    assert TL._msg_attachments({"voice": {"file_id": "VID", "duration": 3}}) == [
        {"type": "voice", "file_id": "VID"}]
    assert TL._msg_attachments({
        "voice": {"file_id": "VID", "duration": 3},
        "document": {"file_id": "DID"},
        "photo": _photo_variants(),
    }) == [
        {"type": "voice", "file_id": "VID"},
        {"type": "document", "file_id": "DID"},
        {"type": "photo", "file_id": "FULL_1280"},
    ]


def test_absent_media_stays_absent():
    """GREEN AT e243dae. Absent is not "a handle we lost", so no descriptor —
    including the falsy-but-present shapes (`{}`, `[]`) an empty `photo` array
    already followed."""
    assert TL._msg_attachments({"text": "hi"}) == []
    assert TL._msg_attachments({"video": {}, "sticker": None, "photo": []}) == []


def test_types_without_a_file_id_are_still_recorded_as_nothing():
    """GREEN AT e243dae, and deliberately so. `location`/`contact`/`venue`/
    `poll`/`dice` carry NO file_id — what their handle would be is a separate
    judgement call (T-0787 scope: "say so rather than silently skipping"). If
    one of them should be recorded, that is its own ticket, not a quiet branch
    added here."""
    assert TL._msg_attachments({"location": {"latitude": 1.0, "longitude": 2.0}}) == []
    assert TL._msg_attachments({"contact": {"phone_number": "+1"}}) == []
    assert TL._msg_attachments({"poll": {"question": "?"}}) == []
    assert TL._msg_attachments({"dice": {"value": 6}}) == []


def test_append_conversation_noop_without_env(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch, base=None, token=None)
    msg = {"from": _from(), "text": "hi"}
    with patch("httpx.post", side_effect=AssertionError("must not POST")):
        assert TL.append_conversation(cfg, "test-project", "gu_abc", msg) is None


def test_append_conversation_noop_without_gid(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    msg = {"from": _from(), "text": "hi"}
    with patch("httpx.post", side_effect=AssertionError("must not POST")):
        assert TL.append_conversation(cfg, "test-project", "", msg) is None


def test_append_conversation_swallows_http_error(tmp_path, monkeypatch):
    import httpx
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    msg = {"from": _from(), "text": "hi"}
    with patch("httpx.post", side_effect=httpx.ConnectError("down")):
        assert TL.append_conversation(cfg, "test-project", "gu_abc", msg) is None


# ---------------------------------------------------------------------------
# T-0660: append_conversation_fyi — a passive, non-actionable append into a
# project's attendant thread, prefixed unambiguously and marked fyi=True.
# ---------------------------------------------------------------------------


def test_append_conversation_fyi_posts_prefixed_marked_payload(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    with patch("httpx.post", side_effect=fake_post):
        ok = TL.append_conversation_fyi(
            cfg, "test-project", "gu_abc",
            author="session:S-dev-p9", text="on it, fixing now",
        )

    assert ok is True
    assert captured["url"] == "https://mship.test/api/m/worker/conversations/test-project/gu_abc/messages"
    assert captured["json"]["author"] == "session:S-dev-p9"
    assert captured["json"]["fyi"] is True
    assert captured["json"]["text"].startswith("[FYI — ответ не требуется]")
    assert "on it, fixing now" in captured["json"]["text"]
    assert captured["headers"]["Authorization"] == "Bearer WTOKEN"


def test_append_conversation_fyi_noop_without_env(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch, base=None, token=None)
    with patch("httpx.post", side_effect=AssertionError("must not POST")):
        assert TL.append_conversation_fyi(cfg, "test-project", "gu_abc", author="user", text="x") is None


def test_append_conversation_fyi_noop_without_gid(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    with patch("httpx.post", side_effect=AssertionError("must not POST")):
        assert TL.append_conversation_fyi(cfg, "test-project", "", author="user", text="x") is None


def test_append_conversation_fyi_swallows_http_error(tmp_path, monkeypatch):
    import httpx
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    with patch("httpx.post", side_effect=httpx.ConnectError("down")):
        assert TL.append_conversation_fyi(cfg, "test-project", "gu_abc", author="user", text="x") is None


def test_handle_update_records_conversation(tmp_path, monkeypatch):
    """An inbound message with a recognized sender is recorded to the store."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    monkeypatch.setattr(
        TL, "resolve_or_link_sender",
        lambda c, m, slug: {"global_user_id": "gu_zzz", "created": False, "slug": slug},
    )
    recorded = []
    monkeypatch.setattr(
        TL, "append_conversation",
        lambda c, slug, gid, msg: recorded.append((slug, gid, msg.get("text"))),
    )
    update = {"update_id": 9, "message": {"chat": {"id": 12345}, "from": _from(), "text": "hello"}}
    TL.handle_update(cfg, update)
    assert recorded == [("test-project", "gu_zzz", "hello")]


def test_handle_update_no_record_without_identity(tmp_path, monkeypatch):
    """No recognized sender (linkage off / failed) => nothing recorded."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    monkeypatch.setattr(TL, "resolve_or_link_sender", lambda c, m, slug: None)
    called = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, msg: called.append(1))
    update = {"update_id": 9, "message": {"chat": {"id": 12345}, "from": _from(), "text": "hi"}}
    TL.handle_update(cfg, update)
    assert called == []


# ---------------------------------------------------------------------------
# T-0492: hardwired project routing. A user pins a current project (button /
# /project <slug>); subsequent unquoted messages sticky-route to it; the bot
# ASKS which project when unset. The pin is read/written through the API
# (single-writer = API, pins_store), env-gated + best-effort.
# ---------------------------------------------------------------------------


def _make_multi_cfg(tmp_path, *, chat="111"):
    cfg = _make_cfg(tmp_path, tg_chat=chat)
    cfg.projects = {
        "alpha": types.SimpleNamespace(tg_chat="111"),
        "beta": types.SimpleNamespace(tg_chat="222"),
    }
    return cfg


def test_extract_slash_command_project():
    assert TL.extract_slash_command(_slash_message("/project beta")) == ("project", "beta")
    assert TL.extract_slash_command(_slash_message("/project")) == ("project", "")


def test_get_current_project_http(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"slug": "beta"}
        return resp

    with patch("httpx.get", side_effect=fake_get):
        assert TL.get_current_project(cfg, "gu_1") == "beta"
    assert captured["url"] == "https://mship.test/api/m/worker/routing/gu_1/current-project"


def test_get_current_project_noop_without_env(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path)
    _link_env(monkeypatch, base=None, token=None)
    with patch("httpx.get", side_effect=AssertionError("must not GET")):
        assert TL.get_current_project(cfg, "gu_1") is None


def test_set_current_project_http(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"slug": "beta", "at": "t"}
        return resp

    with patch("httpx.post", side_effect=fake_post):
        assert TL.set_current_project(cfg, "gu_1", "beta") is True
    assert captured["url"] == "https://mship.test/api/m/worker/routing/gu_1/current-project"
    assert captured["json"] == {"slug": "beta"}


def test_handle_update_project_command_pins(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    set_calls = []
    monkeypatch.setattr(TL, "set_current_project",
                        lambda c, gid, slug: set_calls.append((gid, slug)) or True)
    notify = []
    monkeypatch.setattr(TL, "_notify", lambda c, chat, text, **k: notify.append(text))

    update = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "/project beta"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "project_set"
    assert result["slug"] == "beta"
    assert set_calls == [("gu_1", "beta")]
    assert any("beta" in t for t in notify)  # "you're on project beta"


def test_handle_update_project_command_in_topic_replies_into_same_topic(tmp_path, monkeypatch):
    """T-0676 item 3 (misrouted reply): a /project command typed inside a
    forum topic must get its confirmation delivered back into THAT topic —
    before this fix, _notify/_channel_notify dropped message_thread_id
    entirely, so every command reply landed in the chat's general feed no
    matter which topic triggered it."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "set_current_project", lambda c, gid, slug: True)
    notices = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat, text, **k: notices.append((chat, text, k)))

    msg = _topic_msg("/project beta", chat_id=111, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "project_set"
    assert notices and notices[-1][2].get("thread_id") == 42


def test_handle_update_project_command_unknown_slug(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "set_current_project",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not set")))
    asks = []
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat, **k: asks.append(chat))
    monkeypatch.setattr(TL, "_notify", lambda c, chat, text, **k: None)

    update = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "/project ghost"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "project_unknown"
    assert asks == ["111"]  # re-offered the picker


def test_handle_update_project_command_no_args_asks(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    asks = []
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat, **k: asks.append(chat))

    update = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "/project"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "ask_project"
    assert asks == ["111"]


def test_handle_update_unquoted_sticky_routes_to_pinned(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    # The user's pinned project is beta — even though the message arrived in
    # alpha's chat, sticky routing wins (hardwired, voice-04).
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "beta")
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    update = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "do the thing"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "route"
    assert result["slug"] == "beta"
    assert result["global_user_id"] == "gu_1"


def test_handle_update_unquoted_switch_reroutes(tmp_path, monkeypatch):
    """pin -> route -> switch -> route: the second message follows the switch."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    pinned = {"slug": "alpha"}
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: pinned["slug"])
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    u1 = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "m1"}}
    assert TL.handle_update(cfg, u1)["slug"] == "alpha"
    pinned["slug"] = "beta"  # user switched
    u2 = {"update_id": 2, "message": {"chat": {"id": 111}, "from": _from(), "text": "m2"}}
    assert TL.handle_update(cfg, u2)["slug"] == "beta"


def test_handle_update_unquoted_unset_asks(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: None)
    asks = []
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat, **k: asks.append(chat))

    update = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "hi there"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "ask_project"
    assert asks == ["111"]


def test_handle_update_unquoted_no_identity_still_skips(tmp_path, monkeypatch):
    """No recognized sender (linkage off) => old skip behavior, no routing."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender", lambda c, m, slug: None)
    monkeypatch.setattr(TL, "get_current_project",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not route")))
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "hi"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "skip"


# ---------------------------------------------------------------------------
# T-0485: firehose ingest — wire the unquoted message to a (continued-or-
# spawned) user-conversation session via ensure_user_conversation.
# ---------------------------------------------------------------------------


def _dated_msg(text="do the thing", chat_id=111, date=1_700_000_000):
    return {"chat": {"id": chat_id}, "from": _from(), "text": text, "date": date}


def test_handle_update_unquoted_routes_to_user_conversation(tmp_path, monkeypatch):
    """An unquoted message from a recognized user reaches a user-conversation
    session via ensure_user_conversation(slug, global_user_id, message_ref),
    where message_ref points at the just-appended store record (its timestamp =
    its key in the (slug,gid) thread)."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "beta")
    calls = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: calls.append((slug, gid, ref)))

    msg = _dated_msg()
    update = {"update_id": 1, "message": msg}
    result = TL.handle_update(cfg, update)

    assert result["action"] == "route" and result["slug"] == "beta"
    # The wiring fired once with (routed slug, gid, message_ref=store-record ts).
    assert calls == [("beta", "gu_1", TL._msg_ts(msg))]


def test_handle_update_unquoted_burst_routes_to_one_session(tmp_path, monkeypatch):
    """A burst from the same user routes every message to ONE attendant — each
    call carries the same (slug, gid), so ensure_user_conversation's idempotent
    single-attendant reuse keeps it one session (no fan-out)."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "beta")
    routed = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: routed.append((slug, gid)))

    for i, txt in enumerate(["problem 1", "problem 2", "problem 3"]):
        TL.handle_update(cfg, {"update_id": i, "message": _dated_msg(text=txt)})

    # Every message in the burst targets the same (slug, gid) attendant key.
    assert routed == [("beta", "gu_1")] * 3


def test_ensure_user_conversation_dispatches_action(tmp_path, monkeypatch):
    """The helper dispatches the ensure_user_conversation worker action with
    exactly (slug, global_user_id, message_ref)."""
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    captured = {}
    monkeypatch.setattr(A, "dispatch",
                        lambda name, params: captured.update(name=name, params=params)
                        or {"ok": True, "sid": "S-x", "spawned": True})

    TL._ensure_user_conversation(cfg, "beta", "gu_1", "2026-06-27T00:00:00Z")

    assert captured["name"] == "ensure_user_conversation"
    assert captured["params"] == {
        "slug": "beta", "global_user_id": "gu_1",
        "message_ref": "2026-06-27T00:00:00Z",
    }


def test_ensure_user_conversation_best_effort_swallows(tmp_path, monkeypatch):
    """A spawn/pane hiccup must never break inbound routing — the message is
    already durable in the store (T-0489), so the helper swallows and returns
    None rather than propagating."""
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(A, "dispatch",
                        lambda name, params: (_ for _ in ()).throw(A.ActionError("no pane")))

    assert TL._ensure_user_conversation(cfg, "beta", "gu_1", "ref") is None


# ---------------------------------------------------------------------------
# T-0494: misattribution guard — a message explicitly targeting a different
# (accessible) project than the pinned one triggers a reroute-confirm offer
# (not a silent misroute).
# ---------------------------------------------------------------------------


def test_detect_cross_project_target_explicit_mention(tmp_path):
    cfg = _make_multi_cfg(tmp_path)  # projects: alpha, beta
    # "in beta I see ..." while pinned to alpha -> target beta.
    assert TL._detect_cross_project_target(cfg, "in beta I see a bug", "alpha") == "beta"
    # cue synonyms.
    assert TL._detect_cross_project_target(cfg, "regarding beta: broken", "alpha") == "beta"


def test_detect_cross_project_target_dash_space_tolerant(tmp_path):
    cfg = _make_multi_cfg(tmp_path)
    cfg.projects = {
        "alpha": types.SimpleNamespace(tg_chat="111"),
        "bot-squad": types.SimpleNamespace(tg_chat="222"),
    }
    # The slug's dash may be typed as a space (voice-02: "in bot-squad I see").
    assert TL._detect_cross_project_target(cfg, "in bot squad I see X", "alpha") == "bot-squad"
    assert TL._detect_cross_project_target(cfg, "in bot-squad I see X", "alpha") == "bot-squad"


def test_detect_cross_project_target_no_false_positive(tmp_path):
    cfg = _make_multi_cfg(tmp_path)
    # No cue + slug == the pinned one, or an incidental mention without a cue.
    assert TL._detect_cross_project_target(cfg, "fix the deploy timeout", "alpha") is None
    assert TL._detect_cross_project_target(cfg, "in alpha I see a bug", "alpha") is None
    # An incidental mid-sentence mention without a targeting cue is not a target.
    assert TL._detect_cross_project_target(cfg, "the alpha build mentions beta colors", "alpha") is None


def test_handle_update_unquoted_cross_project_mention_routes_no_reroute(tmp_path, monkeypatch):
    """T-0666: a message explicitly mentioning another project no longer
    triggers a reroute-confirm prompt (stakeholder found it disruptive,
    'отключи, мешаются') — it routes straight to the pinned project-of-record
    like any other unquoted message. ``_offer_reroute``/``_user_can_access_project``
    are gone entirely; ``_detect_cross_project_target`` is retained (D-0055 §2 /
    T-0640 reuse) but no longer wired to any prompt."""
    assert not hasattr(TL, "_offer_reroute")
    assert not hasattr(TL, "_user_can_access_project")

    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")  # pinned alpha
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))
    notified = []
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: notified.append(a))

    msg = _dated_msg(text="in beta I see the buttons overlap")
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route" and result["slug"] == "alpha"
    assert ensures == [("alpha", "gu_1")]  # routed straight to the POR, no confirm gate
    assert notified == []  # no reroute-confirm prompt sent


def test_handle_update_unquoted_no_cross_mention_routes_normally(tmp_path, monkeypatch):
    """No explicit cross-project mention -> ordinary T-0485 routing, unchanged."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))

    msg = _dated_msg(text="fix the deploy timeout bug")
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route" and result["slug"] == "alpha"
    assert ensures == [("alpha", "gu_1")]


# ---------------------------------------------------------------------------
# T-0639: gateway topic-supergroup routing — the runtime (chat_id,thread_id)
# binding store (tg_bindings.py) is consulted FIRST in project-of-record
# resolution, ahead of both the legacy static tg_chat map AND the sticky pin;
# a bound chat is folded into the listener's allowlist even when it isn't any
# project's static tg_chat.
# ---------------------------------------------------------------------------


def _topic_msg(text, chat_id, thread_id):
    return {
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": _from(),
        "text": text,
        "date": 1_700_000_000,
        "message_thread_id": thread_id,
    }


def test_handle_update_allowlist_admits_bound_chat_outside_projects(tmp_path, monkeypatch):
    """A chat_id with a topic binding is allowlisted even though it is not any
    project's static tg_chat (D-0055 §3: the supergroup isn't project.tg_chat)."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")  # projects alpha(111)/beta(222)
    tg_bindings.set_binding(cfg, "999", 42, "alpha")  # a THIRD, unregistered chat

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _topic_msg("hello", chat_id=999, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route_bound_topic" and result["slug"] == "alpha"


def test_handle_update_unbound_unregistered_chat_still_skips(tmp_path):
    """No regression: an unregistered chat WITHOUT a binding is still rejected."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    msg = _topic_msg("hello", chat_id=999, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})
    assert result == {"ok": True, "action": "skip", "reason": "chat 999 not allowlisted"}


def test_handle_update_bound_topic_wins_over_static_slug_and_pin(tmp_path, monkeypatch):
    """Resolver precedence: a bound topic routes to the BOUND slug — not the
    chat's static tg_chat slug, and not the user's sticky pin. Binding is the
    strongest signal (D-0055 §2 step 1)."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")  # chat 111's static slug is alpha
    tg_bindings.set_binding(cfg, "111", 7, "beta")  # topic 7 in that SAME chat -> beta

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append(slug))
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")  # pinned alpha
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))

    msg = _topic_msg("what's the status", chat_id=111, thread_id=7)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route_bound_topic"
    assert result["slug"] == "beta"  # binding beats BOTH the static slug and the pin
    assert appended == ["beta"]
    assert ensures == [("beta", "gu_1")]


def test_handle_update_unbound_thread_falls_back_to_static_slug(tmp_path, monkeypatch):
    """No binding for this (chat_id, thread_id) -> legacy static tg_chat
    resolution + the existing pin-based routing, unaffected."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))

    msg = _topic_msg("no binding for this thread", chat_id=111, thread_id=999)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route" and result["slug"] == "alpha"
    assert ensures == [("alpha", "gu_1")]


# ---------------------------------------------------------------------------
# T-0693: an unbound topic in a chat that ALREADY does per-topic routing must
# NOT silently impersonate the chat's static default project (the live
# incident this closes — a stakeholder's watchrobot question in a
# freshly-created, not-yet-bound topic was mis-slugged into bot-squad's
# conversation store with zero error signal). A chat that has NEVER used
# per-topic binding at all keeps the pre-T-0693 fallback (test immediately
# above, unaffected).
# ---------------------------------------------------------------------------


def test_handle_update_unbound_topic_in_topic_routed_chat_held_not_slugged(tmp_path, monkeypatch):
    """Chat 111 already routes topic 7 to `beta` — it's a topic-routed chat.
    A message on topic 999, which was NEVER bound, must be held unrouted, not
    silently mis-slugged to alpha (chat 111's static default project)."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")  # chat 111's static slug is alpha
    tg_bindings.set_binding(cfg, "111", 7, "beta")  # SOME other topic IS bound

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not resolve identity for a held message")))
    monkeypatch.setattr(TL, "append_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not append a held message anywhere")))
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not wake any attendant for a held message")))
    notified = []
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: notified.append((a, k)))

    msg = _topic_msg("watchrobot question", chat_id=111, thread_id=999)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "unbound_topic_held"
    assert "111" in result["reason"] and "999" in result["reason"]
    # The sender is told, IN THE SAME TOPIC, rather than routed silently.
    assert len(notified) == 1
    assert notified[0][1].get("thread_id") == 999


def test_handle_update_unbound_topic_logs_warning(tmp_path, caplog):
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")

    msg = _topic_msg("watchrobot question", chat_id=111, thread_id=999)
    with caplog.at_level("WARNING"):
        TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert any("UNBOUND TOPIC" in r.message and "111" in r.message and "999" in r.message
               for r in caplog.records)


def test_handle_update_unbound_topic_writes_discovery_record(tmp_path):
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")

    msg = _topic_msg("watchrobot question", chat_id=111, thread_id=999)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    import json
    record = json.loads(TL._unbound_topics_path(cfg).read_text())
    entry = record["111:999"]
    assert entry["chat_id"] == "111" and entry["thread_id"] == 999
    assert entry["count"] == 1
    assert entry["first_seen_at"] == entry["last_seen_at"]


def test_handle_update_unbound_topic_second_message_updates_in_place(tmp_path, monkeypatch):
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")

    msg = _topic_msg("watchrobot question", chat_id=111, thread_id=999)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    times = iter(["2026-07-26T00:00:00Z", "2026-07-26T00:05:00Z"])
    monkeypatch.setattr(TL, "_now_iso", lambda: next(times))
    TL.handle_update(cfg, {"update_id": 2, "message": msg})

    import json
    record = json.loads(TL._unbound_topics_path(cfg).read_text())
    assert len(record) == 1
    assert record["111:999"]["count"] == 2


def test_handle_update_topic_in_never_bound_chat_still_falls_back(tmp_path, monkeypatch):
    """No regression: a chat with NO bindings at all (not `_make_multi_cfg`'s
    topic-routed chat, a plain static per-project one) keeps the exact
    pre-T-0693 fallback — this is the same scenario as
    ``test_handle_update_unbound_thread_falls_back_to_static_slug`` above,
    restated here to sit next to the new unbound-topic-HELD tests so the two
    outcomes (held vs. falls back) are contrasted side by side."""
    cfg = _make_multi_cfg(tmp_path, chat="111")  # no tg_bindings.set_binding calls at all
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))

    msg = _topic_msg("some other topic message", chat_id=111, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route" and result["slug"] == "alpha"
    assert ensures == [("alpha", "gu_1")]


# ---------------------------------------------------------------------------
# T-0700: the SAME failure mode as T-0693 above, but for a General-feed
# message (thread_id=None, no binding) in a chat that already routes topics —
# the T-0693 guard was gated on `thread_id is not None` and so let this door
# stay open. Scoped MULTI-SLUG-ONLY (operator policy call, T-0700 progress
# notes): held only when the chat's bound topics span MORE THAN ONE distinct
# project slug (a genuinely multi-project shared forum); a single-project
# chat's General tab keeps working with zero added friction.
# ---------------------------------------------------------------------------


def test_handle_update_general_feed_multi_project_chat_held_not_slugged(tmp_path, monkeypatch):
    """Chat 111 statically defaults to alpha, but its bound topics span TWO
    distinct slugs (topic 7 -> alpha, topic 8 -> beta) — a genuinely
    multi-project shared forum. A message typed into General (thread_id=None,
    no binding of its own, no pin) must be held unrouted too, not silently
    mis-slugged to alpha."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")  # chat 111's static slug is alpha
    tg_bindings.set_binding(cfg, "111", 7, "alpha")
    tg_bindings.set_binding(cfg, "111", 8, "beta")  # a SECOND distinct slug -> multi-project forum

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not resolve identity for a held message")))
    monkeypatch.setattr(TL, "append_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not append a held message anywhere")))
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not wake any attendant for a held message")))
    notified = []
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: notified.append((a, k)))

    msg = _topic_msg("general feed message", chat_id=111, thread_id=None)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "unbound_topic_held"
    assert "111" in result["reason"]
    # The sender is told, in the same (General) feed, rather than routed silently.
    assert len(notified) == 1
    assert notified[0][1].get("thread_id") is None


def test_handle_update_general_feed_single_project_chat_still_falls_back(tmp_path, monkeypatch):
    """Same topic-routed chat 111, but its bound topics are ALL on the SAME
    slug (a single-project chat that merely uses per-topic binding for its
    own project) — the General tab must keep working exactly as today, zero
    added friction (T-0700 scoping: multi-slug-only, not unconditional)."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "alpha")
    tg_bindings.set_binding(cfg, "111", 8, "alpha")  # SAME slug both times -> single-project forum

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))

    msg = _topic_msg("general feed message", chat_id=111, thread_id=None)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route" and result["slug"] == "alpha"
    assert ensures == [("alpha", "gu_1")]


def test_handle_update_general_feed_genuine_dm_unaffected(tmp_path, monkeypatch):
    """A chat that never appears in bound_chat_ids at all (a genuine DM/plain
    static chat, never touched by topic binding) keeps the exact pre-T-0693/
    T-0700 fallback for its General feed too — unaffected by either guard."""
    cfg = _make_multi_cfg(tmp_path, chat="111")  # no tg_bindings.set_binding calls at all
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))

    msg = _topic_msg("hi", chat_id=111, thread_id=None)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route" and result["slug"] == "alpha"
    assert ensures == [("alpha", "gu_1")]


def test_handle_update_general_feed_explicit_binding_still_routes(tmp_path, monkeypatch):
    """A chat_id with an EXPLICIT General-feed binding ((chat_id, None) ->
    slug, T-0693 Finding B's own addition) resolves it normally — the T-0700
    hold-and-warn only fires when `binding is None`, so an explicit General
    binding short-circuits it regardless of how many distinct slugs the
    chat's other topics span."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "alpha")
    tg_bindings.set_binding(cfg, "111", 8, "beta")     # multi-project forum
    tg_bindings.set_binding(cfg, "111", None, "beta")  # its OWN General-feed binding

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append(slug))
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append((slug, gid)))

    msg = _topic_msg("general feed message", chat_id=111, thread_id=None)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route_bound_topic" and result["slug"] == "beta"
    assert appended == ["beta"]
    assert ensures == [("beta", "gu_1")]


def test_handle_update_multi_chat_per_project_binding(tmp_path, monkeypatch):
    """A project may have MULTIPLE bound (chat_id, thread_id) entries (D-0055
    §3, explicit stakeholder requirement — not 1:1); both route to it."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 1, "beta")
    tg_bindings.set_binding(cfg, "111", 2, "beta")

    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: ensures.append(slug))

    for thread in (1, 2):
        result = TL.handle_update(
            cfg, {"update_id": 1, "message": _topic_msg("x", 111, thread)})
        assert result["action"] == "route_bound_topic" and result["slug"] == "beta"
    assert ensures == ["beta", "beta"]


def test_handle_topic_bound_no_identity_skips(tmp_path, monkeypatch):
    """No resolved sender identity -> skip, same guard as _handle_unquoted."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")
    monkeypatch.setattr(TL, "resolve_or_link_sender", lambda c, m, slug: None)

    msg = _topic_msg("hi", chat_id=111, thread_id=7)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})
    assert result == {"ok": True, "action": "skip", "reason": "not a reply or command"}


def test_handle_topic_bound_parked_notifies_user(tmp_path, monkeypatch):
    """T-0570 parity: a bound-topic route refused under backoff/saturation
    still tells the user — no silent drop."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: {"ok": False, "parked": True})
    notices = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat_id, text, **k: notices.append((chat_id, text, k)))

    msg = _topic_msg("x", chat_id=111, thread_id=7)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route_parked" and result["slug"] == "beta"
    assert len(notices) == 1 and notices[0][0] == "111"
    assert "заняты" in notices[0][1]
    # T-0676 item 3: the parked ack must land back in the SAME forum topic
    # the message arrived on, not the chat's general feed.
    assert notices[0][2].get("thread_id") == 7


# ---------------------------------------------------------------------------
# T-0660 Phase 2: a per-TASK topic binding (carries a session_id) routes an
# unquoted message straight to the ORIGINATING session by id — reusing the
# same inject_input path an explicit [<sid>] reply uses — never through the
# project's user-conversation attendant, and does NOT touch the conversation
# locus (that must stay pointed at the project's General room).
# ---------------------------------------------------------------------------


def test_handle_topic_bound_with_session_id_injects_to_that_session(tmp_path, monkeypatch):
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", ticket_id="T-0700", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    injected = []
    monkeypatch.setattr(A, "dispatch", lambda name, params: (
        injected.append((name, params)) or {"ok": True}
    ))
    # These must NOT be called for a task-topic route.
    monkeypatch.setattr(TL, "append_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not append to project thread")))
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not wake the attendant")))

    msg = _topic_msg("fix the flaky test please", chat_id=111, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["ok"] is True
    assert result["action"] == "task_topic_inject"
    assert result["sid"] == "S-dev-p9"
    assert result["slug"] == "beta"
    # T-0770: this used to assert the payload was the BARE text — that pin was
    # the defect, written down. What is delivered now is the provenance
    # envelope, as ONE block (`inject_prompt`, not one-Enter-per-line
    # `inject_input`), and his words are inside it verbatim.
    assert len(injected) == 1
    verb, params = injected[0]
    assert verb == "inject_prompt"
    assert params["sid"] == "S-dev-p9"
    assert "fix the flaky test please" in params["text"]
    assert "topic 42" in params["text"]
    assert 'bsq topic say --chat 111 --topic 42' in params["text"]


def test_handle_topic_bound_photo_reaches_the_session_as_more_than_empty(tmp_path, monkeypatch):
    """T-0785, the defect at the seam it ships through — the FULL inbound path.

    A photo carries no `text`, and this branch forwards `msg["text"]` only, so
    at 7deced6 the session was woken by an envelope whose verbatim fence was
    empty: not "he sent a photo", not a marker, nothing to react to. Measured
    that way against HEAD before the fix, with a text message through the same
    probe as the control.

    What it now says is that a photo ARRIVED and that the session cannot open
    it — there is no download path behind a photo (T-0782 (b)). The file_id is
    T-0782's descriptor, carried so a later fetch is possible at all, NOT
    because anything fetches today."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", ticket_id="T-0785", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: True)
    injected = []
    monkeypatch.setattr(A, "dispatch", lambda name, params: (
        injected.append((name, params)) or {"ok": True}
    ))

    msg = _topic_msg(None, chat_id=111, thread_id=42)
    del msg["text"]                      # a photo-only message has no text key
    msg["photo"] = _photo_variants()
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "task_topic_inject" and result["sid"] == "S-dev-p9"
    assert len(injected) == 1
    verb, params = injected[0]
    assert verb == "inject_prompt"
    body = params["text"]
    assert "photo" in body
    # The LARGEST variant's handle (T-0782's decision), not the thumbnail.
    assert "FULL_1280" in body and "THUMB_90" not in body
    # …and it must not read as "photos work now".
    assert "CANNOT open it" in body


def test_handle_topic_bound_text_message_envelope_unchanged_by_attachments(tmp_path, monkeypatch):
    """Green regression guard, not a defect pin: an ordinary typed message has
    no attachment, so its envelope must be exactly what T-0780 left it."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", ticket_id="T-0785", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: True)
    injected = []
    monkeypatch.setattr(A, "dispatch", lambda name, params: (
        injected.append((name, params)) or {"ok": True}
    ))

    msg = _topic_msg("fix the flaky test please", chat_id=111, thread_id=42)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    body = injected[0][1]["text"]
    assert "fix the flaky test please" in body
    assert "ATTACHED" not in body


def test_handle_topic_bound_video_reaches_the_session_as_more_than_empty(tmp_path, monkeypatch):
    """T-0787 at the seam it ships through — the FULL inbound path, RED at
    e243dae. An UNCAPTIONED video into a session-bound task topic produced no
    descriptor, so T-0785's ATTACHED section never rendered and the session was
    woken by an envelope whose verbatim fence was empty. Measured with a photo
    through the same probe as the control (the test below): the photo arm
    produced a descriptor in the same run, so this arm's `[]` is a reading and
    not a blind instrument."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", ticket_id="T-0787", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: True)
    injected = []
    monkeypatch.setattr(A, "dispatch", lambda name, params: (
        injected.append((name, params)) or {"ok": True}
    ))

    msg = _topic_msg(None, chat_id=111, thread_id=42)
    del msg["text"]                      # an uncaptioned video has no text key
    msg["video"] = {"file_id": "VIDEO_HANDLE", "duration": 7,
                    "thumbnail": {"file_id": "VTHUMB"}}
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "task_topic_inject" and result["sid"] == "S-dev-p9"
    body = injected[0][1]["text"]
    assert "ATTACHED to that message" in body
    assert "video" in body and "VIDEO_HANDLE" in body
    assert "VTHUMB" not in body          # the media's own handle, not its thumb
    # …and it must not read as "videos work now". Nothing downloads anything.
    assert "CANNOT open it" in body


def test_handle_topic_bound_sticker_and_animation_also_reach_the_session(tmp_path, monkeypatch):
    """RED at e243dae. The same for the rest of the file_id-bearing types —
    a partial fix is what produced this ticket in the first place."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    for key, media, handle in (
        ("sticker", {"file_id": "STK_HANDLE", "emoji": "🔥"}, "STK_HANDLE"),
        ("animation", {"file_id": "ANIM_HANDLE"}, "ANIM_HANDLE"),
        ("video_note", {"file_id": "VN_HANDLE"}, "VN_HANDLE"),
        ("audio", {"file_id": "AUD_HANDLE", "title": "t"}, "AUD_HANDLE"),
    ):
        cfg = _make_multi_cfg(tmp_path / key, chat="111")
        tg_bindings.set_binding(cfg, "111", 42, "beta",
                                ticket_id="T-0787", session_id="S-dev-p9")
        monkeypatch.setattr(TL, "resolve_or_link_sender",
                            lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
        monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: True)
        injected = []
        monkeypatch.setattr(A, "dispatch", lambda name, params: (
            injected.append((name, params)) or {"ok": True}
        ))

        msg = _topic_msg(None, chat_id=111, thread_id=42)
        del msg["text"]
        msg[key] = media
        TL.handle_update(cfg, {"update_id": 1, "message": msg})

        body = injected[0][1]["text"]
        assert "ATTACHED to that message" in body, key
        assert key in body and handle in body, key


def test_handle_topic_bound_photo_control_discriminates(tmp_path, monkeypatch):
    """The CONTROL for the two above, in the same file and the same shape: at
    e243dae a photo through this probe DID produce an ATTACHED section (T-0782/
    T-0785) while the video produced none. Two arms agreeing on nothing would
    be a blind instrument, not a result. GREEN at e243dae — this one is the
    instrument's positive control, not a defect pin."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", ticket_id="T-0787", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: True)
    injected = []
    monkeypatch.setattr(A, "dispatch", lambda name, params: (
        injected.append((name, params)) or {"ok": True}
    ))

    msg = _topic_msg(None, chat_id=111, thread_id=42)
    del msg["text"]
    msg["photo"] = _photo_variants()
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    body = injected[0][1]["text"]
    assert "ATTACHED to that message" in body and "FULL_1280" in body


def test_append_conversation_records_a_video_handle(tmp_path, monkeypatch):
    """RED at e243dae, on the durable-record side of the same defect: the
    store said `attachments: []` — not lossy-but-honest, false. The thread we
    "can always look up" (voice-04) recorded that nothing arrived."""
    cfg = _make_cfg(tmp_path)
    _link_env(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    msg = {"from": _from(), "video": {"file_id": "VIDEO_HANDLE", "duration": 7}}
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert captured["json"]["attachments"] == [
        {"type": "video", "file_id": "VIDEO_HANDLE"}]


def test_handle_topic_bound_with_session_id_does_not_touch_locus(tmp_path, monkeypatch):
    from bot_squad_worker import tg_bindings, conversation_locus
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    # The project's own General is elsewhere; a task topic must not steal it.
    conversation_locus.set_locus(cfg, "beta", "gu_1", "999", None)
    tg_bindings.set_binding(cfg, "111", 42, "beta", ticket_id="T-0700", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True})

    msg = _topic_msg("status?", chat_id=111, thread_id=42)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert conversation_locus.get_locus(cfg, "beta", "gu_1") == {
        "chat_id": "999", "thread_id": None,
        "at": conversation_locus.get_locus(cfg, "beta", "gu_1")["at"],
    }


def test_handle_topic_bound_with_session_id_no_identity_still_skips(tmp_path, monkeypatch):
    """The identity guard applies uniformly — a task topic doesn't bypass it."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender", lambda c, m, slug: None)

    msg = _topic_msg("hi", chat_id=111, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})
    assert result == {"ok": True, "action": "skip", "reason": "not a reply or command"}


def test_handle_topic_bound_session_id_inject_failure_notifies(tmp_path, monkeypatch):
    """The bound session isn't active -> the same failure path _handle_reply
    already has for an explicit [<sid>] reply (no silent drop)."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", session_id="S-gone-p1")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})

    def _raise(name, params):
        raise A.ActionError("no such pane")
    monkeypatch.setattr(A, "dispatch", _raise)
    notices = []
    monkeypatch.setattr(TL, "_notify", lambda c, chat_id, text, **k: notices.append((chat_id, text)))

    msg = _topic_msg("hello?", chat_id=111, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["ok"] is False
    assert result["action"] == "task_topic_inject_failed"
    assert len(notices) == 1 and "S-gone-p1" in notices[0][1]


# ---------------------------------------------------------------------------
# T-0660 Phase 2 mechanic #3: the stakeholder replying directly in a task
# topic ALSO records a passive FYI append into the project's own (slug, gid)
# attendant thread — so the attendant keeps context without treating it as
# its own actionable inbox item.
# ---------------------------------------------------------------------------


def test_handle_topic_bound_session_id_records_fyi_to_attendant_thread(tmp_path, monkeypatch):
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True})
    fyi_calls = []
    # T-0780 added the `reply_to` kwarg (the quoted original, when the message
    # was a reply). The stub takes it so it stays a faithful mirror of the real
    # signature — a `**kwargs` catch-all here would let the next kwarg drift in
    # unnoticed, which is what this double is meant to guard against.
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda cfg, slug, gid, *, author, text, reply_to=None:
                        fyi_calls.append(
                            {"slug": slug, "gid": gid, "author": author,
                             "text": text, "reply_to": reply_to}
                        ))

    msg = _topic_msg("looks good, ship it", chat_id=111, thread_id=42)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert len(fyi_calls) == 1
    call = fyi_calls[0]
    assert call["slug"] == "beta"
    assert call["gid"] == "gu_1"
    # T-0746: the text is the SYSTEM's summary of what happened, wrapping his
    # words — it was never something he wrote, so it is no longer stored as if
    # it were (item c, applied to this file's own writer).
    assert call["author"] == "system:direct-reply"
    assert "S-dev-p9" in call["text"]
    assert "looks good, ship it" in call["text"]
    # T-0780: this message was not a reply, so there is no quoted original.
    assert call["reply_to"] is None


def test_handle_topic_bound_session_id_falls_back_on_inject_failure(tmp_path, monkeypatch):
    """T-0746 (was: "records fyi even on inject failure"). The attendant must
    still learn the stakeholder tried to reach a session that is no longer
    active — but the FYI summary was a WEAKER form of that: marked "ответ не
    требуется", it told the attendant to ignore it. The fallback now records
    the situation AND his verbatim text and wakes the attendant, so the FYI
    would only repeat his words inside a no-reply-needed wrapper."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", session_id="S-gone-p1")
    (cfg.data_dir / "beta" / "sessions").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "beta" / "sessions" / "S-gone-p1.md").write_text(
        "---\nsid: S-gone-p1\n---\n", encoding="utf-8")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})

    def _raise(name, params):
        raise A.ActionError("no such pane")
    monkeypatch.setattr(A, "dispatch", _raise)
    notices = []
    monkeypatch.setattr(TL, "_notify", lambda c, ch, t, **k: notices.append((k.get("thread_id"), t)))
    fyi_calls = []
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda *a, **k: fyi_calls.append((a, k)))
    posts = []
    monkeypatch.setattr(TL, "_post_conversation",
                        lambda c, slug, gid, payload: posts.append((slug, payload)) or True)
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: {"ok": True, "spawned": False})

    msg = _topic_msg("hello?", chat_id=111, thread_id=42)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "task_topic_inject_fallback"
    assert fyi_calls == []
    assert [p[0] for p in posts] == ["beta", "beta"]
    assert posts[0][1]["author"] == "system:undelivered"
    assert posts[1][1]["author"] == "user" and posts[1][1]["text"] == "hello?"
    # The outcome lands in the TASK TOPIC he is looking at, not the general feed.
    assert notices and notices[0][0] == 42


# ---------------------------------------------------------------------------
# T-0770: the direct-mode injection carries its PROVENANCE, and the answer it
# owes is written down. The defect it replaces was reproduced against a copy of
# the live bindings before any of this was written: the session received
# `прием-прием` and nothing else, four times, and he wrote «опять игнор меня».
# ---------------------------------------------------------------------------


def _direct_topic_cfg(tmp_path, monkeypatch, *, sid="S-dev-p9", ticket_id="T-0314"):
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", ticket_id=ticket_id, session_id=sid)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: None)
    return cfg


def test_topic_bound_injection_names_the_human_the_topic_and_the_reply_command(
        tmp_path, monkeypatch):
    """S1+S3 end to end through the REAL handle_update."""
    import bot_squad_worker.actions as A
    cfg = _direct_topic_cfg(tmp_path, monkeypatch)
    injected = []
    monkeypatch.setattr(A, "dispatch",
                        lambda n, p: injected.append((n, p)) or {"ok": True})

    TL.handle_update(cfg, {"update_id": 1,
                           "message": _topic_msg("прием-прием", chat_id=111, thread_id=42)})

    verb, params = injected[0]
    assert verb == "inject_prompt"          # ONE submission, not one per line
    text = params["text"]
    assert "TELEGRAM" in text and "a human" in text
    # T-0795: highlighted ids instead of the prose `Where` row — exact form
    # pinned in test_ping_ids_highlight.py.
    assert "▶ CHAT ID: 111   ▶ TOPIC ID: 42" in text
    assert "прием-прием" in text
    assert 'bsq topic say --chat 111 --topic 42 "<your answer>"' in text
    # His display name, taken from the update rather than assumed.
    assert "Alexey" in text


def test_topic_bound_injection_records_the_answer_it_owes(tmp_path, monkeypatch):
    """The behavioural half: the debt exists the moment the envelope lands, so
    a session that stays quiet is detectable."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_direct_reply as TDR
    cfg = _direct_topic_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "dispatch", lambda n, p: {"ok": True})

    TL.handle_update(cfg, {"update_id": 1,
                           "message": _topic_msg("ну и что тут?", chat_id=111, thread_id=42)})

    entry = TDR.load(cfg)["111:42:S-dev-p9"]
    assert entry["sid"] == "S-dev-p9" and entry["thread_id"] == 42
    assert entry["slug"] == "beta" and entry["gid"] == "gu_1"
    assert entry["ticket_id"] == "T-0314"
    assert entry["text"] == "ну и что тут?"   # HIS words, not the envelope


def test_a_message_the_session_never_received_owes_nothing(tmp_path, monkeypatch):
    """S10. The T-0746 fallback has already handed his text to an attendant
    that WILL answer — a debt on a session that never saw it would re-drive a
    dead pane and then escalate a second time."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_direct_reply as TDR
    cfg = _direct_topic_cfg(tmp_path, monkeypatch, sid="S-gone-p1")
    (cfg.data_dir / "beta" / "sessions").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "beta" / "sessions" / "S-gone-p1.md").write_text(
        "---\nsid: S-gone-p1\n---\n", encoding="utf-8")

    def _raise(name, params):
        raise A.ActionError("no such pane")
    monkeypatch.setattr(A, "dispatch", _raise)
    monkeypatch.setattr(TL, "_notify", lambda *a, **k: None)
    posts = []
    monkeypatch.setattr(TL, "_post_conversation",
                        lambda c, s, g, payload: posts.append(payload) or True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: {"ok": True})

    result = TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_msg("hello?", chat_id=111, thread_id=42)})

    assert result["action"] == "task_topic_inject_fallback"
    assert TDR.load(cfg) == {}
    # ★ The fallback stores HIS words, never our envelope around them: the
    # T-0746 rule that system prose must not enter the record as his.
    assert posts[1]["author"] == "user" and posts[1]["text"] == "hello?"
    assert "TELEGRAM" not in posts[1]["text"]


def test_the_envelope_moves_neither_the_locus_nor_a_reply_route(tmp_path, monkeypatch):
    """★ The T-0667 constraint the ticket names as the reason this is not a
    two-line change: a task-topic message must never redirect the project's
    attendant-reply relay into the task topic. The provenance therefore rides
    in the MESSAGE — nothing on this path writes the locus or the reply map."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import conversation_locus, tg_reply_map
    cfg = _direct_topic_cfg(tmp_path, monkeypatch)
    conversation_locus.set_locus(cfg, "beta", "gu_1", "999", None)
    before = conversation_locus.load(cfg)
    monkeypatch.setattr(A, "dispatch", lambda n, p: {"ok": True})

    TL.handle_update(cfg, {"update_id": 1,
                           "message": _topic_msg("статус?", chat_id=111, thread_id=42)})

    assert conversation_locus.load(cfg) == before
    assert tg_reply_map.load(cfg.data_dir) == {}


def test_a_plain_project_topic_gets_no_envelope_and_owes_nothing(tmp_path, monkeypatch):
    """The neighbour that must not move: a binding with no session_id is the
    T-0639 base case — attendant path, no direct injection, no debt."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import tg_bindings, tg_direct_reply as TDR
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")           # no session_id
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    injected = []
    monkeypatch.setattr(A, "dispatch", lambda n, p: injected.append(n) or {"ok": True})

    result = TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_msg("привет", chat_id=111, thread_id=7)})

    assert result["action"] == "route_bound_topic"
    assert injected == [] and TDR.load(cfg) == {}


def test_session_bound_topic_never_appends_inbound_to_the_topic_store(
    tmp_path, monkeypatch,
):
    """T-0769: the property the API now STATES on every read of such a topic.

    ``routes_conversations._inbound_capture`` answers the question two sessions
    got wrong on 2026-07-28 — "are his messages in this topic or not?" — with a
    flat "not, and here is where they are". That answer is derived from the
    BINDING, not from the routing code, so if this branch ever starts appending
    inbound into the topic's own store the API keeps saying the opposite and
    the marker becomes a confident lie instead of a missing one. This test is
    the coupling: it fails the moment the statement stops being true.

    Note what this does NOT say. Not appending here is deliberate (T-0660/
    T-0667 — a task-topic message must never redirect the project's
    attendant-reply relay), and T-0769 explicitly refused to "fix" the
    asymmetry by writing a per-topic copy of content already recorded. So if
    you are here because you want that copy, the marker is not what is standing
    in your way; the ticket is.

    Carries its OWN positive control (T-0740). "No append was recorded" is
    exactly what a spy patched onto the wrong name reports, and it reports it
    forever — so the same spy also has to be SEEN catching the append a plain
    bound topic really does make.
    """
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", session_id="S-dev-p9")
    tg_bindings.set_binding(cfg, "111", 7, "beta")  # plain topic: the control
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: None)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    durable = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda *a, **k: durable.append(k.get("thread_id")))

    TL.handle_update(cfg, {
        "update_id": 1,
        "message": _topic_msg("прием-прием", chat_id=111, thread_id=42),
    })
    assert durable == [], (
        "a session-bound topic must not durably append inbound into its own "
        "store — routes_conversations._inbound_capture tells every reader it "
        "does not (T-0769)"
    )

    TL.handle_update(cfg, {
        "update_id": 2,
        "message": _topic_msg("прием-прием", chat_id=111, thread_id=7),
    })
    assert durable == [7], (
        "positive control: the spy must be able to see an append at all, or "
        "the assertion above pins nothing"
    )


def test_handle_topic_bound_plain_project_topic_never_records_fyi(tmp_path, monkeypatch):
    """A plain project/General binding (no session_id) is the T-0639 base
    case, not a task-topic direct-reply — must never trigger the FYI mechanic."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")  # no session_id
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    fyi_calls = []
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda *a, **k: fyi_calls.append((a, k)))

    msg = _topic_msg("hi", chat_id=111, thread_id=7)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})
    assert fyi_calls == []


# ---------------------------------------------------------------------------
# T-0667: OUTGOING routing follows the conversation LOCUS (last-seen
# chat_id/thread_id per (slug,gid)) recorded here on every INCOMING route, so
# a reply lands where the user actually wrote instead of always the static
# project DM/tg_chat.
# ---------------------------------------------------------------------------


def test_handle_topic_bound_records_locus(tmp_path, monkeypatch):
    from bot_squad_worker import tg_bindings, conversation_locus
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _topic_msg("hello", chat_id=111, thread_id=7)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    rec = conversation_locus.get_locus(cfg, "beta", "gu_1", 7)
    assert rec == {"chat_id": "111", "thread_id": 7, "at": rec["at"]}


def test_handle_unquoted_records_locus(tmp_path, monkeypatch):
    from bot_squad_worker import conversation_locus
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _dated_msg(text="hi", chat_id=111)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    rec = conversation_locus.get_locus(cfg, "alpha", "gu_1")
    assert rec["chat_id"] == "111" and rec["thread_id"] is None


def test_handle_unquoted_records_locus_thread_id_when_present(tmp_path, monkeypatch):
    """An unquoted message inside an UNBOUND topic (falls through to
    _handle_unquoted via the static tg_chat map, T-0639) still records its own
    real thread_id in the locus, not None — so a later relay lands back in
    that SAME topic, not the chat's general feed."""
    from bot_squad_worker import conversation_locus
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _topic_msg("no binding for this thread", chat_id=111, thread_id=999)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    rec = conversation_locus.get_locus(cfg, "alpha", "gu_1", 999)
    assert rec["chat_id"] == "111" and rec["thread_id"] == 999


def test_handle_unquoted_forwards_thread_id_to_append_matching_locus(tmp_path, monkeypatch):
    """T-0693 Finding B ('dropped/omitted by mistake'): append_conversation
    must get the SAME thread_id conversation_locus does, not a silently
    omitted one — before this fix, the live incident's message landed in the
    bare per-user store file while the locus claimed a real thread, and
    untangling it needed a manual backfill."""
    from bot_squad_worker import conversation_locus
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append(k.get("thread_id")) or True)

    msg = _topic_msg("no binding for this thread", chat_id=111, thread_id=999)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert appended == [999]  # matches the locus's thread_id (999), not omitted/None
    locus_rec = conversation_locus.get_locus(cfg, "alpha", "gu_1", 999)
    assert locus_rec["thread_id"] == 999


# ---------------------------------------------------------------------------
# T-0740: the reply-quote / GROUP-VOICE branch of handle_update was the last
# inbound path still dropping the origin thread. _handle_topic_bound and
# _handle_unquoted (above) both carry thread_id into the store AND record the
# locus; this branch did neither, so a voice note or a reply typed in a bound
# topic was filed into the project's collapsed thread-less history and left
# the locus naming whatever topic (or DM) was last used on the OTHER paths.
# The attendant then woke unscoped and its answer relayed to that stale
# locus — the reported "the voice ack landed in the right topic, then the
# actual answer went to a different one".
# ---------------------------------------------------------------------------


def _voice_topic_msg(chat_id, thread_id):
    return {
        "message_id": 9, "date": 1_700_000_000,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": _from(),
        "message_thread_id": thread_id,
        "voice": {"file_id": "VID", "file_unique_id": "u", "duration": 3},
    }


def test_group_voice_in_bound_topic_appends_with_thread_id(tmp_path, monkeypatch):
    """The record for a voice note sent in a topic belongs to THAT topic's
    isolated thread, not the project's mixed history."""
    from bot_squad_worker import tg_bindings, voice_intake as VI
    cfg = _make_multi_cfg(tmp_path, chat="111")
    cfg.voice_enabled = True
    tg_bindings.set_binding(cfg, "111", 7, "beta")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(VI, "process_voice", lambda *a, **k: {"ok": True})
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append((slug, k.get("thread_id"))) or True)

    out = TL.handle_update(cfg, {"update_id": 1, "message": _voice_topic_msg(111, 7)})

    assert out["action"] == "voice"
    assert appended == [("beta", 7)]


def test_group_voice_in_bound_topic_records_locus(tmp_path, monkeypatch):
    """...and the locus now names that topic, so a later slug-only send (and
    the API relay's bare-key lookup) no longer resolves a days-stale topic."""
    from bot_squad_worker import tg_bindings, conversation_locus, voice_intake as VI
    cfg = _make_multi_cfg(tmp_path, chat="111")
    cfg.voice_enabled = True
    tg_bindings.set_binding(cfg, "111", 7, "beta")
    # A pre-existing, STALE project-level locus pointing at a different topic —
    # exactly the live shape (a bare `slug:gid` entry from days earlier).
    conversation_locus.set_locus(cfg, "beta", "gu_1", "111", 23)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(VI, "process_voice", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)

    TL.handle_update(cfg, {"update_id": 1, "message": _voice_topic_msg(111, 7)})

    rec = conversation_locus.get_locus(cfg, "beta", "gu_1", 7)
    assert rec["chat_id"] == "111" and rec["thread_id"] == 7


def test_reply_quote_in_bound_topic_appends_with_thread_id_and_records_locus(
    tmp_path, monkeypatch,
):
    """The TEXT twin of the voice case: a reply-quote typed in a bound topic
    goes through the same branch and was losing the thread the same way."""
    from bot_squad_worker import tg_bindings, conversation_locus
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True, "lines_sent": 1})
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append((slug, k.get("thread_id"))) or True)

    msg = _reply_message("S-alice-spec5-p3", "go ahead", chat_id=111)
    msg["chat"]["type"] = "supergroup"
    msg["message_thread_id"] = 7
    out = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert out["action"] == "inject"
    assert appended == [("beta", 7)]
    rec = conversation_locus.get_locus(cfg, "beta", "gu_1", 7)
    assert rec["chat_id"] == "111" and rec["thread_id"] == 7


def test_thread_less_reply_quote_does_not_touch_the_locus(tmp_path, monkeypatch):
    """Deliberate limit of the fix: with no thread there is no binding, so
    `chat_slug` is the "incidental" static one this branch is explicitly told
    not to trust. Writing a locus under it could point a project's replies at
    a chat the user never addressed it in — so a thread-less message in this
    branch leaves the locus exactly as it was before this change."""
    from bot_squad_worker import conversation_locus
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(A, "dispatch", lambda name, params: {"ok": True, "lines_sent": 1})
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append(k.get("thread_id")) or True)

    out = TL.handle_update(cfg, {"update_id": 1,
                                 "message": _reply_message("S-a-p3", "ok", chat_id=111)})

    assert out["action"] == "inject"
    assert appended == [None]
    assert conversation_locus.load(cfg) == {}


# ---------------------------------------------------------------------------
# T-0693 Finding B: conversation_locus/conversation_store treat "no thread_id"
# as one generic bare key regardless of WHY it's absent — a genuine DM, an
# intentional General-feed tg_bindings entry (thread_id=None bound on
# purpose), or a real thread dropped by mistake all looked identical. The
# `general_feed` marker (set only when `_handle_topic_bound` — reachable ONLY
# via a resolved binding — matches on thread_id=None) makes the General-feed
# case distinguishable; `_warn_if_general_feed_collides` makes the one actual
# collision risk (a General-feed binding sharing a slug with per-topic
# bindings) loud instead of silent.
# ---------------------------------------------------------------------------


def test_handle_topic_bound_general_feed_binding_marks_general_feed(tmp_path, monkeypatch):
    """A message that matches an EXPLICIT General-feed binding (thread_id=
    None, bound on purpose) is marked general_feed=True — distinguishing it
    from a genuine DM, which also has thread_id=None but is NOT bound at all
    and never reaches _handle_topic_bound."""
    from bot_squad_worker import tg_bindings, conversation_locus
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", None, "beta")  # General-feed binding
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append(k.get("general_feed")) or True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _topic_msg("general feed message", chat_id=111, thread_id=None)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route_bound_topic" and result["slug"] == "beta"
    assert appended == [True]
    rec = conversation_locus.get_locus(cfg, "beta", "gu_1")
    assert rec.get("general_feed") is True


def test_handle_topic_bound_numbered_topic_does_not_mark_general_feed(tmp_path, monkeypatch):
    """A REAL numbered-topic binding must NOT be marked general_feed — only
    an explicit thread_id=None binding match is a General-feed case."""
    from bot_squad_worker import tg_bindings, conversation_locus
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 7, "beta")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    appended = []
    monkeypatch.setattr(TL, "append_conversation",
                        lambda c, slug, gid, m, **k: appended.append(k.get("general_feed")) or True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _topic_msg("topic 7 message", chat_id=111, thread_id=7)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert appended == [False]
    rec = conversation_locus.get_locus(cfg, "beta", "gu_1", 7)
    assert "general_feed" not in rec


def test_handle_topic_bound_general_feed_collision_with_same_slug_warns(tmp_path, monkeypatch, caplog):
    """The one actual collision risk (Finding B): a General-feed binding AND a
    per-topic binding on the SAME slug, in the SAME chat — not exercised in
    production today, but if it ever happens it must not be silent."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", None, "shared")  # General-feed -> shared
    tg_bindings.set_binding(cfg, "111", 5, "shared")     # ALSO a per-topic binding -> shared
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _topic_msg("general feed message", chat_id=111, thread_id=None)
    with caplog.at_level("WARNING"):
        TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert any("shared" in r.message and "General-feed" in r.message for r in caplog.records)


def test_handle_topic_bound_general_feed_different_slugs_no_collision_warning(tmp_path, monkeypatch, caplog):
    """Mirrors the LIVE bot-squad setup: a General-feed binding on one slug
    (bot-squad) coexists in the SAME chat with per-topic bindings on a
    DIFFERENT slug (watchrobot) — not a collision, must stay quiet."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", None, "alpha")  # General-feed -> alpha
    tg_bindings.set_binding(cfg, "111", 5, "beta")      # per-topic -> a DIFFERENT slug
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)

    msg = _topic_msg("general feed message", chat_id=111, thread_id=None)
    with caplog.at_level("WARNING"):
        TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert not any("General-feed" in r.message for r in caplog.records)


def test_ask_which_project_sends_button_keyboard(tmp_path, monkeypatch):
    """T-0513: the picker now routes through the channel abstraction (was a raw
    httpx sendMessage). The keyboard must reach the TG client via the channel,
    with the interactive-reply flags (urgent, no SID prefix, no debounce)."""
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    calls: list[dict] = []

    class _FakeTg:
        def send(self, **kw):
            calls.append(kw)
            return True

    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: _FakeTg())
    # A raw sendMessage would be a regression of the migration — fail loud if so.
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("raw httpx.post — must route via the channel")
        ),
    )

    TL._ask_which_project(cfg, "111")

    assert calls, "picker did not route through the channel"
    kw = calls[-1]
    # A reply-keyboard whose buttons send "/project <slug>" as a normal message
    # (so it arrives under allowed_updates:["message"], no poll-contract change).
    markup = kw["reply_markup"]
    btn_texts = [btn["text"] for row in markup["keyboard"] for btn in row]
    assert "/project alpha" in btn_texts
    assert "/project beta" in btn_texts
    assert kw["urgent"] is True and kw["sid"] == "" and kw["debounce"] is False


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def test_tick_reads_polls_and_writes(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    # Seed a last_update_id
    TL._write_last_update_id(cfg, 99)

    fake_updates = [
        {"update_id": 100, "message": {"chat": {"id": 99999}, "text": "ignored"}},
        {"update_id": 101, "message": {"chat": {"id": 99999}, "text": "also ignored"}},
    ]
    monkeypatch.setattr(TL, "poll_updates", lambda cfg, last_id, timeout=25: fake_updates)
    monkeypatch.setattr(TL, "handle_update", lambda cfg, upd: {"ok": True, "action": "skip"})

    result = TL.tick(cfg)
    assert result["polled"] == 2
    assert result["max_update_id"] == 101
    assert TL._read_last_update_id(cfg) == 101


def test_tick_empty_result(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    TL._write_last_update_id(cfg, 50)
    monkeypatch.setattr(TL, "poll_updates", lambda cfg, last_id, timeout=25: [])

    result = TL.tick(cfg)
    assert result["polled"] == 0
    assert result["max_update_id"] == 50
    # No new ID written
    assert TL._read_last_update_id(cfg) == 50


def test_tick_network_error_handled(tmp_path, monkeypatch):
    """poll_updates returns [] on network error — tick should complete cleanly."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(TL, "poll_updates", lambda cfg, last_id, timeout=25: [])

    result = TL.tick(cfg)
    assert result["ok"] is True
    assert result["polled"] == 0


def test_tick_missing_token(tmp_path, monkeypatch):
    """No bot token — poll_updates returns [] immediately, tick is a no-op."""
    cfg = _make_cfg(tmp_path, bot_token="")
    # poll_updates should return [] because no token
    result = TL.tick(cfg)
    assert result["ok"] is True
    assert result["polled"] == 0


def test_tick_bad_update_does_not_poison_offset(tmp_path, monkeypatch):
    """One update that raises in handle_update should not prevent writing max_id."""
    cfg = _make_cfg(tmp_path)
    TL._write_last_update_id(cfg, 0)

    fake_updates = [
        {"update_id": 200},
        {"update_id": 201},
    ]
    call_count = [0]

    def bad_handle(cfg, upd):
        call_count[0] += 1
        if upd["update_id"] == 200:
            raise RuntimeError("simulated crash")
        return {"ok": True, "action": "skip"}

    monkeypatch.setattr(TL, "poll_updates", lambda cfg, last_id, timeout=25: fake_updates)
    monkeypatch.setattr(TL, "handle_update", bad_handle)

    result = TL.tick(cfg)
    # handled only 1 (200 crashed, 201 succeeded)
    assert result["handled"] == 1
    # max_id should still be 201
    assert result["max_update_id"] == 201
    assert TL._read_last_update_id(cfg) == 201


def test_tick_bad_update_logs_the_drop(tmp_path, monkeypatch, caplog):
    """T-0684 audit: a crash in handle_update still silently loses the message
    (offset advances past it by design, per the test above) — before this fix
    nothing recorded that it happened at all, making a genuine drop
    indistinguishable from ordinary attendant latency. Must be logged."""
    cfg = _make_cfg(tmp_path)
    TL._write_last_update_id(cfg, 0)

    fake_updates = [{"update_id": 300}]
    monkeypatch.setattr(TL, "poll_updates", lambda cfg, last_id, timeout=25: fake_updates)

    def bad_handle(cfg, upd):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(TL, "handle_update", bad_handle)

    with caplog.at_level(logging.ERROR, logger=TL.log.name):
        TL.tick(cfg)

    assert any("300" in r.message and "dropped" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# T-0194: per-installation TG egress proxy — getUpdates + _notify route through
# cfg.tg_proxy_url when set.
# ---------------------------------------------------------------------------


def test_poll_updates_passes_proxy_when_configured(tmp_path):
    cfg = _make_cfg(tmp_path, proxy_url="http://153.80.195.83:8888")
    captured: dict = {}

    def fake_get(url, params=None, timeout=None, proxy=None):
        captured["proxy"] = proxy
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"result": []}
        return resp

    with patch("httpx.get", side_effect=fake_get):
        TL.poll_updates(cfg, 0, timeout=1)
    assert captured["proxy"] == "http://153.80.195.83:8888"


def test_poll_updates_omits_proxy_when_not_configured(tmp_path):
    cfg = _make_cfg(tmp_path)  # no proxy
    captured: dict = {}

    # Sig lacks proxy: a proxy kwarg would raise TypeError here.
    def fake_get(url, params=None, timeout=None):
        captured["called"] = True
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"result": []}
        return resp

    with patch("httpx.get", side_effect=fake_get):
        TL.poll_updates(cfg, 0, timeout=1)
    assert captured.get("called") is True


def test_notify_passes_proxy_when_configured(tmp_path, monkeypatch):
    """T-0513: _notify routes through the channel; the egress proxy now lives in
    tg.py (via the channel→TgClient), not a raw httpx in _notify. End-to-end: a
    configured proxy still reaches httpx.post, and the body carries no SID prefix.
    """
    from bot_squad_worker.tg import TgClient
    import bot_squad_worker.actions as A

    monkeypatch.setenv("BOT_SQUAD_DISABLE_QUIET_HOURS", "1")
    cfg = _make_cfg(tmp_path, proxy_url="socks5://10.0.0.1:1080")
    # Build a real TgClient from this cfg so the proxy + payload assembly is the
    # genuine production path; route the channel singleton at it for this test.
    monkeypatch.setattr(A, "_get_tg_client", lambda _c: TgClient(cfg))
    captured: dict = {}

    def fake_post(url, json=None, timeout=None, proxy=None):  # noqa: A002
        captured["proxy"] = proxy
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    with patch("httpx.post", side_effect=fake_post):
        TL._notify(cfg, "12345", "hello")
    assert captured["proxy"] == "socks5://10.0.0.1:1080"
    assert captured["json"]["text"] == "hello"  # no [SID] prefix on command replies


def test_notify_routes_through_channel(tmp_path, monkeypatch):
    """_notify delegates to the channel with the interactive-reply flags."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)
    calls: list[dict] = []

    class _FakeTg:
        def send(self, **kw):
            calls.append(kw)
            return True

    monkeypatch.setattr(A, "_get_tg_client", lambda _c: _FakeTg())
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("raw httpx.post — must route via the channel")
        ),
    )
    TL._notify(cfg, "12345", "hi there")
    assert calls and calls[-1]["text"] == "hi there"
    assert calls[-1]["urgent"] is True and calls[-1]["sid"] == ""
    assert calls[-1]["debounce"] is False


def test_notify_forwards_thread_id_as_topic_id(tmp_path, monkeypatch):
    """T-0676 item 3: a caller that knows which forum topic the triggering
    message arrived on can pass ``thread_id`` through _notify/_channel_notify
    so the ack lands back in THAT topic — before this fix the parameter
    didn't exist at all and every ack went to the chat's general feed."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)
    calls: list[dict] = []

    class _FakeTg:
        def send(self, **kw):
            calls.append(kw)
            return True

    monkeypatch.setattr(A, "_get_tg_client", lambda _c: _FakeTg())
    TL._notify(cfg, "12345", "hi there", thread_id=7)
    assert calls and calls[-1]["topic_id"] == 7

    # Omitted thread_id -> topic_id stays None (exact pre-fix call shape).
    calls.clear()
    TL._notify(cfg, "12345", "hi there")
    assert calls and calls[-1]["topic_id"] is None


def test_notify_noop_without_token(tmp_path, monkeypatch):
    """No bot token → _notify is a silent no-op (no channel call)."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path, bot_token="")
    monkeypatch.setattr(
        A, "_get_tg_client",
        lambda _c: (_ for _ in ()).throw(AssertionError("must not build a client")),
    )
    TL._notify(cfg, "12345", "x")  # must not raise / must not send


def test_notify_swallows_transport_error(tmp_path, monkeypatch):
    """A messenger outage must not break inbound command handling."""
    import bot_squad_worker.actions as A
    cfg = _make_cfg(tmp_path)

    class _BoomTg:
        def send(self, **kw):
            raise RuntimeError("egress blocked")

    monkeypatch.setattr(A, "_get_tg_client", lambda _c: _BoomTg())
    TL._notify(cfg, "12345", "x")  # must not raise


# ---------------------------------------------------------------------------
# T-0386 Phase 2: voice messages route to voice_intake.process_voice
# ---------------------------------------------------------------------------

def test_handle_update_routes_voice_to_process(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="-100777", voice_enabled=True)
    captured = {}

    def fake_process(c, slug, message, *, ts):
        captured["slug"] = slug
        captured["file_id"] = message["voice"]["file_id"]
        captured["ts"] = ts
        return {"ok": True}

    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(VI, "process_voice", fake_process)

    update = {
        "update_id": 5,
        "message": {
            "message_id": 9, "date": 1750000000,
            "chat": {"id": -100777, "type": "supergroup"},
            "from": {"id": 1, "first_name": "A"},
            "voice": {"file_id": "VID", "file_unique_id": "u", "duration": 3},
        },
    }
    out = TL.handle_update(cfg, update)
    assert out["action"] == "voice"
    assert captured["slug"] == "test-project"
    assert captured["file_id"] == "VID"
    assert captured["ts"]  # an ISO timestamp was derived


def test_handle_update_voice_disabled_skipped(tmp_path, monkeypatch):
    """Flag-off-safe (operator directive): with [voice].enabled off (default),
    a voice message is NOT processed — voice intake is dormant until the
    stakeholder setup flips it on."""
    cfg = _make_cfg(tmp_path, tg_chat="-100777", voice_enabled=False)
    from bot_squad_worker import voice_intake as VI
    called = []
    monkeypatch.setattr(VI, "process_voice", lambda *a, **k: called.append(1))
    update = {"update_id": 7, "message": {
        "message_id": 9, "date": 1750000000,
        "chat": {"id": -100777, "type": "supergroup"},
        "from": {"id": 1, "first_name": "A"},
        "voice": {"file_id": "VID", "file_unique_id": "u", "duration": 3}}}
    out = TL.handle_update(cfg, update)
    assert out["action"] == "skip"
    assert called == []


def test_handle_update_voice_not_allowlisted_skipped(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="-100777", voice_enabled=True)
    from bot_squad_worker import voice_intake as VI
    called = []
    monkeypatch.setattr(VI, "process_voice", lambda *a, **k: called.append(1))
    update = {"update_id": 6, "message": {
        "chat": {"id": -999999, "type": "supergroup"},
        "voice": {"file_id": "X", "duration": 1}}}
    out = TL.handle_update(cfg, update)
    assert out["action"] == "skip"
    assert called == []


# ---------------------------------------------------------------------------
# next-wave #13 (T-0453): poll-health signal. poll_updates used to return []
# on BOTH no-mail and network error (silent fail on the primary operator
# channel on a DPI-blocked host). It now records last_ok_poll_at on success
# and last_error/last_error_at on a network error to _worker/tg_poll_health.json
# — so an operator (later, a FE pill) can tell "no mail" from "egress broken".
# ---------------------------------------------------------------------------
import json as _json


def _health(cfg) -> dict:
    p = TL._poll_health_path(cfg)
    return _json.loads(p.read_text()) if p.exists() else {}


# ---------------------------------------------------------------------------
# T-0569: a DM voice note routes as an unquoted TEXT message (the transcript),
# not a feedback artifact. A group/topic voice note keeps the pre-existing
# process_voice (feedback-artifact) behavior.
# ---------------------------------------------------------------------------


def _voice_msg(chat_id=12345, chat_type="private", duration=5):
    return {
        "message_id": 42, "date": 1750000000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": _from(),
        "voice": {"file_id": "VID", "file_unique_id": "u1", "duration": duration},
    }


def test_handle_update_private_voice_routes_transcript_like_text(tmp_path, monkeypatch):
    """A DM voice note (voice_enabled) is transcribed via transcribe_only (NOT
    process_voice — no feedback artifact), echoed back, and routed through the
    SAME path as an unquoted text message: the transcript becomes the recorded
    + routed text, and the voice attachment descriptor survives on the record
    (no double-append)."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")

    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "process_voice",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not hit the feedback path")))
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {
            "ok": True, "transcript": "dark mode please", "lang": "en",
            "engine": "faster-whisper:small", "duration": msg["voice"]["duration"],
        })

    recorded = []
    monkeypatch.setattr(
        TL, "append_conversation",
        lambda c, slug, gid, msg, **k: recorded.append((slug, gid, msg.get("text"), msg.get("voice"))) or True)
    routed = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: routed.append((slug, gid)))
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))

    update = {"update_id": 1, "message": _voice_msg()}
    result = TL.handle_update(cfg, update)

    assert result["action"] == "voice_private_route"
    assert result["slug"] == "test-project"
    assert result["transcript"] == "dark mode please"
    # The transcript (not the empty voice "text") is the recorded text; the
    # voice attachment descriptor survives on the SAME record (no double-append).
    assert recorded == [(
        "test-project", "gu_1", "dark mode please",
        {"file_id": "VID", "file_unique_id": "u1", "duration": 5},
    )]
    assert routed == [("test-project", "gu_1")]
    assert any("dark mode please" in t and "\U0001f399" in t for t in echoes)


def test_handle_update_private_voice_transcription_failure_notifies(tmp_path, monkeypatch):
    """A transcription failure (download/decode) is reported to the user with
    the ACTUAL reason, nothing is routed (no transcript), and the refusal
    leaves a no-drop marker record in the thread (T-0586) carrying the voice
    attachment so the drop is visible to the attendant."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": False, "reason": "download_failed", "transcript": ""})
    notified = []
    monkeypatch.setattr(TL, "_notify",
                        lambda c, chat, text, **k: notified.append(text))
    routed = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: routed.append((slug, gid)))
    unquoted = []
    monkeypatch.setattr(TL, "_handle_unquoted",
                        lambda *a, **k: unquoted.append(1) or {"ok": True})
    recorded = []
    monkeypatch.setattr(
        TL, "_post_conversation",
        lambda c, slug, gid, payload: recorded.append((slug, gid, payload)) or True)

    update = {"update_id": 1, "message": _voice_msg()}
    result = TL.handle_update(cfg, update)

    assert result["ok"] is False
    assert result["action"] == "voice_private_failed"
    assert result["reason"] == "download_failed"
    assert notified and "скачать" in notified[0]  # honest cause, not a generic decode error
    # Nothing is ROUTED — there is no transcript to route.
    assert unquoted == []
    # T-0746: the attendant wake is now EXPLICIT. It always happened; it just
    # used to be a server-side side effect of the marker being author="user",
    # so asserting `_ensure_user_conversation` was never called conflated "no
    # transcript routed" with "attendant not told". Correcting the attribution
    # (system:voice-rejected) removes the implicit wake, so T-0586's actual
    # property — the attendant SEES the drop — has to be asserted directly.
    assert routed == [("test-project", "gu_1")]
    # T-0586 no-drop: the refusal itself is recorded — marker text + attachment.
    assert len(recorded) == 1
    slug, gid, payload = recorded[0]
    assert (slug, gid) == ("test-project", "gu_1")
    assert "НЕ обработано" in payload["text"] and "download_failed" in payload["text"]
    assert payload["author"] == "system:voice-rejected"
    assert payload["attachments"] == [{"type": "voice", "file_id": "VID"}]


def test_handle_update_private_voice_too_big_honest_no_retry_lie(tmp_path, monkeypatch):
    """T-0611: a >20MB DM voice note gets the honest Bot-API-cap refusal
    (resending can't help — say so) + the no-drop marker with file_id."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": False, "reason": "too_big", "transcript": "",
                              "duration": 1540, "file_size": 25_000_000})
    notified = []
    monkeypatch.setattr(TL, "_notify",
                        lambda c, chat, text, **k: notified.append(text))
    # T-0746: the refusal marker is SYSTEM text about an inbound message, so it
    # is written through _post_conversation as system:voice-rejected rather
    # than as the stakeholder's own words. It used to ride on author="user".
    recorded = []
    monkeypatch.setattr(
        TL, "_post_conversation",
        lambda c, slug, gid, payload: recorded.append(payload) or True)
    woken = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: woken.append((slug, gid)) or {"ok": True})

    result = TL.handle_update(cfg, {"update_id": 1, "message": _voice_msg()})

    assert result["reason"] == "too_big"
    (text_sent,) = notified
    assert "20МБ" in text_sent and "25:40" in text_sent
    assert "НЕ поможет" in text_sent  # resending is explicitly called out as futile
    marker = recorded[0]
    assert marker["author"] == "system:voice-rejected"
    marker_text = marker["text"]
    assert "too_big" in marker_text
    assert marker["attachments"] == [{"type": "voice", "file_id": "VID"}]
    assert woken == [("test-project", "gu_1")]


def test_private_voice_long_transcript_echo_is_chunked(tmp_path, monkeypatch):
    """T-0586: a transcript longer than one TG message (4096-char API cap) is
    echoed as numbered parts instead of vanishing — the 2026-07-05 13.5-min
    note's echo died on a silent API 400. Every part must fit the cap and the
    full transcript must survive concatenation."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    long_transcript = "слово" * 1800  # ~9000 chars, > 2 TG messages
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": True, "transcript": long_transcript, "lang": "ru",
                              "engine": "faster-whisper:small", "duration": 810})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))

    update = {"update_id": 1, "message": _voice_msg()}
    result = TL.handle_update(cfg, update)

    assert result["transcript"] == long_transcript
    assert len(echoes) == 3  # 9000 chars / 3900 per chunk
    assert all(len(e) <= 4096 for e in echoes)
    assert all("\U0001f399" in e for e in echoes)
    assert "(1/3)" in echoes[0] and "(3/3)" in echoes[2]
    # concatenating the quoted chunk bodies reproduces the full transcript
    import re as _re
    bodies = [_re.search("«(.*)»$", e, _re.S).group(1) for e in echoes]
    assert "".join(bodies) == long_transcript


# T-0725: the DM echo is threaded to the note it transcribes. The DM path's
# CHAT was already correct (it uses the inbound chat_id) — only the threading
# was missing, so these tests assert threading without touching chat handling.

def test_private_voice_echo_replies_to_the_voice_note(tmp_path, monkeypatch):
    """T-0725: the 🎙-echo goes out as an actual TG reply to the note, in the
    chat it arrived in."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": True, "transcript": "тёмная тема", "lang": "ru",
                              "engine": "faster-whisper:small", "duration": 5})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat, text, **kw: echoes.append((chat, text, kw)))

    TL.handle_update(cfg, {"update_id": 1, "message": _voice_msg()})

    assert len(echoes) == 1
    chat, text, kw = echoes[0]
    assert chat == "12345"                      # unchanged: already correct
    assert kw["reply_to_message_id"] == 42      # T-0725: now threaded to the note
    assert "тёмная тема" in text


def test_private_voice_every_echo_part_replies_to_the_note(tmp_path, monkeypatch):
    """A multi-part echo threads EVERY part to the note — a tail floating free
    of the note is the same detachment complaint at a smaller scale."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": True, "transcript": "слово" * 1800, "lang": "ru",
                              "engine": "faster-whisper:small", "duration": 810})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    kwargs = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat, text, **kw: kwargs.append(kw))

    TL.handle_update(cfg, {"update_id": 1, "message": _voice_msg()})

    assert len(kwargs) == 3
    assert all(kw["reply_to_message_id"] == 42 for kw in kwargs)


def test_private_voice_rejection_replies_to_the_note(tmp_path, monkeypatch):
    """T-0725: a refusal ("note too long") is about THIS note, so it is threaded
    to it too — the group path threads every outcome through _confirm."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    cfg.voice_max_duration_sec = 300
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": False, "reason": "too_long", "transcript": "",
                              "duration": 900})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    notified = []
    monkeypatch.setattr(TL, "_notify",
                        lambda c, chat, text, **kw: notified.append((chat, kw)))

    TL.handle_update(cfg, {"update_id": 1, "message": _voice_msg(duration=900)})

    assert notified and notified[0][0] == "12345"
    assert notified[0][1]["reply_to_message_id"] == 42


def test_private_voice_long_transcript_echo_uses_the_shared_splitter(tmp_path, monkeypatch):
    """T-0721: the 🎙-echo and the stakeholder-page path now share ONE chunker
    (``tg.split_for_tg``) — so the echo inherits its sentence-boundary cuts
    instead of the old fixed-width slicing, and the two can't drift apart
    (T-0714's lesson). Nothing is lost either way."""
    from bot_squad_worker import tg as _tg
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    long_transcript = "Это предложение из диктовки. " * 400   # ~11.6k, has boundaries
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": True, "transcript": long_transcript, "lang": "ru",
                              "engine": "faster-whisper:small", "duration": 810})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))

    TL.handle_update(cfg, {"update_id": 1, "message": _voice_msg()})

    assert len(echoes) > 1 and all(len(e) <= _tg.TG_MSG_CAP for e in echoes)
    import re as _re
    bodies = [_re.search("«(.*)»$", e, _re.S).group(1) for e in echoes]
    for b in bodies[:-1]:
        assert b.endswith("диктовки.")            # cut on a sentence boundary
    assert "".join("".join(b.split()) for b in bodies) == "".join(long_transcript.split())


def test_private_voice_short_transcript_echo_single_message(tmp_path, monkeypatch):
    """The common case stays a single 🎙-echo message (no part markers)."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": True, "transcript": "короткое", "lang": "ru",
                              "engine": "faster-whisper:small", "duration": 3})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify", lambda c, chat, text, **kw: echoes.append(text))

    TL.handle_update(cfg, {"update_id": 1, "message": _voice_msg()})

    assert echoes == ["\U0001f399 Распознал так: «короткое»"]


def test_handle_update_private_voice_too_long_names_cap_and_records(tmp_path, monkeypatch):
    """T-0586: an over-cap DM voice note gets an HONEST refusal naming the
    duration + cap (never the generic 'не удалось распознать — попробуйте ещё
    раз', which invites a retry that can never pass), and leaves the no-drop
    marker with the file_id — the 2026-07-05 376s-incident regression."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=True)
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    from bot_squad_worker import voice_intake as VI
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda c, slug, msg: {"ok": False, "reason": "too_long", "transcript": "",
                              "duration": 376})
    notified = []
    monkeypatch.setattr(TL, "_notify",
                        lambda c, chat, text, **k: notified.append(text))
    # T-0746: the refusal marker is SYSTEM text about an inbound message, so it
    # is written through _post_conversation as system:voice-rejected rather
    # than as the stakeholder's own words. It used to ride on author="user".
    recorded = []
    monkeypatch.setattr(
        TL, "_post_conversation",
        lambda c, slug, gid, payload: recorded.append(payload) or True)
    woken = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref, **k: woken.append((slug, gid)) or {"ok": True})

    update = {"update_id": 1, "message": _voice_msg()}
    result = TL.handle_update(cfg, update)

    assert result["reason"] == "too_long"
    (text_sent,) = notified
    assert "6:16" in text_sent  # the note's actual duration
    assert "5 мин" in text_sent  # the cap (default 300s), so the user knows the rule
    assert "попробуйте ещё раз" not in text_sent  # the old lie
    marker = recorded[0]
    assert "too_long" in marker["text"] and "376" in marker["text"]
    # The file_id is what makes late recovery from TG possible at all (T-0586).
    assert marker["attachments"] == [{"type": "voice", "file_id": "VID"}]
    assert marker["author"] == "system:voice-rejected"
    assert marker["direction"] == "in"     # our note ABOUT something inbound
    # T-0746: the wake used to be a side effect of author="user"; correcting the
    # attribution without this would have silently undone T-0586's whole point.
    assert woken == [("test-project", "gu_1")]


def test_handle_update_group_voice_still_uses_feedback_path(tmp_path, monkeypatch):
    """A non-private (group/supergroup/topic) voice note keeps the pre-existing
    process_voice (feedback-artifact) behavior — T-0569 only changes DM voice."""
    cfg = _make_cfg(tmp_path, tg_chat="-100777", voice_enabled=True)
    from bot_squad_worker import voice_intake as VI
    captured = {}
    monkeypatch.setattr(
        VI, "process_voice",
        lambda c, slug, message, *, ts: captured.update(slug=slug) or {"ok": True})
    monkeypatch.setattr(
        VI, "transcribe_only",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("group voice must not use transcribe_only")))

    update = {"update_id": 1, "message": _voice_msg(chat_id=-100777, chat_type="supergroup")}
    result = TL.handle_update(cfg, update)

    assert result["action"] == "voice"
    assert captured["slug"] == "test-project"


def test_handle_update_private_voice_disabled_falls_through_unquoted(tmp_path, monkeypatch):
    """voice_enabled=False keeps the CURRENT (pre-T-0569) behavior even in a
    private chat — the voice message falls through to the unquoted path (no
    identity configured here, so it just skips), never reaching transcribe_only
    or process_voice."""
    cfg = _make_cfg(tmp_path, tg_chat="12345", voice_enabled=False)
    from bot_squad_worker import voice_intake as VI
    called = []
    monkeypatch.setattr(VI, "transcribe_only", lambda *a, **k: called.append("transcribe_only"))
    monkeypatch.setattr(VI, "process_voice", lambda *a, **k: called.append("process_voice"))
    monkeypatch.setattr(TL, "resolve_or_link_sender", lambda c, m, slug: None)

    update = {"update_id": 1, "message": _voice_msg()}
    result = TL.handle_update(cfg, update)

    assert result["action"] == "skip"
    assert called == []


def test_poll_records_ok_on_success(tmp_path):
    cfg = _make_cfg(tmp_path)

    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"result": []}
        return resp

    with patch("httpx.get", side_effect=fake_get):
        TL.poll_updates(cfg, 0, timeout=1)

    h = _health(cfg)
    assert h.get("last_ok_poll_at")          # stamped
    assert not h.get("last_error")           # no error recorded


def test_poll_records_error_on_network_failure(tmp_path):
    import httpx
    cfg = _make_cfg(tmp_path)

    def fake_get(url, params=None, timeout=None):
        raise httpx.ConnectError("egress blocked")

    with patch("httpx.get", side_effect=fake_get):
        out = TL.poll_updates(cfg, 0, timeout=1)

    assert out == []                          # still swallows → returns []
    h = _health(cfg)
    assert h.get("last_error")                # the error is now visible
    assert "egress blocked" in h["last_error"]
    assert h.get("last_error_at")


def test_poll_no_token_writes_no_health(tmp_path):
    cfg = _make_cfg(tmp_path, bot_token="")
    TL.poll_updates(cfg, 0, timeout=1)
    assert not TL._poll_health_path(cfg).exists()   # disabled, not "broken"


def test_poll_ok_preserved_across_later_error(tmp_path):
    import httpx
    cfg = _make_cfg(tmp_path)

    def ok_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"result": []}
        return resp

    with patch("httpx.get", side_effect=ok_get):
        TL.poll_updates(cfg, 0, timeout=1)
    ok_at = _health(cfg)["last_ok_poll_at"]

    with patch("httpx.get", side_effect=lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("boom"))):
        TL.poll_updates(cfg, 0, timeout=1)

    h = _health(cfg)
    assert h["last_ok_poll_at"] == ok_at      # last good poll still visible
    assert h.get("last_error")                # alongside the new error


def test_handle_update_unquoted_parked_notifies_user(tmp_path, monkeypatch):
    """T-0570: when ensure_user_conversation is refused under backoff (spawn
    saturation), the user gets a 'parked, will attend' notice instead of
    silence; the route result marks the parked state."""
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: True)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "beta")
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: {"ok": False, "parked": True})
    notices = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat_id, text, **k: notices.append((chat_id, text)))

    result = TL.handle_update(cfg, {"update_id": 1, "message": _dated_msg()})

    assert result["action"] == "route_parked"
    assert len(notices) == 1 and notices[0][0] == "111"
    assert "заняты" in notices[0][1]


def test_ensure_user_conversation_backoff_maps_to_parked(monkeypatch):
    """T-0570: an ActionError mentioning backoff maps to {'parked': True};
    other failures keep the silent-None best-effort contract."""
    from bot_squad_worker import actions as A

    def _boom_backoff(action, params):
        raise A.ActionError("spawn: backoff — 3/2 effective concurrency")

    monkeypatch.setattr(A, "dispatch", _boom_backoff)
    assert TL._ensure_user_conversation(object(), "s", "g", "ref") == {
        "ok": False, "parked": True}

    def _boom_other(action, params):
        raise A.ActionError("unknown project slug")

    monkeypatch.setattr(A, "dispatch", _boom_other)
    assert TL._ensure_user_conversation(object(), "s", "g", "ref") is None


# ---------------------------------------------------------------------------
# T-0677 — /pin-session: the per-topic direct-mode toggle
#
# Stakeholder verbatim (2026-07-25): "it should be like /pin-session, replying
# with buttons to pick the session to pin, and on choice, the message about
# this should get pinned in the topic."
# ---------------------------------------------------------------------------


_PIN_CHAT = "999888"
_PIN_THREAD = 42


def _pin_cfg(tmp_path):
    """A cfg whose bound forum topic is NOT the project's static tg_chat —
    the real topology (see handle_update's allowlist comment)."""
    return _make_cfg(tmp_path, tg_chat="12345")


def _bind_topic(cfg, *, ticket_id=None, session_id=None):
    from bot_squad_worker import tg_bindings
    tg_bindings.set_binding(
        cfg, _PIN_CHAT, _PIN_THREAD, "test-project",
        ticket_id=ticket_id, session_id=session_id,
    )


def _topic_slash(text: str, *, chat_id: str = _PIN_CHAT, thread_id=_PIN_THREAD) -> dict:
    msg = {
        "message_id": 301,
        "chat": {"id": int(chat_id), "type": "supergroup"},
        "text": text,
        "from": _from(),
    }
    if thread_id is not None:
        msg["message_thread_id"] = thread_id
    return msg


def _row(sid: str, *, status="active", role="dev", task_id=None, archived=False) -> dict:
    return {"sid": sid, "status": status, "role": role, "task_id": task_id,
            "archived": archived}


class _FakePinTg:
    """TG client double recording send_and_pin / unpin_message calls."""

    def __init__(self, *, pinned=True, message_id=777):
        self.sent: list[dict] = []
        self.unpinned: list[int] = []
        self._pinned = pinned
        self._message_id = message_id

    def send_and_pin(self, **kw):
        self.sent.append(kw)
        return {"sent": True, "message_id": self._message_id,
                "pinned": self._pinned,
                "pin_error": "" if self._pinned else "CHAT_ADMIN_REQUIRED"}

    def unpin_message(self, **kw):
        self.unpinned.append(int(kw["message_id"]))


def _install_pin_doubles(monkeypatch, rows, *, pinned=True):
    """Wire the two collaborators /pin-session touches: the session roster and
    the TG client. Returns (fake_tg, notices, channel_sends)."""
    import bot_squad_worker.actions as A
    from bot_squad_worker import sessions as S

    fake = _FakePinTg(pinned=pinned)
    monkeypatch.setattr(S, "list_sessions", lambda _cfg, _slug: rows)
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake)
    notices: list[str] = []
    monkeypatch.setattr(TL, "_notify",
                        lambda c, chat_id, text, **k: notices.append(text))
    sends: list[dict] = []
    monkeypatch.setattr(
        TL, "_channel_notify",
        lambda c, chat_id, text, **kw: sends.append({"text": text, **kw}),
    )
    return fake, notices, sends


def test_extract_slash_command_pin_session():
    assert TL.extract_slash_command(_slash_message("/pin-session")) == ("pin-session", "")
    assert TL.extract_slash_command(
        _slash_message("/pin-session S-alice-dev-p3")
    ) == ("pin-session", "S-alice-dev-p3")


def test_extract_slash_command_pin_session_underscore_alias():
    """TG's own command registry can't carry a dash, so the underscore form a
    user gets from autocomplete must normalize to the dashed command."""
    assert TL.extract_slash_command(_slash_message("/pin_session off")) == (
        "pin-session", "off")
    assert TL.extract_slash_command(_slash_message("/pin_session@thebot")) == (
        "pin-session", "")


def test_pin_session_picker_is_a_reply_keyboard(tmp_path, monkeypatch):
    """The picker must be REPLY-keyboard buttons that send '/pin-session <sid>'
    as a normal message — the poller runs allowed_updates:["message"] and an
    inline/callback_query picker would be a poll-contract change (the
    _ask_which_project precedent)."""
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    _, _, sends = _install_pin_doubles(
        monkeypatch, [_row("S-alice-dev-p3"), _row("S-alice-operator-p1", role="operator")]
    )

    result = TL.handle_update(cfg, {"update_id": 1, "message": _topic_slash("/pin-session")})

    assert result["action"] == "pin_session_ask"
    markup = sends[-1]["reply_markup"]
    assert "inline_keyboard" not in markup
    btns = [b["text"] for row in markup["keyboard"] for b in row]
    assert "/pin-session S-alice-dev-p3" in btns
    assert "/pin-session S-alice-operator-p1" in btns
    assert "/pin-session off" in btns          # the way back to the attendant
    assert sends[-1]["thread_id"] == _PIN_THREAD


def test_pin_session_picker_skips_suspended_and_archived(tmp_path, monkeypatch):
    """A binding pointing at a dead session would make the topic silently eat
    every message (_handle_reply's "not active — dropped" branch), so those
    rows are never offered."""
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    _, _, sends = _install_pin_doubles(monkeypatch, [
        _row("S-alice-dev-p3"),
        _row("S-alice-old-p2", status="suspended"),
        _row("S-alice-gone-p9", archived=True),
    ])

    TL.handle_update(cfg, {"update_id": 1, "message": _topic_slash("/pin-session")})

    btns = [b["text"] for row in sends[-1]["reply_markup"]["keyboard"] for b in row]
    assert btns == ["/pin-session S-alice-dev-p3", "/pin-session off"]


def test_pin_session_picker_offers_the_topics_own_task_session_first(tmp_path, monkeypatch):
    """A T-0660 per-task topic binds a ticket_id — the session working THAT
    ticket is the obvious pick, so it heads the list of look-alike SIDs."""
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg, ticket_id="T-0677")
    _, _, sends = _install_pin_doubles(monkeypatch, [
        _row("S-alice-aaa-p1", task_id="T-0001"),
        _row("S-alice-zzz-p9", task_id="T-0677"),
    ])

    TL.handle_update(cfg, {"update_id": 1, "message": _topic_slash("/pin-session")})

    btns = [b["text"] for row in sends[-1]["reply_markup"]["keyboard"] for b in row]
    assert btns[0] == "/pin-session S-alice-zzz-p9"


def test_pin_session_sets_direct_mode_and_pins_the_confirmation(tmp_path, monkeypatch):
    """The core of the ask: choosing a session points the topic binding at it
    (the existing T-0660 direct-route field) AND pins the confirmation message
    in that topic."""
    from bot_squad_worker import tg_bindings
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg, ticket_id="T-0677")
    fake, _, _ = _install_pin_doubles(monkeypatch, [_row("S-alice-dev-p3")])

    result = TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_slash("/pin-session S-alice-dev-p3")})

    assert result == {"ok": True, "action": "pin_session_set",
                      "slug": "test-project", "sid": "S-alice-dev-p3",
                      "pinned": True}
    rec = tg_bindings.resolve(cfg, _PIN_CHAT, _PIN_THREAD)
    assert rec["session_id"] == "S-alice-dev-p3"
    assert rec["slug"] == "test-project" and rec["ticket_id"] == "T-0677"
    assert rec["pinned_message_id"] == 777
    assert fake.sent[-1]["topic_id"] == _PIN_THREAD   # pinned IN the topic
    assert "S-alice-dev-p3" in fake.sent[-1]["text"]


def test_pin_session_flips_routing_to_the_pinned_session(tmp_path, monkeypatch):
    """The payoff: after pinning, a plain message in the topic is injected
    straight into that session instead of waking the project's attendant."""
    import bot_squad_worker.actions as A
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    _install_pin_doubles(monkeypatch, [_row("S-alice-dev-p3")])
    TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_slash("/pin-session S-alice-dev-p3")})

    injected: list[dict] = []
    monkeypatch.setattr(A, "dispatch",
                        lambda name, params: injected.append((name, params)) or {"ok": True})
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug="": {"global_user_id": "gu_1", "created": False})
    monkeypatch.setattr(TL, "append_conversation_fyi", lambda *a, **k: None)
    ensured: list = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: ensured.append(a) or None)

    plain = _topic_slash("what's the status?")
    result = TL.handle_update(cfg, {"update_id": 2, "message": plain})

    assert result["action"] == "task_topic_inject"
    # T-0770: the pinned session gets the provenance envelope (one block), not
    # the bare text — see the T-0770 block below for what it must contain.
    assert len(injected) == 1
    verb, params = injected[0]
    assert verb == "inject_prompt"
    assert params["sid"] == "S-alice-dev-p3"
    assert "what's the status?" in params["text"]
    assert not ensured, "direct mode must bypass the user-conversation attendant"


def test_pin_session_off_returns_to_the_attendant_and_unpins(tmp_path, monkeypatch):
    """Attendant-routed is the DEFAULT, so there must be a way back — and the
    stale pin must go with it (it is the topic's visible claim about routing)."""
    from bot_squad_worker import tg_bindings
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    fake, notices, _ = _install_pin_doubles(monkeypatch, [_row("S-alice-dev-p3")])
    TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_slash("/pin-session S-alice-dev-p3")})

    result = TL.handle_update(
        cfg, {"update_id": 2, "message": _topic_slash("/pin-session off")})

    assert result["action"] == "pin_session_cleared"
    rec = tg_bindings.resolve(cfg, _PIN_CHAT, _PIN_THREAD)
    assert rec["session_id"] is None and rec["pinned_message_id"] is None
    assert rec["slug"] == "test-project"
    assert fake.unpinned == [777]
    assert "attendant" in notices[-1]


def test_pin_session_repoint_unpins_the_previous_marker(tmp_path, monkeypatch):
    """Two pins claiming two different targets is worse than one — re-pointing
    drops the old marker first."""
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    fake, _, _ = _install_pin_doubles(
        monkeypatch, [_row("S-alice-dev-p3"), _row("S-alice-other-p4")])
    TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_slash("/pin-session S-alice-dev-p3")})
    TL.handle_update(
        cfg, {"update_id": 2, "message": _topic_slash("/pin-session S-alice-other-p4")})

    assert fake.unpinned == [777]
    assert len(fake.sent) == 2


def test_pin_session_unknown_session_refuses_and_reoffers_the_picker(tmp_path, monkeypatch):
    from bot_squad_worker import tg_bindings
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    fake, notices, sends = _install_pin_doubles(monkeypatch, [_row("S-alice-dev-p3")])

    result = TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_slash("/pin-session S-nope-p0")})

    assert result["action"] == "pin_session_unknown"
    assert tg_bindings.resolve(cfg, _PIN_CHAT, _PIN_THREAD)["session_id"] is None
    assert not fake.sent, "nothing may be pinned when nothing was bound"
    assert "S-nope-p0" in notices[-1]
    assert sends[-1]["reply_markup"]["keyboard"]      # picker re-offered


def test_pin_session_resolves_a_session_alias(tmp_path, monkeypatch):
    """T-0662 aliases exist so a session can be addressed by a short nickname
    instead of a long SID — accept one here too."""
    from bot_squad_worker import session_aliases, tg_bindings
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    _install_pin_doubles(monkeypatch, [_row("S-alice-dev-p3")])
    session_aliases.set_alias(cfg.data_dir, "builder", "S-alice-dev-p3")

    result = TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_slash("/pin-session builder")})

    assert result["action"] == "pin_session_set"
    assert tg_bindings.resolve(cfg, _PIN_CHAT, _PIN_THREAD)["session_id"] == "S-alice-dev-p3"


def test_pin_session_in_an_unbound_chat_refuses(tmp_path, monkeypatch):
    """No binding = no project = no candidate set. Refuse and say how to fix it
    rather than guessing a project for the topic (the T-0693 lesson)."""
    cfg = _pin_cfg(tmp_path)
    fake, notices, _ = _install_pin_doubles(monkeypatch, [_row("S-alice-dev-p3")])

    result = TL.handle_update(cfg, {
        "update_id": 1,
        "message": _topic_slash("/pin-session", chat_id="12345", thread_id=None),
    })

    assert result["action"] == "pin_session_unbound"
    assert not fake.sent
    assert "bsq topic bind" in notices[-1]


def test_pin_session_reports_a_failed_pin_but_keeps_the_routing(tmp_path, monkeypatch):
    """A missing can_pin_messages right must not swallow the toggle — the
    routing change stands and the user is told the marker is missing."""
    from bot_squad_worker import tg_bindings
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    _, notices, _ = _install_pin_doubles(
        monkeypatch, [_row("S-alice-dev-p3")], pinned=False)

    result = TL.handle_update(
        cfg, {"update_id": 1, "message": _topic_slash("/pin-session S-alice-dev-p3")})

    assert result["ok"] is True and result["pinned"] is False
    assert tg_bindings.resolve(cfg, _PIN_CHAT, _PIN_THREAD)["session_id"] == "S-alice-dev-p3"
    assert "Pin messages" in notices[-1]


def test_pin_session_with_no_live_sessions_says_so(tmp_path, monkeypatch):
    cfg = _pin_cfg(tmp_path)
    _bind_topic(cfg)
    _, notices, sends = _install_pin_doubles(monkeypatch, [])

    result = TL.handle_update(cfg, {"update_id": 1, "message": _topic_slash("/pin-session")})

    assert result["action"] == "pin_session_no_candidates"
    assert not sends, "no picker without candidates"
    assert "No live sessions" in notices[-1]


# ---------------------------------------------------------------------------
# T-0300 — `/remote-control`: continue a session in the Claude app
#
# Stakeholder verbatim (T-0155): "...which might also be a /remote-control
# command for me to continue in the claude app". Until T-0300 the handoff was
# only a footer on a stall escalation; these pin it as an on-demand command.
# ---------------------------------------------------------------------------

def _rc_cfg(tmp_path: Path, *, url: str = "", projects=("test-project",)):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    cfg.tg_remote_control_url = url
    cfg.projects = {s: types.SimpleNamespace(tg_chat="12345") for s in projects}
    return cfg


def _rc_sessions(monkeypatch, by_slug: dict):
    """Stub the live-session source `_pinnable_sessions` reads."""
    import bot_squad_worker.sessions as S
    monkeypatch.setattr(
        S, "list_sessions",
        lambda _cfg, slug: [
            {"sid": sid, "window": sid, "status": "active", "role": "dev"}
            for sid in by_slug.get(slug, [])
        ],
    )


def _rc_capture(monkeypatch):
    """Capture the two outbound funnels; return (notices, pickers)."""
    notices: list[str] = []
    pickers: list[dict] = []
    monkeypatch.setattr(TL, "_notify",
                        lambda cfg, chat_id, text, **k: notices.append(text))
    monkeypatch.setattr(
        TL, "_channel_notify",
        lambda cfg, chat_id, text, *, reply_markup=None, **k:
            pickers.append({"text": text, "markup": reply_markup}),
    )
    return notices, pickers


def _rc_pane(monkeypatch, session_name):
    import bot_squad_worker.tg_stall as TS
    monkeypatch.setattr(
        TS, "_pane_for_sid",
        lambda sid: types.SimpleNamespace(session=session_name) if session_name else None,
    )


def test_extract_slash_command_remote_control():
    assert TL.extract_slash_command({"text": "/remote-control S-a-b-p1"}) == (
        "remote-control", "S-a-b-p1")
    # TG autocomplete can only offer [a-z0-9_]; both spellings normalize.
    assert TL.extract_slash_command({"text": "/remote_control"}) == (
        "remote-control", "")
    assert TL.extract_slash_command({"text": "/remote-control@thebot S-x"}) == (
        "remote-control", "S-x")


def test_remote_control_bare_offers_picker(tmp_path, monkeypatch):
    cfg = _rc_cfg(tmp_path)
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1", "S-a-two-p2"]})
    notices, pickers = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "12345", "")

    assert r["ok"] and r["action"] == "remote_control_ask" and r["count"] == 2
    buttons = [b[0]["text"] for b in pickers[0]["markup"]["keyboard"]]
    assert buttons == ["/remote-control S-a-one-p1", "/remote-control S-a-two-p2"]
    # No `off` button — unlike /pin-session this toggles nothing.
    assert all(b.startswith("/remote-control S-") for b in buttons)


def test_remote_control_named_session_emits_tmux_fallback(tmp_path, monkeypatch):
    """No remote_control_url configured (the LIVE install today) -> tmux attach."""
    cfg = _rc_cfg(tmp_path, url="")
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1"]})
    _rc_pane(monkeypatch, "one-window")
    notices, _ = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "12345", "S-a-one-p1")

    assert r["ok"] and r["sid"] == "S-a-one-p1"
    assert "tmux attach -t one-window" in notices[-1]
    assert "S-a-one-p1" in notices[-1]


def test_remote_control_named_session_emits_claude_app_url(tmp_path, monkeypatch):
    cfg = _rc_cfg(tmp_path, url="https://claude.ai/code?session={sid}")
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1"]})
    _rc_pane(monkeypatch, "one-window")
    notices, _ = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "12345", "S-a-one-p1")

    assert r["ok"]
    assert "https://claude.ai/code?session=S-a-one-p1" in notices[-1]
    assert "tmux attach" not in notices[-1]


def test_remote_control_resolves_a_sid_from_another_project(tmp_path, monkeypatch):
    """Regression for a defect the T-0158 manual walkthrough found on LIVE.

    The stakeholder's chat statically resolves to ONE project (`watchrobot` on
    the live install), so a project-scoped lookup refused a bot-squad SID typed
    in that very chat. A SID is globally unique and this command changes no
    routing, so it resolves across projects — chat's own project first."""
    cfg = _rc_cfg(tmp_path, projects=("test-project", "other-project"))
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1"],
                               "other-project": ["S-a-far-p9"]})
    _rc_pane(monkeypatch, "far-window")
    notices, _ = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "12345", "S-a-far-p9")

    assert r["ok"] and r["sid"] == "S-a-far-p9"
    assert r["slug"] == "other-project", "must report the OWNING project"
    assert "tmux attach -t far-window" in notices[-1]


def test_remote_control_picker_spans_projects_local_first(tmp_path, monkeypatch):
    cfg = _rc_cfg(tmp_path, projects=("test-project", "other-project"))
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1"],
                               "other-project": ["S-a-far-p9"]})
    _rc_capture(monkeypatch)
    notices, pickers = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "12345", "")

    assert r["count"] == 2
    buttons = [b[0]["text"] for b in pickers[0]["markup"]["keyboard"]]
    assert buttons[0].endswith("S-a-one-p1"), "chat's own project ranks first"
    # The list mixes projects, so each row must name its own.
    assert "test-project" in pickers[0]["text"]
    assert "other-project" in pickers[0]["text"]


def test_remote_control_unknown_session_refuses_and_re_offers(tmp_path, monkeypatch):
    cfg = _rc_cfg(tmp_path)
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1"]})
    notices, pickers = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "12345", "nonsense")

    assert not r["ok"] and r["action"] == "remote_control_unknown"
    assert "No live session 'nonsense'" in notices[0]
    # Never emit a handoff line with an empty target.
    assert not any("Remote-control" in n for n in notices)
    assert pickers, "re-offers the picker"


def test_remote_control_bare_in_pinned_topic_uses_that_session(tmp_path, monkeypatch):
    """He is already talking to the pinned session — re-asking would be noise."""
    cfg = _rc_cfg(tmp_path)
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1", "S-a-two-p2"]})
    _rc_pane(monkeypatch, "two-window")
    notices, pickers = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(
        cfg, "12345", "", thread_id=7,
        binding={"slug": "test-project", "session_id": "S-a-two-p2"},
    )

    assert r["ok"] and r["sid"] == "S-a-two-p2"
    assert not pickers, "no picker when the topic already names a session"
    assert "tmux attach -t two-window" in notices[-1]


def test_remote_control_without_url_or_pane_says_so(tmp_path, monkeypatch):
    """Neither affordance exists — say which, don't send an empty handoff."""
    cfg = _rc_cfg(tmp_path, url="")
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1"]})
    _rc_pane(monkeypatch, None)          # no live tmux pane
    notices, _ = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "12345", "S-a-one-p1")

    assert not r["ok"] and r["action"] == "remote_control_unavailable"
    assert "nothing to hand over" in notices[-1]
    assert "Remote-control" not in notices[-1]


def test_remote_control_unregistered_chat_refuses(tmp_path, monkeypatch):
    cfg = _rc_cfg(tmp_path)
    notices, _ = _rc_capture(monkeypatch)

    r = TL._handle_remote_control(cfg, "99999", "")

    assert not r["ok"] and r["action"] == "remote_control_no_project"
    assert "isn't linked to a registered project" in notices[-1]


def test_handle_update_dispatches_remote_control_slash(tmp_path, monkeypatch):
    """End-to-end through handle_update — the command is actually wired."""
    cfg = _rc_cfg(tmp_path, url="https://claude.ai/code?session={sid}")
    _rc_sessions(monkeypatch, {"test-project": ["S-a-one-p1"]})
    _rc_pane(monkeypatch, "one-window")
    notices, _ = _rc_capture(monkeypatch)

    result = TL.handle_update(cfg, {
        "update_id": 9,
        "message": _slash_message("/remote-control S-a-one-p1", chat_id=12345),
    })

    assert result["ok"] and result["action"] == "remote_control"
    assert "https://claude.ai/code?session=S-a-one-p1" in notices[-1]


def test_help_lists_remote_control(tmp_path, monkeypatch):
    cfg = _rc_cfg(tmp_path)
    notices, _ = _rc_capture(monkeypatch)

    TL._handle_slash(cfg, "12345", "help", "")

    assert "/remote-control" in notices[-1]


# ---------------------------------------------------------------------------
# T-0746 item (c): append_conversation asks WHO WROTE this, not who sent it.
# The live record it prevents is a machine-generated notice stored as the
# stakeholder's own words — see bot_squad_worker.echo_guard.
# ---------------------------------------------------------------------------

_T0746_NOTICE = ("❌ session S-almdudleer-rv-pair-trading-signals-poc-review-real--p266 "
                 "not active — message dropped")


def _capture_post(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    return captured, fake_post


def test_append_conversation_records_our_echoed_notice_as_system(tmp_path, monkeypatch):
    """THE regression. Our own notice, forwarded back from our own bot, must
    never land as author=user."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    msg = {
        "from": _from(), "text": _T0746_NOTICE, "date": 1750000000,
        "forward_origin": {"type": "user", "date": 1750000000,
                           "sender_user": {"id": 8206895402, "is_bot": True}},
    }
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert captured["json"]["author"] == "system:bot-echo"
    assert captured["json"]["forwarded_from"] == "bot"
    # T-0755 derives `system:` as OUTBOUND; this one reached us, so it must say so.
    assert captured["json"]["direction"] == "in"
    # The body is preserved verbatim — the attribution changed, not the record.
    assert captured["json"]["text"] == _T0746_NOTICE


def test_append_conversation_annotates_someone_elses_forward(tmp_path, monkeypatch):
    """A human wrote it, so `user` stays right — but the record must not imply
    the SENDER composed it."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    msg = {"from": _from(), "text": "глянь что пишут", "date": 1750000000,
           "forward_origin": {"type": "user", "date": 1750000000,
                              "sender_user": {"id": 999, "is_bot": False}}}
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert captured["json"]["author"] == "user"
    assert captured["json"]["forwarded_from"] == "user:999"
    assert "direction" not in captured["json"]


def test_append_conversation_payload_unchanged_for_a_typed_message(tmp_path, monkeypatch):
    """NEGATIVE GUARD — passes before AND after T-0746. A genuine message's
    request body must be byte-identical to the pre-fix one; this is the
    overwhelming common case and the fix must not touch it."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    msg = {"from": _from(), "text": "deploy please", "date": 1750000000}
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert captured["json"] == {
        "author": "user", "text": "deploy please",
        "attachments": [], "timestamp": TL._msg_ts(msg),
    }


def test_append_conversation_echo_keeps_thread_and_general_feed(tmp_path, monkeypatch):
    """The routing fields are orthogonal to authorship — an echo arriving in a
    bound topic still belongs to that topic's thread."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    msg = {"from": _from(), "text": _T0746_NOTICE, "date": 1750000000,
           "forward_origin": {"type": "user", "date": 1,
                              "sender_user": {"id": 8206895402, "is_bot": True}}}
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg, thread_id=275)

    assert captured["json"]["thread_id"] == 275
    assert captured["json"]["author"] == "system:bot-echo"


def test_append_conversation_survives_a_broken_echo_guard(tmp_path, monkeypatch):
    """A classifier failure must degrade to the pre-T-0746 behaviour, not break
    inbound routing."""
    from bot_squad_worker import echo_guard
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    monkeypatch.setattr(echo_guard, "is_own_bot_forward",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    msg = {"from": _from(), "text": "deploy please", "date": 1750000000}
    with patch("httpx.post", side_effect=fake_post):
        ok = TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    assert ok is True
    assert captured["json"]["author"] == "user"


# ---------------------------------------------------------------------------
# T-0786 — a photo CAPTION is his own typed words, and every inbound reader
# asked only for `msg["text"]`.
#
# Reproduced against b7182ec on the FULL inbound path (`handle_update`), not on
# the helpers: the task-topic envelope's verbatim fence came out '', the
# answer-owed ledger stored text='' so the T-0770 reminder quoted «» back at the
# session, the `system:direct-reply` FYI line ended at its colon, and the
# durable store recorded text: '' on both the bound-topic and DM paths. Each
# run below is paired with the plain-TEXT control that discriminated.
# ---------------------------------------------------------------------------

_T0786_WORDS = "вот скрин, посмотри"


def _photo_msg_with_caption(caption, *, chat_id=111, thread_id=42):
    """A real photo-with-caption message: no `text` key exists at all."""
    msg = _topic_msg(None, chat_id=chat_id, thread_id=thread_id)
    del msg["text"]
    msg["photo"] = _photo_variants()
    if caption is not None:
        msg["caption"] = caption
    return msg


def _bound_session_probe(tmp_path, monkeypatch, *, thread_id=42):
    """Wire the session-bound task topic and capture all three of its writers."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", thread_id, "beta",
                            ticket_id="T-0786", session_id="S-dev-p9")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    seen = {"injected": [], "fyi": []}
    monkeypatch.setattr(A, "dispatch", lambda name, params: (
        seen["injected"].append((name, params)) or {"ok": True}))
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda c, s, g, *, author, text, reply_to=None: (
                            seen["fyi"].append({"author": author, "text": text}) or True))
    return cfg, seen


def _fence(envelope):
    """Only the verbatim fence — scoped to the thing under test (T-0777), so a
    caption appearing in a header could never pass this for the wrong reason."""
    assert "--- 8< ---" in envelope, envelope
    return envelope.split("--- 8< ---", 1)[1].split("--- >8 ---", 1)[0].strip("\n")


def test_photo_caption_reaches_the_session_verbatim(tmp_path, monkeypatch):
    """RED PIN. The words that say what the picture is FOR, delivered into the
    session's composer — measured '' at b7182ec through this exact path."""
    cfg, seen = _bound_session_probe(tmp_path, monkeypatch)
    result = TL.handle_update(cfg, {"update_id": 1,
                                    "message": _photo_msg_with_caption(_T0786_WORDS)})

    assert result["action"] == "task_topic_inject"
    assert _fence(seen["injected"][0][1]["text"]) == _T0786_WORDS


def test_photo_caption_control_plain_text_delivers_identically(tmp_path, monkeypatch):
    """THE CONTROL that makes the pin above a reading and not a blind
    instrument: the same words typed as ordinary text reach the same fence.
    GREEN at b7182ec — it is the working half."""
    cfg, seen = _bound_session_probe(tmp_path, monkeypatch)
    TL.handle_update(cfg, {"update_id": 1,
                           "message": _topic_msg(_T0786_WORDS, chat_id=111, thread_id=42)})

    assert _fence(seen["injected"][0][1]["text"]) == _T0786_WORDS


def test_photo_caption_lands_in_the_answer_owed_ledger(tmp_path, monkeypatch):
    """RED PIN, writer 2 of 4. `record_owed` stored text='' for a captioned
    photo, so T-0770's re-drive nagged the session with `He wrote: «»` — a
    reminder quoting nothing at all."""
    from bot_squad_worker import tg_direct_reply
    cfg, _seen = _bound_session_probe(tmp_path, monkeypatch)
    TL.handle_update(cfg, {"update_id": 1,
                           "message": _photo_msg_with_caption(_T0786_WORDS)})

    owed = list(tg_direct_reply.load(cfg).values())
    assert len(owed) == 1 and owed[0]["text"] == _T0786_WORDS
    reminder = tg_direct_reply.compose_reminder(owed[0], attempt=1, attempts_left=1)
    assert f"«{_T0786_WORDS}»" in reminder and "«»" not in reminder


def test_photo_caption_lands_in_the_direct_reply_fyi_record(tmp_path, monkeypatch):
    """RED PIN, writer 3 of 4. The attendant's context line read «Пользователь
    ответил сессии … напрямую: » with nothing after the colon."""
    cfg, seen = _bound_session_probe(tmp_path, monkeypatch)
    TL.handle_update(cfg, {"update_id": 1,
                           "message": _photo_msg_with_caption(_T0786_WORDS)})

    assert len(seen["fyi"]) == 1
    rec = seen["fyi"][0]
    assert rec["text"].endswith(f"напрямую: {_T0786_WORDS}")
    # T-0746 stays in force: the system prose around his words keeps the
    # `system:` authorship it has always had. Reading a caption changes WHOSE
    # words are quoted, never who the record says wrote the line.
    assert rec["author"] == "system:direct-reply"


def test_photo_caption_is_recorded_in_the_durable_store(tmp_path, monkeypatch):
    """RED PIN, writer 4 of 4 — the store record, driven through
    `append_conversation` itself so the payload is read as sent."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    msg = {"from": _from(), "date": 1750000000,
           "photo": _photo_variants(), "caption": _T0786_WORDS}
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc", msg)

    body = captured["json"]
    assert body["text"] == _T0786_WORDS
    # His own words, so the authorship is unchanged and NOTHING synthetic is
    # stapled on — the record says exactly what he typed and what arrived.
    assert body["author"] == "user"
    assert body["attachments"] == [{"type": "photo", "file_id": "FULL_1280"}]


def test_uncaptioned_photo_still_records_empty_text(tmp_path, monkeypatch):
    """GREEN REGRESSION GUARD, not a defect pin — passes at b7182ec too. A photo
    sent with nothing typed is not a caption we lost: the record stays empty
    rather than gaining a marker. That "he sent a photo" is stated by the
    ATTACHMENT descriptor (T-0782/T-0785), where a fact about the image
    belongs."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc",
                               {"from": _from(), "date": 1750000000,
                                "photo": _photo_variants()})

    assert captured["json"]["text"] == ""
    assert captured["json"]["attachments"] == [{"type": "photo", "file_id": "FULL_1280"}]


def test_plain_text_record_is_unchanged_by_caption_support(tmp_path, monkeypatch):
    """GREEN REGRESSION GUARD. The overwhelming common case must be
    byte-identical to before: an ordinary typed message has no caption, so this
    reads exactly what `msg.get("text") or ""` read."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    with patch("httpx.post", side_effect=fake_post):
        TL.append_conversation(cfg, "test-project", "gu_abc",
                               {"from": _from(), "date": 1750000000, "text": "deploy please"})

    assert captured["json"]["text"] == "deploy please"
    assert captured["json"]["attachments"] == []


def test_bound_project_topic_records_the_caption_full_path(tmp_path, monkeypatch):
    """RED PIN — the store record via the FULL inbound path this time (a bound
    project topic with no session_id), so the fix is pinned at the seam it
    ships through and not only at the function."""
    from bot_squad_worker import tg_bindings
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 43, "beta")      # no session_id
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: None)
    posted = []
    monkeypatch.setattr(TL, "_post_conversation",
                        lambda c, s, g, payload: (posted.append(payload) or True))

    msg = _photo_msg_with_caption(_T0786_WORDS, thread_id=43)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route_bound_topic"
    assert [p["text"] for p in posted] == [_T0786_WORDS]


def test_extract_reply_target_reads_a_photo_caption():
    """RED PIN — the FIFTH carrier, found by re-scanning the file rather than
    stopping at the four the ticket names. A reply to a session's message with a
    photo + «вот это» returned (sid, ''), and `_handle_reply` then refused it as
    «нечего передавать (пустой текст)»: not recorded-empty, never delivered."""
    msg = {"message_id": 200, "chat": {"id": 111, "type": "private"},
           "photo": _photo_variants(), "caption": _T0786_WORDS,
           "reply_to_message": {"message_id": 100,
                                "text": "[S-dev-p9] needs your input — waiting"}}
    assert TL.extract_reply_target(msg) == ("S-dev-p9", _T0786_WORDS)


def test_extract_reply_target_plain_text_unchanged():
    """GREEN REGRESSION GUARD for the same seam."""
    msg = {"message_id": 200, "chat": {"id": 111, "type": "private"},
           "text": _T0786_WORDS,
           "reply_to_message": {"message_id": 100,
                                "text": "[S-dev-p9] needs your input — waiting"}}
    assert TL.extract_reply_target(msg) == ("S-dev-p9", _T0786_WORDS)


def test_a_slash_command_in_a_caption_is_still_content_not_a_command():
    """GREEN REGRESSION GUARD, and a SCOPE DECISION written down.
    `extract_slash_command` deliberately keeps reading `text` only. Teaching it
    captions would make a photo captioned "/project beta" switch projects AND —
    because T-0659 deliberately does not append slash commands to the store —
    stop his words being recorded at all. That inverts this ticket, so the
    caption stays content."""
    assert TL.extract_slash_command({"caption": "/project beta"}) is None
    assert TL.extract_slash_command({"text": "/project beta"}) == ("project", "beta")


def test_junk_caption_does_not_lose_the_message(tmp_path, monkeypatch):
    """DEFENSIVE COERCION at the seam: the inbound path must degrade to "no
    words", never raise. `append_conversation`'s callers do not wrap it."""
    cfg = _make_cfg(tmp_path, bot_token="8206895402:SECRET")
    _link_env(monkeypatch)
    captured, fake_post = _capture_post(monkeypatch)
    with patch("httpx.post", side_effect=fake_post):
        ok = TL.append_conversation(cfg, "test-project", "gu_abc",
                                    {"from": _from(), "date": 1750000000,
                                     "photo": _photo_variants(),
                                     "caption": {"unexpected": "shape"}})

    assert ok is True
    assert captured["json"]["attachments"] == [{"type": "photo", "file_id": "FULL_1280"}]


# --- T-0830 / D-0069 — the intake call site for the drive-SCOPE ladder ------
#
# TL p534 ruled this wiring into T-0830 (14:58Z): a setter nothing calls is not
# a delivered lane, and recognition that cannot take effect is the same silence
# he complained about. `_maybe_switch_drive_scope` is the ONLY production caller
# of dispatch.apply_drive_scope.

# His words, 2026-07-30T07:54:16Z, verbatim. HUMAN-ONLY.
_HIS_SENTENCE = (
    "В третьих, нужно более чёткое понимание для меня, какой режим драйва щас "
    "стоит, я просил закончить всё что в опен, но видимо это не "
    "интерпретировалось как переключить режим драйва"
)


def _scope_cfg(tmp_path, monkeypatch):
    """A cfg whose data dir pace.py can actually write into, with the routing
    machinery around the seam stubbed out."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    (cfg.data_dir / "test-project" / "_worker").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        TL, "resolve_or_link_sender",
        lambda c, m, slug: {"global_user_id": "gu_alexey", "created": False, "slug": slug},
    )
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "test-project")
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: True)
    from bot_squad_worker import conversation_locus
    monkeypatch.setattr(conversation_locus, "set_locus", lambda *a, **k: None)
    return cfg


def _update(text: str) -> dict:
    return {"update_id": 1,
            "message": {"chat": {"id": 12345}, "from": _from(), "text": text}}


def test_inbound_scope_instruction_switches_and_confirms(tmp_path, monkeypatch):
    """THE end-to-end acceptance test: his recorded sentence arrives on the real
    intake path, the scope is SET, and he is TOLD."""
    from bot_squad_worker import pace

    cfg = _scope_cfg(tmp_path, monkeypatch)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat, text, **kw: echoes.append(text))

    TL.handle_update(cfg, _update(_HIS_SENTENCE))

    block = pace.read_drive(cfg, "test-project")
    assert block["scope"] == "open_reopened"
    assert block["configured"] is True
    assert block["set_by"] == "tg:gu_alexey"
    assert block["source_text"] == "закончить всё что в опен"

    # DoD 4 — the confirmation. Silence here IS the defect he reported.
    assert echoes, "recognised switch produced NO confirmation"
    assert any(t == "Режим драйва: Open / Reopened." for t in echoes), echoes


def test_inbound_scope_switch_does_not_intercept_routing(tmp_path, monkeypatch):
    """NON-INTERCEPTING. «закончить всё что в опен» is a scope switch AND work
    he wants done — the message must still route to the attendant. T-0666 had
    already removed one intercepting confirm prompt from this path."""
    cfg = _scope_cfg(tmp_path, monkeypatch)
    ensured = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda *a, **k: ensured.append(a) or True)
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: None)

    result = TL.handle_update(cfg, _update(_HIS_SENTENCE))

    assert result["action"] == "route"
    assert result["slug"] == "test-project"
    assert ensured, "the attendant was not woken — the switch swallowed the message"


def test_inbound_passing_remark_switches_nothing_and_stays_silent(tmp_path, monkeypatch):
    """DoD 6 at the call site. A message that merely MENTIONS a status must not
    re-scope the project — and must not emit a confirmation either, or he learns
    to ignore them."""
    from bot_squad_worker import pace

    cfg = _scope_cfg(tmp_path, monkeypatch)
    echoes = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat, text, **kw: echoes.append(text))

    TL.handle_update(cfg, _update("кстати T-0719 всё ещё в опен, посмотри"))

    assert pace.read_drive(cfg, "test-project")["configured"] is False
    assert not any("Режим драйва" in t for t in echoes), echoes


def test_inbound_scope_failure_never_breaks_intake(tmp_path, monkeypatch):
    """Best-effort by construction: the durable record and the routing have
    already happened when this runs, so a setting must never cost them."""
    cfg = _scope_cfg(tmp_path, monkeypatch)
    from bot_squad_worker import dispatch as _dispatch
    monkeypatch.setattr(_dispatch, "apply_drive_scope",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: None)

    result = TL.handle_update(cfg, _update(_HIS_SENTENCE))

    assert result["action"] == "route"


def test_inbound_confirmation_replies_in_place_to_the_same_topic(tmp_path, monkeypatch):
    """The confirmation is a reply-in-place: it spells its own destination and
    passes NO msg_type, so no route map can redirect it away from the topic he
    typed in."""
    cfg = _scope_cfg(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(TL, "_channel_notify",
                        lambda c, chat, text, **kw: calls.append((chat, kw)))

    update = _update(_HIS_SENTENCE)
    update["message"]["message_thread_id"] = 77
    TL.handle_update(cfg, update)

    assert calls, "no confirmation was sent"
    chat, kw = calls[-1]
    assert chat == "12345"
    assert kw.get("thread_id") == 77
    assert "msg_type" not in kw


def test_inbound_forwarded_confirmation_does_not_reswitch(tmp_path, monkeypatch):
    """THE ECHO LOOP, closed at the call site.

    Our own words DO come back on this channel — echo_guard exists because that
    happened, through _handle_topic_bound, one of the two paths this ladder is
    wired to. Under the CHANNEL-authority rule a re-entering echo is authorised,
    which is exactly what makes it dangerous: nothing else rejects it.

    Guard is composed-by-sender, which is class-independent — it catches any of
    our text coming back, not one string's shape.
    """
    from bot_squad_worker import pace, echo_guard

    cfg = _scope_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: None)
    monkeypatch.setattr(
        echo_guard, "classify_inbound",
        lambda c, m, **k: {"author": echo_guard.ECHO_AUTHOR,
                           "forwarded_from": echo_guard.ECHO_ORIGIN,
                           "reason": "outbound-match"},
    )

    result = TL.handle_update(cfg, _update(_HIS_SENTENCE))

    assert pace.read_drive(cfg, "test-project")["configured"] is False
    # ...and the message still routes. The guard suppresses the SWITCH only.
    assert result["action"] == "route"


def test_inbound_forward_of_another_human_does_not_switch(tmp_path, monkeypatch):
    """He did not write it, so it is not his instruction — even though the
    author stays "user" (a human did compose it, just not the sender)."""
    from bot_squad_worker import pace, echo_guard

    cfg = _scope_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: None)
    monkeypatch.setattr(
        echo_guard, "classify_inbound",
        lambda c, m, **k: {"author": "user", "forwarded_from": "Someone Else",
                           "reason": "forwarded"},
    )

    TL.handle_update(cfg, _update(_HIS_SENTENCE))

    assert pace.read_drive(cfg, "test-project")["configured"] is False


def test_inbound_echo_guard_failure_does_not_switch(tmp_path, monkeypatch):
    """Fail CLOSED on the authority check specifically: if we cannot tell
    whether he composed it, we do not re-scope the project. (The rest of the
    helper fails open — it must never cost the record or the routing.)"""
    from bot_squad_worker import pace, echo_guard

    cfg = _scope_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: None)
    monkeypatch.setattr(
        echo_guard, "classify_inbound",
        lambda c, m, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    result = TL.handle_update(cfg, _update(_HIS_SENTENCE))

    assert pace.read_drive(cfg, "test-project")["configured"] is False
    assert result["action"] == "route"


@pytest.mark.parametrize("verdict", [
    {},                                          # empty
    None,                                        # not a dict at all
    {"forwarded_from": "", "reason": "x"},       # author key missing
    {"author": None, "forwarded_from": ""},      # author explicitly unknown
    {"author": "system:some-future-class", "forwarded_from": ""},
    "not-a-dict",
])
def test_inbound_unknown_echo_verdict_is_no_switch(tmp_path, monkeypatch, verdict):
    """An UNKNOWN verdict must never read as permission.

    The gate is a positive ALLOWLIST — author exactly "user" AND no forward
    provenance — not a denylist of known-bad classes. So a missing key, a None,
    a shape change, or an author class invented after this code was written all
    land on NO SWITCH rather than falling through to the permissive branch.
    This is the one place the fail-closed could quietly become fail-open.
    """
    from bot_squad_worker import pace, echo_guard

    cfg = _scope_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(TL, "_channel_notify", lambda *a, **k: None)
    monkeypatch.setattr(echo_guard, "classify_inbound", lambda c, m, **k: verdict)

    result = TL.handle_update(cfg, _update(_HIS_SENTENCE))

    assert pace.read_drive(cfg, "test-project")["configured"] is False
    assert result["action"] == "route"
