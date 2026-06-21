"""Tests for worker.tg_listener — all network + subprocess mocked."""
from __future__ import annotations

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


def test_handle_update_skips_non_allowlisted_chat(tmp_path):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    update = {
        "update_id": 2,
        "message": {"chat": {"id": 99999}, "text": "hi"},
    }
    result = TL.handle_update(cfg, update)
    assert result["action"] == "skip"
    assert "not allowlisted" in result["reason"]


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


def test_handle_update_dispatches_sessions_slash(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _slash_message("/sessions", chat_id=12345)
    update = {"update_id": 4, "message": msg}

    import bot_squad_worker.sessions as S
    monkeypatch.setattr(S, "list_sessions", lambda _cfg, _slug: [
        {"sid": "S-alice-spec5-p3", "window": "spec5", "status": "active"}
    ])

    # Mock _notify to avoid real HTTP
    monkeypatch.setattr(TL, "_notify", lambda cfg, chat_id, text: None)

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
    assert dispatch_calls[0] == ("inject_input", {"sid": "S-alice-spec5-p3", "text": "hello"})


def test_handle_update_inject_failed_notifies(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = _reply_message("S-alice-spec5-p3", "text", chat_id=12345)
    update = {"update_id": 6, "message": msg}

    import bot_squad_worker.actions as A
    monkeypatch.setattr(A, "dispatch", MagicMock(side_effect=A.ActionError("no pane")))

    notify_calls = []
    monkeypatch.setattr(TL, "_notify", lambda cfg, chat_id, text: notify_calls.append(text))

    result = TL.handle_update(cfg, update)
    assert result["ok"] is False
    assert result["action"] == "inject_failed"
    assert len(notify_calls) == 1
    assert "not active" in notify_calls[0]


def test_handle_update_skips_plain_message(tmp_path):
    """A plain (non-reply, non-slash) message in an allowlisted chat is skipped."""
    cfg = _make_cfg(tmp_path, tg_chat="12345")
    msg = {"chat": {"id": 12345}, "text": "just chatting"}
    update = {"update_id": 7, "message": msg}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "skip"


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


def test_notify_passes_proxy_when_configured(tmp_path):
    cfg = _make_cfg(tmp_path, proxy_url="socks5://10.0.0.1:1080")
    captured: dict = {}

    def fake_post(url, data=None, timeout=None, proxy=None):
        captured["proxy"] = proxy
        return MagicMock()

    with patch("httpx.post", side_effect=fake_post):
        TL._notify(cfg, "12345", "hello")
    assert captured["proxy"] == "socks5://10.0.0.1:1080"


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
