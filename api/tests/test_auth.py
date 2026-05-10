"""Tests for JWT helpers and password verification in app.auth."""
import time

import pytest

from app.auth import (
    AuthError,
    issue_jwt,
    verify_jwt,
    verify_password,
)


def test_jwt_round_trip():
    secret = "supersecret"
    token = issue_jwt({"username": "alexey"}, secret, ttl_seconds=60)
    claims = verify_jwt(token, secret)
    assert claims["username"] == "alexey"


def test_jwt_expired():
    secret = "supersecret"
    token = issue_jwt({"username": "alexey"}, secret, ttl_seconds=-1)
    with pytest.raises(AuthError):
        verify_jwt(token, secret)


def test_verify_password_correct():
    # bcrypt hash of "test" (rounds=12)
    hash_ = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"
    assert verify_password("test", hash_) is True


def test_verify_password_wrong():
    hash_ = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"
    assert verify_password("wrongpassword", hash_) is False


def test_verify_password_garbage_hash():
    assert verify_password("test", "not-a-valid-hash") is False
