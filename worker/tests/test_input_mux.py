"""Tests for input multiplexing queue (T-0469, M1/F1.6).

The multiplexer fans many logical inputs (peer agents, system events, user-
conversation wakes) onto ONE tmux channel per session: queue concurrent writes
(NEVER reject), deliver them BATCHED with author captions, and never clobber
the user's live-typed composer text.
"""
from __future__ import annotations

import logging
import threading
import time

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
# T-0957 DoD2: a payload that landed but whose Enter was swallowed — the
# composer still shows it, unsubmitted.
_BUF_PARKED = (
    "✻ Worked for 40s\n"
    "────────────────────────────────────────\n"
    "❯ check mail\n"
    "────────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on\n"
)
# A permission/choice dialog sharing the ❯ rune — not this composer at all.
_BUF_DIALOG = (
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. No\n"
    " Esc to cancel\n"
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


# ---------------------------------------------------------------------------
# DoD (T-0578): direct lane vs queued lane never splice keystrokes together
# ---------------------------------------------------------------------------

def test_direct_lane_and_queued_lane_serialize_on_delivery_lock(tmp_path, monkeypatch):
    """A peer 'check mail' nudge (queued lane, ``flush()``) racing an
    operator ``send_input`` (direct lane, ``deliver_direct()``) must never
    interleave keystrokes into one composer line. Both lanes hold the same
    per-sid ``delivery_lock`` — exercise the real lock (not a stub) and
    record wall-clock spans for each lane's critical section to prove they
    never overlap, however the thread scheduler interleaves them.
    """
    sid = "S-almdudleer-target-p9"
    pane_id = "%1"
    spans: list[tuple[str, float, float]] = []
    spans_lock = threading.Lock()

    def fake_raw_keys(pid, *keys):
        # Direct-lane keystrokes: slow them down to widen the race window a
        # real interleave bug would exploit.
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        with spans_lock:
            spans.append(("direct", start, end))

    monkeypatch.setattr(input_mux, "raw_keys", fake_raw_keys)

    def queued_deliver(pid, text):
        start = time.monotonic()
        time.sleep(0.15)
        end = time.monotonic()
        with spans_lock:
            spans.append(("queued", start, end))

    input_mux.enqueue(tmp_path, sid, "check mail", "S-alice")

    def run_queued():
        input_mux.flush(tmp_path, sid, pane_lookup=lambda s: pane_id,
                        capture=lambda p: _BUF_EMPTY, deliver=queued_deliver)

    def run_direct():
        input_mux.deliver_direct(tmp_path, sid, pane_id, "operator send-input",
                                 capture=lambda p: _BUF_EMPTY)

    t_queued = threading.Thread(target=run_queued)
    t_direct = threading.Thread(target=run_direct)
    t_queued.start()
    time.sleep(0.02)   # let the queued lane grab the lock first
    t_direct.start()
    t_queued.join()
    t_direct.join()

    queued_spans = [s for s in spans if s[0] == "queued"]
    direct_spans = [s for s in spans if s[0] == "direct"]
    assert queued_spans and direct_spans   # both lanes actually ran

    def overlaps(a, b):
        return a[1] < b[2] and b[1] < a[2]

    for q in queued_spans:
        for d in direct_spans:
            assert not overlaps(q, d), (
                "direct-lane keystroke landed inside the queued-lane delivery "
                "window — the two lanes spliced into one composer line"
            )


# ---------------------------------------------------------------------------
# T-0957 DoD 2: a swallowed Enter must not be counted as delivered
# ---------------------------------------------------------------------------

def _fast_confirm(monkeypatch):
    """Shrink the confirm-retry knobs so these tests run in milliseconds."""
    monkeypatch.setattr(input_mux, "_DELIVER_CONFIRM_TIMEOUT_SEC", 0.02)
    monkeypatch.setattr(input_mux, "_DELIVER_CONFIRM_POLL_INTERVAL_SEC", 0.005)
    monkeypatch.setattr(input_mux.time, "sleep", lambda s: None)


def test_deliver_to_pane_raises_when_enter_is_swallowed(monkeypatch):
    """The queued lane's payload paste+Enter can have its Enter swallowed
    (T-0201's failure mode). Before this fix `_deliver_to_pane` returned
    silently either way, so `flush()` reported the batch delivered while it
    was still sitting unsubmitted in the composer. It must now raise so
    `flush()`'s existing except-clause requeues instead."""
    import bot_squad_worker.sessions as S
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "_capture_pane", lambda pane_id: _BUF_PARKED)

    with pytest.raises(input_mux.DeliveryNotConfirmed):
        input_mux._deliver_to_pane("%1", "check mail")


