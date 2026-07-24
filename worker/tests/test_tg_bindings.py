"""T-0639: runtime (chat_id, message_thread_id) -> project binding store."""
from __future__ import annotations

import json
import types
from pathlib import Path

from bot_squad_worker import tg_bindings as TB


def _cfg(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    return types.SimpleNamespace(data_dir=data_dir)


def test_set_and_resolve_binding(tmp_path):
    cfg = _cfg(tmp_path)
    rec = TB.set_binding(cfg, "111", 7, "bot-squad")
    assert rec == {"slug": "bot-squad", "ticket_id": None, "session_id": None}
    assert TB.resolve(cfg, "111", 7) == {"slug": "bot-squad", "ticket_id": None, "session_id": None}


def test_resolve_unbound_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert TB.resolve(cfg, "111", 7) is None
    TB.set_binding(cfg, "111", 7, "bot-squad")
    # A different thread in the SAME chat is a different key -> still unbound.
    assert TB.resolve(cfg, "111", 8) is None


def test_resolve_none_thread_id_is_a_distinct_key(tmp_path):
    """A chat's non-topic/General feed (thread_id=None) is keyed distinctly
    from any real thread id (T-0660's project-General catch-all reuses this)."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", None, "bot-squad")
    assert TB.resolve(cfg, "111", None) == {"slug": "bot-squad", "ticket_id": None, "session_id": None}
    assert TB.resolve(cfg, "111", 1) is None


def test_rebinding_same_key_replaces(tmp_path):
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 7, "alpha")
    TB.set_binding(cfg, "111", 7, "beta")
    assert TB.resolve(cfg, "111", 7)["slug"] == "beta"
    assert len(TB.load(cfg)) == 1


def test_clear_binding(tmp_path):
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 7, "bot-squad")
    assert TB.clear_binding(cfg, "111", 7) is True
    assert TB.resolve(cfg, "111", 7) is None
    # Idempotent — clearing an already-cleared binding is a no-op, not an error.
    assert TB.clear_binding(cfg, "111", 7) is False


def test_multiple_bindings_same_project(tmp_path):
    """A project may have MULTIPLE bound (chat_id, thread_id) entries (D-0055
    §3, stakeholder requirement — not 1:1)."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 1, "bot-squad")
    TB.set_binding(cfg, "222", 5, "bot-squad")
    TB.set_binding(cfg, "111", 2, "watchrobot")

    assert TB.resolve(cfg, "111", 1)["slug"] == "bot-squad"
    assert TB.resolve(cfg, "222", 5)["slug"] == "bot-squad"
    assert TB.resolve(cfg, "111", 2)["slug"] == "watchrobot"
    assert len(TB.load(cfg)) == 3


def test_bound_chat_ids(tmp_path):
    cfg = _cfg(tmp_path)
    assert TB.bound_chat_ids(cfg) == set()
    TB.set_binding(cfg, "111", 1, "bot-squad")
    TB.set_binding(cfg, "111", 2, "watchrobot")
    TB.set_binding(cfg, "222", 5, "bot-squad")
    assert TB.bound_chat_ids(cfg) == {"111", "222"}


def test_persists_across_reload(tmp_path):
    """A fresh ``load()`` call (simulating a new process) sees the same data —
    single-writer JSON in the data dir, not in-memory state."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 7, "bot-squad")
    assert TB.bindings_path(cfg).exists()
    assert TB.load(cfg) == {"111:7": {"slug": "bot-squad", "ticket_id": None, "session_id": None}}


def test_load_missing_file_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert TB.load(cfg) == {}


def test_load_corrupt_json_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    p = TB.bindings_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert TB.load(cfg) == {}


def test_load_drops_entries_missing_slug(tmp_path):
    """A corrupt/partial entry (no slug) is dropped rather than surfaced as a
    valid binding — a binding without a project to route to is meaningless."""
    cfg = _cfg(tmp_path)
    p = TB.bindings_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"111:7": {"ticket_id": "T-1"}, "111:8": {"slug": "ok"}}))
    assert TB.load(cfg) == {"111:8": {"slug": "ok", "ticket_id": None, "session_id": None}}


def test_find_by_ticket_returns_bound_topic(tmp_path):
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 42, "beta", ticket_id="T-0700", session_id="S-dev-p9")
    found = TB.find_by_ticket(cfg, "T-0700")
    assert found == {"chat_id": "111", "thread_id": 42, "slug": "beta", "session_id": "S-dev-p9"}


def test_find_by_ticket_none_thread_id_roundtrips(tmp_path):
    """A ticket bound to a chat's General feed (thread_id=None) round-trips
    that None, not an empty string."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", None, "beta", ticket_id="T-0700")
    found = TB.find_by_ticket(cfg, "T-0700")
    assert found["thread_id"] is None


def test_find_by_ticket_unbound_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 42, "beta", ticket_id="T-0700")
    assert TB.find_by_ticket(cfg, "T-9999") is None


def test_find_by_ticket_ignores_project_topics_without_ticket_id(tmp_path):
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 1, "beta")  # plain project/General topic
    assert TB.find_by_ticket(cfg, "T-0700") is None


def test_set_binding_carries_optional_ticket_and_session_id(tmp_path):
    """T-0660 (per-task topics) generalization: the record already carries
    optional ticket_id/session_id so that layer bolts on with no rewrite —
    this MVP just never populates them via the T-0639 bind surface."""
    cfg = _cfg(tmp_path)
    rec = TB.set_binding(cfg, "111", 9, "bot-squad", ticket_id="T-0660", session_id="S-x-p1")
    assert rec == {"slug": "bot-squad", "ticket_id": "T-0660", "session_id": "S-x-p1"}
    assert TB.resolve(cfg, "111", 9) == rec
