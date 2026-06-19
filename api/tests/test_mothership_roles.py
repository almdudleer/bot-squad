"""T-0216 Phase A: GlobalUser carries an explicit GlobalRole (replacing the
overloaded is_super_admin bool), dual-read back-compat, is_super_admin property,
and a rollback-safe on-disk mirror (behavior-preserving)."""
from __future__ import annotations

import json
from pathlib import Path

from app.mothership_users_store import GlobalUser, MothershipUsersStore
from app.roles import GlobalRole


def test_globaluser_default_is_member() -> None:
    u = GlobalUser(id="gu_x", username="u", password_hash="h", created_at="t")
    assert u.global_role is GlobalRole.GLOBAL_MEMBER
    assert u.is_super_admin is False


def test_globaluser_is_super_admin_property_reflects_role() -> None:
    admin = GlobalUser(id="gu_x", username="u", password_hash="h", created_at="t",
                       global_role=GlobalRole.GLOBAL_ADMIN)
    assert admin.is_super_admin is True


def test_create_user_maps_is_super_admin_kwarg_to_role(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    u = store.create_user(username="a", password_hash="h", is_super_admin=True)
    assert u.global_role is GlobalRole.GLOBAL_ADMIN
    assert u.is_super_admin is True
    # persists across reload
    listed = store.list_users()
    assert listed[0].global_role is GlobalRole.GLOBAL_ADMIN
    assert listed[0].is_super_admin is True


def test_list_users_dual_reads_legacy_is_super_admin(tmp_path: Path) -> None:
    # legacy users.json: only is_super_admin on disk, no global_role
    root = tmp_path / "_mothership"
    root.mkdir(parents=True)
    (root / "users.json").write_text(json.dumps({
        "version": 2,
        "users": [{
            "id": "gu_legacy", "username": "old", "password_hash": "h",
            "created_at": "t", "is_super_admin": True,
        }],
    }))
    store = MothershipUsersStore(root)
    u = store.list_users()[0]
    assert u.global_role is GlobalRole.GLOBAL_ADMIN
    assert u.is_super_admin is True


def test_list_users_prefers_explicit_global_role(tmp_path: Path) -> None:
    root = tmp_path / "_mothership"
    root.mkdir(parents=True)
    (root / "users.json").write_text(json.dumps({
        "version": 2,
        "users": [{
            "id": "gu_x", "username": "u", "password_hash": "h", "created_at": "t",
            "is_super_admin": False, "global_role": "global_admin",
        }],
    }))
    store = MothershipUsersStore(root)
    u = store.list_users()[0]
    assert u.global_role is GlobalRole.GLOBAL_ADMIN
    assert u.is_super_admin is True


def test_write_users_emits_both_role_and_legacy_mirror(tmp_path: Path) -> None:
    # rollback safety: on-disk row carries global_role (canonical) AND a legacy
    # is_super_admin mirror so pre-T-0216 code still reads admin correctly.
    store = MothershipUsersStore(tmp_path / "_mothership")
    store.create_user(username="a", password_hash="h", is_super_admin=True)
    raw = json.loads((tmp_path / "_mothership" / "users.json").read_text())
    row = raw["users"][0]
    assert row["global_role"] == "global_admin"
    assert row["is_super_admin"] is True


def test_to_public_keeps_is_super_admin_compat_key(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    u = store.create_user(username="a", password_hash="h", is_super_admin=True)
    pub = u.to_public()
    assert "password_hash" not in pub
    assert pub["is_super_admin"] is True
    assert pub["global_role"] == "global_admin"
