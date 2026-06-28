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
"""
from __future__ import annotations

import fcntl
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from bot_squad_worker.autocompact import composer_ready

# sids are filename-clean (``S-<user>-<slug>-pNNN`` → letters/digits/.-_), so
# the sanitised stem round-trips to the sid. The substitution is purely
# defensive against an unexpected character.
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


def _deliver_to_pane(pane_id: str, text: str) -> None:
    """Deliver a (possibly multi-line) payload as ONE composer message.

    Uses a bracketed paste (``paste-buffer -p``) so embedded newlines insert as
    a multi-line message instead of submitting line-by-line, then a single
    Enter to submit. This is deliberately NOT the raw ``inject_input`` path,
    which sends one Enter per line (correct for single-line nudges, wrong for a
    batched payload).
    """
    import subprocess
    buf_name = f"bsq-input-{pane_id.lstrip('%')}"
    # Load the payload into a named tmux buffer, then bracketed-paste it.
    subprocess.run(["tmux", "set-buffer", "-b", buf_name, "--", text],
                   check=False)
    subprocess.run(
        ["tmux", "paste-buffer", "-t", pane_id, "-b", buf_name, "-p", "-d"],
        check=False,
    )
    time.sleep(0.4)
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], check=False)


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

    queue_dir(data_dir).mkdir(parents=True, exist_ok=True)
    lock_fd = open(_delivery_lock_path(data_dir, sid), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

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
    finally:
        lock_fd.close()


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
