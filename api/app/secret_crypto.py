"""Shared at-rest encryption for secrets.toml values (T-0179).

BYTE-IDENTICAL MIRROR. This file is duplicated, byte-for-byte, at:
  - api/app/secret_crypto.py            (writer: routes_settings)
  - worker/bot_squad_worker/secret_crypto.py  (reader: config.load)
The API (docker) and worker (systemd) run in separate environments and do not
import each other, so the helper is mirrored — same discipline as idalloc.py /
the frontmatter pair. EDIT BOTH COPIES TOGETHER: the mirror registry in
`worker/tests/test_module_mirrors.py` pins them, and `scripts/lint/module_mirrors.py`
runs it on every push (T-0743). Until then this docstring claimed a CI gate that
did not exist — the drift assert lived in test_secret_crypto.py, in a worker
suite no CI job has ever run.

== Scheme ==
Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package. Encrypted
values are stored with an ``enc:`` prefix so encrypted and legacy-plaintext
values can coexist in one file during migration (the value is self-identifying).

== Key management ==
Key source: the ``BOT_SQUAD_SECRETS_KEY`` env var — one urlsafe-base64 Fernet
key, or several comma-separated for rotation (first = primary, used for new
writes; the rest decrypt only, via MultiFernet). The key lives in the install's
``.env`` (gitignored). Both the API container (env_file: .env) and the worker
(EnvironmentFile=-.env on its systemd unit) load it.

Rotation: prepend a freshly generated key (``NEW,OLD``), restart API + worker,
re-save secrets in the Settings UI (re-encrypts under NEW), then drop OLD and
restart.

== Graceful behaviour ==
- No key set (dev / fresh install) → encrypt() is a no-op passthrough (values
  stay plaintext) and decrypt() returns legacy plaintext untouched. Encryption
  is strictly opt-in once a key is provisioned.
- THE LANDMINE (guarded): an ``enc:``-prefixed value with NO usable key, a wrong
  key, or any decrypt failure raises SecretCryptoError — it is NEVER returned as
  ciphertext-as-if-plaintext. Failing loud beats silently handing out garbage.

== Threat model ==
Defends against disclosure of secrets.toml WITHOUT the key (backups/snapshots
that don't capture .env, accidental file disclosure, a less-privileged process
reading the data dir). Does NOT defend against a same-box compromise that reads
both .env and secrets.toml (root, the app user, or code-exec in API/worker — the
key is in process memory). Defense-in-depth, not a boundary against a privileged
local attacker.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_ENV_VAR = "BOT_SQUAD_SECRETS_KEY"
_PREFIX = "enc:"

# Registry of secret field paths in secrets.toml the writer/reader encrypt. The
# scheme is holistic, not bolted to one field: adding a future secret is one
# entry here plus applying encrypt()/decrypt() at that field. Non-secret config
# (e.g. telegram.auth_age_max, an int) stays plaintext.
SECRET_FIELDS: tuple[tuple[str, ...], ...] = (("telegram", "bot_token"),)


class SecretCryptoError(RuntimeError):
    """Raised when an encrypted value cannot be decrypted, or the configured
    key is malformed. Deliberately loud — never swallow into a plaintext read."""


def _keys() -> list[str]:
    raw = os.environ.get(_ENV_VAR, "") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _cipher() -> MultiFernet | None:
    keys = _keys()
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except (ValueError, TypeError) as e:
        raise SecretCryptoError(
            f"{_ENV_VAR} contains a malformed Fernet key "
            "(expected urlsafe-base64 32-byte key(s), comma-separated): "
            f"{e}"
        ) from e


def enabled() -> bool:
    """True when at least one key is configured (encryption is active)."""
    return bool(_keys())


def is_encrypted(stored: str) -> bool:
    """True when ``stored`` is an at-rest ciphertext written by encrypt()."""
    return isinstance(stored, str) and stored.startswith(_PREFIX)


def encrypt(plaintext: str) -> str:
    """Encrypt a secret value for at-rest storage.

    No key configured → return ``plaintext`` unchanged (passthrough). Empty
    values are never encrypted (a cleared secret stays empty, not an enc: blob).
    """
    if not plaintext:
        return plaintext
    cipher = _cipher()
    if cipher is None:
        return plaintext
    token = cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(stored: str) -> str:
    """Return the plaintext for a stored value.

    A non-``enc:`` value is legacy plaintext (or empty) → returned as-is; this
    is the only passthrough on read. An ``enc:`` value is ALWAYS decrypted or it
    raises SecretCryptoError — it is never returned as ciphertext.
    """
    if not is_encrypted(stored):
        return stored
    token = stored[len(_PREFIX):]
    cipher = _cipher()
    if cipher is None:
        raise SecretCryptoError(
            "an encrypted secret is stored but "
            f"{_ENV_VAR} is not set — cannot decrypt"
        )
    try:
        return cipher.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise SecretCryptoError(
            f"failed to decrypt secret — wrong or rotated-out {_ENV_VAR}?"
        ) from e
