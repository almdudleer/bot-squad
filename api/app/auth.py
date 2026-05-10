"""JWT cookie issuance/check for bot-squad API sessions.

TG Login Widget HMAC verification has moved to the worker (bot_squad_worker/auth.py)
as of spec #3 — the bot token is now held exclusively by the worker.
The API proxies TG-Login verification to the worker action `tg_verify_login`.
"""
from __future__ import annotations

import time

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
