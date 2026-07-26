"""T-0696 — stall-sweep: auto-answer known blocking TUI prompts in live panes.

Claude Code's OWN interactive TUI can show a blocking prompt on the FIRST turn
of some (not all) fresh spawns — e.g. observed 2026-07-26, ~40% of fresh
``--model claude-fable-5`` spawns hit a usage-credits gate ("Fable 5 now uses
usage credits ... 1. Set up usage credits / 2. Switch to Sonnet 5 and
continue"). If nobody answers it the pane sits there indefinitely, costing
15-35 min of dead wall-clock per hit, and it is invisible to every existing
watchdog:

* **drift-check** (``drift.py``) watches ticket-note/commit timestamps, not
  raw pane content — a session that never got to write anything never drifts.
* **idle-timeout** (``idle_timeout.py``) reads the Claude Stop-hook / jsonl
  mtime as its idle clock — a session blocked on stdin before its first turn
  ever completes never fires either hook, so it never looks "idle" to that
  clock even though it is doing nothing.

This module is the dedicated sweep for that class of stall: capture each live
session's pane text (mirrors ``detector.py``'s pane-scan shape), match it
against a small table of known stall SIGNATURES, and on a match auto-answer
in the safe/conservative direction, then record the action so it is visible
in the ticket/ops record instead of silently happening. Never terminates or
recycles a session — this is purely "unblock stdin", orthogonal to every
other lifecycle sweep.

Adding a new signature (e.g. a different Claude Code TUI prompt) means adding
one ``StallSignature`` entry to :data:`SIGNATURES` — the sweep loop, marker
lifecycle, and reporting are all signature-agnostic.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StallSignature:
    """One known blocking-prompt shape + the safe/conservative answer for it."""
    name: str
    match: Callable[[str], bool]
    keys: tuple[str, ...]  # raw tmux send-keys sequence, e.g. ("2", "Enter")
    note: str              # human-readable action description for the record


def _fable5_credits_gate(buf: str) -> bool:
    """The Fable-5 usage-credits gate (T-0696, confirmed recurring 2026-07-26).

    Anchor on BOTH numbered-option strings verbatim, not a bare "usage
    credit" substring: the Fable-5 PROMO banner ("... you can continue on
    Fable 5 with usage credits ...") mentions usage credits on a perfectly
    healthy pane and must not trip this — the exact false-positive shape
    ``detector.py`` already hit and fixed for its own limit-marker scan
    (T-0571). Requiring both option lines together is specific to the actual
    modal, not prose merely discussing it.
    """
    if not buf:
        return False
    low = buf.lower()
    if "usage credit" not in low:
        return False
    return "set up usage credits" in low and "switch to sonnet 5 and continue" in low


SIGNATURES: tuple[StallSignature, ...] = (
    StallSignature(
        name="fable5_usage_credits_gate",
        match=_fable5_credits_gate,
        keys=("2", "Enter"),
        note=(
            "auto-answered a stuck interactive prompt (Fable-5 usage-credits "
            "gate) -- sent '2' (Switch to Sonnet 5 and continue), the safe/"
            "conservative direction"
        ),
    ),
    # Add more StallSignature entries here as new stuck-TUI-prompt shapes turn
    # up. Each needs a `match` specific enough to never trip on prose merely
    # discussing/quoting the prompt (see the T-0571 lesson above), the `keys`
    # to send, and a `note` for the audit trail.
)


def _match_signature(buf: str) -> StallSignature | None:
    for sig in SIGNATURES:
        try:
            if sig.match(buf):
                return sig
        except Exception:
            log.exception("stall_sweep: signature %s match() raised", sig.name)
    return None


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

def stall_sweep_enabled() -> bool:
    """Master switch (default ON). ``BOT_SQUAD_STALL_SWEEP=0`` disables."""
    return os.environ.get("BOT_SQUAD_STALL_SWEEP", "1") != "0"


# A short in-tick guard so a just-sent answer gets a chance to land (and the
# TUI to redraw) before the next tick re-captures and possibly resends —
# never hammer a pane every 60s while it is still processing the last answer.
_RETRY_COOLDOWN_SEC = 20.0

# After this many failed attempts to clear the SAME signature, stop retrying
# (never hammer indefinitely) and escalate to a human instead.
_MAX_ATTEMPTS = 3

# Synthetic system author for ticket notes, mirroring autocompact's
# "S-autocompact" orphaned-handoff alert sender.
_NOTE_SID = "S-stall-sweep"

# Pause between keystrokes so a fast interactive prompt reliably registers the
# first key before Enter submits it.
_INTERKEY_PAUSE_SEC = 0.3


# ---------------------------------------------------------------------------
# Marker storage (per-sid, per-project) — anti-loop + attempt tracking
# ---------------------------------------------------------------------------

def _marker_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "_worker" / "stall_sweep"


def _marker_path(cfg: Any, slug: str, sid: str) -> Path:
    safe = (sid or "").replace("/", "_")
    return _marker_dir(cfg, slug) / f"{safe}.json"


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


def _clear(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# tmux seams (monkeypatched in tests)
# ---------------------------------------------------------------------------

def _capture_pane(pane_id: str) -> str:
    # -J joins wrapped lines, same as detector.py's pane scan, so a long
    # prompt line can't split across a visual wrap and dodge the matcher.
    from bot_squad_worker.sessions import _run
    try:
        cap = _run(["tmux", "capture-pane", "-t", pane_id, "-p", "-J"])
        return cap.stdout if cap.returncode == 0 else ""
    except Exception:
        log.exception("stall_sweep: capture-pane failed for %s", pane_id)
        return ""


def _send_keys(data_dir: Any, sid: str, pane_id: str, keys: tuple[str, ...]) -> None:
    """Send ``keys`` (e.g. ``("2", "Enter")``) straight to the pane.

    This is NOT Claude Code's own composer (the ``input_mux``/``deliver_direct``
    lane) — the stall prompt is a different, raw TUI modal that reads
    keystrokes directly, not a composer submission. Still goes through
    ``input_mux.raw_keys`` (the system-wide send-keys choke point, T-0578)
    under the per-sid delivery lock so this can never interleave with a
    concurrent queued-lane flush or teardown keys.
    """
    from bot_squad_worker import input_mux
    with input_mux.delivery_lock(data_dir, sid):
        for key in keys:
            input_mux.raw_keys(pane_id, key)
            time.sleep(_INTERKEY_PAUSE_SEC)


# ---------------------------------------------------------------------------
# Reporting — visible in the record, not just a silent auto-fix
# ---------------------------------------------------------------------------

def _ticket_note(cfg: Any, slug: str, task_id: str, text: str) -> bool:
    try:
        from bot_squad_worker.actions import _action_task_progress_add
        _action_task_progress_add({
            "slug": slug, "task_id": task_id, "sid": _NOTE_SID, "text": text,
        })
        return True
    except Exception:
        log.exception("stall_sweep: ticket note failed for task %s", task_id)
        return False


def _report_answered(cfg: Any, slug: str, sid: str, row: dict,
                     sig: StallSignature) -> None:
    task_id = row.get("task_id")
    noted = bool(task_id) and _ticket_note(cfg, slug, task_id, f"{sid}: {sig.note}")
    log.warning("stall_sweep: %s matched %s -- answered (ticket_note=%s)",
                sid, sig.name, noted)


def _report_escalation(cfg: Any, slug: str, sid: str, row: dict,
                       sig: StallSignature, attempts: int) -> None:
    text = (f"{sid}: stuck on a known interactive prompt ({sig.name}) -- "
            f"auto-answer did not clear it after {attempts} attempts, "
            "needs a manual tmux attach")
    task_id = row.get("task_id")
    if task_id:
        _ticket_note(cfg, slug, task_id, text)
    # Piggyback the EXISTING stall-watchdog (T-0155, tg_stall.py) instead of
    # reinventing TG delivery here: mark this sid "blocked on the operator" so
    # its own tick pages a human via its established routing/dedup/quiet-hours
    # logic once the block ages past its threshold.
    try:
        from bot_squad_worker import tg_stall as _tg_stall
        _tg_stall.mark_blocked(cfg, slug, sid, text)
    except Exception:
        log.exception("stall_sweep: tg_stall.mark_blocked failed for %s", sid)
    log.warning("stall_sweep: %s -- escalated (%s, %d attempts)",
                sid, sig.name, attempts)


# ---------------------------------------------------------------------------
# Per-session check
# ---------------------------------------------------------------------------

def check_session(cfg: Any, slug: str, sid: str, row: dict, pane_id: str,
                  now: float) -> bool:
    """Capture ``pane_id``, match against :data:`SIGNATURES`, act on a hit.

    Returns True iff a keystroke sequence was sent this call. A cleared pane
    (no signature matches) drops any stale marker. A still-matching pane
    within :data:`_RETRY_COOLDOWN_SEC` of its last action is left alone (the
    answer may not have landed/redrawn yet); past that window it retries, up
    to :data:`_MAX_ATTEMPTS`, then escalates instead of hammering the pane.
    """
    marker_path = _marker_path(cfg, slug, sid)
    marker = _read(marker_path)

    buf = _capture_pane(pane_id)
    sig = _match_signature(buf)

    if sig is None:
        if marker:
            _clear(marker_path)
        return False

    if marker and marker.get("escalated"):
        return False  # already gave up + paged a human; don't hammer the pane

    same_signature = bool(marker) and marker.get("signature") == sig.name
    attempts = int(marker.get("attempts", 0)) if same_signature else 0

    if same_signature:
        last_action_at = float(marker.get("last_action_at", 0.0))
        if now - last_action_at < _RETRY_COOLDOWN_SEC:
            return False
        if attempts >= _MAX_ATTEMPTS:
            _report_escalation(cfg, slug, sid, row, sig, attempts)
            marker["escalated"] = True
            _write(marker_path, marker)
            return False

    try:
        _send_keys(cfg.data_dir, sid, pane_id, sig.keys)
    except Exception:
        log.exception("stall_sweep: send-keys failed for %s (%s)", sid, sig.name)
        return False

    attempts += 1
    new_marker = {
        "sid": sid,
        "signature": sig.name,
        "first_seen": marker["first_seen"] if same_signature else now,
        "last_action_at": now,
        "attempts": attempts,
        "escalated": False,
    }
    _write(marker_path, new_marker)
    if attempts == 1:
        _report_answered(cfg, slug, sid, row, sig)
    return True


# ---------------------------------------------------------------------------
# Scheduler entry
# ---------------------------------------------------------------------------

def tick(cfg: Any) -> None:
    """Per-project sweep across every live session's pane.

    Sibling of idle_timeout/tg_stall (its own 60s job). Per-session/per-project
    errors are swallowed so one bad session never kills the sweep.
    """
    if not stall_sweep_enabled():
        return
    from bot_squad_worker import sessions
    now = time.time()
    pane_map = sessions.live_pane_map()
    cur_user = sessions._get_current_user()
    for slug in cfg.projects:
        try:
            rows = sessions.list_sessions(cfg, slug)
        except Exception:
            log.exception("stall_sweep.tick: list_sessions failed for %s", slug)
            continue
        for row in rows:
            if row.get("status") != "active":
                continue
            # Per-user worker reads only its own live tmux panes.
            if (row.get("linux_user") or cur_user) != cur_user:
                continue
            sid = row.get("sid")
            pane_id = pane_map.get(sid) if sid else None
            if not sid or not pane_id:
                continue
            try:
                check_session(cfg, slug, sid, row, pane_id, now)
            except Exception:
                log.exception("stall_sweep.tick: check failed for %s", sid)
