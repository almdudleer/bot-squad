"""T-0496: per-project user-groups store — CRUD + membership + the seam lookup.

Mirrors ``test_mothership_users_store.py``: a tmp data dir, exercise the store
directly, assert on-disk persistence + the ``group_for_user`` lookup the
conversational role (T-0478 seam) consumes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.project_groups_store import ProjectGroupsStore, Group


SLUG = "test-project"


def _store(tmp_path: Path) -> ProjectGroupsStore:
    return ProjectGroupsStore(tmp_path / "data")


# ---- group CRUD -------------------------------------------------------------


def test_create_and_list_group(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers", role="developer",
                           access_scope="full bot-squad control", prompt="you are a dev")
    assert g.id.startswith("grp_")
    assert g.name == "developers"
    assert g.role == "developer"
    assert g.access_scope == "full bot-squad control"
    assert g.prompt == "you are a dev"
    assert g.created_at

    groups = store.list_groups(SLUG)
    assert [x.id for x in groups] == [g.id]


def test_create_persists_to_disk_under_project(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="support")
    path = tmp_path / "data" / SLUG / "groups.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["groups"][0]["id"] == g.id
    assert data["memberships"] == {}


def test_create_empty_name_rejected(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_group(SLUG, name="   ")


def test_create_duplicate_name_rejected(tmp_path: Path):
    store = _store(tmp_path)
    store.create_group(SLUG, name="developers")
    with pytest.raises(ValueError):
        store.create_group(SLUG, name="developers")


def test_same_name_different_projects_ok(tmp_path: Path):
    store = _store(tmp_path)
    a = store.create_group(SLUG, name="developers")
    b = store.create_group("other-project", name="developers")
    assert a.id != b.id
    assert [g.id for g in store.list_groups(SLUG)] == [a.id]
    assert [g.id for g in store.list_groups("other-project")] == [b.id]


def test_get_group(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers")
    assert store.get_group(SLUG, g.id) == g
    assert store.get_group(SLUG, "grp_nope") is None


def test_update_group_partial(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="support", role="support", prompt="old")
    updated = store.update_group(SLUG, g.id, prompt="new prompt")
    assert updated.prompt == "new prompt"
    assert updated.role == "support"  # untouched
    assert updated.name == "support"  # untouched
    # Persisted.
    assert store.get_group(SLUG, g.id).prompt == "new prompt"


def test_update_unknown_group_returns_none(tmp_path: Path):
    store = _store(tmp_path)
    assert store.update_group(SLUG, "grp_nope", prompt="x") is None


def test_update_rename_collision_rejected(tmp_path: Path):
    store = _store(tmp_path)
    store.create_group(SLUG, name="developers")
    g2 = store.create_group(SLUG, name="support")
    with pytest.raises(ValueError):
        store.update_group(SLUG, g2.id, name="developers")


def test_delete_group_idempotent(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="support")
    assert store.delete_group(SLUG, g.id) is True
    assert store.get_group(SLUG, g.id) is None
    assert store.delete_group(SLUG, g.id) is False


def test_delete_group_drops_memberships(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers")
    store.set_membership(SLUG, "gu_abc", g.id)
    assert store.group_for_user(SLUG, "gu_abc").id == g.id
    store.delete_group(SLUG, g.id)
    assert store.group_for_user(SLUG, "gu_abc") is None


# ---- membership -------------------------------------------------------------


def test_set_and_lookup_membership(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers", role="developer", prompt="dev prompt")
    store.set_membership(SLUG, "gu_abc", g.id)
    found = store.group_for_user(SLUG, "gu_abc")
    assert found.id == g.id
    assert found.role == "developer"
    assert found.prompt == "dev prompt"


def test_membership_is_single_group_per_project(tmp_path: Path):
    store = _store(tmp_path)
    a = store.create_group(SLUG, name="developers")
    b = store.create_group(SLUG, name="support")
    store.set_membership(SLUG, "gu_abc", a.id)
    store.set_membership(SLUG, "gu_abc", b.id)  # moves the user
    assert store.group_for_user(SLUG, "gu_abc").id == b.id
    assert store.list_members(SLUG, a.id) == []
    assert store.list_members(SLUG, b.id) == ["gu_abc"]


def test_set_membership_unknown_group_rejected(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.set_membership(SLUG, "gu_abc", "grp_nope")


def test_set_membership_idempotent(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers")
    store.set_membership(SLUG, "gu_abc", g.id)
    store.set_membership(SLUG, "gu_abc", g.id)
    assert store.list_members(SLUG, g.id) == ["gu_abc"]


def test_remove_membership_idempotent(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers")
    store.set_membership(SLUG, "gu_abc", g.id)
    assert store.remove_membership(SLUG, "gu_abc") is True
    assert store.group_for_user(SLUG, "gu_abc") is None
    assert store.remove_membership(SLUG, "gu_abc") is False


def test_group_for_user_none_when_no_membership(tmp_path: Path):
    store = _store(tmp_path)
    store.create_group(SLUG, name="developers")
    assert store.group_for_user(SLUG, "gu_nobody") is None


def test_list_members_sorted(tmp_path: Path):
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers")
    store.set_membership(SLUG, "gu_zzz", g.id)
    store.set_membership(SLUG, "gu_aaa", g.id)
    assert store.list_members(SLUG, g.id) == ["gu_aaa", "gu_zzz"]


# ---- robustness -------------------------------------------------------------


def test_missing_file_reads_empty(tmp_path: Path):
    store = _store(tmp_path)
    assert store.list_groups(SLUG) == []
    assert store.group_for_user(SLUG, "gu_abc") is None


def test_unsafe_slug_rejected(tmp_path: Path):
    store = _store(tmp_path)
    for bad in ("../escape", "a/b", "", ".", ".."):
        with pytest.raises(ValueError):
            store.list_groups(bad)


def test_forward_compat_unknown_field_ignored(tmp_path: Path):
    """A newer process writing an extra group field must not 500 this reader."""
    store = _store(tmp_path)
    g = store.create_group(SLUG, name="developers")
    path = tmp_path / "data" / SLUG / "groups.json"
    data = json.loads(path.read_text())
    data["groups"][0]["future_field"] = "from a newer version"
    path.write_text(json.dumps(data))
    out = store.get_group(SLUG, g.id)
    assert isinstance(out, Group)
    assert out.name == "developers"
