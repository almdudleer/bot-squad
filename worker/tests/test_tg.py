"""Unit tests for bot_squad_worker.tg — TgClient + helpers."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot_squad_worker.tg import TgClient, _prefix


# ---------------------------------------------------------------------------
# _prefix helper
# ---------------------------------------------------------------------------

def test_prefix_both_given():
    assert _prefix("hello", sid="S-x", user="bob") == "[S-x @ bob] hello"


def test_prefix_sid_only():
    assert _prefix("hello", sid="S-x", user="") == "[S-x] hello"


def test_prefix_neither():
    assert _prefix("hello", sid="", user="") == "hello"


def test_prefix_user_only_no_prefix():
    # user without sid → no prefix (no bracket without a SID)
    assert _prefix("hello", sid="", user="bob") == "hello"


# ---------------------------------------------------------------------------
# TgClient with empty token
# ---------------------------------------------------------------------------

class _FakeCfg:
    def __init__(self, token: str, data_dir: Path, proxy_url: str = "") -> None:
        self.tg_bot_token = token
        self.data_dir = data_dir
        self.tg_proxy_url = proxy_url


def test_send_returns_false_when_no_token(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="", data_dir=tmp_path)
    client = TgClient(cfg)
    result = client.send(chat_id="123", text="hello")
    assert result is False


def test_send_no_token_does_not_raise(tmp_path: Path) -> None:
    """Empty token must never raise — test fixtures rely on this."""
    cfg = _FakeCfg(token="", data_dir=tmp_path)
    client = TgClient(cfg)
    # Should silently return False, no exception.
    assert client.send(chat_id="999", text="anything") is False


# ---------------------------------------------------------------------------
# TgClient debounce
# ---------------------------------------------------------------------------

def _make_client_with_mock_post(tmp_path: Path, token: str = "T:ok") -> tuple[TgClient, MagicMock]:
    cfg = _FakeCfg(token=token, data_dir=tmp_path)
    client = TgClient(cfg, cooldown_sec=60)
    mock_post = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_post.return_value = mock_resp
    client._post = mock_post  # type: ignore[method-assign]
    return client, mock_post


def test_first_send_delivers(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    result = client.send(chat_id="123", text="hi")
    assert result is True
    mock_post.assert_called_once()


def test_second_send_same_payload_debounced(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")
    result2 = client.send(chat_id="123", text="hi")
    assert result2 is False
    # Only one actual HTTP call
    assert mock_post.call_count == 1


def test_different_text_not_debounced(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="first")
    result = client.send(chat_id="123", text="second")
    assert result is True
    assert mock_post.call_count == 2


def test_different_sid_not_debounced(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi", sid="S-a")
    result = client.send(chat_id="123", text="hi", sid="S-b")
    assert result is True
    assert mock_post.call_count == 2


def test_debounce_expires_after_cooldown(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")

    # Back-date the debounce file past the cooldown.
    p = client._debounce_path("123", "", "hi")
    past = time.time() - 61
    import os
    os.utime(p, (past, past))

    result = client.send(chat_id="123", text="hi")
    assert result is True
    assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# SID prefix in the actual sent text
# ---------------------------------------------------------------------------

def test_message_has_sid_prefix(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="body", sid="S-test-p5", user="alexey")
    call_kwargs = mock_post.call_args
    # _post receives chat_id and text kwargs
    assert call_kwargs.kwargs["text"] == "[S-test-p5 @ alexey] body"


def test_message_no_prefix_when_no_sid(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="plain text")
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["text"] == "plain text"


# ---------------------------------------------------------------------------
# T-0156: group-chat topic (forum thread) support — message_thread_id
# ---------------------------------------------------------------------------

def test_send_passes_topic_id_to_post(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="-1001234567890", text="hi", topic_id=42)
    assert mock_post.call_args.kwargs["topic_id"] == 42


def test_send_omits_topic_id_when_none(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")
    assert mock_post.call_args.kwargs.get("topic_id") is None


# ---------------------------------------------------------------------------
# T-0513: debounce opt-out + reply_markup pass-through (interactive command
# replies routed via the channel must echo every time and carry a keyboard).
# ---------------------------------------------------------------------------

def test_debounce_false_sends_same_payload_every_time(tmp_path: Path) -> None:
    """debounce=False bypasses the same-payload cooldown (and records nothing)."""
    client, mock_post = _make_client_with_mock_post(tmp_path)
    assert client.send(chat_id="123", text="hi", debounce=False) is True
    assert client.send(chat_id="123", text="hi", debounce=False) is True
    assert mock_post.call_count == 2
    # No debounce file recorded → a later default send is also not debounced.
    assert not client._debounce_path("123", "", "hi").exists()


def test_debounce_default_still_collapses(tmp_path: Path) -> None:
    """Regression: omitting debounce keeps the historical 60s cooldown."""
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")
    assert client.send(chat_id="123", text="hi") is False
    assert mock_post.call_count == 1


def test_send_passes_reply_markup_to_post(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    markup = {"keyboard": [[{"text": "/sessions"}]]}
    client.send(chat_id="123", text="pick", reply_markup=markup)
    assert mock_post.call_args.kwargs["reply_markup"] == markup


def test_send_omits_reply_markup_when_none(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")
    assert mock_post.call_args.kwargs.get("reply_markup") is None


def test_post_includes_reply_markup_in_payload(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    markup = {"keyboard": [[{"text": "/sessions"}]], "one_time_keyboard": True}
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["payload"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="-100999", text="pick", reply_markup=markup)
    assert captured["payload"]["reply_markup"] == markup


def test_post_omits_reply_markup_when_none(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["payload"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="123", text="body")
    assert "reply_markup" not in captured["payload"]


def test_post_includes_message_thread_id_when_topic_set(tmp_path: Path) -> None:
    """_post must put message_thread_id in the Telegram payload when topic given."""
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002 - mirror httpx sig
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="-100999", text="body", topic_id=7)
    assert captured["json"]["message_thread_id"] == 7
    assert captured["json"]["chat_id"] == "-100999"


def test_post_omits_message_thread_id_when_no_topic(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="123", text="body")
    assert "message_thread_id" not in captured["json"]


# ---------------------------------------------------------------------------
# T-0725: reply threading — reply_to_message_id
# ---------------------------------------------------------------------------

def test_post_includes_reply_to_message_id_when_given(tmp_path: Path) -> None:
    """T-0725: the send must carry reply_to_message_id so the answer is an
    actual TG reply to the message it answers — plus allow_sending_without_reply
    so a deleted target degrades to an unthreaded send instead of a 400."""
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="-100999", text="body", topic_id=7, reply_to_message_id=300)
    assert captured["json"]["reply_to_message_id"] == 300
    assert captured["json"]["allow_sending_without_reply"] is True
    # threading is orthogonal to WHERE it goes — the topic still rides along
    assert captured["json"]["message_thread_id"] == 7


def test_post_omits_reply_to_message_id_when_none(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="123", text="body")
    assert "reply_to_message_id" not in captured["json"]
    assert "allow_sending_without_reply" not in captured["json"]


def test_send_forwards_reply_to_message_id_to_post(tmp_path: Path) -> None:
    """The public send() surface plumbs the reply target down to _post."""
    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch.object(client, "_post", return_value={}) as post:
        assert client.send(chat_id="-100999", text="hi", reply_to_message_id=300) is True
    assert post.call_args.kwargs["reply_to_message_id"] == 300


# ---------------------------------------------------------------------------
# T-0194: per-installation TG egress proxy — _post routes through cfg.tg_proxy_url
# ---------------------------------------------------------------------------

def test_post_passes_proxy_when_configured(tmp_path: Path) -> None:
    """When cfg.tg_proxy_url is set, _post must hand it to httpx.post as proxy=."""
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None, proxy=None):  # noqa: A002
        captured["proxy"] = proxy
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path, proxy_url="http://153.80.195.83:8888")
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="123", text="body")
    assert captured["proxy"] == "http://153.80.195.83:8888"


def test_post_omits_proxy_when_not_configured(tmp_path: Path) -> None:
    """No proxy configured → httpx.post is called WITHOUT a proxy kwarg, so
    trust_env behaviour (and the existing fakes) stay intact."""
    captured: dict = {}

    # Sig deliberately lacks proxy: a proxy kwarg would raise TypeError here.
    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["called"] = True
        resp = MagicMock()
        resp.json.return_value = {"ok": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)  # no proxy
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="123", text="body")
    assert captured.get("called") is True


# ---------------------------------------------------------------------------
# Quiet hours config — defaults to 17→5 UTC, override via system_settings.toml
# ---------------------------------------------------------------------------


def test_config_loads_default_quiet_hours(tmp_config_dir: Path) -> None:
    from bot_squad_worker.config import Config
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_quiet_hours_start_utc == 17
    assert cfg.tg_quiet_hours_end_utc == 5


def test_config_loads_quiet_hours_from_system_settings(tmp_config_dir: Path) -> None:
    from bot_squad_worker.config import Config
    (tmp_config_dir / "system_settings.toml").write_text(
        "[tg]\n"
        "quiet_hours_start_utc = 22\n"
        "quiet_hours_end_utc = 6\n"
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_quiet_hours_start_utc == 22
    assert cfg.tg_quiet_hours_end_utc == 6


def test_in_quiet_hours_respects_args(monkeypatch) -> None:
    """_in_quiet_hours uses its start/end args, computed against current UTC.

    The worker conftest sets BOT_SQUAD_DISABLE_QUIET_HOURS so the helper
    returns False unconditionally; pop the override for this test only.
    """
    from bot_squad_worker import tg as tg_module
    monkeypatch.delenv("BOT_SQUAD_DISABLE_QUIET_HOURS", raising=False)

    from datetime import datetime as dt, timezone as tz
    h = dt.now(tz.utc).hour

    # Window guaranteed NOT to include h:
    far_start = (h + 6) % 24
    far_end = (h + 7) % 24
    if far_start < far_end:
        assert tg_module._in_quiet_hours(far_start, far_end) is False

    # Window guaranteed TO include h:
    near_start = (h - 1) % 24
    near_end = (h + 2) % 24
    if near_start < near_end:
        assert tg_module._in_quiet_hours(near_start, near_end) is True


def test_in_quiet_hours_disabled_when_start_equals_end(monkeypatch) -> None:
    """T-0690: start_utc == end_utc means quiet hours are disabled entirely.

    Without the explicit equal-values check, this falls into the wrap-around
    branch (h >= start or h < end), which is True for every hour when the two
    are equal — the opposite of "disabled". The equal-values check returns
    before the current hour is even read, so this holds regardless of when
    the test runs.
    """
    from bot_squad_worker import tg as tg_module
    monkeypatch.delenv("BOT_SQUAD_DISABLE_QUIET_HOURS", raising=False)

    for value in (0, 12, 17, 22, 23):
        assert tg_module._in_quiet_hours(value, value) is False


def test_tg_client_picks_up_configured_quiet_hours(tmp_path: Path) -> None:
    """TgClient stores quiet hours from cfg."""
    class _Cfg:
        tg_bot_token = "t"
        data_dir = tmp_path
        tg_quiet_hours_start_utc = 22
        tg_quiet_hours_end_utc = 6

    client = TgClient(_Cfg())
    assert client._quiet_start_utc == 22
    assert client._quiet_end_utc == 6


# ---------------------------------------------------------------------------
# T-0386 / INI-04: forum-topic CRUD (createForumTopic / closeForumTopic)
# ---------------------------------------------------------------------------

def test_create_forum_topic_posts_payload_and_returns_thread_id(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "result": {"message_thread_id": 555}}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        tid = client.create_forum_topic(chat_id="-100999", name="🚚 deploy-logs")
    assert tid == 555
    assert captured["url"].endswith("/createForumTopic")
    assert captured["json"] == {"chat_id": "-100999", "name": "🚚 deploy-logs"}


def test_create_forum_topic_raises_without_token(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="", data_dir=tmp_path)
    client = TgClient(cfg)
    with pytest.raises(RuntimeError, match="no bot token"):
        client.create_forum_topic(chat_id="-100999", name="x")


def test_close_forum_topic_posts_message_thread_id(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "result": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client.close_forum_topic(chat_id="-100999", thread_id=555)
    assert captured["url"].endswith("/closeForumTopic")
    assert captured["json"] == {"chat_id": "-100999", "message_thread_id": 555}


def test_rename_general_forum_topic_posts_payload(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "result": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client.rename_general_forum_topic(chat_id="-100999", name="[bot-squad] General")
    assert captured["url"].endswith("/editGeneralForumTopic")
    assert captured["json"] == {"chat_id": "-100999", "name": "[bot-squad] General"}


def test_rename_general_forum_topic_raises_without_token(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="", data_dir=tmp_path)
    client = TgClient(cfg)
    with pytest.raises(RuntimeError, match="no bot token"):
        client.rename_general_forum_topic(chat_id="-100999", name="x")


def test_edit_forum_topic_posts_payload(tmp_path: Path) -> None:
    """T-0669/T-0676 item 1: editForumTopic — the regular-topic counterpart
    to rename_general_forum_topic, for a topic with its own thread_id."""
    captured: dict = {}

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "result": True}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client.edit_forum_topic(chat_id="-100999", thread_id=45, name="[watchrobot] T-0270 title")
    assert captured["url"].endswith("/editForumTopic")
    assert captured["json"] == {
        "chat_id": "-100999", "message_thread_id": 45, "name": "[watchrobot] T-0270 title",
    }


def test_edit_forum_topic_raises_without_token(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="", data_dir=tmp_path)
    client = TgClient(cfg)
    with pytest.raises(RuntimeError, match="no bot token"):
        client.edit_forum_topic(chat_id="-100999", thread_id=45, name="x")


# ---------------------------------------------------------------------------
# T-0660 field note (TL p23): a documented Bot API failure (bad chat_id,
# missing can_manage_topics admin right, …) must surface the API's own
# `description` — not get swallowed by raise_for_status() into an opaque
# httpx.HTTPStatusError before the JSON body is ever read.
# ---------------------------------------------------------------------------


def test_call_surfaces_api_description_on_documented_failure(tmp_path: Path) -> None:
    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {
            "ok": False, "error_code": 400,
            "description": "Bad Request: CHAT_ADMIN_REQUIRED",
        }
        resp.raise_for_status.side_effect = AssertionError(
            "must not be reached — the description must be read from the JSON "
            "body first, not thrown away by raise_for_status()"
        )
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        with pytest.raises(RuntimeError, match="CHAT_ADMIN_REQUIRED"):
            client.create_forum_topic(chat_id="-100999", name="x")


def test_call_falls_back_to_http_status_when_no_description(tmp_path: Path) -> None:
    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {"ok": False}
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        with pytest.raises(RuntimeError, match="HTTP 403"):
            client.create_forum_topic(chat_id="-100999", name="x")


def test_call_raises_for_status_on_non_json_failure(tmp_path: Path) -> None:
    """A genuinely non-JSON failure (proxy/network error) still surfaces via
    the raise_for_status() fallback — the description path only applies when
    TG actually returned its documented JSON error shape."""
    import httpx

    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        resp = MagicMock()
        resp.json.side_effect = ValueError("not json")
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "502 Bad Gateway", request=MagicMock(), response=MagicMock()
        )
        return resp

    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        with pytest.raises(httpx.HTTPStatusError):
            client.create_forum_topic(chat_id="-100999", name="x")


# ---------------------------------------------------------------------------
# T-0677: send_and_pin / pin_message / unpin_message — the /pin-session marker
# ---------------------------------------------------------------------------

def _fake_tg_transport(calls: list, *, pin_ok: bool = True, message_id: int = 4242):
    """httpx.post double that answers sendMessage with a message_id and lets
    pinChatMessage succeed or fail like the Bot API does."""
    def fake_httpx_post(url, json=None, timeout=None):  # noqa: A002
        calls.append((url.rsplit("/", 1)[-1], json))
        resp = MagicMock()
        if url.endswith("sendMessage"):
            resp.json.return_value = {"ok": True, "result": {"message_id": message_id}}
        elif url.endswith("pinChatMessage") and not pin_ok:
            resp.json.return_value = {"ok": False, "description": "Bad Request: CHAT_ADMIN_REQUIRED"}
        else:
            resp.json.return_value = {"ok": True, "result": True}
        return resp
    return fake_httpx_post


def test_send_and_pin_sends_into_the_topic_then_pins_that_message(tmp_path: Path) -> None:
    """There is no message_thread_id on pinChatMessage — a forum pin is scoped
    by the message's OWN thread, so the send must carry the topic and the pin
    must target the id it came back with."""
    calls: list = []
    client = TgClient(_FakeCfg(token="T:ok", data_dir=tmp_path))
    with patch("httpx.post", side_effect=_fake_tg_transport(calls)):
        out = client.send_and_pin(chat_id="-100999", text="pinned", topic_id=42)

    assert out == {"sent": True, "message_id": 4242, "pinned": True, "pin_error": ""}
    assert [name for name, _ in calls] == ["sendMessage", "pinChatMessage"]
    assert calls[0][1]["message_thread_id"] == 42
    assert calls[1][1] == {"chat_id": "-100999", "message_id": 4242,
                           "disable_notification": True}


def test_send_and_pin_reports_a_refused_pin_instead_of_raising(tmp_path: Path) -> None:
    """The confirmation is already delivered when the pin is refused (the bot
    lacks can_pin_messages) — the caller needs to tell the user that, not blow
    up the whole command."""
    calls: list = []
    client = TgClient(_FakeCfg(token="T:ok", data_dir=tmp_path))
    with patch("httpx.post", side_effect=_fake_tg_transport(calls, pin_ok=False)):
        out = client.send_and_pin(chat_id="-100999", text="pinned", topic_id=42)

    assert out["sent"] is True and out["message_id"] == 4242
    assert out["pinned"] is False
    assert "CHAT_ADMIN_REQUIRED" in out["pin_error"]


def test_send_and_pin_without_token_is_a_no_op(tmp_path: Path) -> None:
    client = TgClient(_FakeCfg(token="", data_dir=tmp_path))
    with patch("httpx.post", side_effect=AssertionError("must not call TG")):
        assert client.send_and_pin(chat_id="1", text="x") == {
            "sent": False, "message_id": None, "pinned": False, "pin_error": ""}


def test_unpin_message_targets_one_message(tmp_path: Path) -> None:
    """Never unpinAllChatMessages — that would clear pins this bot didn't set."""
    calls: list = []
    client = TgClient(_FakeCfg(token="T:ok", data_dir=tmp_path))
    with patch("httpx.post", side_effect=_fake_tg_transport(calls)):
        client.unpin_message(chat_id="-100999", message_id=4242)

    assert calls == [("unpinChatMessage", {"chat_id": "-100999", "message_id": 4242})]