def test_deliver_to_pane_confirms_after_a_retry(monkeypatch):
    """Positive control: a delivery that clears on a LATER Enter (the first
    one or two swallowed) must not raise — retrying is success, not failure."""
    import bot_squad_worker.sessions as S
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    calls = {"n": 0}

    def capture(pane_id):
        calls["n"] += 1
        return _BUF_PARKED if calls["n"] < 3 else _BUF_EMPTY

    monkeypatch.setattr(input_mux, "_capture_pane", capture)

    input_mux._deliver_to_pane("%1", "check mail")   # must not raise


def test_flush_requeues_rather_than_reports_delivered_on_swallowed_enter(tmp_path, monkeypatch):
    """Integration: flush() over the REAL _deliver_to_pane (not a fake
    `deliver`) must leave the message in the queue — never drained — when
    the composer never confirms, and must not report `delivered`."""
    import bot_squad_worker.sessions as S
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "_capture_pane", lambda pane_id: _BUF_PARKED)

    sid = "S-almdudleer-target-p9"
    input_mux.enqueue(tmp_path, sid, "check mail", "S-alice")

    with pytest.raises(input_mux.DeliveryNotConfirmed):
        input_mux.flush(tmp_path, sid, pane_lookup=lambda s: "%1",
                        capture=lambda p: _BUF_EMPTY)

    # Requeued, not lost — a later tick gets another attempt.
    msgs = input_mux.read_queue(tmp_path, sid)
    assert len(msgs) == 1 and msgs[0]["text"] == "check mail"


def test_flush_pending_one_stuck_queue_does_not_block_the_rest(tmp_path, monkeypatch):
    """T-0957 DoD2 + flush_pending's own docstring contract: one sid's
    unconfirmed delivery must not stop the sweep from reaching the next sid,
    or the fleet-wide 'one bad queue never kills the sweep' claim is false
    for exactly the failure this ticket is about."""
    import bot_squad_worker.sessions as S
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(S, "_run", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "_capture_pane",
                        lambda pane_id: _BUF_PARKED if pane_id == "%stuck" else _BUF_EMPTY)
    monkeypatch.setattr(input_mux, "_default_pane_lookup",
                        lambda sid: "%stuck" if sid == "S-stuck" else "%ok")

    input_mux.enqueue(tmp_path, "S-stuck", "check mail", "S-alice")
    input_mux.enqueue(tmp_path, "S-ok", "check mail", "S-bob")

    res = input_mux.flush_pending(tmp_path)

    assert res["queues"] == 2
    assert res["delivered"] == 1                       # S-ok got through
    stuck_msgs = input_mux.read_queue(tmp_path, "S-stuck")
    assert len(stuck_msgs) == 1 and stuck_msgs[0]["text"] == "check mail"
    assert stuck_msgs[0]["author"] == "S-alice"          # requeued, not lost
    assert input_mux.read_queue(tmp_path, "S-ok") == []  # drained


# ---------------------------------------------------------------------------
# T-0957 DoD 2 (direct lane): _type_lines confirms, retries, and stops for a
# dialog — a nudge landing and stopping there must not be silently assumed
# ---------------------------------------------------------------------------

def test_type_lines_retries_swallowed_enter_then_confirms(monkeypatch):
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    calls = {"n": 0}

    def capture(pane_id):
        calls["n"] += 1
        return _BUF_PARKED if calls["n"] < 3 else _BUF_EMPTY

    sent = input_mux._type_lines("%1", "check mail", capture=capture)
    assert sent == 1


