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
