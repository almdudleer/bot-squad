"""TG Login Widget HMAC verification for the worker.

Deliberately NOT imported from api.app.auth — the worker is a separate process
and must not depend on the API package. This is a verbatim functional copy
of the verification logic, adapted to take explicit arguments instead of an
AuthConfig dataclass.
"""
from __future__ import annotations

import hashlib
import hmac
import time


class AuthError(Exception):
    pass


def verify_tg_login(payload: dict, bot_token: str, auth_age_max: int = 86400) -> dict:
    """Verify a TG Login Widget payload per Telegram's spec.

    https://core.telegram.org/widgets/login#checking-authorization

    Returns the user dict (without `hash`) on success; raises AuthError
    on any failure: missing fields, bad HMAC, or stale auth_date.

    Note: allowed_ids check is intentionally NOT done here — that is the
    API's responsibility (it knows which users are permitted for the session).
    """
    if "hash" not in payload or "auth_date" not in payload or "id" not in payload:
        raise AuthError("missing required field (hash, auth_date, id)")

    received_hash = payload["hash"]
    fields = {k: v for k, v in payload.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))

    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("bad TG login hash")

    if int(time.time()) - int(fields["auth_date"]) > auth_age_max:
        raise AuthError("TG login auth_date is stale")

    return fields
