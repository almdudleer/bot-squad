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
                        lambda cfg, slug, gid, m: append_calls.append((slug, gid)))
    # _handle_project reads/sets the pin over HTTP — stub it out.
    monkeypatch.setattr(TL, "_handle_project",
                        lambda cfg, chat_id, gid, args: {"ok": True, "action": "project", "slug": args})

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
                        lambda cfg, slug, gid, m: append_calls.append((slug, gid)))
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
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat: None)
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
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat: None)
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
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat: None)
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
    monkeypatch.setattr(TL, "_notify", lambda c, chat, text: notify.append(text))

    update = {"update_id": 1, "message": {"chat": {"id": 111}, "from": _from(), "text": "/project beta"}}
    result = TL.handle_update(cfg, update)
    assert result["action"] == "project_set"
    assert result["slug"] == "beta"
    assert set_calls == [("gu_1", "beta")]
    assert any("beta" in t for t in notify)  # "you're on project beta"


def test_handle_update_project_command_unknown_slug(tmp_path, monkeypatch):
    cfg = _make_multi_cfg(tmp_path, chat="111")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})
    monkeypatch.setattr(TL, "append_conversation", lambda *a, **k: None)
    monkeypatch.setattr(TL, "set_current_project",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not set")))
    asks = []
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat: asks.append(chat))
    monkeypatch.setattr(TL, "_notify", lambda c, chat, text: None)

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
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat: asks.append(chat))

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
    monkeypatch.setattr(TL, "_ask_which_project", lambda c, chat: asks.append(chat))

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
                        lambda c, slug, gid, ref: calls.append((slug, gid, ref)))

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
                        lambda c, slug, gid, ref: routed.append((slug, gid)))

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
                        lambda c, slug, gid, ref: ensures.append((slug, gid)))
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
                        lambda c, slug, gid, ref: ensures.append((slug, gid)))

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
                        lambda c, slug, gid, m: appended.append(slug))
    monkeypatch.setattr(TL, "get_current_project", lambda c, gid: "alpha")  # pinned alpha
    ensures = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref: ensures.append((slug, gid)))

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
                        lambda c, slug, gid, ref: ensures.append((slug, gid)))

    msg = _topic_msg("no binding for this thread", chat_id=111, thread_id=999)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route" and result["slug"] == "alpha"
    assert ensures == [("alpha", "gu_1")]


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
                        lambda c, slug, gid, ref: ensures.append(slug))

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
                        lambda c, chat_id, text, **k: notices.append((chat_id, text)))

    msg = _topic_msg("x", chat_id=111, thread_id=7)
    result = TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert result["action"] == "route_parked" and result["slug"] == "beta"
    assert len(notices) == 1 and notices[0][0] == "111"
    assert "заняты" in notices[0][1]


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
    assert injected == [("inject_input", {"sid": "S-dev-p9", "text": "fix the flaky test please"})]


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
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda cfg, slug, gid, *, author, text: fyi_calls.append(
                            {"slug": slug, "gid": gid, "author": author, "text": text}
                        ))

    msg = _topic_msg("looks good, ship it", chat_id=111, thread_id=42)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})

    assert len(fyi_calls) == 1
    call = fyi_calls[0]
    assert call["slug"] == "beta"
    assert call["gid"] == "gu_1"
    assert call["author"] == "user"
    assert "S-dev-p9" in call["text"]
    assert "looks good, ship it" in call["text"]


