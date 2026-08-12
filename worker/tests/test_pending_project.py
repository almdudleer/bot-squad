"""T-0640: the park store behind the ask-when-ambiguous fallback.

The store's whole job is that a message asked about survives until the answer
arrives, so these tests are about survival and about the two ways survival can
go wrong: losing a message that should have been replayed, and replaying one
that should not have been.
"""
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot_squad_worker import pending_project as PP


def _cfg(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "_worker").mkdir(parents=True)
    return types.SimpleNamespace(data_dir=data_dir)


def _msg(text="hi"):
    return {"chat": {"id": 111}, "text": text, "date": 1_700_000_000}


def test_park_then_take_round_trips_the_message(tmp_path):
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", None, _msg("do the thing"))
    got = PP.take(cfg, "gu_1")
    assert [e["msg"]["text"] for e in got] == ["do the thing"]
    assert got[0]["chat_id"] == "111"


def test_take_clears_so_an_answer_cannot_replay_twice(tmp_path):
    """Take-and-clear is the point: a second answer must not re-deliver the
    same dump into another project's thread, where nothing would mark it as a
    duplicate."""
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", None, _msg())
    assert len(PP.take(cfg, "gu_1")) == 1
    assert PP.take(cfg, "gu_1") == []


def test_park_keeps_order_so_a_replay_reads_as_a_conversation(tmp_path):
    cfg = _cfg(tmp_path)
    for t in ("first", "second", "third"):
        PP.park(cfg, "gu_1", "111", None, _msg(t))
    assert [e["msg"]["text"] for e in PP.take(cfg, "gu_1")] == ["first", "second", "third"]


def test_pending_is_per_user(tmp_path):
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", None, _msg("mine"))
    PP.park(cfg, "gu_2", "222", None, _msg("theirs"))
    assert [e["msg"]["text"] for e in PP.take(cfg, "gu_1")] == ["mine"]
    assert [e["msg"]["text"] for e in PP.take(cfg, "gu_2")] == ["theirs"]


def test_park_is_capped_dropping_the_oldest(tmp_path):
    """An unanswered question must not grow the file without limit. Dropping
    the OLDEST is the safe end: every parked message was already written
    durably to the incidental chat's thread before it was parked, so a drop
    costs a replay, not the message."""
    cfg = _cfg(tmp_path)
    for i in range(PP.MAX_PENDING + 5):
        PP.park(cfg, "gu_1", "111", None, _msg(f"m{i}"))
    got = [e["msg"]["text"] for e in PP.take(cfg, "gu_1")]
    assert len(got) == PP.MAX_PENDING
    assert got[0] == "m5" and got[-1] == f"m{PP.MAX_PENDING + 4}"


def test_stale_entries_are_not_replayed(tmp_path):
    """A day-old dump replayed because the user happened to type that
    project's name today would be worse than not replaying it."""
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", None, _msg("old"))
    PP.park(cfg, "gu_1", "111", None, _msg("recent"))
    raw = json.loads(PP.pending_path(cfg).read_text())
    stale = datetime.now(timezone.utc) - timedelta(seconds=PP.TTL_SECONDS + 60)
    raw["gu_1"][0]["asked_at"] = stale.isoformat().replace("+00:00", "Z")
    PP.pending_path(cfg).write_text(json.dumps(raw))

    assert [e["msg"]["text"] for e in PP.take(cfg, "gu_1")] == ["recent"]


def test_undatable_entry_is_treated_as_stale(tmp_path):
    """A replay is a WRITE into a project's thread. An entry we cannot date is
    one we cannot show is answering the current question, so it is dropped
    rather than delivered — the failure direction that costs a re-send instead
    of a misrouted dump."""
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", None, _msg("undatable"))
    raw = json.loads(PP.pending_path(cfg).read_text())
    raw["gu_1"][0]["asked_at"] = "not-a-timestamp"
    PP.pending_path(cfg).write_text(json.dumps(raw))

    assert PP.take(cfg, "gu_1") == []


def test_has_pending_does_not_consume(tmp_path):
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", None, _msg())
    assert PP.has_pending(cfg, "gu_1") is True
    assert PP.has_pending(cfg, "gu_1") is True
    assert len(PP.take(cfg, "gu_1")) == 1
    assert PP.has_pending(cfg, "gu_1") is False


def test_unreadable_store_is_treated_as_empty(tmp_path):
    """Same posture as tg_bindings: a corrupt park store must never break
    inbound routing."""
    cfg = _cfg(tmp_path)
    PP.pending_path(cfg).write_text("{not json")
    assert PP.load(cfg) == {}
    assert PP.take(cfg, "gu_1") == []
    assert PP.has_pending(cfg, "gu_1") is False


def test_entries_without_a_message_are_dropped_as_corrupt(tmp_path):
    cfg = _cfg(tmp_path)
    PP.pending_path(cfg).write_text(json.dumps({"gu_1": [{"chat_id": "111"}]}))
    assert PP.load(cfg) == {}


def test_park_survives_a_worker_restart(tmp_path):
    """Persisted, not in-memory: the answer often arrives minutes later, and a
    deploy in between must not silently swallow the question's subject."""
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", None, _msg("across a restart"))
    reloaded = types.SimpleNamespace(data_dir=cfg.data_dir)   # a fresh process
    assert [e["msg"]["text"] for e in PP.take(reloaded, "gu_1")] == ["across a restart"]


def test_thread_id_is_preserved(tmp_path):
    cfg = _cfg(tmp_path)
    PP.park(cfg, "gu_1", "111", 42, _msg())
    assert PP.take(cfg, "gu_1")[0]["thread_id"] == 42
