"""Tests for input multiplexing queue (T-0469, M1/F1.6).

The multiplexer fans many logical inputs (peer agents, system events, user-
conversation wakes) onto ONE tmux channel per session: queue concurrent writes
(NEVER reject), deliver them BATCHED with author captions, and never clobber
the user's live-typed composer text.
"""
from __future__ import annotations

import json
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


# T-1040: KNOWN GAP, not fixed here (filed as a follow-up -- see the ticket's
# Context). `agent_provider.CodexProvider.composer_markers` is `("›", "❯")` --
# measured live 2026-09-07, current codex-cli (v0.153.4) renders `›`, not `❯`
# -- but none of `user_is_typing`/`deliverable`/`composer_ready` consult that
# per-provider table; all three are hardcoded to `❯` (autocompact.
# composer_ready's own docstring: "rendered by Claude Code's input box").
def _codex_buf(composer_text: str) -> str:
    from bot_squad_worker import agent_provider
    rune = agent_provider.get("codex").composer_markers[0]
    assert rune == "›"  # "›" -- fails loudly if codex's rune ever moves
    return f"  gpt-5.6-sol medium · /tmp/scratch\n\n{rune} {composer_text}\n"


def test_composer_state_helpers_are_blind_to_a_non_claude_composer_rune():
    """Two distinct misreadings, both from the same hardcoded `❯`:

    1. A codex composer holding UNSENT text (the exact shape `user_is_typing`
       exists to detect, so a nudge never clobbers it) reads as empty --
       `user_is_typing` never finds a `❯` line to inspect at all.
    2. `composer_ready` requires a literal `❯` in the buffer, so even a truly
       READY, idle codex composer never satisfies it -- `deliverable()` (the
       queued lane's `flush()` gate) is False for EVERY codex buffer, busy or
       not, which would defer a codex pane's queue forever rather than only
       while it is genuinely busy."""
    busy = _codex_buf("half a thought the codex user is still wri")
    assert input_mux.user_is_typing(busy) is False   # should be True

    idle = _codex_buf("")
    assert input_mux.deliverable(idle) is False       # should be True (ready)


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


