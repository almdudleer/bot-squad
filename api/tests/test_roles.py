"""T-0216 Phase A: the per-scope role enum + legacy-bool dual-read helpers."""
import pytest

from app.roles import (
    GlobalRole,
    ProjectRole,
    ServerRole,
    parse_global_role,
    parse_server_role,
)


def test_server_role_values_are_canonical_strings() -> None:
    assert ServerRole.SERVER_ADMIN.value == "server_admin"
    assert ServerRole.SERVER_MEMBER.value == "server_member"


def test_global_role_values_are_canonical_strings() -> None:
    assert GlobalRole.GLOBAL_ADMIN.value == "global_admin"
    assert GlobalRole.GLOBAL_MEMBER.value == "global_member"


def test_project_role_defined_for_phase_b() -> None:
    # Phase B is not wired, but the enum exists so storage can anticipate it.
    assert ProjectRole.PROJECT_ADMIN.value == "project_admin"
    assert ProjectRole.PROJECT_MEMBER.value == "project_member"


def test_parse_server_role_prefers_explicit_enum_value() -> None:
    # explicit server_role wins over the legacy bool, both directions
    assert parse_server_role("server_admin", legacy_is_admin=False) is ServerRole.SERVER_ADMIN
    assert parse_server_role("server_member", legacy_is_admin=True) is ServerRole.SERVER_MEMBER


def test_parse_server_role_falls_back_to_legacy_bool() -> None:
    # un-migrated row: no server_role, derive from is_admin
    assert parse_server_role(None, legacy_is_admin=True) is ServerRole.SERVER_ADMIN
    assert parse_server_role(None, legacy_is_admin=False) is ServerRole.SERVER_MEMBER
    assert parse_server_role("", legacy_is_admin=True) is ServerRole.SERVER_ADMIN


def test_parse_server_role_unknown_string_falls_back_to_legacy() -> None:
    # a corrupt/unknown value must not crash auth load — fall back to the bool
    assert parse_server_role("garbage", legacy_is_admin=True) is ServerRole.SERVER_ADMIN
    assert parse_server_role("garbage", legacy_is_admin=False) is ServerRole.SERVER_MEMBER


def test_parse_global_role_prefers_explicit_enum_value() -> None:
    assert parse_global_role("global_admin", legacy_is_super_admin=False) is GlobalRole.GLOBAL_ADMIN
    assert parse_global_role("global_member", legacy_is_super_admin=True) is GlobalRole.GLOBAL_MEMBER


def test_parse_global_role_falls_back_to_legacy_bool() -> None:
    assert parse_global_role(None, legacy_is_super_admin=True) is GlobalRole.GLOBAL_ADMIN
    assert parse_global_role(None, legacy_is_super_admin=False) is GlobalRole.GLOBAL_MEMBER


def test_parse_global_role_unknown_string_falls_back_to_legacy() -> None:
    assert parse_global_role("garbage", legacy_is_super_admin=True) is GlobalRole.GLOBAL_ADMIN
    assert parse_global_role("garbage", legacy_is_super_admin=False) is GlobalRole.GLOBAL_MEMBER