def test_type_lines_gives_up_and_logs_after_bound(monkeypatch, caplog):
    """The direct lane has no queue to fall back on — a payload it can never
    confirm must still be LOGGED, not silently assumed delivered."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)

    with caplog.at_level(logging.ERROR, logger="bot_squad_worker.input_mux"):
        sent = input_mux._type_lines("%1", "check mail",
                                     capture=lambda p: _BUF_PARKED)
    assert sent == 1   # still counted as "sent" — the direct lane's contract
    assert "never confirmed submitted" in caplog.text


def test_type_lines_stops_retrying_into_a_permission_dialog(monkeypatch, caplog):
    """A permission dialog sharing the ❯ rune is not this session's to
    answer — the retry must stop and say so, not blast Enter at a prompt
    belonging to a human."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger="bot_squad_worker.input_mux"):
        sent = input_mux._type_lines("%1", "check mail",
                                     capture=lambda p: _BUF_DIALOG)
    assert sent == 1
    assert "permission dialog" in caplog.text


# ---------------------------------------------------------------------------
# T-1038: an injected payload with a NEWLINE must arrive as ONE turn
#
# The instrument below counts COMPOSER SUBMISSIONS, not text. That distinction
# is the whole ticket: `test_compact_handoff.py`'s marker tests assert
# `startswith(MARKER)` on the composer's RETURN VALUE and were green while the
# marker was arriving detached from its own body, because the split happens in
# the transport, two layers below where they look.
# ---------------------------------------------------------------------------

class _Submissions:
    """Counts what the pane actually receives.

    ``submissions`` — Enter keystrokes, i.e. composer messages. The capture
    always reports an EMPTY composer, so every Enter confirms on its first
    attempt and no retry inflates the count (`_fast_confirm` keeps that fast).
    ``pastes`` — ``tmux paste-buffer`` calls (the block transport).
    ``typed`` — literal ``send-keys`` payloads (the per-line transport).
    """

    def __init__(self):
        self.submissions = 0
        self.pastes = 0
        self.typed: list[str] = []
        self.pasted: list[str] = []

    def install(self, monkeypatch):
        import bot_squad_worker.sessions as S
        _fast_confirm(monkeypatch)
        monkeypatch.setattr(input_mux, "raw_keys", self._raw_keys)
        monkeypatch.setattr(S, "_run", self._run)
        monkeypatch.setattr(input_mux, "_capture_pane", lambda pane_id: _BUF_EMPTY)
        return self

    def _raw_keys(self, pane_id, *keys):
        if keys and keys[0] == "--":
            self.typed.append(keys[1] if len(keys) > 1 else "")
        elif "Enter" in keys:
            self.submissions += 1

    def _run(self, argv, **kw):
        if len(argv) > 1 and argv[1] == "paste-buffer":
            self.pastes += 1
        if len(argv) > 1 and argv[1] == "load-buffer":
            self.pasted.append(kw.get("input", ""))
        return None


def _real_context_handoff() -> str:
    """The REAL composed text, not a stand-in — a hand-typed literal would pin
    my model of the prompt instead of the prompt (T-1038 DoD 2)."""
    from bot_squad_worker import autocompact
    return autocompact.context_handoff_prompt("T-1038", relaunch=True)


def _real_artifact_handoff() -> str:
    from bot_squad_worker import autocompact
    return autocompact.handoff_prompt("/art/operator-state.md", "operator")


# --- the HEALTHY case first, so a green below is not the instrument saying
#     "1" to everything (T-1038 DoD 4) ------------------------------------

def test_type_lines_single_line_nudge_is_one_submission_with_its_marker(monkeypatch):
    """The idle_timeout shape: marker joined to the body with a SPACE. It was
    correct before this ticket and must stay on the unchanged send-keys path —
    no paste, and the marker travelling in the SAME keystroke as the body."""
    from bot_squad_worker.input_mux import HARNESS_NUDGE_MARKER
    rec = _Submissions().install(monkeypatch)
    text = (f"{HARNESS_NUDGE_MARKER} CONTEXT FULL: your session is at the "
            "context ceiling; write your forward-state now.")
    assert "\n" not in text

    assert input_mux._type_lines("%1", text, capture=lambda p: _BUF_EMPTY) == 1

    assert rec.submissions == 1
    assert rec.pastes == 0                       # unchanged transport
    assert rec.typed == [text]                   # marker inline with the body


