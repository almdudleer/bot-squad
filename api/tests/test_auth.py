import hashlib
import hmac
import time

import pytest

from app.auth import (
    AuthError,
    issue_jwt,
    verify_jwt,
    verify_tg_login,
)
from app.config import AuthConfig


def make_tg_payload(bot_token: str, **fields) -> dict:
    """Construct a TG Login Widget payload with valid HMAC."""
    base = {
        "id": 12345,
        "first_name": "Alexey",
        "username": "alexeysdk",
        "auth_date": int(time.time()),
        **fields,
    }
    secret = hashlib.sha256(bot_token.encode()).digest()
    data_check_string = "\n".join(
        f"{k}={base[k]}" for k in sorted(base.keys())
    )
    h = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {**base, "hash": h}


def test_verify_tg_login_ok():
    bot_token = "BOT:TOKEN"
    auth_cfg = AuthConfig(bot_token=bot_token, allowed_ids=(12345,),
                          session_ttl_seconds=86400, auth_age_max=86400)
    payload = make_tg_payload(bot_token)
    user = verify_tg_login(payload, auth_cfg)
    assert user["id"] == 12345


def test_verify_tg_login_bad_hash():
    bot_token = "BOT:TOKEN"
    auth_cfg = AuthConfig(bot_token=bot_token, allowed_ids=(12345,),
                          session_ttl_seconds=86400, auth_age_max=86400)
    payload = make_tg_payload(bot_token)
    payload["hash"] = "00" * 32
    with pytest.raises(AuthError):
        verify_tg_login(payload, auth_cfg)


def test_verify_tg_login_unallowed_id():
    bot_token = "BOT:TOKEN"
    auth_cfg = AuthConfig(bot_token=bot_token, allowed_ids=(99999,),
                          session_ttl_seconds=86400, auth_age_max=86400)
    payload = make_tg_payload(bot_token)
    with pytest.raises(AuthError) as excinfo:
        verify_tg_login(payload, auth_cfg)
    assert "not allowed" in str(excinfo.value).lower()


def test_verify_tg_login_stale_auth_date():
    bot_token = "BOT:TOKEN"
    auth_cfg = AuthConfig(bot_token=bot_token, allowed_ids=(12345,),
                          session_ttl_seconds=86400, auth_age_max=60)
    payload = make_tg_payload(bot_token, auth_date=int(time.time()) - 3600)
    # Need to recompute hash because auth_date changed.
    payload = make_tg_payload(bot_token, auth_date=int(time.time()) - 3600)
    with pytest.raises(AuthError):
        verify_tg_login(payload, auth_cfg)


def test_jwt_round_trip():
    secret = "supersecret"
    token = issue_jwt({"tg_id": 12345, "name": "Alexey"}, secret, ttl_seconds=60)
    claims = verify_jwt(token, secret)
    assert claims["tg_id"] == 12345


def test_jwt_expired():
    secret = "supersecret"
    token = issue_jwt({"tg_id": 12345}, secret, ttl_seconds=-1)
    with pytest.raises(AuthError):
        verify_jwt(token, secret)