# ---------------------------------------------------------------------------
# T-0719 — send() pins message_id -> raw routing SID for reply routing
#
# The display `sid` and the routing `route_sid` are DIFFERENT things since
# T-0676 item 5: the label carries no SID at all. These pin that send() records
# the routing key rather than anything derived from the rendered text.
# ---------------------------------------------------------------------------

def _sendmessage_transport(message_id: int = 7777):
    """httpx.post stand-in returning a real-shaped sendMessage response."""
    def _post(url, json=None, timeout=None, **kw):  # noqa: A002
        resp = MagicMock()
        resp.json.return_value = {
            "ok": True,
            "result": {"message_id": message_id, "text": json["text"]},
        }
        return resp
    return _post


def test_send_records_reply_route_under_the_compact_label(tmp_path: Path) -> None:
    from bot_squad_worker import tg_reply_map

    client = TgClient(_FakeCfg(token="T:ok", data_dir=tmp_path))
    with patch("httpx.post", side_effect=_sendmessage_transport(7777)):
        sent = client.send(
            chat_id="404580642",
            text="operator here",
            sid="bot-squad operator",                 # T-0676 compact DISPLAY label
            route_sid="S-almdudleer-operator-p241",   # T-0719 real routing key
        )

    assert sent is True
    # The wire text is unchanged — the compact label stays (DoD: do NOT revert).
    assert tg_reply_map.lookup(
        tmp_path, chat_id="404580642", message_id=7777,
    ) == "S-almdudleer-operator-p241"