def test_codex_composer_rune_defeats_the_swallowed_enter_safety_net(monkeypatch):
    """T-1040: the mirror image of `test_type_lines_gives_up_and_logs_after_
    bound` above -- same never-submitted composer state (this is the T-0913
    live failure: `inject_input` returned `lines_sent=1` for a payload the
    Codex composer accepted and never submitted), except the pane is
    codex-shaped (`_codex_buf`, rune `›`) instead of Claude-shaped
    (`_BUF_PARKED`, rune `❯`).

    The Claude-shaped case above correctly retries up to the bound, logs, and
    reports unconfirmed. This one is read as an EMPTY (cleared) composer on
    the very FIRST poll -- zero retries, no error log, reported delivered --
    because `composer_watch.composer_text` cannot find `❯` in a `›` buffer and
    an unrecognised composer looks identical to an empty one. The T-0957
    safety net exists specifically to catch a silently-unsubmitted payload;
    for a codex pane it cannot see one at all. Pinned rather than fixed here
    -- the composer-state stack is cross-cutting (recycle gate, draft-swap,
    the queued lane's `deliverable` gate) and out of this ticket's scope;
    filed as a follow-up (see T-1040's Context for the ticket id)."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    calls = {"n": 0}

    def capture(pane_id):
        calls["n"] += 1
        return _codex_buf("check mail — this is bot-squad's peer message "
                          "bus, not e-mail; run: bsq inbox check")

    sent = input_mux._type_lines("%1", "check mail", capture=capture)

    # STILL reported delivered, and that is the point of pinning it. T-0913's
    # was_generating fix does NOT close this: it separates "no composer
    # because the pane is mid-turn" from "no composer because it cleared", and
    # a codex pane is neither — its composer is simply unparseable, which is
    # indistinguishable from empty at this layer.
    assert sent.outcome == "cleared"        # ...the net does not even fire
    assert sent.submitted is True           # ...and the caller is told it went
    assert "unconfirmed" not in (sent.outcome or "")


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


def test_multiline_paste_routing_has_no_provider_branch(monkeypatch):
    """T-1040 DoD 1+3. This is the exact 3-line marker-headed payload measured
    LIVE 2026-09-07 against a fresh codex TUI (v0.153.4) and, as the healthy
    control, a fresh Claude Code pane in the same run: each receiver's own
    transcript held exactly ONE record for it, marker-first, all three lines
    inside that one record -- codex did not split or swallow the paste.
    `_type_lines` never inspects the pane's provider or rune before choosing
    the paste-vs-typed path (`if len(lines) > 1`), so codex inherits the
    T-1038 fix automatically and there is no per-provider transport decision
    to make."""
    rec = _Submissions().install(monkeypatch)
    text = ("[T-1040-PROBE] marker line — multi-line bracketed-paste "
            "measurement\nbody line 2 of 3\nbody line 3 of 3, end of payload")

    lines = input_mux._type_lines("%1", text, capture=lambda p: _BUF_EMPTY)

    assert rec.submissions == 1
    assert rec.pastes == 1
    assert rec.pasted == [text]
    assert rec.pasted[0].startswith("[T-1040-PROBE]")
    assert lines == 3


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


# ---------------------------------------------------------------------------
# T-1082 — a worker restart between the drain and the composer
#
# The question the ticket asks is whether our restart window drops peer/TG
# messages the way watchrobot's startup 404 did. For the INGEST the answer is
# no: nothing pushes into bot-squad, and both inbound lanes are durable before
# anything acts on them. The one hop where it was true is here — `_drain`
# truncated the queue and the batch lived only in a local variable until
# `_deliver_to_pane` returned, and `flush` requeues on an EXCEPTION, which a
# SIGKILL is not.
#
# A restart cannot be staged inside a unit test, so these pin the property that
# makes it survivable instead: at no instant is the claimed batch only in RAM,
# and a claim left behind by a process that is gone is put back.
# ---------------------------------------------------------------------------

class _NoSleep:
    """`input_mux.time` with the pauses removed and monotonic left real."""
    sleep = staticmethod(lambda *_a, **_k: None)
    monotonic = staticmethod(time.monotonic)
    time = staticmethod(time.time)


def _queued(tmp_path, sid, *texts):
    for t in texts:
        input_mux.enqueue(tmp_path, sid, t, "tg-answer-owed")


def test_drain_writes_the_claim_before_it_empties_the_queue(tmp_path):
    """The invariant a kill cannot violate: after `_drain`, the batch is on
    disk under the claim even though the queue is empty. This is the ordering,
    not a nicety — the reverse order leaves the same RAM-only window."""
    sid = "S-almdudleer-dev-p1"
    _queued(tmp_path, sid, "его слова из телеграма")

    batch = input_mux._drain(tmp_path, sid)

    assert [m["text"] for m in batch] == ["его слова из телеграма"]
    assert input_mux.read_queue(tmp_path, sid) == []          # queue emptied
    claim = input_mux._read_inflight(tmp_path, sid)           # but not lost
    assert [m["text"] for m in claim] == ["его слова из телеграма"]


def test_flush_pending_recovers_a_batch_orphaned_by_a_restart(tmp_path):
    """The state a SIGKILLed worker leaves behind — an empty queue and a claim
    nobody holds a lock on — comes back as a queued message, not as nothing."""
    sid = "S-almdudleer-dev-p2"
    _queued(tmp_path, sid, "check mail")
    input_mux._drain(tmp_path, sid)          # claimed, then (pretend) killed
    assert input_mux.read_queue(tmp_path, sid) == []

    res = input_mux.flush_pending(tmp_path)

    assert res["recovered"] == 1
    assert [m["text"] for m in input_mux.read_queue(tmp_path, sid)] == ["check mail"]
    assert not input_mux._inflight_path(tmp_path, sid).exists()


def test_recovered_batch_goes_to_the_FRONT_of_anything_queued_since(tmp_path):
    """Order is part of the recovery: the orphan was written first and must be
    read first, otherwise a recovered nudge answers a later message."""
    sid = "S-almdudleer-dev-p3"
    _queued(tmp_path, sid, "first")
    input_mux._drain(tmp_path, sid)
    _queued(tmp_path, sid, "second")

    input_mux.recover_inflight(tmp_path, sid)

    assert [m["text"] for m in input_mux.read_queue(tmp_path, sid)] == ["first", "second"]


def test_a_confirmed_delivery_leaves_no_claim_to_replay(tmp_path):
    """The other direction of the same guard: recovery must not resurrect a
    message that WAS delivered. Without the clear this test sees a duplicate."""
    sid = "S-almdudleer-dev-p4"
    _queued(tmp_path, sid, "delivered once")

    res = input_mux.flush(tmp_path, sid,
                          pane_lookup=lambda s: "%1",
                          capture=lambda p: _BUF_EMPTY,
                          deliver=lambda pane, text: None)

    assert res["delivered"] == 1
    assert not input_mux._inflight_path(tmp_path, sid).exists()
    assert input_mux.recover_inflight(tmp_path, sid) == 0
    assert input_mux.read_queue(tmp_path, sid) == []


def test_recovery_leaves_a_LIVE_delivery_alone(tmp_path):
    """A claim is not evidence of an orphan — a flush running right now has one
    too. The discriminator is the delivery flock, which a dead process cannot
    hold and a live one does. Without this, a slow-but-alive delivery gets its
    batch requeued underneath it and the session is nudged twice."""
    sid = "S-almdudleer-dev-p5"
    _queued(tmp_path, sid, "mid-delivery")
    input_mux._drain(tmp_path, sid)

    entered, release = threading.Event(), threading.Event()

    def _holder():
        with input_mux.delivery_lock(tmp_path, sid):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert entered.wait(5)
    try:
        assert input_mux.recover_inflight(tmp_path, sid) == 0
        assert input_mux._inflight_path(tmp_path, sid).exists()
        assert input_mux.read_queue(tmp_path, sid) == []
    finally:
        release.set()
        t.join(5)

    # ...and once that delivery is gone, the same claim IS recoverable — so the
    # green above is the lock talking, not the recovery being inert.
    assert input_mux.recover_inflight(tmp_path, sid) == 1


def test_a_failed_delivery_requeues_without_leaving_a_duplicate_claim(tmp_path):
    """T-0957's requeue-on-DeliveryNotConfirmed and T-1082's claim must not
    both put the message back — that would deliver it twice."""
    sid = "S-almdudleer-dev-p6"
    _queued(tmp_path, sid, "swallowed Enter")

    def _boom(pane, text):
        raise input_mux.DeliveryNotConfirmed("composer never cleared")

    with pytest.raises(input_mux.DeliveryNotConfirmed):
        input_mux.flush(tmp_path, sid, pane_lookup=lambda s: "%1",
                        capture=lambda p: _BUF_EMPTY, deliver=_boom)

    assert [m["text"] for m in input_mux.read_queue(tmp_path, sid)] == ["swallowed Enter"]
    assert input_mux.flush_pending(tmp_path)["recovered"] == 0


def test_claim_files_are_not_mistaken_for_queues_by_the_sweep(tmp_path):
    """`flush_pending` globs `*.jsonl` and the claim file ends in `.jsonl` too.
    A claim read as a queue would be flushed to a pane that has no such sid."""
    sid = "S-almdudleer-dev-p7"
    _queued(tmp_path, sid, "only one real queue")
    input_mux._drain(tmp_path, sid)

    res = input_mux.flush_pending(tmp_path)

    assert res["recovered"] == 1
    # exactly one queue was considered — the sid's, not its claim. A claim read
    # as a queue would show 2 here and try to deliver to a pane for the
    # "<sid>.inflight" session, which does not exist.
    assert res["queues"] == 1


# ---------------------------------------------------------------------------
# T-0913 — inject_input reported ok/lines_sent=1 for a nudge that was never
# submitted. T-0957 gave the direct lane the confirmation; the RETURN VALUE
# still threw it away, so the report stayed exactly as wrong as before. These
# pin the report, not the retry.
# ---------------------------------------------------------------------------

def test_type_lines_reports_unconfirmed_when_the_composer_never_clears(monkeypatch):
    """The measured T-0904 case: the payload lands in the composer, the Enter
    goes nowhere, no turn starts. The count must still be 1 — it IS one line —
    and the outcome must say it never submitted."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)

    sent = input_mux._type_lines("%1", "check mail",
                                 capture=lambda p: _BUF_PARKED)

    assert sent.lines == 1 and sent == 1     # the number nobody's code may lose
    assert sent.outcome == "unconfirmed"
    assert sent.submitted is False


