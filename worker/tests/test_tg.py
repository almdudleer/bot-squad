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
    def __init__(self, token: str, data_dir: Path) -> None:
        self.tg_bot_token = token
        self.data_dir = data_dir


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
