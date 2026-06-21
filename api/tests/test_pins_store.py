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