def test_type_lines_reports_cleared_when_the_composer_empties(monkeypatch):
    """The positive control for the assertion above — same call, same shape,
    only the pane differs. Without it, `submitted is False` could just be what
    this function always says."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)

    sent = input_mux._type_lines("%1", "check mail",
                                 capture=lambda p: _BUF_EMPTY)

    assert sent.lines == 1
    assert sent.outcome == "cleared"
    assert sent.submitted is True


def test_type_lines_reports_dialog_rather_than_calling_it_delivered(monkeypatch):
    """A permission prompt holding the composer is its own outcome — the retry
    stops there (T-0957), and stopping must not read as success."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)

    sent = input_mux._type_lines("%1", "check mail",
                                 capture=lambda p: _BUF_DIALOG)

    assert sent.outcome == "dialog"
    assert sent.submitted is False


def test_the_worst_line_decides_a_multi_submission_payload(monkeypatch):
    """Under the pre-T-1038 kill switch a payload is N submissions. Reporting
    the LAST line's outcome would call a run delivered because its final line
    happened to go through."""
    monkeypatch.setenv("BOT_SQUAD_DIRECT_PASTE", "0")
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "time", _NoSleep())
    outcomes = iter(["unconfirmed", "cleared"])   # line 1 parks, line 2 goes
    monkeypatch.setattr(input_mux, "_submit_confirmed",
                        lambda pane, what, capture, **kw: next(outcomes))

    sent = input_mux._type_lines("%1", "one\ntwo", capture=lambda p: _BUF_EMPTY)

    assert sent.lines == 2
    assert sent.outcome == "unconfirmed"   # not "cleared" from the last line