def test_type_lines_delivers_the_real_context_handoff_as_one_submission(monkeypatch):
    """T-1038 DoD 1+2. Measured before the fix on this exact text: 21 lines ->
    21 submissions, the first being the bare marker (which is the turn the
    operator actually received). Now: one paste, one Enter, one turn."""
    from bot_squad_worker.input_mux import HARNESS_NUDGE_MARKER
    rec = _Submissions().install(monkeypatch)
    text = _real_context_handoff()
    assert len(text.split("\n")) > 10, "the prompt must still be multi-line"

    lines = input_mux._type_lines("%1", text, capture=lambda p: _BUF_EMPTY)

    assert rec.submissions == 1                  # ONE turn, not one per line
    assert rec.pastes == 1
    assert rec.typed == []                       # nothing typed line-by-line
    assert rec.pasted == [text]                  # verbatim, marker at the head
    assert rec.pasted[0].startswith(HARNESS_NUDGE_MARKER)
    assert lines == len(text.split("\n"))        # lines_sent still counts LINES


def test_type_lines_delivers_the_real_artifact_handoff_as_one_submission(monkeypatch):
    """The twin composer (`handoff_prompt`, the task-LESS session's finalize):
    18 submissions before the fix. Fixing one and leaving the other is this
    repo's standing failure mode."""
    rec = _Submissions().install(monkeypatch)
    text = _real_artifact_handoff()
    assert len(text.split("\n")) > 10

    input_mux._type_lines("%1", text, capture=lambda p: _BUF_EMPTY)

    assert rec.submissions == 1
    assert rec.pastes == 1


def test_type_lines_splits_per_line_when_the_paste_is_switched_off(monkeypatch):
    """The guard's teeth, and the instrument's negative control in one: with
    `BOT_SQUAD_DIRECT_PASTE=0` (the pre-T-1038 transport, kept as the rollback
    lever) the SAME instrument reports one submission PER LINE and the first
    one is the marker ALONE — the detached-marker turn this ticket is about.
    Anything that puts the per-line loop back for multi-line payloads fails
    the tests above and this one turns green."""
    from bot_squad_worker.input_mux import HARNESS_NUDGE_MARKER
    monkeypatch.setenv("BOT_SQUAD_DIRECT_PASTE", "0")
    rec = _Submissions().install(monkeypatch)
    text = _real_context_handoff()

    input_mux._type_lines("%1", text, capture=lambda p: _BUF_EMPTY)

    assert rec.submissions == len(text.split("\n")) > 10
    assert rec.pastes == 0
    assert rec.typed[0] == HARNESS_NUDGE_MARKER   # the marker, alone, as a turn
    assert rec.typed[1] == ""                     # and an empty submission after it


def test_deliver_direct_delivers_the_real_handoff_as_one_submission(tmp_path, monkeypatch):
    """The same claim one layer up, through the lock + typing gate — the level
    `autocompact._inject_context_handoff` -> `inject_input` actually calls."""
    rec = _Submissions().install(monkeypatch)
    text = _real_context_handoff()

    lines = input_mux.deliver_direct(tmp_path, "S-almdudleer-dev-p9", "%1", text,
                                     capture=lambda p: _BUF_EMPTY)

    assert rec.submissions == 1
    assert rec.pasted == [text]
    assert lines == len(text.split("\n"))


def test_queued_lane_delivers_a_multi_line_batch_as_one_submission(monkeypatch):
    """T-1038 DoD 3, open question 1: the queued lane does NOT share the
    defect. Measured on the same text: 1 submission, 1 paste — it has pasted
    since T-0469 and the direct lane has now joined it."""
    rec = _Submissions().install(monkeypatch)
    text = _real_context_handoff()

    input_mux._deliver_to_pane("%1", text)

    assert rec.submissions == 1
    assert rec.pastes == 1
