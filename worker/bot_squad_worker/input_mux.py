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
  used to live inline in the ``inject_input`` action (one send-keys + Enter
  per line, byte-identical content, no caption/batch — a solo "check mail"
  stays "check mail" and "/compact" stays a bare slash command). It holds the
  per-sid delivery lock so it can never interleave keystrokes with a queued-
  lane flush, and briefly gates on live user typing (bounded wait, then
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


def deliverable(buf: str) -> bool:
    """True when it is safe to inject: composer ready AND user not mid-typing.

    Reuses :func:`autocompact.composer_ready` (the ``❯`` rune, no mid-generation
    "esc to interrupt" marker) and adds the live-typing guard.
    """
    return composer_ready(buf) and not user_is_typing(buf)


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
    """
    lock_fd = open(_append_lock_path(data_dir, sid), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        msgs = read_queue(data_dir, sid)
        if msgs:
            queue_path(data_dir, sid).write_text("", encoding="utf-8")
        return msgs
    finally:
        lock_fd.close()


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


def raw_keys(pane_id: str, *keys: str) -> None:
    """The system-wide ``tmux send-keys`` choke point (T-0578).

    Callers outside this module hold :func:`delivery_lock` first (or are a
    pre-mux bootstrap, e.g. install.sh before any worker exists). Delegates to
    ``sessions._run`` — the executor seam the test suite already patches.
    """
    from bot_squad_worker.sessions import _run
    _run(["tmux", "send-keys", "-t", pane_id, *keys])


def _deliver_to_pane(pane_id: str, text: str) -> None:
    """Deliver a (possibly multi-line) payload as ONE composer message.

    Uses a bracketed paste (``paste-buffer -p``) so embedded newlines insert as
    a multi-line message instead of submitting line-by-line, then a single
    Enter to submit. This is deliberately NOT the raw ``inject_input`` path,
    which sends one Enter per line (correct for single-line nudges, wrong for a
    batched payload).
    """
    from bot_squad_worker.sessions import _run
    buf_name = f"bsq-input-{pane_id.lstrip('%')}"
    # Load the payload into a named tmux buffer over STDIN, then bracketed-
    # paste it. `load-buffer -`, NOT `set-buffer -- <arg>`: tmux's command
    # parser rejects a large argument with "command too long" (T-0201, verified
    # live on ~140-line briefs), so a big batch would silently never paste.
    _run(["tmux", "load-buffer", "-b", buf_name, "-"], input=text)
    _run(["tmux", "paste-buffer", "-t", pane_id, "-b", buf_name, "-p", "-d"])
    time.sleep(0.4)
    raw_keys(pane_id, "Enter")


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
                   capture: Callable[[str], str] | None = None) -> int:
    """Verbatim direct-lane transport (the old raw ``inject_input`` loop).

    Sends ``text`` one send-keys per line with a separate Enter each —
    byte-identical keystrokes to the pre-T-0578 inline loop (no caption, no
    batching, one submission per line), but serialised under the per-sid
    :func:`delivery_lock` so it can never interleave with a queued-lane flush
    or teardown keys, and gated (bounded) on live user typing. Returns the
    number of lines sent.
    """
    capture = capture or _capture_pane
    with delivery_lock(data_dir, sid):
        # Bounded live-typing gate (skipped under the mux kill switch so
        # BOT_SQUAD_INPUT_MUX=0 restores the exact legacy timing).
        if os.environ.get("BOT_SQUAD_INPUT_MUX") != "0":
            deadline = time.monotonic() + _DIRECT_GATE_TIMEOUT_SEC
            while user_is_typing(capture(pane_id)):
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
        draft = _live_draft(capture, pane_id) if _draft_swap_enabled() else ""
        if draft and not _swap_is_safe(pane_id, draft):
            # A draft we cannot read in FULL must not be swapped: the reader
            # takes the last `❯` line, so a wrapped or multi-line message would
            # be saved and restored truncated — silently losing the tail. The
            # legacy path merges the nudge into his text, which is the very
            # thing T-0954 set out to fix, but it loses nothing; that is the
            # right way round.
            log.info("input_mux: %s's draft may be wrapped (%d chars) — "
                     "delivering the legacy way rather than risk a truncated "
                     "restore", sid, len(draft))
            draft = ""
        if draft:
            return _deliver_ahead_of_draft(data_dir, sid, pane_id, text, draft,
                                           capture)

        return _type_lines(pane_id, text)


def _pane_width(pane_id: str) -> int:
    """The pane's column count, or 0 when tmux will not say."""
    from bot_squad_worker.sessions import _run
    try:
        out = _run(["tmux", "display-message", "-p", "-t", pane_id,
                    "#{pane_width}"])
        return int(str(getattr(out, "stdout", out) or "").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


#: Columns the composer's own chrome takes before his text starts (`❯ ` plus
#: the box border). Deliberately generous — the cost of being wrong here is one
#: legacy delivery, and the cost of being wrong the other way is his tail.
_COMPOSER_CHROME_COLS = 6


def _swap_is_safe(pane_id: str, draft: str) -> bool:
    """True when the captured draft is certainly the WHOLE draft.

    The reader takes the last ``❯`` line, so anything that wrapped onto a
    following line, or was entered multi-line, is captured short. Swapping on a
    short read would restore a truncated message — a silent edit of something he
    wrote. So the swap is confined to a draft that provably fits one line.
    """
    width = _pane_width(pane_id)
    if width <= 0:
        return False        # cannot prove it fits → do not risk it
    return len(draft) + _COMPOSER_CHROME_COLS < width


def _draft_swap_enabled() -> bool:
    """T-0954 kill switch. ``BOT_SQUAD_DRAFT_SWAP=0`` restores the pre-T-0954
    delivery — the payload types straight into the composer, against whatever he
    has half-written there."""
    return os.environ.get("BOT_SQUAD_DRAFT_SWAP", "1") != "0"


def _type_lines(pane_id: str, text: str) -> int:
    """The verbatim lane's keystrokes: one send-keys per line, one Enter each."""
    lines_sent = 0
    for line in text.split("\n"):
        raw_keys(pane_id, "--", line)
        time.sleep(_DIRECT_INTERLINE_PAUSE_SEC)
        raw_keys(pane_id, "Enter")
        lines_sent += 1
    return lines_sent


def _live_draft(capture: Callable[[str], str], pane_id: str) -> str:
    """His in-progress composer text, or "" when there is nothing to protect.

    A permission/choice dialog is NOT a draft — its ``❯`` belongs to the prompt,
    and clearing it would answer it. That case falls through to the legacy
    keystrokes, which is what a dialog needs anyway (the keys go to the dialog,
    not into a composer that isn't there).
    """
    from bot_squad_worker import composer_watch

    try:
        buf = capture(pane_id)
    except Exception:  # noqa: BLE001 — a capture hiccup must not drop delivery
        return ""
    if composer_watch.looks_like_dialog(buf):
        return ""
    live = composer_watch.composer_text(buf)
    return live if (live or "").strip() else ""


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
                            text: str, draft: str,
                            capture: Callable[[str], str]) -> int:
    """Submit ``text`` as its OWN message, then put ``draft`` back untouched.

    A pane's composer holds exactly one buffer, so "ahead of his text" is three
    steps — save + clear (``C-u``), send, restore.

    **Nothing here branches on a capture.** The obvious design was to verify the
    clear before typing, and it is not implementable: measured on a live pane
    2026-09-03, ``tmux capture-pane`` kept returning the PRE-clear frame for
    more than 2.4 seconds while the composer was already empty (Claude Code
    repaints its input box on its own schedule, and a busy session repaints it
    late). A verification that reads a stale frame concludes "the clear failed",
    takes the legacy path, and never restores — which is exactly how his
    «file the mask-unclassified ticket too» left its pane while the log said the
    delivery was fine.

    So the C-u is TRUSTED (it is what actually works; the same measurement shows
    the composer really was cleared) and every capture below is for the LOG
    only. The failure mode that trade buys is a duplicated draft if a C-u ever
    silently fails — visible, his to fix in one keystroke — instead of a
    silently vanished one. His text is also on disk before anything is touched.
    """
    saved = _save_draft(data_dir, sid, draft)

    raw_keys(pane_id, "C-u")
    time.sleep(_DIRECT_INTERLINE_PAUSE_SEC)

    lines_sent = _type_lines(pane_id, text)

    raw_keys(pane_id, "--", draft)
    time.sleep(_DIRECT_INTERLINE_PAUSE_SEC)
    log.info("input_mux: delivered %d line(s) to %s ahead of his draft "
             "(%d chars, copy at %s)", lines_sent, sid, len(draft), saved)
    return lines_sent


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

        if not deliverable(capture(pane)):
            return {"delivered": 0, "deferred": True, "reason": "composer_busy",
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
            raise
        return {"delivered": len(batch), "deferred": False,
                "reason": "delivered", "pane": pane}


def flush_pending(data_dir: Path | str) -> dict[str, Any]:
    """Flush every non-empty per-sid queue (deferred-delivery scheduler tick).

    Picks up queues left deferred when the user was typing / mid-generation:
    once the composer frees up this re-attempts delivery without needing a new
    write. Idempotent — an empty or still-busy queue is a no-op / re-deferred.
    """
    qdir = queue_dir(data_dir)
    delivered = 0
    flushed = 0
    deferred = 0
    try:
        files = list(qdir.glob("*.jsonl"))
    except FileNotFoundError:
        files = []
    for qf in files:
        sid = qf.stem
        try:
            if qf.stat().st_size == 0:
                continue
        except FileNotFoundError:
            continue
        res = flush(data_dir, sid)
        flushed += 1
        delivered += res.get("delivered", 0)
        if res.get("deferred"):
            deferred += 1
    return {"queues": flushed, "delivered": delivered, "deferred": deferred}
