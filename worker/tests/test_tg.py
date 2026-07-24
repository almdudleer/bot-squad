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
    markup = {"keyboard": [[{"text": "/project a"}]]}
    client.send(chat_id="123", text="pick", reply_markup=markup)
    assert mock_post.call_args.kwargs["reply_markup"] == markup


def test_send_omits_reply_markup_when_none(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")
    assert mock_post.call_args.kwargs.get("reply_markup") is None


def test_post_includes_reply_markup_in_payload(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="T:ok", data_dir=tmp_path)
    client = TgClient(cfg)
    markup = {"keyboard": [[{"text": "/project a"}]], "one_time_keyboard": True}
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