def test_handle_topic_bound_session_id_records_fyi_even_on_inject_failure(tmp_path, monkeypatch):
    """The attendant should still learn the stakeholder tried to reach the
    session, even if that session is no longer active to receive it."""
    from bot_squad_worker import tg_bindings
    import bot_squad_worker.actions as A
    cfg = _make_multi_cfg(tmp_path, chat="111")
    tg_bindings.set_binding(cfg, "111", 42, "beta", session_id="S-gone-p1")
    monkeypatch.setattr(TL, "resolve_or_link_sender",
                        lambda c, m, slug: {"global_user_id": "gu_1", "slug": slug})

    def _raise(name, params):
        raise A.ActionError("no such pane")
    monkeypatch.setattr(A, "dispatch", _raise)
    monkeypatch.setattr(TL, "_notify", lambda *a, **k: None)
    fyi_calls = []
    monkeypatch.setattr(TL, "append_conversation_fyi",
                        lambda *a, **k: fyi_calls.append((a, k)))

    msg = _topic_msg("hello?", chat_id=111, thread_id=42)
    TL.handle_update(cfg, {"update_id": 1, "message": msg})
    assert len(fyi_calls) == 1


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

    rec = conversation_locus.get_locus(cfg, "beta", "gu_1")
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

    rec = conversation_locus.get_locus(cfg, "alpha", "gu_1")
    assert rec["chat_id"] == "111" and rec["thread_id"] == 999


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
        lambda c, slug, gid, msg: recorded.append((slug, gid, msg.get("text"), msg.get("voice"))) or True)
    routed = []
    monkeypatch.setattr(TL, "_ensure_user_conversation",
                        lambda c, slug, gid, ref: routed.append((slug, gid)))
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
    monkeypatch.setattr(TL, "_notify", lambda c, chat, text: notified.append(text))
    routed = []
    monkeypatch.setattr(TL, "_ensure_user_conversation", lambda *a, **k: routed.append(1))
    recorded = []
    monkeypatch.setattr(
        TL, "append_conversation",
        lambda c, slug, gid, msg: recorded.append((slug, gid, msg.get("text"), msg.get("voice"))) or True)

    update = {"update_id": 1, "message": _voice_msg()}
    result = TL.handle_update(cfg, update)

    assert result["ok"] is False
    assert result["action"] == "voice_private_failed"
    assert result["reason"] == "download_failed"
    assert notified and "скачать" in notified[0]  # honest cause, not a generic decode error
    assert routed == []
    # T-0586 no-drop: the refusal itself is recorded — marker text + attachment.
    assert len(recorded) == 1
    slug, gid, text, voice = recorded[0]
    assert (slug, gid) == ("test-project", "gu_1")
    assert "НЕ обработано" in text and "download_failed" in text
    assert voice["file_id"] == "VID"


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
    monkeypatch.setattr(TL, "_notify", lambda c, chat, text: notified.append(text))
    recorded = []
    monkeypatch.setattr(
        TL, "append_conversation",
        lambda c, slug, gid, msg: recorded.append((msg.get("text"), msg.get("voice"))) or True)

    result = TL.handle_update(cfg, {"update_id": 1, "message": _voice_msg()})

    assert result["reason"] == "too_big"
    (text_sent,) = notified
    assert "20МБ" in text_sent and "25:40" in text_sent
    assert "НЕ поможет" in text_sent  # resending is explicitly called out as futile
    (marker_text, marker_voice) = recorded[0]
    assert "too_big" in marker_text and marker_voice["file_id"] == "VID"


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
    monkeypatch.setattr(TL, "_notify", lambda c, chat, text: notified.append(text))
    recorded = []
    monkeypatch.setattr(
        TL, "append_conversation",
        lambda c, slug, gid, msg: recorded.append((msg.get("text"), msg.get("voice"))) or True)

    update = {"update_id": 1, "message": _voice_msg()}
    result = TL.handle_update(cfg, update)

    assert result["reason"] == "too_long"
    (text_sent,) = notified
    assert "6:16" in text_sent  # the note's actual duration
    assert "5 мин" in text_sent  # the cap (default 300s), so the user knows the rule
    assert "попробуйте ещё раз" not in text_sent  # the old lie
    (marker_text, marker_voice) = recorded[0]
    assert "too_long" in marker_text and "376" in marker_text
    assert marker_voice["file_id"] == "VID"


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