def test_inject_input_does_not_report_a_parked_nudge_as_delivered(tmp_path, monkeypatch):
    """The ticket's own sentence, at the action: `{ok: true, lines_sent: 1}`
    for a payload the composer never submitted. `ok` still means the transport
    ran; `submitted` is the separate fact that was missing entirely."""
    from bot_squad_worker import actions as A
    from bot_squad_worker import sessions as S

    cfg = type("C", (), {"data_dir": tmp_path})()
    pane = S.PaneInfo(pane_id="%6", window="w", pid="123",
                      cwd=str(tmp_path), command="claude")
    sid = S.compute_sid("u", pane.window, pane.pane_id)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [pane])
    monkeypatch.setattr(
        input_mux, "deliver_direct",
        lambda *a, **k: input_mux.DirectDelivery(1, "unconfirmed"))

    res = A._action_inject_input({"sid": sid, "text": "hello"})

    assert res["lines_sent"] == 1        # unchanged: it WAS one line
    assert res["submitted"] is False     # ...that never became a turn
    assert res["outcome"] == "unconfirmed"


# ---------------------------------------------------------------------------
# T-0913, the deeper half — measured live 2026-09-07 14:11:51Z.
#
# An operator hold was typed into this session's composer while the session was
# MID-TURN. `inject_input` returned 200, the worker logged nothing at all, and
# the session did not read its inbox for 10 minutes 16 seconds.
#
# The mechanism is not the transport. `composer_watch.composer_text` returns
# None on a generating pane — there is no composer box on screen — and the
# confirmation did `(composer_text(buf) or "").strip()`, so "there is nothing
# to look at" evaluated exactly like "the box is empty", i.e. like proof of
# submission. A false positive by construction, firing in precisely the state
# where a nudge is most likely to park.
#
# The fix is a zero captured BEFORE the keystroke, because "the pane is
# generating now" means opposite things depending on what it was doing before.
# ---------------------------------------------------------------------------

