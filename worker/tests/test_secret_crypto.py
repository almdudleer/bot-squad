"""Tests for the shared at-rest secret encryption helper (T-0179).

The module is a BYTE-IDENTICAL mirror across api/app/secret_crypto.py and
worker/bot_squad_worker/secret_crypto.py — same discipline as idalloc.py.
These tests exercise the worker copy; that the API copy can't drift is proved
by the mirror registry in `test_module_mirrors.py` (T-0743), which runs on every
push via `scripts/lint/module_mirrors.py`.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from bot_squad_worker import secret_crypto

_ENV = "BOT_SQUAD_SECRETS_KEY"


def _key() -> str:
    return Fernet.generate_key().decode("ascii")


def test_disabled_when_no_key(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert secret_crypto.enabled() is False


def test_enabled_when_key_present(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, _key())
    assert secret_crypto.enabled() is True


def test_encrypt_passthrough_when_no_key(monkeypatch) -> None:
    # Dev / fresh install: no key → values are written as plaintext, untouched.
    monkeypatch.delenv(_ENV, raising=False)
    out = secret_crypto.encrypt("123456:ABCDEF")
    assert out == "123456:ABCDEF"
    assert secret_crypto.is_encrypted(out) is False


def test_encrypt_empty_stays_empty(monkeypatch) -> None:
    # An empty/cleared secret never becomes an enc: blob (with or without key).
    monkeypatch.setenv(_ENV, _key())
    assert secret_crypto.encrypt("") == ""


def test_roundtrip_with_key(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, _key())
    enc = secret_crypto.encrypt("123456:ABCDEF")
    assert enc.startswith("enc:")
    assert secret_crypto.is_encrypted(enc) is True
    assert secret_crypto.decrypt(enc) == "123456:ABCDEF"


def test_decrypt_legacy_plaintext_passthrough_with_key(monkeypatch) -> None:
    # A non-enc: value is legacy plaintext; returned as-is even when a key is set.
    monkeypatch.setenv(_ENV, _key())
    assert secret_crypto.decrypt("LEGACY:PLAINTEXT") == "LEGACY:PLAINTEXT"


def test_decrypt_legacy_plaintext_passthrough_without_key(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert secret_crypto.decrypt("LEGACY:PLAINTEXT") == "LEGACY:PLAINTEXT"


def test_decrypt_empty_passthrough(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert secret_crypto.decrypt("") == ""


# --- the landmine: an enc: value must NEVER be returned as ciphertext ---

def test_decrypt_enc_value_without_key_raises_loud(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, _key())
    enc = secret_crypto.encrypt("SECRET")
    monkeypatch.delenv(_ENV, raising=False)
    with pytest.raises(secret_crypto.SecretCryptoError):
        secret_crypto.decrypt(enc)


def test_decrypt_enc_value_with_wrong_key_raises_loud(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, _key())
    enc = secret_crypto.encrypt("SECRET")
    monkeypatch.setenv(_ENV, _key())  # different key
    with pytest.raises(secret_crypto.SecretCryptoError):
        secret_crypto.decrypt(enc)


def test_malformed_key_raises_loud(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "not-a-valid-fernet-key")
    with pytest.raises(secret_crypto.SecretCryptoError):
        secret_crypto.encrypt("SECRET")


# --- rotation via comma-separated MultiFernet ---

def test_rotation_old_key_still_decrypts(monkeypatch) -> None:
    old = _key()
    new = _key()
    monkeypatch.setenv(_ENV, old)
    enc_old = secret_crypto.encrypt("SECRET")
    # Operator prepends the new key; old kept for read-back during migration.
    monkeypatch.setenv(_ENV, f"{new},{old}")
    assert secret_crypto.decrypt(enc_old) == "SECRET"


def test_rotation_new_writes_use_primary(monkeypatch) -> None:
    old = _key()
    new = _key()
    monkeypatch.setenv(_ENV, f"{new},{old}")
    enc_new = secret_crypto.encrypt("SECRET")
    # New writes are encrypted under the primary (first) key: decryptable with
    # new alone (proves the primary, not the old key, was used).
    monkeypatch.setenv(_ENV, new)
    assert secret_crypto.decrypt(enc_new) == "SECRET"


# The worker/api byte-identical mirror check for secret_crypto.py used to live
# here as its own copy of the comparison — and this module's docstring claimed it
# "fails CI", which was false: no CI job has ever run the worker suite. T-0743
# moved it to the single registry in `worker/tests/test_module_mirrors.py`, which
# `scripts/lint/module_mirrors.py` runs in CI and pre-push for real.
