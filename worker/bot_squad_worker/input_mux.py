"""Input multiplexing queue (T-0469, M1/F1.6).

Many logical inputs — peer agents addressing a session, system events, user-
conversation wakes — converge on ONE tmux channel per Claude session. Raw,
concurrent ``send-keys`` race each other and, worse, clobber whatever the
*user* is live-typing into the composer. This module is the multiplexer the
stakeholder asked for (voice-10 + SOURCE-VERBATIM Part A):

* :func:`enqueue` appends a write to a per-sid queue under a short flock. It
  NEVER rejects a concurrent write — that is the stakeholder's hard rule.
* :func:`flush` drains the queue and delivers it as ONE batched payload with an
  author caption per message — but ONLY when the pane's composer is *ready*
  (the ``❯`` rune, no mid-generation marker) AND the user is not mid-typing.
  When the composer is busy the queued writes are LEFT in place (deferred), so
  live user-typed text is never clobbered. A deferred queue is picked up by a
  later flush (the next write, or :func:`flush_pending` on a scheduler tick).

Coalescing falls out of the lock discipline: every caller enqueues (short
lock) then serialises on the per-sid *delivery* lock; whichever caller wins it
drains everything queued so far and ships it in a single captioned batch, so
concurrent writers naturally merge into one delivery.

Delivery uses a bracketed paste + a single Enter (not one Enter per line like
the raw ``inject_input`` primitive): a batched payload is multi-line and must
land as ONE composer message, not N separate submissions.

T-0578 (F1.6 sweep): this module is additionally the SINGLE CHOKE POINT for
tmux keystroke emission system-wide. ``tmux send-keys`` may appear nowhere
else in the worker/CLI:

* :func:`raw_keys` is the one send-keys emitter. Callers outside this module
  use it only under :func:`delivery_lock` (or during pre-mux bootstrap, e.g.
  install.sh before any worker exists).
* :func:`deliver_direct` is the verbatim direct lane — the transport that
  used to live inline in the ``inject_input`` action (byte-identical content,
  no caption/batch — a solo "check mail" stays "check mail" and "/compact"
  stays a bare slash command). One send-keys + Enter per line for a
  single-line payload; a payload WITH a newline goes as one bracketed paste
  and one Enter, i.e. ONE composer message, since T-1038 — before that it
  split into N submissions and detached T-1032's marker from its own body.
  It holds the per-sid delivery lock so it can never interleave keystrokes
  with a queued-lane flush, and briefly gates on live user typing (bounded wait, then
  delivers anyway — the direct lane is synchronous and guaranteed, never
  queued/dropped).
* Teardown/control keys (``C-c``, ``/exit``) go through
  :func:`delivery_lock` + :func:`raw_keys` in sessions.py for the same
  serialization.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from bot_squad_worker.autocompact import composer_ready

# sids are filename-clean (``S-<user>-<slug>-pNNN`` → letters/digits/.-_), so
# the sanitised stem round-trips to the sid. The substitution is purely
# defensive against an unexpected character.
log = logging.getLogger(__name__)

_SID_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

_PROMPT_RUNE = "❯"

# T-1032 (the stakeholder's own T-0976 ask, verbatim: harness system messages
# "should be prefixed as such, to avoid confusion with user input"). A nudge
# typed into an IDLE pane through this module's send-keys transport lands in
# the session's transcript indistinguishable from real human typing —
# type=user, origin={kind:human}, promptSource=typed — because that is
# literally how a human's own keystrokes would show up too; Claude Code's
# transcript format only marks injected text specially when it arrives while
# the pane is BUSY (a queued, wrapped record), never for idle delivery. So a
# caller AUTHORING a system nudge (not relaying verbatim human/stakeholder
# text) must start it with this marker: never something a real human would
# plausibly type, placed FIRST so it survives whitespace normalization and
# any downstream truncation. close_hook.py's harvest (and the CLI's guidance-
# search attribution) exclude comments starting with it.
HARNESS_NUDGE_MARKER = "[BOT-SQUAD SYSTEM MESSAGE — not stakeholder input]"


def _safe(sid: str) -> str:
    return _SID_SAFE.sub("_", sid)


def queue_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / "_input_queue"


def queue_path(data_dir: Path | str, sid: str) -> Path:
    return queue_dir(data_dir) / f"{_safe(sid)}.jsonl"


def _append_lock_path(data_dir: Path | str, sid: str) -> Path:
    return queue_dir(data_dir) / f"{_safe(sid)}.append.lock"


def _delivery_lock_path(data_dir: Path | str, sid: str) -> Path:
    return queue_dir(data_dir) / f"{_safe(sid)}.delivery.lock"


def _inflight_path(data_dir: Path | str, sid: str) -> Path:
    """Where a CLAIMED-but-not-yet-delivered batch lives (T-1082).

    :func:`_drain` used to be the only step between "durable in the queue" and
    "typed into the composer": it truncated the queue file, and from that
    instant until :func:`_deliver_to_pane` returned, the batch existed ONLY in
    a local variable. ``flush`` requeues on an *exception*, but a process death
    is not an exception — a worker restart inside that window destroyed the
    payload with nothing on disk left to say it ever existed, and no sweep
    could find it because there was nothing to find. That is the shape the
    T-1082 question is about, and this file is what makes it recoverable.
    """
    return queue_dir(data_dir) / f"{_safe(sid)}.inflight.jsonl"


@contextlib.contextmanager
def delivery_lock(data_dir: Path | str, sid: str) -> Iterator[None]:
    """Hold ``sid``'s exclusive delivery lock — the anti-interleave gate.

    EVERY keystroke writer to a session's pane (queued-lane flush, direct-lane
    deliver, teardown control keys, spawn/resume prompt paste) serialises on
    this flock, so two concurrent writers can never interleave keystrokes into
    one composer line. Not reentrant — never nest for the same sid.
    """
    queue_dir(data_dir).mkdir(parents=True, exist_ok=True)
    lock_fd = open(_delivery_lock_path(data_dir, sid), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        lock_fd.close()


# ---------------------------------------------------------------------------
# Composer-state detection — never mix with live user-typed text
# ---------------------------------------------------------------------------

def user_is_typing(buf: str) -> bool:
    """True when the live composer (last ``❯`` line) holds user-typed text.

    Claude Code renders the input box as a ``❯`` rune followed by the user's
    in-progress text. An empty composer is ``❯`` + whitespace. Delivering into
    a non-empty composer would either submit our text mixed with theirs or wipe
    what they typed — both forbidden — so we treat a non-empty composer as
    "busy" and defer. We inspect the LAST ``❯`` line (the live composer is at
    the bottom; earlier runes are scrollback).
    """
    if not buf:
        return False
    live = None
    for line in buf.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_PROMPT_RUNE):
            live = stripped[len(_PROMPT_RUNE):]
    if live is None:
        return False
    return bool(live.strip())


def _typing_blocks(buf: str, pane_id: str | None = None) -> bool:
    """True when the composer holds text we must not type over.

    :func:`user_is_typing` judges the composer by its TEXT — and Claude Code
    paints its OWN faint suggestion (the dim replay of the last message
    delivered to this pane) into that very place, T-0962. Read as a draft, that
    echo holds the delivery gate shut for as long as it is on screen, and
    because every delivery repaints it, it never ages out: the blackout feeds
    itself. T-1062 measured that live on four panes on 2026-09-07/08 — the
    detector said "my own suggestion, the box is empty" while this gate still
    said busy, because nobody asked it.

    ``pane_id`` is what lets us ask instead of guess, and the answer is
    tri-state. We act on ONE branch of it: a POSITIVE "this is the renderer's
    own faint text" clears the gate; ``None`` (cannot tell) and ``False`` defer
    exactly as before. Opening on the ABSENCE of evidence would type over his
    half-written message, and losing that is worse than being late — «только не
    надо ее компактить, когда у меня текст во вводе» (stakeholder, T-0930).
    """
    if not user_is_typing(buf):
        return False
    if pane_id and _composer_is_ghost(pane_id) is True:
        log.info("input_mux: %s's composer holds only Claude Code's own faint "
                 "suggestion — the box is empty, delivering (T-1062)", pane_id)
        return False
    return True


def deliverable(buf: str, *, pane_id: str | None = None) -> bool:
    """True when it is safe to inject: composer ready AND nothing to type over.

    Reuses :func:`autocompact.composer_ready` (the composer rune, no
    mid-generation marker) and adds the live-typing guard. Pass ``pane_id`` —
    every production caller has one in hand — so the guard can tell his draft
    from the renderer's own faint echo (:func:`_typing_blocks`). Without it the
    decision is byte-for-byte the pre-T-1062 rule.
    """
    return composer_ready(buf) and not _typing_blocks(buf, pane_id)


# ---------------------------------------------------------------------------
# Caption formatting
# ---------------------------------------------------------------------------

def format_batch(msgs: list[dict[str, Any]]) -> str:
    """Render queued messages as one payload, each captioned by its author.

    A single message gets a one-line caption; multiple messages get a batch
    header plus a per-message ``— from <author>:`` caption, preserving order.
    """
    if not msgs:
        return ""
    if len(msgs) == 1:
        m = msgs[0]
        return f"[input from {m['author']}]\n{m['text']}"
    parts = [f"[{len(msgs)} batched inputs — multiplexed by bsq]"]
    for m in msgs:
        parts.append(f"— from {m['author']}:\n{m['text']}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------

def enqueue(data_dir: Path | str, sid: str, text: str,
            author: str, *, now: float | None = None) -> int:
    """Append one write to the per-sid queue. Never rejects; returns depth.

    Concurrency-safe: a short flock serialises the append so two near-
    simultaneous writers can never interleave a partial line. The write is
    durable (queued) the moment this returns, independent of delivery.
    """
    qdir = queue_dir(data_dir)
    qdir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time() if now is None else now,
        "author": author,
        "text": text,
    }
    line = json.dumps(rec, ensure_ascii=False)
    lock_fd = open(_append_lock_path(data_dir, sid), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        qp = queue_path(data_dir, sid)
        with open(qp, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return _count_lines(qp)
    finally:
        lock_fd.close()


def _count_lines(path: Path) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for ln in fh if ln.strip())
    except FileNotFoundError:
        return 0


def read_queue(data_dir: Path | str, sid: str) -> list[dict[str, Any]]:
    """Read (without draining) the currently-queued messages, in order."""
    qp = queue_path(data_dir, sid)
    out: list[dict[str, Any]] = []
    try:
        with open(qp, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    out.append(json.loads(ln))
    except FileNotFoundError:
        return []
    return out


def _drain(data_dir: Path | str, sid: str) -> list[dict[str, Any]]:
    """Atomically read all queued messages and truncate the queue.

    Held under the append-lock so a concurrent :func:`enqueue` either lands
    fully before the read or fully after the truncate — never a half line, and
    never a message silently dropped (an append after the truncate stays
    queued for the next flush).

    T-1082: the batch is written to :func:`_inflight_path` BEFORE the queue is
    truncated, and the order is the whole point — there is no instant at which
    the only copy is the return value of this function. A worker restart
    between here and delivery therefore leaves a claim on disk that
    :func:`recover_inflight` puts back, instead of a message that never existed.
    """
    lock_fd = open(_append_lock_path(data_dir, sid), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        msgs = read_queue(data_dir, sid)
        if msgs:
            _write_inflight(data_dir, sid, msgs)
            queue_path(data_dir, sid).write_text("", encoding="utf-8")
        return msgs
    finally:
        lock_fd.close()


def _write_inflight(data_dir: Path | str, sid: str,
                    msgs: list[dict[str, Any]]) -> None:
    """Persist a claimed batch, atomically, before the queue is emptied."""
    path = _inflight_path(data_dir, sid)
    body = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def _read_inflight(data_dir: Path | str, sid: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(_inflight_path(data_dir, sid), encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    out.append(json.loads(ln))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        log.exception("input_mux: %s's in-flight claim is unreadable", sid)
        return []
    return out


def _clear_inflight(data_dir: Path | str, sid: str) -> None:
    """Drop the claim — the batch is accounted for (delivered or requeued)."""
    try:
        _inflight_path(data_dir, sid).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("input_mux: could not clear %s's in-flight claim", sid,
                    exc_info=True)


def _requeue_front(data_dir: Path | str, sid: str,
                   msgs: list[dict[str, Any]]) -> None:
    """Put a drained-but-undelivered batch back at the FRONT of the queue.

    Used only on a delivery failure so a transient tmux error never loses a
    message (it is retried on the next flush, ahead of anything appended since).
    """
    lock_fd = open(_append_lock_path(data_dir, sid), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tail = read_queue(data_dir, sid)
        lines = [json.dumps(m, ensure_ascii=False) for m in (msgs + tail)]
        body = ("\n".join(lines) + "\n") if lines else ""
        queue_path(data_dir, sid).write_text(body, encoding="utf-8")
    finally:
        lock_fd.close()


# ---------------------------------------------------------------------------
# tmux delivery seam (monkeypatched in tests)
# ---------------------------------------------------------------------------

def _capture_pane(pane_id: str) -> str:
    from bot_squad_worker.autocompact import _capture_pane as _cap
    return _cap(pane_id)


#: T-0962. Claude Code renders everything in the composer that is NOT his own
#: text — the empty-box hint, ``<no suggestion>``, and the dim replay of the
#: last message he submitted — inside an SGR 2 (FAINT) span. Measured on a
#: fresh session 2026-09-04 with `tmux capture-pane -p -e`:
#:
#:     ghost:      ESC[39m ❯ NBSP ESC[2m check mail ESC[0m
#:     his typing: ESC[39m ❯ NBSP привет это настоящий текст
#:
#: and typing one character replaces the whole ghost, so the two never mix.
#: This attribute is the renderer's own answer to "is this his?", which is why
#: it is used here instead of growing `_PLACEHOLDER_RE` — that whitelist can
#: never be complete, because the ghost is an arbitrary earlier MESSAGE.
_FAINT_ON = "\x1b[2m"
_FAINT_OFF = ("\x1b[0m", "\x1b[22m", "\x1b[m")
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _capture_pane_ansi(pane_id: str) -> str:
    """The pane WITH its escape sequences (`capture-pane -e`), so the faint
    attribute survives. A separate seam from :func:`_capture_pane` on purpose:
    every existing consumer of the plain capture keeps reading exactly what it
    read before."""
    from bot_squad_worker.sessions import _run
    out = _run(["tmux", "capture-pane", "-p", "-e", "-t", pane_id])
    return str(getattr(out, "stdout", out) or "")


def _unfainted(live: str) -> tuple[str, bool]:
    """Split a composer line into (what is NOT faint, whether any faint ran)."""
    parts = _ANSI_SGR_RE.split(live)
    codes = _ANSI_SGR_RE.findall(live)
    visible: list[str] = []
    faint = saw_faint = False
    for i, chunk in enumerate(parts):
        if not faint:
            visible.append(chunk)
        elif chunk:
            saw_faint = True
        if i < len(codes):
            code = codes[i]
            if code == _FAINT_ON:
                faint = True
            elif code in _FAINT_OFF:
                faint = False
    return "".join(visible), saw_faint


def _composer_is_ghost(pane_id: str,
                       capture_ansi: Callable[[str], str] | None = None
                       ) -> bool | None:
    """True when the composer holds ONLY faint text, i.e. the box is empty.

    Returns None when the question cannot be answered (no capture, no rune, no
    faint span at all) so the caller keeps its previous behaviour. Deliberately
    one-directional: it can only ever downgrade "there is a draft" to "the box
    is empty", and only on positive evidence that a faint run covered the whole
    line — never the other way round, because inventing a draft is harmless and
    erasing one is not.
    """
    capture_ansi = capture_ansi or _capture_pane_ansi
    try:
        buf = capture_ansi(pane_id)
    except Exception:  # noqa: BLE001 — a capture hiccup must not drop delivery
        return None
    if not buf:
        return None
    live: str | None = None
    for line in buf.splitlines():
        if _PROMPT_RUNE in line:
            live = line.split(_PROMPT_RUNE, 1)[1]
    if live is None:
        return None
    plain, saw_faint = _unfainted(live)
    if not saw_faint:
        return None
    return not plain.strip(" \u00a0")


def raw_keys(pane_id: str, *keys: str) -> None:
    """The system-wide ``tmux send-keys`` choke point (T-0578).

    Callers outside this module hold :func:`delivery_lock` first (or are a
    pre-mux bootstrap, e.g. install.sh before any worker exists). Delegates to
    ``sessions._run`` — the executor seam the test suite already patches.
    """
    from bot_squad_worker.sessions import _run
    _run(["tmux", "send-keys", "-t", pane_id, *keys])


class DirectDelivery(int):
    """What the direct lane actually achieved (T-0913).

    IS the line count — the number ``inject_input`` has always returned as
    ``lines_sent`` — carrying the half that was missing: whether the payload
    was SUBMITTED, or merely typed into a composer that never cleared. The two
    were conflated, and the conflation IS the ticket: a nudge that reached the
    composer and stopped there returned ``{ok: true, lines_sent: 1}``,
    indistinguishable from one that started a turn, with nothing anywhere
    reporting a failure.

    An ``int`` subclass rather than a tuple, deliberately, and the reason is
    not brevity. This is the return value of a shared hot path with callers and
    tests in five files and two projects; a type that no longer compares equal
    to its own count would have turned "say one more true thing" into an API
    break, and the pressure would then be to skip saying it. ``bool`` is an
    ``int`` for the same reason. ``json.dumps`` still emits the number.
    """
    outcome: str

    def __new__(cls, lines: int, outcome: str) -> "DirectDelivery":
        self = super().__new__(cls, lines)
        self.outcome = outcome
        return self

    @property
    def lines(self) -> int:
        return int(self)

    @property
    def submitted(self) -> bool:
        return self.outcome == "cleared"

    def __repr__(self) -> str:
        return f"DirectDelivery(lines={int(self)}, outcome={self.outcome!r})"


class DeliveryNotConfirmed(RuntimeError):
    """Raised when a payload was sent but the composer never showed it clear.

    T-0957 DoD 2: "a nudge that reaches the composer and stops there must not
    count as delivered." :func:`flush` already requeues the batch on ANY
    exception from its ``deliver`` callable (see its ``except`` clause) — this
    exception is what makes a swallowed Enter use that same path instead of
    silently reporting success.
    """


#: T-0957 DoD 2 knobs for :func:`_deliver_to_pane`'s post-send confirmation.
_DELIVER_CONFIRM_MAX_RETRIES = 3
_DELIVER_CONFIRM_TIMEOUT_SEC = 2.0
_DELIVER_CONFIRM_POLL_INTERVAL_SEC = 0.3


def _paste_block(pane_id: str, text: str) -> None:
    """Put ``text`` into ``pane_id``'s composer as ONE multi-line message.

    Load the payload into a named tmux buffer over STDIN, then bracketed-paste
    it. Two details are load-bearing and neither is stylistic:

    * ``-p`` (bracketed) is what makes an embedded newline INSERT a newline in
      the composer instead of submitting the line — without it a multi-line
      payload becomes N separate messages (T-1038).
    * ``load-buffer -``, NOT ``set-buffer -- <arg>``: tmux's command parser
      rejects a large argument with "command too long" (T-0201, verified live
      on ~140-line briefs), so a big payload would silently never paste.

    Submitting is the caller's job — :func:`_submit_confirmed` — because the
    Enter can land inside the paste wrap and has to be confirmed, not assumed.
    """
    from bot_squad_worker.sessions import _run
    buf_name = f"bsq-input-{pane_id.lstrip('%')}"
    _run(["tmux", "load-buffer", "-b", buf_name, "-"], input=text)
    _run(["tmux", "paste-buffer", "-t", pane_id, "-b", buf_name, "-p", "-d"])


def _deliver_to_pane(pane_id: str, text: str, *,
                     capture: Callable[[str], str] | None = None) -> None:
    """Deliver a (possibly multi-line) payload as ONE composer message.

    Uses a bracketed paste (``paste-buffer -p``) so embedded newlines insert as
    a multi-line message instead of submitting line-by-line, then a single
    Enter to submit. This is deliberately NOT the raw ``inject_input`` path,
    which sends one Enter per line (correct for single-line nudges, wrong for a
    batched payload).

    T-0957 DoD 2: the Enter can land inside the bracketed-paste wrap and never
    submit (T-0201's failure mode, measured on the direct lane; the queued
    lane shares the same paste-buffer+Enter shape and has no reason to be
    immune). Before this, `flush()` counted the batch as delivered the moment
    this returned without raising — a swallowed Enter left the payload sitting
    in the composer while the queue was already drained, which is exactly the
    "reaches the composer and stops there" case the DoD calls out. Now this
    confirms the composer actually cleared, re-sending Enter up to a bound,
    and raises :class:`DeliveryNotConfirmed` if it never does — `flush()`'s
    existing exception handler requeues the batch instead of reporting it
    delivered.
    """
    capture = capture or _capture_pane
    was_generating = _pane_is_generating(pane_id, capture)
    _paste_block(pane_id, text)
    time.sleep(0.4)

    # T-0913: ONE confirmation for both lanes. This loop used to be a second
    # copy, and it carried the same defect — it read "no composer to look at"
    # as "the composer cleared", which on a generating pane is a false
    # positive. Sharing :func:`_submit_confirmed` means that fix cannot drift
    # back apart, and it also gains the permission-dialog stop the queued lane
    # never had.
    outcome = _submit_confirmed(pane_id, text, capture,
                                was_generating=was_generating)
    if outcome == "cleared":
        return
    raise DeliveryNotConfirmed(
        f"composer for {pane_id} did not confirm the payload submitted "
        f"(outcome={outcome})")


# Direct-lane knobs (read at call time so tests can monkeypatch them):
# a bounded wait while the user is live-typing in the target composer. On
# timeout the payload is delivered anyway — the direct lane is synchronous
# and guaranteed (a wake nudge or /compact must never be silently dropped),
# so the gate only narrows the splice window, it never blocks delivery.
_DIRECT_GATE_TIMEOUT_SEC = 3.0
_DIRECT_GATE_POLL_INTERVAL_SEC = 0.3
# Pause between a line's text and its Enter — tmux wraps long send-keys
# payloads in a bracketed-paste escape; an Enter chained in the SAME call
# lands inside the paste and does not submit (pre-T-0578 inject_input value).
_DIRECT_INTERLINE_PAUSE_SEC = 0.4


def deliver_direct(data_dir: Path | str, sid: str, pane_id: str, text: str, *,
                   capture: Callable[[str], str] | None = None) -> DirectDelivery:
    """Verbatim direct-lane transport (the old raw ``inject_input`` loop).

    Sends ``text`` verbatim — no caption, no batching — with byte-identical
    keystrokes to the pre-T-0578 inline loop for the single-line nudges this
    lane exists for; a multi-line payload lands as ONE composer message rather
    than one per line (T-1038, see :func:`_type_lines`). Serialised under the
    per-sid :func:`delivery_lock` so it cannot interleave with a queued-lane flush
    or teardown keys, and gated (bounded) on live user typing.

    Returns :class:`DirectDelivery` — the line count this has always returned,
    plus whether the payload was actually SUBMITTED (T-0913). The direct lane
    has no queue to requeue into, so an unsubmitted payload cannot be retried
    later; the only thing that helps is that the caller is TOLD.
    """
    capture = capture or _capture_pane
    with delivery_lock(data_dir, sid):
        # Bounded live-typing gate (skipped under the mux kill switch so
        # BOT_SQUAD_INPUT_MUX=0 restores the exact legacy timing).
        if os.environ.get("BOT_SQUAD_INPUT_MUX") != "0":
            deadline = time.monotonic() + _DIRECT_GATE_TIMEOUT_SEC
            # T-1062: `_typing_blocks`, not `user_is_typing` — waiting out the
            # renderer's own faint echo buys nothing and costs the whole
            # timeout on EVERY nudge to a pane that has received mail. Worse
            # than the delay is where the timeout leads: straight into the
            # draft-swap path, which once "restored" a ghost by TYPING it and
            # parked real unsent text in four panes at once (T-0962). A ghost
            # turned real locks the queued lane LEGITIMATELY, and no detector
            # reopens that. His actual draft is unaffected: only a positive
            # "this is my own faint text" skips the wait.
            while _typing_blocks(capture(pane_id), pane_id):
                if time.monotonic() >= deadline:
                    break
                time.sleep(_DIRECT_GATE_POLL_INTERVAL_SEC)

        # T-0954: the wait can time out with his draft still in the box, and
        # what happened then was the defect he reported — «вот так же check mail
        # штуки, они приходят просто поверх моего текста всегда даже без
        # пробела». `raw_keys` types at the CURSOR, so the payload landed
        # against the tail of his sentence and the Enter submitted both as one
        # line. His fix, verbatim: «он должен слать check mail вперед моего
        # текста, а мой текст оставлять как есть в поле ввода».
        block = (_live_draft_block(capture, pane_id)
                 if _draft_swap_enabled() else None)
        if block is not None and not _swap_is_safe(block):
            # A draft we cannot read in FULL must not be swapped: the swap
            # DELETES what it read and types back what it captured, so a read
            # that stopped short loses his tail in silence. The legacy path
            # merges the nudge into his text, which is the very thing T-0954
            # set out to fix, but it loses nothing; that is the right way round.
            log.info("input_mux: %s's draft cannot be read whole (%d row(s), "
                     "%d chars read) — %s. Delivering the legacy way rather "
                     "than risk a truncated restore", sid, len(block.rows),
                     len(block.text), block.reason or "unreadable")
            block = None
        if block is not None:
            return _deliver_ahead_of_draft(data_dir, sid, pane_id, text, block,
                                           capture)

        return _type_lines(pane_id, text, capture=capture)


def _pane_width(pane_id: str) -> int:
    """The pane's column count, or 0 when tmux will not say."""
    from bot_squad_worker.sessions import _run
    try:
        out = _run(["tmux", "display-message", "-p", "-t", pane_id,
                    "#{pane_width}"])
        return int(str(getattr(out, "stdout", out) or "").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _pane_height(pane_id: str) -> int:
    """The pane's row count, or 0 when tmux will not say. The composer box
    SCROLLS once it hits a height derived from this, and a scrolled box drops
    rows off the top without saying so — see ``composer_watch.composer_max_rows``."""
    from bot_squad_worker.sessions import _run
    try:
        out = _run(["tmux", "display-message", "-p", "-t", pane_id,
                    "#{pane_height}"])
        return int(str(getattr(out, "stdout", out) or "").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _swap_is_safe(block: Any) -> bool:
    """True when the read is PROVABLY the whole draft (T-0978).

    This used to be ``len(draft) + 6 < pane_width``, and the defect it was
    written to prevent is the one that made it pass: the reader handed it the
    first visual row of a wrapped draft, so the value it measured had already
    been cut to fit. Measured on his pane %638 (2026-09-06, 228 columns): a
    220-character capture, 226 < 228, guard satisfied, and the draft it
    described ran on for another two rows that the swap then deleted.

    A width threshold cannot be repaired by widening it — the input is wrong,
    not the bound. So there is no threshold now. The read either bounded the
    composer box, classified every row break inside it and can name the draft
    character for character, or it says it could not; only the first is safe,
    and ``composer_watch.ComposerBlock.complete`` is that answer.
    """
    return bool(block is not None and block.complete and block.text.strip())


def _draft_swap_enabled() -> bool:
    """T-0954 kill switch. ``BOT_SQUAD_DRAFT_SWAP=0`` restores the pre-T-0954
    delivery — the payload types straight into the composer, against whatever he
    has half-written there."""
    return os.environ.get("BOT_SQUAD_DRAFT_SWAP", "1") != "0"


def _preview(text: str, limit: int = 60) -> str:
    """A one-line, bounded rendering of a payload for the log — a 20-line
    handoff prompt must not be echoed whole into every retry line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _pane_is_generating(pane_id: str, capture: Callable[[str], str]) -> bool:
    """Was this pane mid-turn BEFORE we touched it? (T-0913.)

    Fails to ``True`` — a capture we could not take means we cannot claim the
    pane was idle, and claiming idle is what licenses reading a later
    generating frame as proof of submission. The safe direction here is to end
    up reporting ``unknown``, never to invent a ``cleared``.
    """
    from bot_squad_worker import composer_watch
    try:
        return composer_watch.looks_generating(capture(pane_id))
    except Exception:  # noqa: BLE001
        return True


def _submit_confirmed(pane_id: str, what: str,
                      capture: Callable[[str], str], *,
                      was_generating: bool = False) -> str:
    """Press Enter until the composer confirms it cleared. Returns the outcome.

    T-0957 DoD 2: an Enter can be swallowed (it lands inside tmux's
    bracketed-paste wrap and never submits), and the direct lane has no queue
    to fall back on — a payload that "reaches the composer and stops there" can
    only be RE-SUBMITTED, never requeued, and that still has to be logged
    rather than silently assumed. A permission dialog sharing the composer's
    rune is its own outcome: the retry stops rather than blasting Enter into a
    prompt that is not this session's to answer.

    T-0913 (2026-09-07), and this is the defect the FIRST version of this
    function had: ``composer_watch.composer_text`` returns ``None`` on a pane
    that is mid-turn — a generating pane shows no composer box at all — and
    ``(None or "").strip()`` is falsy, so **"there is nothing to look at" was
    read as "it cleared", i.e. as proof of submission.** That is a false
    positive by construction, and it fires in exactly the state where a nudge
    is most likely to park: a busy session. Measured live on this fleet at
    14:11:51Z — an operator hold was typed into a generating pane, `inject_input`
    returned 200 with no warning logged anywhere, and the session did not read
    its inbox for 10m16s.

    The fix is a zero captured BEFORE the keystroke, not a better reading after
    it. ``was_generating`` says what the pane was doing before we pressed
    Enter, because "the pane is generating now" means opposite things depending
    on it:

    * pane was IDLE, is generating now — only our Enter can have started that
      turn, so it submitted. This is the common case and it must stay cheap.
    * pane was ALREADY generating — the same frame tells us nothing at all, and
      the honest outcome is ``"unknown"``, never ``"cleared"``.

    ``"cleared"`` | ``"dialog"`` | ``"unknown"`` | ``"unconfirmed"``.
    """
    from bot_squad_worker import composer_watch
    saw_unknown = False
    for attempt in range(_DELIVER_CONFIRM_MAX_RETRIES):
        raw_keys(pane_id, "Enter")
        deadline = time.monotonic() + _DELIVER_CONFIRM_TIMEOUT_SEC
        outcome = None
        while time.monotonic() < deadline:
            time.sleep(_DELIVER_CONFIRM_POLL_INTERVAL_SEC)
            try:
                buf = capture(pane_id)
            except Exception:  # noqa: BLE001 — a capture hiccup, not proof of anything
                continue
            if composer_watch.looks_like_dialog(buf):
                outcome = "dialog"
                break
            live = composer_watch.composer_text(buf)
            if live is None or composer_watch.looks_generating(buf):
                # No composer to read. Evidence of submission ONLY if this pane
                # was not already in that state before we typed.
                if not was_generating:
                    outcome = "cleared"
                    break
                saw_unknown = True
                continue
            if not live.strip():
                outcome = "cleared"
                break
        if outcome == "cleared":
            if attempt:
                log.info("input_mux: %s's %r submitted after %d retry "
                         "Enter(s) — the first was swallowed", pane_id,
                         _preview(what), attempt)
            return "cleared"
        if outcome == "dialog":
            log.warning("input_mux: %s shows a permission dialog after "
                        "sending %r — not this session's to answer, "
                        "stopping retries (payload may be unsubmitted)",
                        pane_id, _preview(what))
            return "dialog"
    if saw_unknown:
        log.warning(
            "input_mux: %s was ALREADY mid-turn when %r was typed, so its "
            "composer was never visible to confirm against — reporting "
            "unknown, NOT delivered. The session may never have been woken.",
            pane_id, _preview(what))
        return "unknown"
    log.error("input_mux: %s's %r never confirmed submitted after %d "
              "Enter attempts — it may still be sitting in the composer "
              "(direct lane has no queue to requeue into)", pane_id,
              _preview(what), _DELIVER_CONFIRM_MAX_RETRIES)
    return "unconfirmed"


def _direct_paste_enabled() -> bool:
    """T-1038 kill switch. ``BOT_SQUAD_DIRECT_PASTE=0`` restores the pre-T-1038
    direct lane exactly: one send-keys + Enter per line, so a payload with a
    newline in it arrives as N separate composer submissions."""
    return os.environ.get("BOT_SQUAD_DIRECT_PASTE", "1") != "0"


def _type_lines(pane_id: str, text: str, *,
                capture: Callable[[str], str] | None = None) -> DirectDelivery:
    """The verbatim lane's keystrokes. Returns :class:`DirectDelivery`.

    A SINGLE-LINE payload — every nudge this lane was written for ("check
    mail", "/compact", the marker-prefixed idle/keepalive/dev nudges) — is
    typed with ``send-keys`` and submitted with its own confirmed Enter, which
    is byte-identical to the pre-T-0578 inline loop.

    A MULTI-LINE payload goes as ONE bracketed paste and ONE confirmed Enter
    (T-1038). It used to take the same per-line loop, and the docstring said so
    approvingly — "one submission per line" — which meant an injected payload
    containing a newline did not arrive as one message, it arrived as N
    messages. Measured on the real composed autocompact prompts before the fix:
    ``context_handoff_prompt`` produced **21** submissions and
    ``handoff_prompt`` **18**, the first of each being T-1032's marker ALONE on
    line 1. So the marker did not mark the message it was minted to mark — it
    became a separate, contentless turn, and the payload it was supposed to
    label arrived looking exactly like stakeholder input (9 of those 21
    fragments passed ``close_hook``'s stakeholder-harvest filter, which is
    prefix-matched on the marker and therefore blind to a detached one).

    Marking every line instead was the other candidate and is worse: it leaves
    a checkpoint ORDER fragmented across N turns — the session starts answering
    line 1 while the rest is still landing — which is the same defect T-0773
    fixed on the TG reply path, wearing a marker.

    The number of lines, not submissions, stays the ``lines`` field: it is what
    ``inject_input`` has always reported as ``lines_sent``. T-0913 adds the
    ``outcome`` beside that count rather than changing it, so no caller's
    number moves and the one fact that was missing becomes available.
    """
    capture = capture or _capture_pane
    lines = text.split("\n")
    # T-0913: the reference for "did it submit" has to be taken BEFORE the
    # keystrokes. A pane that is generating AFTERWARDS proves submission only
    # if it was not already generating BEFORE — see :func:`_submit_confirmed`.
    was_generating = _pane_is_generating(pane_id, capture)

    if len(lines) > 1 and _direct_paste_enabled():
        _paste_block(pane_id, text)
        time.sleep(_DIRECT_INTERLINE_PAUSE_SEC)
        outcome = _submit_confirmed(pane_id, text, capture,
                                    was_generating=was_generating)
        return DirectDelivery(len(lines), outcome)

    # T-0913: the WORST outcome wins. Under the pre-T-1038 kill switch a payload
    # is N submissions, and reporting the last line's success for a run where
    # line 3 stuck in the composer is the same lie one level down.
    worst = "cleared"
    for line in lines:
        raw_keys(pane_id, "--", line)
        time.sleep(_DIRECT_INTERLINE_PAUSE_SEC)
        outcome = _submit_confirmed(pane_id, line, capture,
                                    was_generating=was_generating)
        if outcome != "cleared" and worst == "cleared":
            worst = outcome
        was_generating = _pane_is_generating(pane_id, capture)
    return DirectDelivery(len(lines), worst)


def _live_draft_block(capture: Callable[[str], str], pane_id: str,
                      capture_ansi: Callable[[str], str] | None = None) -> Any:
    """The WHOLE composer box for ``pane_id``, or None when there is nothing
    to protect (T-0978).

    Returns a ``composer_watch.ComposerBlock``. Its ``complete`` flag — not its
    text — is what a caller that is about to delete his text branches on; see
    :func:`_swap_is_safe`.

    A permission/choice dialog is NOT a draft — its ``❯`` belongs to the prompt,
    and clearing it would answer it. That case falls through to the legacy
    keystrokes, which is what a dialog needs anyway (the keys go to the dialog,
    not into a composer that isn't there).
    """
    from bot_squad_worker import composer_watch

    try:
        buf = capture(pane_id)
    except Exception:  # noqa: BLE001 — a capture hiccup must not drop delivery
        return None
    if composer_watch.looks_like_dialog(buf):
        return None
    block = composer_watch.composer_block(buf, width=_pane_width(pane_id),
                                          height=_pane_height(pane_id))
    if block is None or not block.text.strip():
        return None
    if _composer_is_ghost(pane_id, capture_ansi) is True:
        # The renderer says this line is its own dim suggestion, not his text.
        # Protecting it is what parked `check mail` unsent in four panes at
        # once: the swap "restored" the ghost by TYPING it, which turned a hint
        # into real unsent content, and his next delivery read that back.
        log.info("input_mux: the composer for %s holds only Claude Code's own "
                 "faint suggestion (%d chars) — the box is empty (T-0962)",
                 pane_id, len(block.text))
        return None
    return block


def _live_draft(capture: Callable[[str], str], pane_id: str) -> str:
    """His in-progress composer text, or "" when there is nothing to protect.

    A reading, not a proof — since T-0978 it is the whole box rather than the
    box's first row, but a caller that DELETES his text must go through
    :func:`_live_draft_block` and :func:`_swap_is_safe` instead, because a read
    can be all of the text and still not be provably all of it.
    """
    block = _live_draft_block(capture, pane_id)
    return block.text if block is not None else ""


def drafts_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / "_worker" / "drafts"


def _save_draft(data_dir: Path | str, sid: str, draft: str) -> Path | None:
    """Persist his text before we touch the composer. The swap below is the only
    place the system deletes something a human typed, so it is also the only
    place that keeps a copy first."""
    try:
        d = drafts_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_safe(sid)}-{int(time.time())}.txt"
        path.write_text(draft, encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001
        log.warning("input_mux: could not save %s's draft before the swap",
                    sid, exc_info=True)
        return None


def _deliver_ahead_of_draft(data_dir: Path | str, sid: str, pane_id: str,
                            text: str, block: Any,
                            capture: Callable[[str], str]) -> int:
    """Submit ``text`` as its OWN message, then put his draft back untouched.

    A pane's composer holds exactly one buffer, so "ahead of his text" is three
    steps — save + clear, send, restore.

    **THE CLEAR IS N KEYSTROKES, NOT ONE (T-0978).** ``C-u`` kills to the start
    of the current VISUAL ROW, not the composer: measured on a real Claude Code
    composer 2026-09-07, a 577-character draft occupying three rows needed
    three ``C-u`` presses, and after the first two of his rows were still
    sitting in the box. One press was therefore never a clear for a wrapped
    draft — the payload was typed after his surviving rows and the Enter under
    it SUBMITTED them, which is his «мало того, что он его отсылает». So the
    press count comes from the block we read, one per row it actually contains.

    **Almost nothing here branches on a capture.** The obvious design was to
    verify the clear before typing, and it is not implementable: measured on a
    live pane 2026-09-03, ``tmux capture-pane`` kept returning the PRE-clear
    frame for more than 2.4 seconds while the composer was already empty
    (Claude Code repaints its input box on its own schedule, and a busy session
    repaints it late). A verification that reads a stale frame concludes "the
    clear failed", takes the legacy path, and never restores — which is exactly
    how his «file the mask-unclassified ticket too» left its pane while the log
    said the delivery was fine.

    The one capture that IS consulted below can only ADD ``C-u`` presses, never
    skip the restore, so a stale frame costs at most a few no-op keystrokes
    (measured: ``C-u`` on an empty composer does nothing). That is the safe
    direction of the same trade, and it is what bounds the damage if the row
    count is ever short.

    So the clear is TRUSTED and every other capture is for the LOG only. The
    failure mode that trade buys is a duplicated draft if a clear ever silently
    fails — visible, his to fix in one keystroke — instead of a silently
    vanished one. His text is also on disk before anything is touched.
    """
    draft = block.text
    saved = _save_draft(data_dir, sid, draft)

    for _ in range(max(1, len(block.rows))):
        raw_keys(pane_id, "C-u")
        time.sleep(_CLEAR_KEY_PAUSE_SEC)
    _drain_leftover_rows(pane_id, capture)

    sent = _type_lines(pane_id, text, capture=capture)

    _restore_draft(pane_id, draft)
    time.sleep(_DIRECT_INTERLINE_PAUSE_SEC)
    log.info("input_mux: delivered %d line(s) to %s ahead of his draft "
             "(%d chars over %d row(s), submitted=%s, copy at %s)", sent.lines,
             sid, len(draft), len(block.rows), sent.submitted, saved)
    return sent


#: Pause between the ``C-u`` presses that clear the composer. Shorter than
#: :data:`_DIRECT_INTERLINE_PAUSE_SEC` because no text is being typed between
#: them — this is only to keep the repaint from coalescing the presses.
_CLEAR_KEY_PAUSE_SEC = 0.15

#: How many EXTRA ``C-u`` presses the drain below will spend when the composer
#: still reads non-empty. Bounded because the frame it reads may simply be
#: stale; the presses are no-ops on an already-empty composer.
_CLEAR_DRAIN_MAX = 4


def _drain_leftover_rows(pane_id: str, capture: Callable[[str], str]) -> None:
    """Spend a few more ``C-u`` presses if the box still shows rows.

    Belt for the row count: if the block we read ever under-counts, the rows it
    missed would otherwise be submitted by the Enter that follows. Reading a
    stale frame here is harmless — it only buys no-op keystrokes — because this
    never decides whether to restore, which is the branch that cost his text in
    T-0954.
    """
    from bot_squad_worker import composer_watch
    for _ in range(_CLEAR_DRAIN_MAX):
        try:
            leftover = composer_watch.composer_text(capture(pane_id))
        except Exception:  # noqa: BLE001
            return
        if not (leftover or "").strip():
            return
        raw_keys(pane_id, "C-u")
        time.sleep(_CLEAR_KEY_PAUSE_SEC)


def _restore_draft(pane_id: str, draft: str) -> None:
    """Type his draft back, byte for byte.

    A draft he broke with a newline cannot go back through ``send-keys``: the
    newline would be an Enter and would SUBMIT it. It goes through the same
    bracketed paste the multi-line payload path uses, which inserts newlines
    instead of submitting (T-1038) — and with no Enter after it, because
    restoring is putting it back in the box, not sending it.
    """
    if "\n" in draft:
        _paste_block(pane_id, draft)
        return
    raw_keys(pane_id, "--", draft)


def _default_pane_lookup(sid: str) -> str | None:
    from bot_squad_worker.sessions import live_pane_map
    try:
        return live_pane_map().get(sid)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Flush — coalesce + deliver (or defer)
# ---------------------------------------------------------------------------

def flush(data_dir: Path | str, sid: str, *,
          pane_lookup: Callable[[str], str | None] | None = None,
          capture: Callable[[str], str] | None = None,
          deliver: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    """Deliver the queued batch for ``sid`` — or defer if the composer is busy.

    Serialised per-sid on the delivery lock so concurrent flushers coalesce
    (the winner drains everything queued so far; the rest find an empty queue).
    Returns ``{delivered, deferred, reason, pane}``.

    Deferral leaves the queue fully intact (never rejected) so a later flush
    delivers it once the user stops typing / generation finishes / a pane
    appears.
    """
    pane_lookup = pane_lookup or _default_pane_lookup
    capture = capture or _capture_pane
    deliver = deliver or _deliver_to_pane

    with delivery_lock(data_dir, sid):
        if not read_queue(data_dir, sid):
            return {"delivered": 0, "deferred": False, "reason": "empty",
                    "pane": None}

        pane = pane_lookup(sid)
        if not pane:
            return {"delivered": 0, "deferred": True, "reason": "no_pane",
                    "pane": None}

        buf = capture(pane)
        if not deliverable(buf, pane_id=pane):
            # T-1062 DoD 2: name WHICH lock closed. `generation` is ordinary
            # work and ages out on its own; `draft` is text sitting in the box
            # that nothing in the system will ever clear, and that is the state
            # that goes silent forever. The sweep alerts on the second only.
            return {"delivered": 0, "deferred": True, "reason": "composer_busy",
                    "blocked_by": ("generation" if not composer_ready(buf)
                                   else "draft"),
                    "pane": pane}

        # Composer is ready — claim the batch exclusively, then deliver.
        batch = _drain(data_dir, sid)
        if not batch:
            return {"delivered": 0, "deferred": False, "reason": "empty",
                    "pane": pane}
        try:
            deliver(pane, format_batch(batch))
        except Exception:
            _requeue_front(data_dir, sid, batch)
            _clear_inflight(data_dir, sid)
            raise
        _clear_inflight(data_dir, sid)
        return {"delivered": len(batch), "deferred": False,
                "reason": "delivered", "pane": pane}


def recover_inflight(data_dir: Path | str, sid: str) -> int:
    """Put a batch orphaned by a worker restart back at the FRONT of the queue.

    T-1082. A claim file outlives the process that made it, so its mere
    presence is not proof of an orphan — a flush running RIGHT NOW has one too.
    What distinguishes them is the delivery flock: a live flush holds it for
    the whole of :func:`_deliver_to_pane`, and a dead one cannot hold anything,
    because the kernel drops a dead process's flocks. So the test for "orphaned"
    is "can I take this sid's delivery lock without waiting", and it is exact
    rather than a timeout guess — no age threshold to tune, and no window in
    which a slow-but-alive delivery gets its batch requeued underneath it and
    delivered twice.

    Returns the number of messages recovered (0 when there is nothing to do or
    a delivery is live).

    The requeue happens BEFORE the claim is cleared, deliberately: a death in
    the millisecond between them costs a DUPLICATE nudge on the next sweep,
    and the other order costs the message. This whole function exists because
    losing it is the worse failure.
    """
    path = _inflight_path(data_dir, sid)
    if not path.exists():
        return 0
    queue_dir(data_dir).mkdir(parents=True, exist_ok=True)
    lock_fd = open(_delivery_lock_path(data_dir, sid), "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # a flush is mid-delivery; its own handlers own this claim
        msgs = _read_inflight(data_dir, sid)
        if not msgs:
            _clear_inflight(data_dir, sid)
            return 0
        _requeue_front(data_dir, sid, msgs)
        _clear_inflight(data_dir, sid)
        log.warning(
            "input_mux: recovered %d message(s) for %s that were claimed for "
            "delivery but never confirmed — a worker restart landed between "
            "the drain and the composer. Requeued at the front; NOT counted "
            "as delivered.", len(msgs), sid)
        return len(msgs)
    finally:
        lock_fd.close()


def recover_pending(data_dir: Path | str) -> dict[str, Any]:
    """Sweep every orphaned claim. Runs before the flush pass on each tick."""
    qdir = queue_dir(data_dir)
    try:
        claims = list(qdir.glob("*.inflight.jsonl"))
    except FileNotFoundError:
        return {"sids": 0, "recovered": 0}
    sids = 0
    recovered = 0
    for cf in claims:
        sid = cf.name[: -len(".inflight.jsonl")]
        try:
            got = recover_inflight(data_dir, sid)
        except Exception:  # noqa: BLE001 — one bad claim never kills the sweep
            log.exception("input_mux: recover_inflight failed for %s", sid)
            continue
        if got:
            sids += 1
            recovered += got
    return {"sids": sids, "recovered": recovered}


def stall_alert_after_sec() -> float:
    """How long an unclearable deferral may last before it has to ring.

    Ten minutes, matching the T-0954 staleness window: text that has sat in the
    composer that long is text nobody is writing. ``0`` disables the alert.
    """
    try:
        return float(os.environ.get("BOT_SQUAD_INPUT_STALL_ALERT_SEC", "600"))
    except ValueError:
        return 600.0


def _stall_report(data_dir: Path | str, sid: str, res: dict[str, Any],
                  now: float) -> dict[str, Any] | None:
    """A deferral that has outlived the threshold and cannot clear itself.

    T-1062 DoD 2. Being undeliverable was SILENT: `flush` handed a reason back
    to a sweep that dropped it, and the only way to learn a session had gone
    deaf was to open its queue file by hand. Two hours of the fleet's alarm
    handler reporting "all quiet" is what that cost on 2026-09-07.

    Deliberately narrow, because an alarm that cries during ordinary work gets
    muted and then it is worse than none:
      * ``generation`` never rings — a long turn is work, and it ends;
      * ``no_pane`` never rings — the session is gone, and the reaper owns that;
      * a draft in the box rings once it has held the queue past the threshold,
        because NOTHING in the system clears it. That is also DoD 5: unsent text
        blocking delivery indefinitely now tells someone instead of just
        blocking.
    """
    threshold = stall_alert_after_sec()
    if threshold <= 0 or res.get("blocked_by") != "draft":
        return None
    queued = read_queue(data_dir, sid)
    if not queued:
        return None
    oldest = min((float(m.get("ts") or now) for m in queued), default=now)
    age = now - oldest
    if age < threshold:
        return None
    return {"sid": sid, "pane": res.get("pane"), "records": len(queued),
            "oldest_age_sec": age, "blocked_by": "draft"}


def flush_pending(data_dir: Path | str) -> dict[str, Any]:
    """Flush every non-empty per-sid queue (deferred-delivery scheduler tick).

    Picks up queues left deferred when the user was typing / mid-generation:
    once the composer frees up this re-attempts delivery without needing a new
    write. Idempotent — an empty or still-busy queue is a no-op / re-deferred.

    T-0957 DoD 2: ``flush`` re-raises on a delivery it could not confirm (see
    :class:`DeliveryNotConfirmed`), after requeuing the batch, so the sid gets
    another attempt on the next tick rather than losing the payload. One sid's
    exception must not stop the REST of this tick's sweep — the caller-level
    ``except`` around the whole scheduler tick (``jobs.input_flush_tick``)
    already swallows a single uncaught exception, but a loop with no per-sid
    guard lets the FIRST failing sid abort every sid after it in the same
    pass, which is exactly what "one bad queue never kills the sweep" (this
    function's own docstring, T-0469) says must not happen.
    """
    qdir = queue_dir(data_dir)
    delivered = 0
    flushed = 0
    deferred = 0
    stalled: list[dict[str, Any]] = []
    now = time.time()
    # T-1082: reclaim anything a restart orphaned BEFORE this pass reads the
    # queues, so a recovered batch is delivered by this same tick rather than
    # waiting another 15s.
    recovered = recover_pending(data_dir).get("recovered", 0)
    try:
        files = list(qdir.glob("*.jsonl"))
    except FileNotFoundError:
        files = []
    files = [f for f in files if not f.name.endswith(".inflight.jsonl")]
    for qf in files:
        sid = qf.stem
        try:
            if qf.stat().st_size == 0:
                continue
        except FileNotFoundError:
            continue
        try:
            res = flush(data_dir, sid)
        except Exception:
            log.exception("input_mux: flush_pending — %s's delivery failed "
                          "and was requeued; continuing with the rest of "
                          "this sweep", sid)
            flushed += 1
            continue
        flushed += 1
        delivered += res.get("delivered", 0)
        if res.get("deferred"):
            deferred += 1
            stall = _stall_report(data_dir, sid, res, now)
            if stall is not None:
                stalled.append(stall)
    return {"queues": flushed, "delivered": delivered, "deferred": deferred,
            "recovered": recovered, "stalled": stalled}