_BUF_GENERATING = (
    "✻ Cerebrating… (12s · esc to interrupt)\n"
    "  ⎿  running a long journalctl\n"
)


def test_a_pane_that_was_ALREADY_generating_reports_unknown_not_delivered(monkeypatch):
    """THE 14:11:51Z FAILURE. The pane was busy before the nudge and busy
    after; nothing about that frame says the payload submitted. Before this,
    the same frames returned "cleared"."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)

    sent = input_mux._type_lines("%1", "check mail",
                                 capture=lambda p: _BUF_GENERATING)

    assert sent.outcome == "unknown"
    assert sent.submitted is False


def test_an_idle_pane_that_starts_generating_IS_a_confirmed_submit(monkeypatch):
    """THE POSITIVE CONTROL, and it is what stops the fix over-correcting: a
    successful Enter CAUSES generation. If a generating frame never counted,
    every nudge to an idle pane would report unconfirmed and blast retries —
    the opposite defect, and a worse one."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    frames = iter([_BUF_EMPTY])          # idle BEFORE; generating after

    sent = input_mux._type_lines("%1", "check mail",
                                 capture=lambda p: next(frames, _BUF_GENERATING))

    assert sent.outcome == "cleared"
    assert sent.submitted is True


def test_the_reference_is_taken_before_the_keystrokes_not_after(monkeypatch):
    """The zero must be captured BEFORE Enter. Reading it afterwards is the
    moving-origin defect: by then our own submission has changed the state we
    are using to interpret our own submission."""
    _fast_confirm(monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(input_mux, "raw_keys",
                        lambda pane, *a, **k: order.append("key"))

    def capture(pane_id):
        order.append("look")
        return _BUF_GENERATING

    input_mux._type_lines("%1", "check mail", capture=capture)

    assert order[0] == "look", "the pane was typed into before it was read"


def test_a_probe_that_cannot_read_the_pane_fails_to_generating(monkeypatch):
    """Fails to True on purpose. Claiming the pane was IDLE is what licenses
    reading a later generating frame as proof of submission, so a capture we
    could not take must never produce that claim — it ends at "unknown"."""
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)

    def boom(pane_id):
        raise RuntimeError("tmux went away")

    assert input_mux._pane_is_generating("%1", boom) is True


def test_the_queued_lane_shares_the_one_confirmation(monkeypatch):
    """`_deliver_to_pane` used to carry a SECOND copy of the confirm loop with
    the same defect in it. One implementation means the fix cannot drift back
    apart — so the queued lane refuses to report a busy pane delivered too."""
    _fast_confirm(monkeypatch)
    monkeypatch.setattr(input_mux, "raw_keys", lambda *a, **k: None)
    monkeypatch.setattr(input_mux, "_paste_block", lambda *a, **k: None)

    with pytest.raises(input_mux.DeliveryNotConfirmed) as e:
        input_mux._deliver_to_pane("%1", "his words",
                                   capture=lambda p: _BUF_GENERATING)

    assert "unknown" in str(e.value)


