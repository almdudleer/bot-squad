"""GlobalUser + Attachment store (T-0066) — atomic writes, idempotency."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mothership_users_store import (
    Attachment,
    GlobalUser,
    MothershipUsersStore,
)


def test_create_and_list_user_roundtrip(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    u = store.create_user(
        username="alexey",
        password_hash="$2b$12$dummyhash",
        display_name="Alexey",
        email="a@example.com",
        timezone_name="Europe/Moscow",
        is_super_admin=True,
    )
    assert isinstance(u, GlobalUser)
    assert u.id.startswith("gu_")
    assert u.is_super_admin is True

    listed = store.list_users()
    assert len(listed) == 1
    assert listed[0].username == "alexey"
    assert listed[0].password_hash == "$2b$12$dummyhash"


def test_create_duplicate_raises(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    store.create_user(username="x", password_hash="h")
    with pytest.raises(ValueError):
        store.create_user(username="x", password_hash="h2")


def test_upsert_user_is_idempotent(tmp_path: Path) -> None:
    """Migration re-runs must return the same id, not mint a new one."""
    store = MothershipUsersStore(tmp_path / "_mothership")
    first, created1 = store.upsert_user_by_username(
        username="alexey", password_hash="h1",
    )
    assert created1 is True

    second, created2 = store.upsert_user_by_username(
        username="alexey", password_hash="h2-ignored",
    )
    assert created2 is False
    # Same id — re-running migration doesn't double-mint.
    assert second.id == first.id
    # Existing hash preserved — we never overwrite an established password.
    assert second.password_hash == "h1"
    assert len(store.list_users()) == 1


def test_to_public_strips_password_hash(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    u = store.create_user(username="x", password_hash="$2b$12$secret")
    pub = u.to_public()
    assert "password_hash" not in pub
    assert pub["username"] == "x"
    assert pub["id"] == u.id


def test_unknown_field_on_disk_is_ignored(tmp_path: Path) -> None:
    """Rollback safety: a newer process may write a field we don't know about."""
    root = tmp_path / "_mothership"
    root.mkdir()
    (root / "users.json").write_text(
        json.dumps(
            {
                "version": 1,
                "users": [
                    {
                        "id": "gu_abc",
                        "username": "x",
                        "password_hash": "h",
                        "created_at": "2026-05-23T12:00:00Z",
                        "future_field": "ignored",
                    }
                ],
            }
        )
    )
    store = MothershipUsersStore(root)
    users = store.list_users()
    assert len(users) == 1
    assert users[0].username == "x"


def test_attachment_upsert_create_then_update(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    u = store.create_user(username="x", password_hash="h")
    first, created1 = store.upsert_attachment(
        global_user_id=u.id,
        server_id="srv_abc",
        server_username="x",
        tg_chat_id="12345",
        seen_steps=("a", "b"),
    )
    assert created1 is True
    assert isinstance(first, Attachment)
    assert first.tg_chat_id == "12345"
    assert first.seen_steps == ("a", "b")
    attached_at = first.attached_at

    # Update the same (user, server) pair — attached_at MUST be preserved
    # (it records first-binding time, not latest write).
    second, created2 = store.upsert_attachment(
        global_user_id=u.id,
        server_id="srv_abc",
        server_username="x",
        tg_chat_id="99999",
        seen_steps=("a", "b", "c"),
    )
    assert created2 is False
    assert second.attached_at == attached_at
    assert second.tg_chat_id == "99999"
    assert second.seen_steps == ("a", "b", "c")

    # On-disk file is in the right shape: nested directory per global_user_id.
    path = tmp_path / "_mothership" / "attachments" / u.id / "srv_abc.json"
    assert path.is_file()
    on_disk = json.loads(path.read_text())
    assert on_disk["server_username"] == "x"
    assert on_disk["tg_chat_id"] == "99999"


def test_get_attachment_missing_returns_none(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    assert store.get_attachment("gu_nope", "srv_nope") is None


def test_list_attachments_for_user(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    u = store.create_user(username="x", password_hash="h")
    store.upsert_attachment(
        global_user_id=u.id, server_id="srv_a", server_username="x",
    )
    store.upsert_attachment(
        global_user_id=u.id, server_id="srv_b", server_username="xtra",
    )
    out = store.list_attachments_for_user(u.id)
    ids = sorted(a.server_id for a in out)
    assert ids == ["srv_a", "srv_b"]


def test_touch_last_seen_updates_timestamp(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    u = store.create_user(username="x", password_hash="h")
    store.upsert_attachment(
        global_user_id=u.id, server_id="srv_a", server_username="x",
    )
    updated = store.touch_last_seen(u.id, "srv_a")
    assert updated is not None
    assert updated.last_seen_at is not None


def test_touch_last_seen_unknown_pair_is_noop(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    assert store.touch_last_seen("gu_nope", "srv_nope") is None


# ---- T-0488: TG sender -> GlobalUser linkage (single bot user recognition) ----


def test_resolve_or_link_tg_user_first_contact_creates(tmp_path: Path) -> None:
    """First contact from a TG sender mints a GlobalUser carrying its tg_user_id."""
    store = MothershipUsersStore(tmp_path / "_mothership")
    user, created = store.resolve_or_link_tg_user(
        tg_user_id="555123", display_name="Alexey S"
    )
    assert created is True
    assert isinstance(user, GlobalUser)
    assert user.id.startswith("gu_")
    assert user.tg_user_id == "555123"
    assert user.display_name == "Alexey S"
    # The link is durable across a fresh store handle (separate "server").
    reloaded = MothershipUsersStore(tmp_path / "_mothership")
    assert reloaded.user_by_tg_user_id("555123") is not None
    assert reloaded.user_by_tg_user_id("555123").id == user.id


def test_resolve_or_link_tg_user_recognized_on_subsequent(tmp_path: Path) -> None:
    """A second message from the same TG sender is recognized — same GlobalUser,
    no new mint, and the recognition holds across servers (cross-server registry)."""
    store = MothershipUsersStore(tmp_path / "_mothership")
    first, created1 = store.resolve_or_link_tg_user(tg_user_id="555123")
    assert created1 is True

    # Recognition as seen from ANOTHER server (a fresh store over the same
    # cross-server registry) returns the same identity without re-minting.
    other_server_view = MothershipUsersStore(tmp_path / "_mothership")
    second, created2 = other_server_view.resolve_or_link_tg_user(tg_user_id="555123")
    assert created2 is False
    assert second.id == first.id
    assert len(store.list_users()) == 1


def test_user_by_tg_user_id_unknown_returns_none(tmp_path: Path) -> None:
    store = MothershipUsersStore(tmp_path / "_mothership")
    assert store.user_by_tg_user_id("does-not-exist") is None


def test_tg_user_id_roundtrips_through_list(tmp_path: Path) -> None:
    """tg_user_id survives the on-disk serialize/deserialize cycle."""
    store = MothershipUsersStore(tmp_path / "_mothership")
    store.resolve_or_link_tg_user(tg_user_id="999", display_name="Z")
    reloaded = MothershipUsersStore(tmp_path / "_mothership").list_users()
    assert len(reloaded) == 1
    assert reloaded[0].tg_user_id == "999"
