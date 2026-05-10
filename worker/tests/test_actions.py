"""Tests for worker.actions."""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import pytest

from bot_squad_worker.actions import ACTION_REGISTRY, ActionError, dispatch
from bot_squad_worker.config import Config


def _make_tg_payload(bot_token: str, tg_id: int = 12345, **extra) -> dict:
    """Construct a TG Login Widget payload with valid HMAC."""
    base: dict = {
        "id": tg_id,
        "first_name": "Alexey",
        "username": "alexeysdk",
        "auth_date": int(time.time()),
        **extra,
    }
    secret = hashlib.sha256(bot_token.encode()).digest()
    data_check_string = "\n".join(f"{k}={base[k]}" for k in sorted(base.keys()))
    h = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {**base, "hash": h}


def test_noop_returns_ok():
    result = dispatch("noop", {})
    assert result["ok"] is True
    assert "ts" in result


def test_unknown_action_raises():
    with pytest.raises(ActionError) as excinfo:
        dispatch("rm_rf_root", {})
    assert "unknown action" in str(excinfo.value)


def test_registry_lists_only_allowed_actions():
    # The whole point of the registry: closed allowlist. v1 has noop + tg_verify_login.
    assert set(ACTION_REGISTRY.keys()) == {"noop", "tg_verify_login"}


def test_noop_rejects_extra_params():
    # Typed contract — noop takes no params; extras are an error.
    with pytest.raises(ActionError) as excinfo:
        dispatch("noop", {"unexpected": 1})
    assert "unexpected" in str(excinfo.value)


def test_tg_verify_login_dispatches(tmp_config_dir: Path, monkeypatch):
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    payload = _make_tg_payload("TESTBOT:TOKEN", tg_id=12345)
    out = A.dispatch("tg_verify_login", {"payload": payload})
    assert out["ok"] is True
    assert out["user"]["id"] == 12345


def test_tg_verify_login_rejects_empty_payload(tmp_config_dir: Path, monkeypatch):
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    out = A.dispatch("tg_verify_login", {"payload": {}})
    assert out["ok"] is False
    assert "error" in out


def test_tg_verify_login_rejects_extra_params(tmp_config_dir: Path, monkeypatch):
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    import bot_squad_worker.actions as A

    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(ActionError):
        A.dispatch("tg_verify_login", {"payload": {}, "extra": "bad"})