def test_an_unknown_wake_earns_no_quiet_from_T0979(tmp_path, monkeypatch):
    """The two tickets compose, and this is where. T-0979 suppresses a repeat
    nudge only when the previous one was CONFIRMED submitted; an "unknown"
    outcome is not confirmed, so the next peer_send re-nudges instead of
    inheriting a silence nobody earned."""
    from bot_squad_worker import actions as A
    from bot_squad_worker import sessions as S
    from bot_squad_worker import boot_orientation as B

    cfg = type("C", (), {"data_dir": tmp_path})()
    pane = S.PaneInfo(pane_id="%6", window="w", pid="123",
                      cwd=str(tmp_path), command="claude")
    sid = S.compute_sid("u", pane.window, pane.pane_id)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: [pane])
    monkeypatch.setattr("bot_squad_worker.park._slug_for_sid",
                        lambda c, s: "bot-squad")
    monkeypatch.setattr(A, "_provider_for_pane", lambda *a, **k: "claude")
    chat = tmp_path / "bot-squad" / "_chat"
    chat.mkdir(parents=True)
    delivered: list[str] = []
    monkeypatch.setattr(
        input_mux, "deliver_direct",
        lambda *a, **k: (delivered.append(1),
                         input_mux.DirectDelivery(1, "unknown"))[1])

    for msg in ("one", "two"):
        with (chat / f"inbox-{sid}.log").open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
        A._action_inject_input({"sid": sid, "text": B.MAIL_SIGNAL})

    assert len(delivered) == 2


# ---------------------------------------------------------------------------
# T-1062: the gate must ask the renderer whether the "draft" is its own echo
# ---------------------------------------------------------------------------
# Claude Code paints the dim replay of the last message delivered to a pane
# into the composer itself. `user_is_typing` reads that echo as a half-typed
# draft and defers -- and since every delivery repaints the echo, the deferral
# never ages out: the session goes silently and permanently blind. Measured
# live 2026-09-07/08 on four panes (T-1062 Context): the detector answered
# "my own faint text, the box is empty" while this gate still said busy,
# because `deliverable(buf)` could not ask it -- no pane in its signature.
#
# The rule is deliberately ONE-SIDED: we open on POSITIVE evidence only. `None`
# (cannot tell) and `False` defer exactly as before, because typing over his
# half-written message is worse than being late (T-0930, stakeholder).


def _ghost_answering(monkeypatch, verdict, seen=None):
    """Pin the renderer's answer, recording which pane was asked."""
    def _fake(pane_id, capture_ansi=None):
        if seen is not None:
            seen.append(pane_id)
        return verdict
    monkeypatch.setattr(input_mux, "_composer_is_ghost", _fake)


def test_t1062_gate_opens_over_the_renderers_own_faint_echo(monkeypatch):
    # ARM A. The composer shows text, but the renderer says it painted it.
    _ghost_answering(monkeypatch, True)
    assert input_mux.deliverable(_BUF_PARKED, pane_id="%1") is True


def test_t1062_gate_still_defers_over_real_typed_text(monkeypatch):
    # ARM B, the control that makes ARM A mean something: the renderer says
    # this is NOT its own text, so it is his -- and his text is untouchable.
    _ghost_answering(monkeypatch, False)
    assert input_mux.deliverable(_BUF_TYPING, pane_id="%1") is False


def test_t1062_gate_defers_when_the_renderer_cannot_say(monkeypatch):
    # Tri-state, third branch. Absence of evidence is NOT evidence of absence:
    # `None` must behave exactly like the pre-T-1062 rule.
    _ghost_answering(monkeypatch, None)
    assert input_mux.deliverable(_BUF_PARKED, pane_id="%1") is False


def test_t1062_a_ghost_does_not_override_mid_generation(monkeypatch):
    # The typing lock is not the only lock. A pane that is producing output
    # stays busy no matter what the composer holds.
    _ghost_answering(monkeypatch, True)
    assert input_mux.deliverable(_BUF_BUSY, pane_id="%1") is False


def test_t1062_without_a_pane_the_decision_is_byte_for_byte_the_old_one(monkeypatch):
    # Callers with no pane keep the old behaviour, by an explicit path: the
    # detector is never even consulted.
    asked: list[str] = []
    _ghost_answering(monkeypatch, True, seen=asked)
    assert input_mux.deliverable(_BUF_PARKED) is False
    assert input_mux.deliverable(_BUF_EMPTY) is True
    assert asked == []