def test_send_without_route_sid_records_nothing(tmp_path: Path) -> None:
    from bot_squad_worker import tg_reply_map

    client = TgClient(_FakeCfg(token="T:ok", data_dir=tmp_path))
    with patch("httpx.post", side_effect=_sendmessage_transport(7778)):
        client.send(chat_id="404580642", text="hi", sid="bot-squad operator")

    assert tg_reply_map.load(tmp_path) == {}


def test_send_survives_a_response_without_message_id(tmp_path: Path) -> None:
    """Recording is best-effort: a delivered page must not be reported as
    failed just because the reply-map entry could not be written."""
    def _post(url, json=None, timeout=None, **kw):  # noqa: A002
        resp = MagicMock()
        resp.json.return_value = {"ok": True}       # no `result`
        return resp

    client = TgClient(_FakeCfg(token="T:ok", data_dir=tmp_path))
    with patch("httpx.post", side_effect=_post):
        assert client.send(
            chat_id="1", text="hi", route_sid="S-almdudleer-operator-p241") is True


def test_debounced_send_records_no_route(tmp_path: Path) -> None:
    """A suppressed send never reaches Telegram, so there is no message id to
    map — the store must not gain a bogus entry."""
    from bot_squad_worker import tg_reply_map

    client = TgClient(_FakeCfg(token="T:ok", data_dir=tmp_path), cooldown_sec=60)
    with patch("httpx.post", side_effect=_sendmessage_transport(7779)):
        assert client.send(chat_id="1", text="hi",
                           route_sid="S-almdudleer-operator-p241") is True
        assert client.send(chat_id="1", text="hi",
                           route_sid="S-almdudleer-operator-p241") is False

    assert list(tg_reply_map.load(tmp_path)) == ["1:7779"]


