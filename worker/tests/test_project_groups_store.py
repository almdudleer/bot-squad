"""Tests for bot_squad_worker.project_groups_store — worker-side read-only
mirror of app.project_groups_store's group_for_user lookup (T-0496, T-0591
F5.10 seam consumption)."""
from __future__ import annotations

import json

import pytest

from bot_squad_worker.project_groups_store import group_for_user


def _write_groups(data_dir, slug, *, groups, memberships):
    d = data_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "groups.json").write_text(json.dumps({
        "version": 1, "groups": groups, "memberships": memberships,
    }))


def test_no_groups_file_returns_none(tmp_path):
    assert group_for_user(tmp_path, "proj", "gu_a") is None


def test_user_with_no_membership_returns_none(tmp_path):
    _write_groups(tmp_path, "proj",
                  groups=[{"id": "grp_1", "name": "support", "prompt": "be terse"}],
                  memberships={})
    assert group_for_user(tmp_path, "proj", "gu_a") is None


def test_user_membership_resolves_to_group(tmp_path):
    _write_groups(tmp_path, "proj",
                  groups=[{"id": "grp_1", "name": "support",
                           "role": "support", "access_scope": "scoped",
                           "prompt": "be terse"}],
                  memberships={"gu_a": "grp_1"})
    g = group_for_user(tmp_path, "proj", "gu_a")
    assert g["name"] == "support"
    assert g["prompt"] == "be terse"


def test_membership_pointing_at_deleted_group_returns_none(tmp_path):
    _write_groups(tmp_path, "proj", groups=[], memberships={"gu_a": "grp_gone"})
    assert group_for_user(tmp_path, "proj", "gu_a") is None


def test_garbage_groups_file_is_tolerated(tmp_path):
    d = tmp_path / "proj"
    d.mkdir(parents=True)
    (d / "groups.json").write_text("{not json")
    assert group_for_user(tmp_path, "proj", "gu_a") is None


def test_unsafe_slug_raises():
    import pathlib
    with pytest.raises(ValueError):
        group_for_user(pathlib.Path("/tmp"), "../escape", "gu_a")