def test_t1062_flush_hands_the_pane_to_the_gate(tmp_path, monkeypatch):
    # End to end on the queued lane: this is the path that went blind. The
    # pane was in `flush`'s hand all along (`pane_lookup` one line above the
    # gate) and simply never reached the detector.
    sid = "S-almdudleer-target-p785"
    asked: list[str] = []
    _ghost_answering(monkeypatch, True, seen=asked)
    delivered: list[str] = []

    input_mux.enqueue(tmp_path, sid, "the alarm nobody heard", "S-routine-handler")
    res = input_mux.flush(tmp_path, sid,
                          pane_lookup=lambda s: "%785",
                          capture=lambda p: _BUF_PARKED,
                          deliver=lambda p, t: delivered.append(t))

    assert asked == ["%785"]           # the gate asked about the RIGHT pane
    assert res["delivered"] == 1
    assert "the alarm nobody heard" in delivered[0]
    assert input_mux.read_queue(tmp_path, sid) == []


def test_t1062_flush_leaves_a_real_draft_alone(tmp_path, monkeypatch):
    # The same path, control arm: nothing is typed and the queue is intact,
    # so a later flush still has the payload once he submits.
    sid = "S-almdudleer-target-p786"
    _ghost_answering(monkeypatch, False)
    delivered: list[str] = []

    input_mux.enqueue(tmp_path, sid, "must wait", "S-routine-handler")
    res = input_mux.flush(tmp_path, sid,
                          pane_lookup=lambda s: "%786",
                          capture=lambda p: _BUF_TYPING,
                          deliver=lambda p, t: delivered.append(t))

    assert res["deferred"] is True
    assert res["reason"] == "composer_busy"
    assert delivered == []
    assert len(input_mux.read_queue(tmp_path, sid)) == 1


def _direct_lane_stubs(monkeypatch, typed):
    """Neutralise everything past the wait so the test measures the WAIT."""
    monkeypatch.setattr(input_mux, "_draft_swap_enabled", lambda: False)
    monkeypatch.setattr(input_mux, "_type_lines",
                        lambda pane_id, text, capture=None: typed.append(text)
                        or input_mux.DirectDelivery(1, "cleared"))
    monkeypatch.setattr(input_mux, "_DIRECT_GATE_TIMEOUT_SEC", 0.6)
    monkeypatch.setattr(input_mux, "_DIRECT_GATE_POLL_INTERVAL_SEC", 0.05)


def test_t1062_direct_lane_does_not_wait_out_a_ghost(tmp_path, monkeypatch):
    # The direct lane's wait is bounded, so a ghost never blacked it out --
    # but it burned the full timeout on every nudge to a pane that had
    # received mail, and then walked into the draft-swap path that once made
    # a ghost REAL. Positive evidence ends the wait immediately.
    typed: list[str] = []
    _direct_lane_stubs(monkeypatch, typed)
    _ghost_answering(monkeypatch, True)

    started = time.monotonic()
    res = input_mux.deliver_direct(tmp_path, "S-almdudleer-target-p787", "%787",
                                   "wake up", capture=lambda p: _BUF_PARKED)
    elapsed = time.monotonic() - started

    assert typed == ["wake up"]
    assert res.lines == 1
    assert elapsed < input_mux._DIRECT_GATE_TIMEOUT_SEC


def test_t1062_direct_lane_still_waits_out_real_typed_text(tmp_path, monkeypatch):
    # The control. His half-written message must still buy the full bounded
    # wait -- this arm is what makes the arm above mean something.
    typed: list[str] = []
    _direct_lane_stubs(monkeypatch, typed)
    _ghost_answering(monkeypatch, False)

    started = time.monotonic()
    input_mux.deliver_direct(tmp_path, "S-almdudleer-target-p788", "%788",
                             "wake up", capture=lambda p: _BUF_TYPING)
    elapsed = time.monotonic() - started

    assert elapsed >= input_mux._DIRECT_GATE_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# T-1062 DoD 2 + DoD 5: an undeliverable queue stops being silent
