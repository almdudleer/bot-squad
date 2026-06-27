"""Unit tests for the per-project pinned-session store (T-0437)."""
from __future__ import annotations

import json
from pathlib import Path

from app import pins_store


def test_load_missing_returns_empty(tmp_path: Path):
    assert pins_store.load(tmp_path, "proj") == {}


def test_pin_then_load_roundtrip(tmp_path: Path):
    pins_store.pin(tmp_path, "proj", "S-a-b-p1", by="alice", at="2026-06-21T00:00:00Z")
    got = pins_store.load(tmp_path, "proj")
    assert got == {"S-a-b-p1": {"by": "alice", "at": "2026-06-21T00:00:00Z"}}
    assert pins_store.is_pinned(tmp_path, "proj", "S-a-b-p1") is True
    assert pins_store.is_pinned(tmp_path, "proj", "S-x-y-p9") is False


def test_pin_is_idempotent_and_refreshes(tmp_path: Path):
    pins_store.pin(tmp_path, "proj", "S-a-b-p1", by="alice", at="2026-06-21T00:00:00Z")
    pins_store.pin(tmp_path, "proj", "S-a-b-p1", by="bob", at="2026-06-21T01:00:00Z")
    got = pins_store.load(tmp_path, "proj")
    assert len(got) == 1
    assert got["S-a-b-p1"] == {"by": "bob", "at": "2026-06-21T01:00:00Z"}


def test_unpin_returns_existed_flag_and_is_idempotent(tmp_path: Path):
    pins_store.pin(tmp_path, "proj", "S-a-b-p1", by="alice", at="t")
    assert pins_store.unpin(tmp_path, "proj", "S-a-b-p1") is True
    assert pins_store.unpin(tmp_path, "proj", "S-a-b-p1") is False
    assert pins_store.load(tmp_path, "proj") == {}


def test_pins_are_per_project_scoped(tmp_path: Path):
    pins_store.pin(tmp_path, "proj-a", "S-a-b-p1", by="alice", at="t")
    assert pins_store.is_pinned(tmp_path, "proj-a", "S-a-b-p1") is True
    assert pins_store.is_pinned(tmp_path, "proj-b", "S-a-b-p1") is False


def test_unreadable_store_treated_as_empty(tmp_path: Path):
    p = pins_store.pins_path(tmp_path, "proj")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert pins_store.load(tmp_path, "proj") == {}


def test_non_dict_meta_is_normalised(tmp_path: Path):
    p = pins_store.pins_path(tmp_path, "proj")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"S-a-b-p1": "garbage"}))
    got = pins_store.load(tmp_path, "proj")
    assert got == {"S-a-b-p1": {"by": None, "at": None}}


# ---------------------------------------------------------------------------
# T-0492: per-(user, server) CURRENT-PROJECT map. Distinct from the per-session
# sid pins above — this is the user's sticky "I'm talking to project X" routing
# target, keyed by global_user_id (the file lives on this server, so it is
# inherently per-server). Hardwired routing: unquoted messages attach here.
# ---------------------------------------------------------------------------


def test_get_current_project_missing_returns_none(tmp_path: Path):
    assert pins_store.get_current_project(tmp_path, "gu_1") is None


def test_set_then_get_current_project(tmp_path: Path):
    rec = pins_store.set_current_project(tmp_path, "gu_1", "proj-a", at="2026-06-27T00:00:00Z")
    assert rec == {"slug": "proj-a", "at": "2026-06-27T00:00:00Z"}
    assert pins_store.get_current_project(tmp_path, "gu_1") == "proj-a"


def test_switch_current_project_replaces_single_entry(tmp_path: Path):
    pins_store.set_current_project(tmp_path, "gu_1", "proj-a", at="t1")
    pins_store.set_current_project(tmp_path, "gu_1", "proj-b", at="t2")
    assert pins_store.get_current_project(tmp_path, "gu_1") == "proj-b"
    # one user -> one current project (the switch replaces, not appends)
    assert list(pins_store.load_current_projects(tmp_path).keys()) == ["gu_1"]


def test_current_project_is_per_user(tmp_path: Path):
    pins_store.set_current_project(tmp_path, "gu_1", "proj-a", at="t")
    pins_store.set_current_project(tmp_path, "gu_2", "proj-b", at="t")
    assert pins_store.get_current_project(tmp_path, "gu_1") == "proj-a"
    assert pins_store.get_current_project(tmp_path, "gu_2") == "proj-b"


def test_clear_current_project(tmp_path: Path):
    pins_store.set_current_project(tmp_path, "gu_1", "proj-a", at="t")
    assert pins_store.clear_current_project(tmp_path, "gu_1") is True
    assert pins_store.get_current_project(tmp_path, "gu_1") is None
    assert pins_store.clear_current_project(tmp_path, "gu_1") is False


def test_current_project_unreadable_treated_as_empty(tmp_path: Path):
    p = pins_store.current_project_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert pins_store.get_current_project(tmp_path, "gu_1") is None