# ---------------------------------------------------------------------------
# split_for_tg / part_marker (T-0721) — the ONE long-message chunker
# ---------------------------------------------------------------------------

def test_split_for_tg_short_text_is_one_unchanged_part():
    """The common short send must stay byte-identical — no marker, no strip."""
    from bot_squad_worker.tg import split_for_tg
    assert split_for_tg("  привет  ") == ["  привет  "]


def test_split_for_tg_never_truncates_and_respects_the_api_cap():
    """T-0721: «long ones should just split, that's it». Every char of the
    input comes back across the parts, and no part can trip TG's 4096 API
    cap."""
    from bot_squad_worker.tg import TG_MSG_CAP, split_for_tg
    text = "Предложение номер один. " * 800            # ~19k chars
    parts = split_for_tg(text)
    assert len(parts) > 4
    assert all(len(p) <= TG_MSG_CAP for p in parts)
    assert "".join("".join(p.split()) for p in parts) == "".join(text.split())


def test_split_for_tg_cuts_on_sentence_and_line_boundaries():
    """Splits land on a boundary, not mid-word: each part ends a sentence/line
    and no part starts mid-word."""
    from bot_squad_worker.tg import split_for_tg
    text = ("Абзац с деталями. " * 100 + "\n") * 6
    parts = split_for_tg(text, limit=1000)
    assert len(parts) > 1
    for p in parts[:-1]:
        assert p.endswith(".")
    for p in parts:
        assert p.startswith("Абзац")