# ---------------------------------------------------------------------------


def _queue_aged(tmp_path, sid, age_sec, n=1):
    """Enqueue n messages and backdate them by `age_sec`."""
    for i in range(n):
        input_mux.enqueue(tmp_path, sid, f"held message {i}", "S-routine-handler")
    path = input_mux.queue_path(tmp_path, sid)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        rec["ts"] = rec["ts"] - age_sec
        rows.append(json.dumps(rec))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_t1062_flush_names_which_lock_deferred_it(tmp_path, monkeypatch):
    # `composer_busy` covered two states that need opposite treatment: a turn
    # in progress ends by itself, text in the box does not.
    _ghost_answering(monkeypatch, False)
    input_mux.enqueue(tmp_path, "S-a", "x", "S-b")
    res = input_mux.flush(tmp_path, "S-a", pane_lookup=lambda s: "%1",
                          capture=lambda p: _BUF_TYPING, deliver=lambda p, t: None)
    assert res["blocked_by"] == "draft"

    input_mux.enqueue(tmp_path, "S-c", "x", "S-b")
    res = input_mux.flush(tmp_path, "S-c", pane_lookup=lambda s: "%1",
                          capture=lambda p: _BUF_BUSY, deliver=lambda p, t: None)
    assert res["blocked_by"] == "generation"


def test_t1062_a_queue_held_past_the_threshold_is_reported(tmp_path, monkeypatch):
    # The alarm this ticket exists for: the queue grew, nothing cleared it, and
    # until now the only way to find out was to read the file by hand.
    _ghost_answering(monkeypatch, False)
    monkeypatch.setattr(input_mux, "_capture_pane", lambda p: _BUF_TYPING)
    monkeypatch.setattr(input_mux, "_default_pane_lookup", lambda s: "%1")
    _queue_aged(tmp_path, "S-deaf-p1", age_sec=1200, n=3)

    res = input_mux.flush_pending(tmp_path)

    assert len(res["stalled"]) == 1
    stall = res["stalled"][0]
    assert stall["sid"] == "S-deaf-p1"
    assert stall["records"] == 3
    assert stall["oldest_age_sec"] >= 1200
    assert stall["blocked_by"] == "draft"


def test_t1062_a_young_queue_and_a_generating_pane_stay_quiet(tmp_path, monkeypatch):
    # The two ways to make this alarm useless are to miss the stall and to cry
    # during ordinary work. A fresh deferral is not a stall...
    _ghost_answering(monkeypatch, False)
    monkeypatch.setattr(input_mux, "_capture_pane", lambda p: _BUF_TYPING)
    monkeypatch.setattr(input_mux, "_default_pane_lookup", lambda s: "%1")
    _queue_aged(tmp_path, "S-fresh-p2", age_sec=30)
    assert input_mux.flush_pending(tmp_path)["stalled"] == []

    # ...and a long turn is work, however long the queue has waited behind it.
    monkeypatch.setattr(input_mux, "_capture_pane", lambda p: _BUF_BUSY)
    _queue_aged(tmp_path, "S-working-p3", age_sec=99999)
    assert input_mux.flush_pending(tmp_path)["stalled"] == []


def test_t1062_the_stall_alert_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_INPUT_STALL_ALERT_SEC", "0")
    _ghost_answering(monkeypatch, False)
    monkeypatch.setattr(input_mux, "_capture_pane", lambda p: _BUF_TYPING)
    monkeypatch.setattr(input_mux, "_default_pane_lookup", lambda s: "%1")
    _queue_aged(tmp_path, "S-deaf-p4", age_sec=99999)
    assert input_mux.flush_pending(tmp_path)["stalled"] == []
