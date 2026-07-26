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


def _rec(slug: str, *, ticket_id=None, session_id=None, pinned_message_id=None) -> dict:
    """The full stored record shape — every optional field explicit, so a
    future field addition shows up as one edit here instead of N."""
    return {
        "slug": slug, "ticket_id": ticket_id, "session_id": session_id,
        "pinned_message_id": pinned_message_id,
    }


def test_set_and_resolve_binding(tmp_path):
    cfg = _cfg(tmp_path)
    rec = TB.set_binding(cfg, "111", 7, "bot-squad")
    assert rec == _rec("bot-squad")
    assert TB.resolve(cfg, "111", 7) == _rec("bot-squad")


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
    assert TB.resolve(cfg, "111", None) == _rec("bot-squad")
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


def test_bound_topic_slugs_excludes_general_feed_entry(tmp_path):
    """T-0700: only REAL topics (thread_id is not None) count — a chat's own
    General-feed (chat_id, None) binding is excluded, since it isn't a topic
    and the caller (the multi-project-forum check) must not conflate the two."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 1, "alpha")
    TB.set_binding(cfg, "111", None, "gamma")  # General-feed entry, not a topic
    TB.set_binding(cfg, "222", 9, "beta")      # a different chat, irrelevant
    assert TB.bound_topic_slugs(cfg, "111") == {"alpha"}


def test_bound_topic_slugs_multiple_distinct(tmp_path):
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 1, "alpha")
    TB.set_binding(cfg, "111", 2, "beta")
    assert TB.bound_topic_slugs(cfg, "111") == {"alpha", "beta"}


def test_bound_topic_slugs_same_slug_collapses_to_one(tmp_path):
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 1, "alpha")
    TB.set_binding(cfg, "111", 2, "alpha")
    assert TB.bound_topic_slugs(cfg, "111") == {"alpha"}


def test_bound_topic_slugs_no_bindings_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert TB.bound_topic_slugs(cfg, "111") == set()


def test_persists_across_reload(tmp_path):
    """A fresh ``load()`` call (simulating a new process) sees the same data —
    single-writer JSON in the data dir, not in-memory state."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 7, "bot-squad")
    assert TB.bindings_path(cfg).exists()
    assert TB.load(cfg) == {"111:7": _rec("bot-squad")}


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
    assert TB.load(cfg) == {"111:8": _rec("ok")}


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
    assert rec == _rec("bot-squad", ticket_id="T-0660", session_id="S-x-p1")
    assert TB.resolve(cfg, "111", 9) == rec


# ---------------------------------------------------------------------------
# T-0677: set_direct_session — the per-topic direct-mode toggle's one field
# ---------------------------------------------------------------------------

def test_set_direct_session_flips_only_the_session_id(tmp_path):
    """Direct mode is a MODE of an existing binding, not a new record: the
    topic's project (and its per-task ticket_id) must survive the toggle."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 7, "bot-squad", ticket_id="T-0677")

    rec = TB.set_direct_session(cfg, "111", 7, "S-dev-p9", pinned_message_id=555)

    assert rec == _rec("bot-squad", ticket_id="T-0677", session_id="S-dev-p9",
                       pinned_message_id=555)
    assert TB.resolve(cfg, "111", 7) == rec


def test_set_direct_session_none_returns_to_the_attendant(tmp_path):
    """Attendant-routed is the default — clearing session_id (and the stale pin
    id) is the way back."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 7, "bot-squad")
    TB.set_direct_session(cfg, "111", 7, "S-dev-p9", pinned_message_id=555)

    rec = TB.set_direct_session(cfg, "111", 7, None)

    assert rec == _rec("bot-squad")


def test_set_direct_session_refuses_to_create_a_binding(tmp_path):
    """An unbound topic has no project to route to — inventing one is the
    silent mis-slugging T-0693 removed. Report it instead."""
    cfg = _cfg(tmp_path)
    assert TB.set_direct_session(cfg, "111", 7, "S-dev-p9") is None
    assert TB.load(cfg) == {}


def test_pinned_message_id_survives_a_reload(tmp_path):
    """The pin id must outlive the process — a worker restart still has to be
    able to unpin the marker it placed."""
    cfg = _cfg(tmp_path)
    TB.set_binding(cfg, "111", 7, "bot-squad")
    TB.set_direct_session(cfg, "111", 7, "S-dev-p9", pinned_message_id=555)
    assert TB.load(cfg)["111:7"]["pinned_message_id"] == 555