def test_split_for_tg_hard_cuts_unbreakable_text():
    """A single boundary-free token (a base64 blob, a stack-free log line) is
    hard-cut at the limit — the API cap leaves no alternative — but still
    loses nothing."""
    from bot_squad_worker.tg import split_for_tg
    blob = "x" * 9000
    parts = split_for_tg(blob, limit=4000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]
    assert "".join(parts) == blob


def test_split_for_tg_no_runt_parts_from_an_early_boundary():
    """A boundary in the first half of the window is ignored, so an early
    newline can't produce a 5-char part followed by a full one."""
    from bot_squad_worker.tg import split_for_tg
    text = "hi\n" + "a" * 3000
    parts = split_for_tg(text, limit=1000)
    assert parts[0].startswith("hi\n")
    assert len(parts[0]) == 1000


def test_part_marker_is_the_shared_numbered_convention():
    from bot_squad_worker.tg import part_marker
    assert part_marker(2, 7) == "(2/7)"


# --------------------------------------------------------------------------
# render_transcript_echo (T-0741) — the ONE voice-echo render, shared by the
# DM 🎙-echo and the group/topic ACK.
# --------------------------------------------------------------------------

def test_render_transcript_echo_short_note_is_one_message():
    from bot_squad_worker.tg import render_transcript_echo
    out = render_transcript_echo("тёмная тема", prefix="🎙 Распознал так: ")
    assert out == ["🎙 Распознал так: «тёмная тема»"]


