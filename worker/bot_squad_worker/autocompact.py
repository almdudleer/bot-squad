"""T-0333 / T-0334: self-healing context compaction — a CLOSED LOOP.

Doctrine (docs/design/closed-loops-principle.md): every process must have an
explicit downstream owner that closes it. Context growth used to leak two ways —
(1) telemetry pinged the *human* to "plan a /compact" (a system action dumped on
a person), and (2) the operator session had NO compaction mechanism at all and
grew until the harness surprise-compacted it mid-orchestration.

This module is the owner of that close: when a session's context crosses the
ceiling, the SYSTEM /compacts it. The human is NOT in this loop. The operator
session is a first-class member (T-0334) — no session is exempt from its own
close.

SAFETY: a /compact is only ever sent to an **idle** pane whose composer is ready
(the ``❯`` rune, no "esc to interrupt" mid-generation marker). We never interrupt
active work, a Ctrl-C'd (paused) pane, or a confirmation dialog. A per-session
cooldown stops a re-/compact while a compaction is still in flight (it rotates the
transcript, so context only resets a tick or two later).
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Don't re-/compact a session within this window — a compaction rotates the
# transcript (new claude_uuid) and context only reads back down a tick later.
DEFAULT_COMPACT_COOLDOWN_SEC = 600  # 10 min


def autocompact_enabled() -> bool:
    """Master switch (default ON). ``BOT_SQUAD_AUTOCOMPACT=0`` disables the loop
    (the leak returns, but it's a deliberate operator kill-switch for incidents)."""
    return os.environ.get("BOT_SQUAD_AUTOCOMPACT", "1") != "0"


def compact_cooldown_sec() -> int:
    """Per-session /compact cooldown. Overridable via
    ``BOT_SQUAD_COMPACT_COOLDOWN_SEC``; non-positive/garbage → default."""
    raw = os.environ.get("BOT_SQUAD_COMPACT_COOLDOWN_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_COMPACT_COOLDOWN_SEC


# --- pure decision helpers --------------------------------------------------

def compact_safe(activity: str) -> bool:
    """True only for an ``idle`` pane — Claude finished its turn and is awaiting
    input. ``running`` (mid-turn), ``paused`` (Ctrl-C'd) and ``suspended`` (no
    live pane) are all unsafe to /compact."""
    return activity == "idle"


def compact_due(level: str, fired_at: dict, now: float, cooldown: int | None = None) -> bool:
    """True when context is at the compaction threshold (``urgent`` = at/above the
    ceiling) AND we haven't compacted within the cooldown window."""
    if level != "urgent":
        return False
    if cooldown is None:
        cooldown = compact_cooldown_sec()
    last = (fired_at or {}).get("compact")
    if last is None:
        return True
    try:
        return (now - float(last)) >= cooldown
    except (TypeError, ValueError):
        return True


def composer_ready(buf: str) -> bool:
    """True when the captured pane buffer shows the composer ready for input.

    The ``❯`` rune is rendered by Claude Code's input box only when it accepts
    keystrokes (T-0126). A mid-generation pane shows an "esc to interrupt"
    marker — never /compact then, even if a stale ``❯`` lingers in scrollback.
    """
    if not buf or "❯" not in buf:
        return False
    if "esc to interrupt" in buf.lower():
        return False
    return True


# --- side-effecting seams (monkeypatched in tests) --------------------------

def _pane_for(sid: str) -> str | None:
    from bot_squad_worker.sessions import live_pane_map
    try:
        return live_pane_map().get(sid)
    except Exception:
        log.exception("autocompact: live_pane_map failed for %s", sid)
        return None


def _capture_pane(pane_id: str) -> str:
    from bot_squad_worker.sessions import _run
    try:
        cap = _run(["tmux", "capture-pane", "-t", pane_id, "-p"])
        return cap.stdout if cap.returncode == 0 else ""
    except Exception:
        log.exception("autocompact: capture-pane failed for %s", pane_id)
        return ""


def _send_compact(sid: str) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid, "text": "/compact"})


# --- executor ---------------------------------------------------------------

def maybe_compact(cfg: Any, slug: str, rec: dict, level: str, now: float) -> bool:
    """Auto-/compact ``rec``'s session if it's over-threshold AND safe.

    Returns True iff a ``/compact`` was actually sent (and stamps the cooldown in
    ``rec['alert_fired_at']['compact']`` so the caller persists it). Every gate
    fails closed — an unresolved pane / not-ready composer simply defers to the
    next tick rather than risk interrupting work.
    """
    if not autocompact_enabled():
        return False
    activity = rec.get("activity", "")
    fired = rec.get("alert_fired_at") or {}
    if not (compact_due(level, fired, now) and compact_safe(activity)):
        return False
    sid = rec.get("sid")
    if not sid:
        return False
    pane = _pane_for(sid)
    if not pane:
        return False
    if not composer_ready(_capture_pane(pane)):
        return False
    try:
        _send_compact(sid)
    except Exception:
        log.exception("autocompact: /compact send failed for %s (will retry)", sid)
        return False
    fired["compact"] = now
    rec["alert_fired_at"] = fired
    log.info("autocompact: sent /compact to idle over-ceiling session %s", sid)
    return True
