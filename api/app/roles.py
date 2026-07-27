"""T-0216 Phase A — the fixed user/permission role taxonomy.

Canonical spec: ``vision/roles/role-hierarchy.md`` (stakeholder verbatim
2026-06-02). Six FIXED roles = global/server/project × admin/member. This
module is the single home for the role enums and the legacy-bool dual-read
helpers so the auth/mothership stores can migrate lazily without rewriting
``auth.toml`` / ``users.json`` at deploy.

Phase A wires ServerRole (replaces ``UserMeta.is_admin``) and GlobalRole
(replaces ``GlobalUser.is_super_admin``). ProjectRole is defined but NOT wired
— project roles + ownership are Phase B (T-0216 follow-up, gated). The
build-flag bridge that derives super-admin from ``server is_admin AND
MOTHERSHIP=1`` is preserved in Phase A and split to T-0228 for removal.
"""
from __future__ import annotations

from enum import Enum


class ServerRole(str, Enum):
    """Authority on a single bot-squad installation (server scope)."""

    SERVER_ADMIN = "server_admin"
    SERVER_MEMBER = "server_member"


class GlobalRole(str, Enum):
    """Authority on the mothership (global scope)."""

    GLOBAL_ADMIN = "global_admin"
    GLOBAL_MEMBER = "global_member"


class ProjectRole(str, Enum):
    """Authority inside a single project (project scope). Phase B — defined so
    storage can anticipate it, not wired into any gate yet."""

    PROJECT_ADMIN = "project_admin"
    PROJECT_MEMBER = "project_member"


def server_role_for(is_admin: bool) -> ServerRole:
    """Map a legacy ``is_admin`` bool to its ServerRole. Ergonomic shorthand
    for construction sites migrating off the bool."""
    return ServerRole.SERVER_ADMIN if is_admin else ServerRole.SERVER_MEMBER


def global_role_for(is_super_admin: bool) -> GlobalRole:
    """Map a legacy ``is_super_admin`` bool to its GlobalRole."""
    return GlobalRole.GLOBAL_ADMIN if is_super_admin else GlobalRole.GLOBAL_MEMBER


def parse_server_role(raw: object, *, legacy_is_admin: bool = False) -> ServerRole:
    """Resolve a ServerRole from an explicit stored value, falling back to the
    legacy ``is_admin`` bool for un-migrated rows.

    An explicit, recognized value wins. Anything else (None, empty, unknown
    string) derives from ``legacy_is_admin`` so a corrupt value can never
    crash auth load nor silently escalate.
    """
    if isinstance(raw, str):
        try:
            return ServerRole(raw)
        except ValueError:
            pass
    return ServerRole.SERVER_ADMIN if legacy_is_admin else ServerRole.SERVER_MEMBER


def parse_global_role(raw: object, *, legacy_is_super_admin: bool = False) -> GlobalRole:
    """Resolve a GlobalRole from an explicit stored value, falling back to the
    legacy ``is_super_admin`` bool for un-migrated rows. Same safety contract
    as :func:`parse_server_role`."""
    if isinstance(raw, str):
        try:
            return GlobalRole(raw)
        except ValueError:
            pass
    return GlobalRole.GLOBAL_ADMIN if legacy_is_super_admin else GlobalRole.GLOBAL_MEMBER