def test_render_transcript_echo_unquoted_for_the_ack():
    from bot_squad_worker.tg import render_transcript_echo
    out = render_transcript_echo("hi", prefix="✅ got your voice note (5s): ", quote=False)
    assert out == ["✅ got your voice note (5s): hi"]


def test_render_transcript_echo_never_truncates_a_real_note():
    """T-0741 REGRESSION GUARD — the stakeholder's exact complaint, "всё ещё
    обрезанное получается".

    His 2026-07-27T04:04:47Z note transcribed to 456 chars and the group/topic
    ACK sent back 140 of them plus a "…". Any render that drops a character of
    a note this size is that bug. 456 chars is nowhere near TG's 4096 cap —
    there was never a transport reason to cut it.
    """
    from bot_squad_worker.tg import render_transcript_echo
    transcript = "По суда, я не понимаю, в целом, зачем нам нужна суда. " * 9  # ~477
    assert 140 < len(transcript) < 4000
    out = render_transcript_echo(transcript, prefix="✅ got your voice note (37s): ",
                                 quote=False)
    assert len(out) == 1, "a note well under the cap must not be split"
    assert transcript in out[0], "the ACK dropped part of the transcript"
    assert "…" not in out[0]


def test_render_transcript_echo_splits_over_the_cap_and_keeps_every_char():
    """Over the API cap it becomes numbered parts — never a truncation."""
    from bot_squad_worker.tg import TG_MSG_CAP, render_transcript_echo
    transcript = "Длинная фраза про воркеры и топики. " * 300
    out = render_transcript_echo(transcript, prefix="🎙 Распознал так: ")
    assert len(out) > 1
    assert all(len(p) <= TG_MSG_CAP for p in out)
    assert out[0].startswith("🎙 Распознал так: (1/")
    # Every part is prefix + (n/N) + «chunk»; strip the frame and rejoin.
    bodies = [p.split("«", 1)[1].rsplit("»", 1)[0] for p in out]
    assert "".join(bodies).replace(" ", "") == transcript.replace(" ", ""), \
        "chunking lost characters"


