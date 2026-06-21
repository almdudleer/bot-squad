"""Unit tests for bot_squad_worker.tg_topics — per-project forum-topic routing.

T-0386 / INI-04 Phase 1. The store holds the runtime-created forum thread-ids
(keyed by message class) under data/<slug>/_worker/tg_topics.json; the resolver
maps a message class → the right message_thread_id, falling back to the
project's legacy tg_topic_id (then None = general feed) so unprovisioned
projects behave exactly as before.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bot_squad_worker import tg_topics


def _cfg(tmp_path: Path, slug: str = "demo", tg_topic_id: int | None = None):
    data_dir = tmp_path / "data"
    project = SimpleNamespace(slug=slug, tg_chat="-100999", tg_topic_id=tg_topic_id)
    return SimpleNamespace(data_dir=data_dir, projects={slug: project})


def test_standard_topics_cover_the_three_classes():
    assert set(tg_topics.STANDARD_TOPICS) == {"feedback", "deploy_logs", "team_queries"}


def test_load_returns_empty_when_no_file(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert tg_topics.load(cfg, "demo") == {}


def test_save_then_load_roundtrips(tmp_path: Path):
    cfg = _cfg(tmp_path)
    tg_topics.save(cfg, "demo", {"feedback": 11, "deploy_logs": 22})
    assert tg_topics.load(cfg, "demo") == {"feedback": 11, "deploy_logs": 22}


def test_resolve_returns_class_topic_when_present(tmp_path: Path):
    cfg = _cfg(tmp_path)
    tg_topics.save(cfg, "demo", {"deploy_logs": 22})
    assert tg_topics.resolve(cfg, "demo", "deploy_logs") == 22


def test_resolve_falls_back_to_legacy_tg_topic_id(tmp_path: Path):
    # No per-class map; project has a legacy single topic → use it (no regression).
    cfg = _cfg(tmp_path, tg_topic_id=7)
    assert tg_topics.resolve(cfg, "demo", "deploy_logs") == 7


def test_resolve_none_when_no_class_and_no_legacy_topic(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert tg_topics.resolve(cfg, "demo", "deploy_logs") is None


def test_resolve_unknown_slug_is_none(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert tg_topics.resolve(cfg, "nope", "deploy_logs") is None


# ---------------------------------------------------------------------------
# T-0386 hardening (post-INI-04-Phase-1 verify-first): the resolve precedence,
# partial-map fallback, the DM flag-off-safe case, and load() resilience.
# ---------------------------------------------------------------------------

def test_resolve_class_map_wins_over_legacy_topic(tmp_path: Path):
    """Precedence: a per-class thread-id beats the legacy single tg_topic_id."""
    cfg = _cfg(tmp_path, tg_topic_id=7)
    tg_topics.save(cfg, "demo", {"deploy_logs": 22})
    assert tg_topics.resolve(cfg, "demo", "deploy_logs") == 22  # map wins, not 7


def test_resolve_partial_map_falls_back_to_legacy_for_unmapped_class(tmp_path: Path):
    """A class NOT in the map falls back to the legacy topic — it must NEVER
    return a sibling class's thread-id."""
    cfg = _cfg(tmp_path, tg_topic_id=7)
    tg_topics.save(cfg, "demo", {"deploy_logs": 22})
    assert tg_topics.resolve(cfg, "demo", "feedback") == 7  # not 22


def test_resolve_partial_map_unmapped_class_none_when_no_legacy(tmp_path: Path):
    cfg = _cfg(tmp_path)  # no tg_topic_id
    tg_topics.save(cfg, "demo", {"deploy_logs": 22})
    assert tg_topics.resolve(cfg, "demo", "feedback") is None  # not 22


def test_resolve_dm_chat_is_flag_off_safe(tmp_path: Path):
    """Flag-off-safe (the operator's named case): an unprovisioned project whose
    tg_chat is a plain DM (no map, no legacy topic) resolves EVERY class to None
    → the send lands on the DM/general feed exactly as before topic-routing."""
    cfg = _cfg(tmp_path)  # no map, no tg_topic_id
    for cls in tg_topics.STANDARD_TOPICS:
        assert tg_topics.resolve(cfg, "demo", cls) is None


def test_resolve_unknown_class_uses_same_fallback(tmp_path: Path):
    """An unknown message class follows the same chain — map miss → legacy →
    None — and never raises."""
    cfg = _cfg(tmp_path, tg_topic_id=7)
    assert tg_topics.resolve(cfg, "demo", "nonexistent_class") == 7


def test_load_corrupt_json_returns_empty(tmp_path: Path):
    """A truncated/garbage map file degrades to {} (general feed), never raises."""
    cfg = _cfg(tmp_path)
    p = tg_topics.topics_path(cfg, "demo")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json")
    assert tg_topics.load(cfg, "demo") == {}


def test_load_drops_null_values_and_coerces_str_ids(tmp_path: Path):
    """A half-provisioned class (null thread-id) is dropped; string ids coerce
    to int so resolve() always returns an int|None."""
    cfg = _cfg(tmp_path)
    p = tg_topics.topics_path(cfg, "demo")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"feedback": "22", "deploy_logs": null}')
    assert tg_topics.load(cfg, "demo") == {"feedback": 22}


def test_save_overwrites_existing_map(tmp_path: Path):
    cfg = _cfg(tmp_path)
    tg_topics.save(cfg, "demo", {"feedback": 11})
    tg_topics.save(cfg, "demo", {"deploy_logs": 22})
    assert tg_topics.load(cfg, "demo") == {"deploy_logs": 22}
