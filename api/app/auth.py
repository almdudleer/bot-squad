"""Telegram Login Widget verification + JWT cookie issuance/check."""
from __future__ import annotations

import hashlib
import hmac
import time

import jwt as pyjwt

from app.config import AuthConfig


class AuthError(Exception):
    pass


def verify_tg_login(payload: dict, cfg: AuthConfig) -> dict:
    """Verify a TG Login Widget payload per Telegram's spec.

    https://core.telegram.org/widgets/login#checking-authorization

    Returns the user dict (without `hash`) on success; raises AuthError
    on any failure: bad HMAC, stale auth_date, or id not in allowlist.
    """
    if "hash" not in payload or "auth_date" not in payload or "id" not in payload:
        raise AuthError("missing required field (hash, auth_date, id)")

    received_hash = payload["hash"]
    fields = {k: v for k, v in payload.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))

    secret = hashlib.sha256(cfg.bot_token.encode()).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("bad TG login hash")

    if int(time.time()) - int(fields["auth_date"]) > cfg.auth_age_max:
        raise AuthError("TG login auth_date is stale")

    if int(fields["id"]) not in cfg.allowed_ids:
        raise AuthError(f"TG id {fields['id']} not allowed")

    return fields


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