def test_render_transcript_echo_reserve_accounts_for_the_sid_prefix():
    """``send`` prepends "[<sid>] ". A transcript that fits only WITHOUT that
    label must still split, or TG answers 400 and the whole ACK is lost."""
    from bot_squad_worker.tg import TG_MSG_CAP, render_transcript_echo
    prefix = "✅ got your voice note (900s): "
    transcript = "я" * (TG_MSG_CAP - len(prefix) - 5)
    assert len(render_transcript_echo(transcript, prefix=prefix, quote=False)) == 1
    assert len(render_transcript_echo(transcript, prefix=prefix, quote=False,
                                      reserve=len("[voice_intake] "))) > 1


# ---------------------------------------------------------------------------
# msg_text — his own typed words, wherever Telegram put them (T-0786)
#
# Every test in this block is a RED PIN against b7182ec: `tg.msg_text` does not
# exist there, and nothing in the worker package read `msg["caption"]` for the
# INBOUND message at all (the one `caption` read was `reply_quote.extract`,
# which reads the QUOTED message's).
# ---------------------------------------------------------------------------

_HIS_WORDS = "вот скрин, посмотри"


def test_msg_text_reads_a_photo_caption():
    """THE defect. Telegram puts the words typed with a picture in `caption`,
    so a photo-with-caption message has no `text` key at all."""
    from bot_squad_worker.tg import msg_text
    assert msg_text({"photo": [{"file_id": "F"}], "caption": _HIS_WORDS}) == _HIS_WORDS


