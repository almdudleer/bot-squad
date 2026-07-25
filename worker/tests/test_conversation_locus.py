"""T-0667: last-seen (chat_id, thread_id) locus per (slug, global_user_id)."""
from __future__ import annotations

import json
import types
from pathlib import Path

from bot_squad_worker import conversation_locus as CL


def _cfg(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    return types.SimpleNamespace(data_dir=data_dir)


def test_set_and_get_locus(tmp_path):
    cfg = _cfg(tmp_path)
    rec = CL.set_locus(cfg, "bot-squad", "gu_1", "111", 7)
    assert rec["chat_id"] == "111" and rec["thread_id"] == 7 and rec.get("at")
    # T-0676 items 3/6: a threaded locus lives at its OWN (slug,gid,thread)
    # key — fetch it back by passing the same thread_id.
    assert CL.get_locus(cfg, "bot-squad", "gu_1", 7) == rec


def test_get_locus_unset_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert CL.get_locus(cfg, "bot-squad", "gu_1") is None


def test_set_locus_thread_id_none_for_dm(tmp_path):
    cfg = _cfg(tmp_path)
    rec = CL.set_locus(cfg, "bot-squad", "gu_1", "555222111", None)
    assert rec["thread_id"] is None
    assert CL.get_locus(cfg, "bot-squad", "gu_1")["thread_id"] is None


def test_resetting_locus_overwrites(tmp_path):
    """Idempotent for the SAME key — the LATEST inbound message's origin at
    that (slug, gid[, thread]) always wins."""
    cfg = _cfg(tmp_path)
    CL.set_locus(cfg, "bot-squad", "gu_1", "111", None)
    CL.set_locus(cfg, "bot-squad", "gu_1", "222", None)
    rec = CL.get_locus(cfg, "bot-squad", "gu_1")
    assert rec["chat_id"] == "222" and rec["thread_id"] is None
    assert len(CL.load(cfg)) == 1


def test_locus_isolated_per_thread_id(tmp_path):
    """T-0676 items 3/6: two different bound topics of the SAME (slug, gid)
    get their OWN isolated locus entries — a message in topic 7 must never
    overwrite (or be shadowed by) topic 9's locus, and the bare (no-thread)
    DM locus is a THIRD, separate entry."""
    cfg = _cfg(tmp_path)
    CL.set_locus(cfg, "bot-squad", "gu_1", "111", 7)
    CL.set_locus(cfg, "bot-squad", "gu_1", "222", 9)
    CL.set_locus(cfg, "bot-squad", "gu_1", "333", None)

    assert CL.get_locus(cfg, "bot-squad", "gu_1", 7)["chat_id"] == "111"
    assert CL.get_locus(cfg, "bot-squad", "gu_1", 9)["chat_id"] == "222"
    assert CL.get_locus(cfg, "bot-squad", "gu_1")["chat_id"] == "333"
    assert len(CL.load(cfg)) == 3


def test_locus_scoped_per_slug_and_gid(tmp_path):
    cfg = _cfg(tmp_path)
    CL.set_locus(cfg, "alpha", "gu_1", "111", 1)
    CL.set_locus(cfg, "beta", "gu_1", "222", 2)
    CL.set_locus(cfg, "alpha", "gu_2", "333", 3)

    assert CL.get_locus(cfg, "alpha", "gu_1", 1)["chat_id"] == "111"
    assert CL.get_locus(cfg, "beta", "gu_1", 2)["chat_id"] == "222"
    assert CL.get_locus(cfg, "alpha", "gu_2", 3)["chat_id"] == "333"


def test_persists_across_reload(tmp_path):
    cfg = _cfg(tmp_path)
    CL.set_locus(cfg, "bot-squad", "gu_1", "111", 7)
    assert CL.locus_path(cfg).exists()
    reloaded = CL.load(cfg)
    assert reloaded["bot-squad:gu_1:7"]["chat_id"] == "111"


def test_load_missing_file_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert CL.load(cfg) == {}


def test_load_corrupt_json_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    p = CL.locus_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert CL.load(cfg) == {}


def test_load_drops_entries_missing_chat_id(tmp_path):
    cfg = _cfg(tmp_path)
    p = CL.locus_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "bot-squad:gu_1": {"thread_id": 7},
        "bot-squad:gu_2": {"chat_id": "111", "thread_id": None},
    }))
    loaded = CL.load(cfg)
    assert set(loaded) == {"bot-squad:gu_2"}


def test_latest_for_slug_picks_most_recent_across_gids(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ticks = iter(["2026-07-24T10:00:00Z", "2026-07-24T11:00:00Z"])
    monkeypatch.setattr(CL, "_now_iso", lambda: next(ticks))

    CL.set_locus(cfg, "bot-squad", "gu_1", "111", 7)   # earlier
    CL.set_locus(cfg, "bot-squad", "gu_2", "222", None)  # later -> wins

    latest = CL.latest_for_slug(cfg, "bot-squad")
    assert latest["chat_id"] == "222"
    assert latest["gid"] == "gu_2"  # T-0660: FYI-append mechanic needs the gid too


def test_latest_for_slug_scoped_to_slug(tmp_path):
    cfg = _cfg(tmp_path)
    CL.set_locus(cfg, "alpha", "gu_1", "111", 1)
    CL.set_locus(cfg, "beta", "gu_1", "222", 2)
    assert CL.latest_for_slug(cfg, "alpha")["chat_id"] == "111"
    assert CL.latest_for_slug(cfg, "alpha")["gid"] == "gu_1"


def test_latest_for_slug_none_when_unset(tmp_path):
    cfg = _cfg(tmp_path)
    assert CL.latest_for_slug(cfg, "bot-squad") is None
