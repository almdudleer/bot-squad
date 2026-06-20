"""Unit tests for bot_squad_worker.max — MaxClient + helpers (T-0247).

MaxClient mirrors TgClient (debounce, SID prefix, quiet hours, egress proxy)
but speaks the MAX (max.ru) Bot API: POST {base}/messages?<recipient_kind>=<id>
with the bot token in the Authorization header and a JSON body {"text": ...}.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot_squad_worker.max import MaxClient


class _FakeCfg:
    def __init__(
        self,
        token: str,
        data_dir: Path,
        proxy_url: str = "",
        recipient_kind: str = "chat_id",
    ) -> None:
        self.max_bot_token = token
        self.data_dir = data_dir
        self.max_proxy_url = proxy_url
        self.max_recipient_kind = recipient_kind


# ---------------------------------------------------------------------------
# Empty token
# ---------------------------------------------------------------------------

def test_send_returns_false_when_no_token(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="", data_dir=tmp_path)
    client = MaxClient(cfg)
    assert client.send(chat_id="123", text="hello") is False


def test_send_no_token_does_not_raise(tmp_path: Path) -> None:
    cfg = _FakeCfg(token="", data_dir=tmp_path)
    client = MaxClient(cfg)
    assert client.send(chat_id="999", text="anything") is False


# ---------------------------------------------------------------------------
# Debounce (mirrors TgClient)
# ---------------------------------------------------------------------------

def _make_client_with_mock_post(tmp_path: Path, token: str = "MAXTOKEN") -> tuple[MaxClient, MagicMock]:
    cfg = _FakeCfg(token=token, data_dir=tmp_path)
    client = MaxClient(cfg, cooldown_sec=60)
    mock_post = MagicMock()
    client._post = mock_post  # type: ignore[method-assign]
    return client, mock_post


def test_first_send_delivers(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    assert client.send(chat_id="123", text="hi") is True
    mock_post.assert_called_once()


def test_second_send_same_payload_debounced(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")
    assert client.send(chat_id="123", text="hi") is False
    assert mock_post.call_count == 1


def test_different_text_not_debounced(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="first")
    assert client.send(chat_id="123", text="second") is True
    assert mock_post.call_count == 2


def test_debounce_expires_after_cooldown(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="hi")
    p = client._debounce_path("123", "", "hi")
    past = time.time() - 61
    import os
    os.utime(p, (past, past))
    assert client.send(chat_id="123", text="hi") is True
    assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# SID prefix in the sent text (reuses tg._prefix semantics)
# ---------------------------------------------------------------------------

def test_message_has_sid_prefix(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="body", sid="S-test-p5", user="alexey")
    assert mock_post.call_args.kwargs["text"] == "[S-test-p5 @ alexey] body"


def test_message_no_prefix_when_no_sid(tmp_path: Path) -> None:
    client, mock_post = _make_client_with_mock_post(tmp_path)
    client.send(chat_id="123", text="plain text")
    assert mock_post.call_args.kwargs["text"] == "plain text"


# ---------------------------------------------------------------------------
# _post wire shape: Authorization header, recipient query param, JSON body
# ---------------------------------------------------------------------------

def test_post_sends_authorization_header_and_chat_id_param(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_httpx_post(url, params=None, json=None, headers=None, timeout=None):  # noqa: A002
        captured.update(url=url, params=params, json=json, headers=headers)
        resp = MagicMock()
        resp.json.return_value = {"message": {"body": {"mid": "m1"}}}
        return resp

    cfg = _FakeCfg(token="MAXTOKEN", data_dir=tmp_path)
    client = MaxClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="555", text="body")
    assert captured["params"] == {"chat_id": "555"}
    assert captured["json"] == {"text": "body"}
    assert captured["headers"]["Authorization"] == "MAXTOKEN"
    assert captured["url"].endswith("/messages")


def test_post_uses_user_id_recipient_kind(tmp_path: Path) -> None:
    """A DM to a user goes out as ?user_id=<id> when recipient_kind=user_id."""
    captured: dict = {}

    def fake_httpx_post(url, params=None, json=None, headers=None, timeout=None):  # noqa: A002
        captured["params"] = params
        resp = MagicMock()
        resp.json.return_value = {"message": {}}
        return resp

    cfg = _FakeCfg(token="MAXTOKEN", data_dir=tmp_path, recipient_kind="user_id")
    client = MaxClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        # send() carries the configured recipient_kind through to _post.
        assert client.send(chat_id="777", text="hi") is True
    assert captured["params"] == {"user_id": "777"}


def test_post_raises_on_api_error_code(tmp_path: Path) -> None:
    def fake_httpx_post(url, params=None, json=None, headers=None, timeout=None):  # noqa: A002
        resp = MagicMock()
        resp.json.return_value = {"code": "invalid.recipient", "message": "no such recipient"}
        return resp

    cfg = _FakeCfg(token="MAXTOKEN", data_dir=tmp_path)
    client = MaxClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        with pytest.raises(RuntimeError, match="MAX API error"):
            client._post(chat_id="1", text="body")


# ---------------------------------------------------------------------------
# Egress proxy (mirrors T-0194 for TG)
# ---------------------------------------------------------------------------

def test_post_passes_proxy_when_configured(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_httpx_post(url, params=None, json=None, headers=None, timeout=None, proxy=None):  # noqa: A002
        captured["proxy"] = proxy
        resp = MagicMock()
        resp.json.return_value = {"message": {}}
        return resp

    cfg = _FakeCfg(token="MAXTOKEN", data_dir=tmp_path, proxy_url="socks5://127.0.0.1:9050")
    client = MaxClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="1", text="body")
    assert captured["proxy"] == "socks5://127.0.0.1:9050"


def test_post_omits_proxy_when_not_configured(tmp_path: Path) -> None:
    captured: dict = {}

    # No proxy kwarg in the sig: a proxy= would raise TypeError if passed.
    def fake_httpx_post(url, params=None, json=None, headers=None, timeout=None):  # noqa: A002
        captured["called"] = True
        resp = MagicMock()
        resp.json.return_value = {"message": {}}
        return resp

    cfg = _FakeCfg(token="MAXTOKEN", data_dir=tmp_path)
    client = MaxClient(cfg)
    with patch("httpx.post", side_effect=fake_httpx_post):
        client._post(chat_id="1", text="body")
    assert captured.get("called") is True


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------

def test_config_loads_max_section(tmp_config_dir: Path) -> None:
    from bot_squad_worker.config import Config
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
        '[max]\nbot_token = "MAXFAKE:TOKEN"\n'
    )
    (tmp_config_dir / "system_settings.toml").write_text(
        '[max]\ndefault_chat_id = "MAXCHAT99"\nrecipient_kind = "user_id"\n'
        'proxy_url = "socks5://127.0.0.1:9050"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.max_bot_token == "MAXFAKE:TOKEN"
    assert cfg.max_default_chat_id == "MAXCHAT99"
    assert cfg.max_recipient_kind == "user_id"
    assert cfg.max_proxy_url == "socks5://127.0.0.1:9050"


def test_config_max_defaults_empty(tmp_config_dir: Path) -> None:
    from bot_squad_worker.config import Config
    cfg = Config.load(tmp_config_dir)
    assert cfg.max_bot_token == ""
    assert cfg.max_default_chat_id == ""
    assert cfg.max_recipient_kind == "chat_id"
