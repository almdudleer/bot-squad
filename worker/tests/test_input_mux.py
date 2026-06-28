"""Tests for input multiplexing queue (T-0469, M1/F1.6).

The multiplexer fans many logical inputs (peer agents, system events, user-
conversation wakes) onto ONE tmux channel per session: queue concurrent writes
(NEVER reject), deliver them BATCHED with author captions, and never clobber
the user's live-typed composer text.
"""
from __future__ import annotations

import threading

import pytest

from bot_squad_worker import input_mux


# ---------------------------------------------------------------------------
# Composer-state detection (never mix with live user-typed text)
# ---------------------------------------------------------------------------

# Empty composer — the ❯ rune followed by whitespace only.
_BUF_EMPTY = (
    "✻ Worked for 40s\n"
    "────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on\n"
)
# User mid-typing — unsent text sitting in the composer.
_BUF_TYPING = (
    "✻ Worked for 40s\n"
    "────────────────────────────────────────\n"
    "❯ half a thought the user is still wri\n"
    "────────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on\n"
)
# Mid-generation — Claude is producing output, no ready composer.
_BUF_BUSY = (
    "● Doing work...\n"
    "  ⎿ running (esc to interrupt)\n"
)


def test_user_is_typing_false_on_empty_composer():
    assert input_mux.user_is_typing(_BUF_EMPTY) is False


def test_user_is_typing_true_when_text_in_composer():
    assert input_mux.user_is_typing(_BUF_TYPING) is True


def test_deliverable_only_when_ready_and_not_typing():
    assert input_mux.deliverable(_BUF_EMPTY) is True
    assert input_mux.deliverable(_BUF_TYPING) is False   # would clobber user text
    assert input_mux.deliverable(_BUF_BUSY) is False      # mid-generation


# ---------------------------------------------------------------------------
# Caption formatting
# ---------------------------------------------------------------------------

def test_format_batch_captions_each_by_author_in_order():
    out = input_mux.format_batch([
        {"author": "S-alice", "text": "first message"},
        {"author": "S-bob", "text": "second message"},
    ])
    assert "S-alice" in out and "S-bob" in out
    assert out.index("S-alice") < out.index("S-bob")
    assert "first message" in out and "second message" in out


# ---------------------------------------------------------------------------
# Queue: never reject, concurrency-safe
# ---------------------------------------------------------------------------

def test_enqueue_never_rejects_concurrent_writes(tmp_path):
    sid = "S-almdudleer-target-p9"
    n = 20

    def writer(i):
        input_mux.enqueue(tmp_path, sid, f"msg-{i}", f"author-{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    msgs = input_mux.read_queue(tmp_path, sid)
    assert len(msgs) == n                      # none rejected, none lost
    assert {m["text"] for m in msgs} == {f"msg-{i}" for i in range(n)}


# ---------------------------------------------------------------------------
# Flush: batched captioned delivery, deferral when composer busy
# ---------------------------------------------------------------------------

def _ready_lookups(buf):
    """Build (pane_lookup, capture, deliver-recorder) seams for flush()."""
    delivered: list[str] = []

    def pane_lookup(sid):
        return "%1"

    def capture(pane_id):
        return buf

    def deliver(pane_id, text):
        delivered.append(text)

    return pane_lookup, capture, deliver, delivered


def test_flush_delivers_batched_captioned_when_ready(tmp_path):
    sid = "S-almdudleer-target-p9"
    input_mux.enqueue(tmp_path, sid, "hello", "S-alice")
    input_mux.enqueue(tmp_path, sid, "world", "S-bob")

    pane_lookup, capture, deliver, delivered = _ready_lookups(_BUF_EMPTY)
    res = input_mux.flush(
        tmp_path, sid,
        pane_lookup=pane_lookup, capture=capture, deliver=deliver,
    )

    assert res["delivered"] == 2
    assert len(delivered) == 1                 # coalesced into ONE batch
    payload = delivered[0]
    assert "S-alice" in payload and "S-bob" in payload
    assert payload.index("S-alice") < payload.index("S-bob")
    assert input_mux.read_queue(tmp_path, sid) == []   # drained


def test_flush_defers_and_preserves_queue_when_user_typing(tmp_path):
    sid = "S-almdudleer-target-p9"
    input_mux.enqueue(tmp_path, sid, "do not clobber my typing", "S-alice")

    pane_lookup, capture, deliver, delivered = _ready_lookups(_BUF_TYPING)
    res = input_mux.flush(
        tmp_path, sid,
        pane_lookup=pane_lookup, capture=capture, deliver=deliver,
    )

    assert res["delivered"] == 0
    assert res["deferred"] is True
    assert delivered == []                              # nothing sent
    # Queue is intact — message preserved for a later flush, never rejected.
    msgs = input_mux.read_queue(tmp_path, sid)
    assert len(msgs) == 1 and msgs[0]["text"] == "do not clobber my typing"


# ---------------------------------------------------------------------------
# DoD: two concurrent injects both land, captioned, in order
# ---------------------------------------------------------------------------

def test_two_concurrent_injects_land_captioned_in_order(tmp_path):
    sid = "S-almdudleer-target-p9"
    delivered: list[str] = []
    lock = threading.Lock()

    def pane_lookup(s):
        return "%1"

    def capture(pane_id):
        return _BUF_EMPTY

    def deliver(pane_id, text):
        with lock:
            delivered.append(text)

    # Two authors race the channel. Ordering is enforced by enqueue arrival:
    # author-1 enqueues, releases a gate, then author-2 enqueues. Each then
    # flushes. Whatever the interleaving, both must land, captioned, in order.
    gate = threading.Event()

    def inject_first():
        input_mux.enqueue(tmp_path, sid, "from one", "S-alice")
        gate.set()
        input_mux.flush(tmp_path, sid, pane_lookup=pane_lookup,
                        capture=capture, deliver=deliver)

    def inject_second():
        gate.wait(timeout=2)
        input_mux.enqueue(tmp_path, sid, "from two", "S-bob")
        input_mux.flush(tmp_path, sid, pane_lookup=pane_lookup,
                        capture=capture, deliver=deliver)

    t1 = threading.Thread(target=inject_first)
    t2 = threading.Thread(target=inject_second)
    t1.start(); t2.start()
    t1.join(); t2.join()

    blob = "\n".join(delivered)
    # Both landed, each captioned by its author, exactly once.
    assert blob.count("from one") == 1
    assert blob.count("from two") == 1
    assert "S-alice" in blob and "S-bob" in blob
    # In order: alice's message precedes bob's (enqueue arrival order).
    assert blob.index("from one") < blob.index("from two")
    # Channel fully drained.
    assert input_mux.read_queue(tmp_path, sid) == []
