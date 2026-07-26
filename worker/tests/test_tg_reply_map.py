"""T-0719: tests for the sent-message-id -> originating-SID reply map."""
from __future__ import annotations

import json
import time
from pathlib import Path

from bot_squad_worker import tg_reply_map as RM


SID = "S-almdudleer-operator-p241"
CHAT = "404580642"


def _data(tmp_path: Path) -> Path:
    (tmp_path / "_worker").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# record / lookup
# ---------------------------------------------------------------------------

def test_record_then_lookup_roundtrip(tmp_path: Path) -> None:
    d = _data(tmp_path)
    assert RM.record(d, chat_id=CHAT, message_id=42, sid=SID) is True
    assert RM.lookup(d, chat_id=CHAT, message_id=42) == SID


def test_lookup_is_scoped_by_chat(tmp_path: Path) -> None:
    """message_id is unique per CHAT, not globally — a same-numbered message
    in another chat must not resolve to this session."""
    d = _data(tmp_path)
    RM.record(d, chat_id=CHAT, message_id=42, sid=SID)
    assert RM.lookup(d, chat_id="999", message_id=42) is None


def test_lookup_accepts_int_or_str_ids(tmp_path: Path) -> None:
    """TG hands ints inbound and the config carries chat ids as strings —
    both must hit the same key."""
    d = _data(tmp_path)
    RM.record(d, chat_id=CHAT, message_id="42", sid=SID)
    assert RM.lookup(d, chat_id=int(CHAT), message_id=42) == SID


def test_lookup_unknown_message_is_none(tmp_path: Path) -> None:
    assert RM.lookup(_data(tmp_path), chat_id=CHAT, message_id=7) is None


def test_lookup_missing_ids_is_none(tmp_path: Path) -> None:
    d = _data(tmp_path)
    assert RM.lookup(d, chat_id=CHAT, message_id=None) is None
    assert RM.lookup(d, chat_id=None, message_id=1) is None


# ---------------------------------------------------------------------------
# what does NOT get recorded
# ---------------------------------------------------------------------------

def test_synthetic_sender_is_not_recorded(tmp_path: Path) -> None:
    """`deploy_monitor` (jobs.deploy_monitor) is not a session — it has no pane
    to inject into, so replying to a deploy notice must keep falling through to
    the attendant exactly as it did before T-0719."""
    d = _data(tmp_path)
    assert RM.record(d, chat_id=CHAT, message_id=1, sid="deploy_monitor") is False
    assert RM.lookup(d, chat_id=CHAT, message_id=1) is None


def test_display_label_is_not_recorded(tmp_path: Path) -> None:
    """Guard against a caller wiring the DISPLAY label into route_sid by
    mistake — that would re-couple routing to presentation, the very bug."""
    d = _data(tmp_path)
    assert RM.record(d, chat_id=CHAT, message_id=1, sid="bot-squad operator") is False
    assert RM.record(d, chat_id=CHAT, message_id=2, sid="[bot-squad] " + SID) is False


def test_record_requires_ids(tmp_path: Path) -> None:
    d = _data(tmp_path)
    assert RM.record(d, chat_id=CHAT, message_id=None, sid=SID) is False
    assert RM.record(d, chat_id="", message_id=1, sid=SID) is False


def test_is_routing_sid() -> None:
    assert RM.is_routing_sid(SID)
    assert RM.is_routing_sid("S-almdudleer-gu_dc8262b6-user-conversation-p5")
    assert not RM.is_routing_sid("deploy_monitor")
    assert not RM.is_routing_sid("bot-squad operator")
    assert not RM.is_routing_sid("")
    assert not RM.is_routing_sid("S-no-pane-suffix")


# ---------------------------------------------------------------------------
# TTL + eviction
# ---------------------------------------------------------------------------

def test_expired_entry_does_not_resolve(tmp_path: Path) -> None:
    d = _data(tmp_path)
    path = RM.map_path(d)
    path.write_text(json.dumps({
        f"{CHAT}:42": {"sid": SID, "ts": time.time() - RM.TTL_SECONDS - 1},
    }))
    assert RM.lookup(d, chat_id=CHAT, message_id=42) is None


def test_fresh_entry_within_ttl_resolves(tmp_path: Path) -> None:
    d = _data(tmp_path)
    RM.map_path(d).write_text(json.dumps({
        f"{CHAT}:42": {"sid": SID, "ts": time.time() - RM.TTL_SECONDS + 3600},
    }))
    assert RM.lookup(d, chat_id=CHAT, message_id=42) == SID


def test_write_prunes_expired_entries(tmp_path: Path) -> None:
    d = _data(tmp_path)
    RM.map_path(d).write_text(json.dumps({
        f"{CHAT}:1": {"sid": SID, "ts": time.time() - RM.TTL_SECONDS - 1},
        f"{CHAT}:2": {"sid": SID, "ts": time.time()},
    }))
    RM.record(d, chat_id=CHAT, message_id=3, sid=SID)
    keys = set(json.loads(RM.map_path(d).read_text()))
    assert keys == {f"{CHAT}:2", f"{CHAT}:3"}


def test_map_is_capped_oldest_first(tmp_path: Path, monkeypatch) -> None:
    d = _data(tmp_path)
    monkeypatch.setattr(RM, "MAX_ENTRIES", 3)
    now = time.time()
    # ts ascending with age: :1 is the oldest, :3 the most recent.
    RM.map_path(d).write_text(json.dumps({
        f"{CHAT}:{i}": {"sid": SID, "ts": now - (10 - i)} for i in range(1, 4)
    }))
    RM.record(d, chat_id=CHAT, message_id=99, sid=SID)
    keys = set(json.loads(RM.map_path(d).read_text()))
    assert len(keys) == 3
    assert f"{CHAT}:99" in keys          # newest kept
    assert f"{CHAT}:1" not in keys       # oldest evicted


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------

def test_corrupt_store_degrades_to_empty(tmp_path: Path) -> None:
    """A corrupt map may cost us the message-id path (SID_RE still runs) — it
    must never raise out into inbound routing."""
    d = _data(tmp_path)
    RM.map_path(d).write_text("{not json")
    assert RM.load(d) == {}
    assert RM.lookup(d, chat_id=CHAT, message_id=42) is None


def test_non_dict_store_degrades_to_empty(tmp_path: Path) -> None:
    d = _data(tmp_path)
    RM.map_path(d).write_text('["a", "b"]')
    assert RM.load(d) == {}


def test_missing_store_is_empty(tmp_path: Path) -> None:
    assert RM.load(_data(tmp_path)) == {}


def test_last_write_wins_for_same_message(tmp_path: Path) -> None:
    d = _data(tmp_path)
    RM.record(d, chat_id=CHAT, message_id=42, sid=SID)
    RM.record(d, chat_id=CHAT, message_id=42, sid="S-almdudleer-dev-p7")
    assert RM.lookup(d, chat_id=CHAT, message_id=42) == "S-almdudleer-dev-p7"
