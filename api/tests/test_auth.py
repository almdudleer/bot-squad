"""Tests for JWT helpers in app.auth.

TG Login Widget HMAC verification tests have moved to the worker
(worker/tests/test_actions.py) since the bot token is now held exclusively
by the worker (spec #3 secrets split).
"""
import time

import pytest

from app.auth import (
    AuthError,
    issue_jwt,
    verify_jwt,
)


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
