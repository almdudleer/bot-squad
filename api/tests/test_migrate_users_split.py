"""Migration script — idempotency + Attachment round-trip (T-0066)."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest


# Find scripts/migrate by walking up from this test file. Robust against
# both the host layout (repo at parents[2]) and the docker test image
# (api/ mounted at /app, so parents[1] is the rootlike).
def _find_migrate_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "scripts" / "migrate"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("scripts/migrate not found from test file location")


sys.path.insert(0, str(_find_migrate_dir()))


@pytest.fixture
def mothership_layout(tmp_bot_squad: Path) -> Path:
    """Augment the fixture with a populated _mothership/servers.json (is_self row)."""
    from app.mothership_store import MothershipStore

    store = MothershipStore(tmp_bot_squad / "data" / "_mothership")
    store.register_self_if_missing(
        base_url="https://mothership.test",
        display_name="mothership.test",
    )
    # Fixture seed has one ServerUser (testuser, admin) — augment with
    # extra rows so the migration has multiple users + a tg_chat_id /
    # seen_steps value to lift into Attachments.
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        'plain = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = true\n'
        'seen_steps = ["srv.intro", "srv.9_3.cross_server"]\n'
        'tg_chat_id = "111222"\n'
        '[user_meta.plain]\n'
        'linux_user = "plain"\n'
        'is_admin = false\n'
        '[session]\nttl = "7d"\n'
    )
    return tmp_bot_squad


def _run(mothership_layout: Path, **kwargs) -> dict:
    from users_split import migrate

    return migrate(
        config_dir=mothership_layout / "config",
        data_dir=mothership_layout / "data",
        server_id=kwargs.get("server_id"),
        dry_run=kwargs.get("dry_run", False),
    )


def test_migration_creates_global_users_and_attachments(mothership_layout: Path):
    stats = _run(mothership_layout)
    assert stats["scanned"] == 2
    assert stats["skipped_already_attached"] == 0
    assert stats["global_users_minted"] == 2
    assert stats["attachments_created"] == 2

    # GlobalUsers landed.
    users_json = json.loads(
        (mothership_layout / "data" / "_mothership" / "users.json").read_text()
    )
    names = sorted(u["username"] for u in users_json["users"])
    assert names == ["plain", "testuser"]

    # auth.toml gained the attached_to_global_user field on both rows.
    raw = tomllib.loads((mothership_layout / "config" / "auth.toml").read_text())
    for name in ["testuser", "plain"]:
        assert raw["user_meta"][name]["attached_to_global_user"].startswith("gu_")

    # Attachment captures the legacy tg_chat_id + seen_steps.
    test_gu = next(u for u in users_json["users"] if u["username"] == "testuser")
    attach_dir = (
        mothership_layout / "data" / "_mothership" / "attachments" / test_gu["id"]
    )
    attachments = list(attach_dir.iterdir())
    assert len(attachments) == 1
    attach = json.loads(attachments[0].read_text())
    assert attach["server_username"] == "testuser"
    assert attach["tg_chat_id"] == "111222"
    assert attach["seen_steps"] == ["srv.intro", "srv.9_3.cross_server"]


def test_migration_is_idempotent(mothership_layout: Path):
    """Re-running the script doesn't double-mint and doesn't churn data."""
    first = _run(mothership_layout)
    assert first["global_users_minted"] == 2

    second = _run(mothership_layout)
    assert second["scanned"] == 2
    # Both rows now have attached_to_global_user set → skipped entirely.
    assert second["skipped_already_attached"] == 2
    assert second["global_users_minted"] == 0
    assert second["attachments_created"] == 0
    assert second["attachments_updated"] == 0

    # Registry still has exactly two users, no doubles.
    users_json = json.loads(
        (mothership_layout / "data" / "_mothership" / "users.json").read_text()
    )
    assert len(users_json["users"]) == 2


def test_dry_run_does_not_mutate(mothership_layout: Path):
    """Dry-run reports counts but writes nothing to disk."""
    before = (mothership_layout / "config" / "auth.toml").read_text()
    stats = _run(mothership_layout, dry_run=True)
    assert stats["dry_run"] is True
    assert stats["global_users_minted"] == 2
    after = (mothership_layout / "config" / "auth.toml").read_text()
    assert before == after
    # No users.json written.
    assert not (mothership_layout / "data" / "_mothership" / "users.json").exists()


def test_migration_picks_up_self_server_id(mothership_layout: Path):
    """When --server-id is omitted, the is_self row's id is used."""
    from app.mothership_store import MothershipStore

    self_id = next(
        s.id for s in MothershipStore(
            mothership_layout / "data" / "_mothership"
        ).list_servers() if s.is_self
    )
    stats = _run(mothership_layout)
    assert stats["server_id"] == self_id


def test_migration_explicit_server_id(mothership_layout: Path):
    stats = _run(mothership_layout, server_id="srv_explicit")
    assert stats["server_id"] == "srv_explicit"
    # Attachments live under the explicit server id directory.
    users_json = json.loads(
        (mothership_layout / "data" / "_mothership" / "users.json").read_text()
    )
    gu = users_json["users"][0]
    path = (
        mothership_layout
        / "data"
        / "_mothership"
        / "attachments"
        / gu["id"]
        / "srv_explicit.json"
    )
    assert path.is_file()


def test_migration_skips_already_attached_rows(mothership_layout: Path):
    """A row whose auth.toml already has attached_to_global_user is left
    alone — the script never overwrites a pre-existing binding."""
    # First migrate normally.
    _run(mothership_layout)
    # Snapshot the registry id of testuser, then mutate auth.toml to point
    # at a fake uuid; rerunning must NOT clobber that fake binding.
    raw = tomllib.loads((mothership_layout / "config" / "auth.toml").read_text())
    fake = "gu_pretend_external_binding"
    new_toml = (mothership_layout / "config" / "auth.toml").read_text().replace(
        raw["user_meta"]["testuser"]["attached_to_global_user"], fake
    )
    (mothership_layout / "config" / "auth.toml").write_text(new_toml)

    stats = _run(mothership_layout)
    assert stats["skipped_already_attached"] == 2
    # auth.toml still has the fake binding.
    raw2 = tomllib.loads((mothership_layout / "config" / "auth.toml").read_text())
    assert raw2["user_meta"]["testuser"]["attached_to_global_user"] == fake