def test_msg_text_prefers_text_over_caption():
    """THE PRECEDENCE DECISION, pinned rather than left to accident. Telegram
    never populates both on one message, so no real payload can distinguish the
    orders — which is exactly why the choice has to be written down."""
    from bot_squad_worker.tg import msg_text
    assert msg_text({"text": "typed", "caption": "captioned"}) == "typed"


def test_msg_text_agrees_with_reply_quote_on_the_same_message():
    """WHY that precedence and not the other: `reply_quote.extract` already
    reads text-then-caption for the message being ANSWERED. One message read as
    "what he just said" and as "what he was answering" must not yield two
    different strings."""
    from bot_squad_worker import reply_quote
    from bot_squad_worker.tg import msg_text
    msg = {"photo": [{"file_id": "F"}], "caption": _HIS_WORDS}
    quote = reply_quote.extract({"text": "ok", "reply_to_message": dict(msg)})
    assert msg_text(msg) == quote["text"] == _HIS_WORDS


def test_msg_text_with_neither_field_is_empty_never_a_marker():
    """ABSENT STAYS ABSENT (the T-0761/T-0780 house rule). This value is written
    into records authored `user`; a placeholder string there would be inventing
    words he never typed — the T-0746 defect, from the other direction."""
    from bot_squad_worker.tg import msg_text
    assert msg_text({"photo": [{"file_id": "F"}]}) == ""
    assert msg_text({}) == ""


def test_msg_text_empty_caption_stays_empty():
    """A photo he sent WITHOUT typing anything is not a caption we lost."""
    from bot_squad_worker.tg import msg_text
    assert msg_text({"photo": [{"file_id": "F"}], "caption": ""}) == ""


def test_msg_text_junk_degrades_instead_of_raising():
    """DEFENSIVE COERCION: this runs on the inbound routing path and
    `append_conversation`'s callers do NOT wrap it. A junk caption must cost the
    words, never the whole message."""
    from bot_squad_worker.tg import msg_text
    assert msg_text({"caption": 12345}) == "12345"
    assert msg_text({"caption": ["a", "b"]}) == "['a', 'b']"
    assert msg_text(None) == ""
    assert msg_text("not-a-message") == ""


def test_msg_text_is_byte_identical_for_a_plain_text_message():
    """GREEN-EQUIVALENT REGRESSION GUARD (it can only run post-fix, since the
    function is new, but the property it pins is the pre-fix behaviour): for
    every message without a caption this must equal the `msg.get("text") or ""`
    it replaces, falsy values included."""
    from bot_squad_worker.tg import msg_text
    for msg in ({"text": "hello"}, {"text": ""}, {"text": None}, {},
                {"text": "0"}, {"voice": {"file_id": "V"}}):
        assert msg_text(msg) == (msg.get("text") or "")
