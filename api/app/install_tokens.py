"""Install + server-bearer token primitives.

Opaque, base64url, 32 bytes of entropy. Three prefixes so the wire format
self-describes which kind of bearer the caller is sending:

- ``bsq_install_<32B base64url>`` — TTL'd, single-use at ``/connect``;
  issued by ``POST /api/m/servers`` for a fresh install.
- ``bsq_server_<32B base64url>``  — long-lived, revocable per server;
  the post-/connect bearer for ``/installer/checkpoint``.
- ``bsq_invite_<32B base64url>``  — TTL'd, single-use at ``/installer/join``;
  issued by ``POST /api/m/servers/{srv_id}/invites`` so an additional
  Linux user can join an EXISTING install (T-0026).

At rest we only ever store the SHA-256 hex of the plaintext. Plaintext
lives only in the URL / Authorization header / the one-shot response body
from ``POST /api/m/servers``, ``POST /api/m/installer/connect``, and
``POST /api/m/servers/{srv_id}/invites``.

Not JWTs: opaque keeps tokens trivially revocable and removes the
claim-migration risk that comes with signed bearer formats.

This module is stateless and detach-safe — no FS, no globals. Imported
by ``routes_mothership.py`` + ``mothership_store.py``, which are
themselves detach-deletable.
"""
from __future__ import annotations

import hashlib
import secrets

INSTALL_PREFIX = "bsq_install_"
SERVER_PREFIX = "bsq_server_"
INVITE_PREFIX = "bsq_invite_"

_ENTROPY_BYTES = 32


def mint_install_token() -> str:
    return INSTALL_PREFIX + secrets.token_urlsafe(_ENTROPY_BYTES)


def mint_server_bearer() -> str:
    return SERVER_PREFIX + secrets.token_urlsafe(_ENTROPY_BYTES)


def mint_invite_token() -> str:
    return INVITE_PREFIX + secrets.token_urlsafe(_ENTROPY_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_install_token(token: str) -> bool:
    return token.startswith(INSTALL_PREFIX)


def is_server_bearer(token: str) -> bool:
    return token.startswith(SERVER_PREFIX)


def is_invite_token(token: str) -> bool:
    return token.startswith(INVITE_PREFIX)
