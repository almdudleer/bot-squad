"""JWT cookie issuance/check and password verification for bot-squad API sessions."""
from __future__ import annotations

import time

import bcrypt
import jwt as pyjwt


class AuthError(Exception):
    pass


def issue_jwt(claims: dict, secret: str, ttl_seconds: int) -> str:
    now = int(time.time())
    payload = {**claims, "iat": now, "exp": now + ttl_seconds}
    return pyjwt.encode(payload, secret, algorithm="HS256")


def verify_jwt(token: str, secret: str) -> dict:
    try:
        return pyjwt.decode(token, secret, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError as e:
        raise AuthError("session expired") from e
    except pyjwt.InvalidTokenError as e:
        raise AuthError(f"bad session token: {e}") from e


def verify_password(plaintext: str, bcrypt_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode(), bcrypt_hash.encode())
    except (ValueError, TypeError):
        return False
