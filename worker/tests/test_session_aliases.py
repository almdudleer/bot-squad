"""T-0662: human-readable label -> session SID alias store.

A label is a short, stakeholder-assignable nickname for a session, orthogonal
to sid_display_label (T-0636 — an auto-derived "[<slug>] <sid>" DISPLAY
string). The alias store is GLOBAL (not per-project): compute_sid carries no
project slug (T-0636), so a raw SID is not scoped to one project either, and
the whole point of a label is to be usable standalone in a future gateway
`to-session <label>` control phrase (T-0660 Addendum 1) without also naming a
project. Mirrors tg_bindings.py: single-writer JSON in the data dir, atomic
write, load-modify-save.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import session_aliases as SA


def _cfg(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    return types.SimpleNamespace(data_dir=data_dir)


def test_set_and_resolve_alias(tmp_path):
    cfg = _cfg(tmp_path)
    norm = SA.set_alias(cfg.data_dir, "gateway-tl", "S-almdudleer-gateway-routing-tl-p23")
    assert norm == "gateway-tl"
    assert SA.resolve_alias(cfg.data_dir, "gateway-tl") == "S-almdudleer-gateway-routing-tl-p23"


def test_resolve_unknown_label_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert SA.resolve_alias(cfg.data_dir, "nope") is None


def test_label_is_normalized_case_and_whitespace(tmp_path):
    cfg = _cfg(tmp_path)
    SA.set_alias(cfg.data_dir, "  Gateway-TL  ", "S-x-p1")
    assert SA.resolve_alias(cfg.data_dir, "gateway-tl") == "S-x-p1"
    assert SA.resolve_alias(cfg.data_dir, "GATEWAY-TL") == "S-x-p1"
    assert SA.resolve_alias(cfg.data_dir, "  gateway-tl") == "S-x-p1"


def test_rebinding_same_label_replaces(tmp_path):
    cfg = _cfg(tmp_path)
    SA.set_alias(cfg.data_dir, "alpha", "S-x-p1")
    SA.set_alias(cfg.data_dir, "alpha", "S-y-p2")
    assert SA.resolve_alias(cfg.data_dir, "alpha") == "S-y-p2"
    assert len(SA.load_aliases(cfg.data_dir)) == 1


def test_remove_alias(tmp_path):
    cfg = _cfg(tmp_path)
    SA.set_alias(cfg.data_dir, "alpha", "S-x-p1")
    assert SA.remove_alias(cfg.data_dir, "alpha") is True
    assert SA.resolve_alias(cfg.data_dir, "alpha") is None
    # Idempotent — removing an already-gone label is a no-op, not an error.
    assert SA.remove_alias(cfg.data_dir, "alpha") is False


def test_multiple_labels_same_sid(tmp_path):
    """One session may carry more than one nickname; the uniqueness contract
    (T-0660 Addendum 1) is label -> exactly one sid, not sid -> one label."""
    cfg = _cfg(tmp_path)
    SA.set_alias(cfg.data_dir, "alpha", "S-x-p1")
    SA.set_alias(cfg.data_dir, "beta", "S-x-p1")
    assert SA.resolve_alias(cfg.data_dir, "alpha") == "S-x-p1"
    assert SA.resolve_alias(cfg.data_dir, "beta") == "S-x-p1"
    assert SA.aliases_for_sid(cfg.data_dir, "S-x-p1") == ["alpha", "beta"]


def test_aliases_for_sid_empty_when_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert SA.aliases_for_sid(cfg.data_dir, "S-x-p1") == []


def test_distinct_labels_resolve_independently(tmp_path):
    cfg = _cfg(tmp_path)
    SA.set_alias(cfg.data_dir, "alpha", "S-x-p1")
    SA.set_alias(cfg.data_dir, "beta", "S-y-p2")
    assert SA.resolve_alias(cfg.data_dir, "alpha") == "S-x-p1"
    assert SA.resolve_alias(cfg.data_dir, "beta") == "S-y-p2"
    assert len(SA.load_aliases(cfg.data_dir)) == 2


def test_persists_across_reload(tmp_path):
    cfg = _cfg(tmp_path)
    SA.set_alias(cfg.data_dir, "alpha", "S-x-p1")
    assert SA.aliases_path(cfg.data_dir).exists()
    assert SA.load_aliases(cfg.data_dir) == {"alpha": "S-x-p1"}


def test_load_missing_file_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert SA.load_aliases(cfg.data_dir) == {}


def test_load_corrupt_json_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    p = SA.aliases_path(cfg.data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert SA.load_aliases(cfg.data_dir) == {}


def test_load_non_dict_json_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    p = SA.aliases_path(cfg.data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(["not", "a", "dict"]))
    assert SA.load_aliases(cfg.data_dir) == {}


def test_store_is_global_not_per_project(tmp_path):
    """The alias JSON lives directly under data/_worker/, not under any single
    project's data/<slug>/ subtree — a raw SID isn't project-scoped either
    (T-0636), so neither is its label."""
    cfg = _cfg(tmp_path)
    SA.set_alias(cfg.data_dir, "alpha", "S-x-p1")
    assert SA.aliases_path(cfg.data_dir) == Path(cfg.data_dir) / "_worker" / "session_aliases.json"


@pytest.mark.parametrize("bad_label", ["", "   ", "1abc", "-abc", "_abc", "has space", "Has!Bang", "a" * 33])
def test_set_alias_rejects_invalid_label(tmp_path, bad_label):
    cfg = _cfg(tmp_path)
    with pytest.raises(SA.InvalidLabelError):
        SA.set_alias(cfg.data_dir, bad_label, "S-x-p1")


@pytest.mark.parametrize("sid_like_label", ["s-almdudleer-teamlead-p3", "S-x-p1"])
def test_set_alias_rejects_label_shaped_like_a_raw_sid(tmp_path, sid_like_label):
    """A label starting with 's-' would be ambiguous for the future to-session
    resolver (can't tell a raw-SID phrase from a label phrase) — T-0662 peer
    guidance from the gateway-routing TL (S-almdudleer-gateway-routing-tl-p23)."""
    cfg = _cfg(tmp_path)
    with pytest.raises(SA.InvalidLabelError):
        SA.set_alias(cfg.data_dir, sid_like_label, "S-y-p2")


def test_set_alias_accepts_lowercase_alnum_hyphen_underscore(tmp_path):
    cfg = _cfg(tmp_path)
    norm = SA.set_alias(cfg.data_dir, "gw-tl_2", "S-x-p1")
    assert norm == "gw-tl_2"


def test_set_alias_rejects_empty_sid(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(SA.InvalidLabelError):
        SA.set_alias(cfg.data_dir, "alpha", "")


def test_normalize_label_lowercases_and_strips():
    assert SA.normalize_label("  Foo-Bar  ") == "foo-bar"
