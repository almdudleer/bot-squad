"""T-0333 / T-0334 / T-0467: self-healing context compaction — a CLOSED LOOP.

Doctrine (docs/design/closed-loops-principle.md): every process must have an
explicit downstream owner that closes it. Context growth used to leak two ways —
(1) telemetry pinged the *human* to "plan a /compact" (a system action dumped on
a person), and (2) the operator session had NO compaction mechanism at all and
grew until the harness surprise-compacted it mid-orchestration.

This module is the owner of that close: when a session's context crosses the
ceiling, the SYSTEM recycles it. The human is NOT in this loop. The operator
session is a first-class member (T-0334) — no session is exempt from its own
close.

Two compaction strategies, both gated on an **idle** pane whose composer is ready
(the ``❯`` rune, no "esc to interrupt" mid-generation marker) — we never interrupt
active work, a Ctrl-C'd (paused) pane, or a confirmation dialog:

* **handoff (T-0467, default)** — OUR custom compact and the process-paradigm
  lifecycle (clarification-01: "the session is asked to write down everything an
  in-system artifact + clear the context"). A 2-phase, cross-tick state machine:

    ARM      over-ceiling + idle + composer-ready & a role artifact resolves →
             inject the HANDOFF prompt (write your full forward-state via
             ``bsq compact-save``) + stamp ``rec['compact'] = {phase: writing}``.
    FINALIZE phase==writing & the artifact was written & the pane is idle →
             ``suspend`` the old pane (CLEAR the context) + relaunch a FRESH
             incarnation that boots from the artifact, re-bound to the SAME
             assignment (binding continuity). No session relies on retained
             in-context memory across the compact.

  The boot-from-artifact reload path (``boot_prompt_from_artifact`` /
  ``_relaunch_from_artifact``) is factored for reuse by T-0471 (crash recovery).

* **claude (legacy, fallback)** — Claude's in-context ``/compact``. Used when no
  role artifact resolves, when the handoff times out (never wedge), or when
  ``BOT_SQUAD_COMPACT_MODE=claude`` forces it. A per-session cooldown stops a
  re-/compact while a compaction is still in flight (it rotates the transcript,
  so context only resets a tick or two later).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from bot_squad_worker import assignment, recycle_gate

log = logging.getLogger(__name__)

# Don't re-/compact a session within this window — a compaction rotates the
# transcript (new claude_uuid) and context only reads back down a tick later.
DEFAULT_COMPACT_COOLDOWN_SEC = 600  # 10 min

# How long to wait for a session to write its forward-state after the handoff
# prompt before falling back to Claude's /compact (never wedge over-ceiling).
DEFAULT_HANDOFF_TIMEOUT_SEC = 900  # 15 min


def autocompact_enabled() -> bool:
    """Master switch (default ON). ``BOT_SQUAD_AUTOCOMPACT=0`` disables the loop
    (the leak returns, but it's a deliberate operator kill-switch for incidents)."""
    return os.environ.get("BOT_SQUAD_AUTOCOMPACT", "1") != "0"


def compact_mode() -> str:
    """Which compaction strategy to use: ``handoff`` (T-0467, default) or
    ``claude`` (legacy in-context /compact). ``BOT_SQUAD_COMPACT_MODE`` overrides;
    anything unrecognised falls back to ``handoff`` so a typo can't silently
    disable the paradigm lifecycle."""
    v = (os.environ.get("BOT_SQUAD_COMPACT_MODE") or "handoff").strip().lower()
    return v if v in ("handoff", "claude") else "handoff"


def handoff_timeout_sec() -> int:
    """Deadline for a session to write its forward-state after being asked.
    Overridable via ``BOT_SQUAD_HANDOFF_TIMEOUT_SEC``; non-positive/garbage →
    default."""
    raw = os.environ.get("BOT_SQUAD_HANDOFF_TIMEOUT_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_HANDOFF_TIMEOUT_SEC


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


# --- prompts (the "ask the session to write everything down" + the reload) --

def handoff_prompt(artifact_path: str, role: str | None = None) -> str:
    """The COMPACT HANDOFF — ask the session to dump its full forward-state into
    its role artifact, then signal done. The system relaunches it fresh after.

    ``role`` grafts role-specific shape guidance onto the generic handoff (T-0473:
    an operator must write the future-focused state-doc schema, not a free dump).
    The graft is a PURE ADDITION — for any role with no
    :func:`assignment.role_compact_guidance` (dev/teamlead/None) the returned
    prompt is byte-identical to the role-agnostic T-0467 handoff."""
    base = (
        "⏳ CONTEXT FULL — COMPACT HANDOFF. Per the process-paradigm lifecycle you "
        "are about to be relaunched as a FRESH incarnation with an EMPTY context. "
        "NOTHING from this conversation survives EXCEPT what you write to your role "
        "artifact now.\n\n"
        "Write your COMPLETE forward-state so your successor continues seamlessly: "
        "the assignment/goal, what's DONE, what's IN PROGRESS, the EXACT next "
        "steps, key file paths + decisions + gotchas, and anything you'd need if "
        "you woke up fresh. Be exhaustive — this is your only memory.\n\n"
        "Run exactly:\n"
        '  bsq compact-save "<your full forward-state markdown>"\n\n'
        f"(It full-replaces your role artifact at {artifact_path}.) After it "
        "returns ok, reply: HANDOFF WRITTEN. The system then relaunches you fresh."
    )
    guidance = assignment.role_compact_guidance(role)
    return base + ("\n\n" + guidance if guidance else "")


def boot_prompt_from_artifact(*, role: str | None, assignment_id: str | None,
                              artifact_path: str) -> str:
    """The reload prompt for a fresh incarnation booting from a role artifact.

    Factored + reusable: T-0471 (crash recovery) boots an ungracefully-crashed
    session through this SAME recover-from-artifact path.
    """
    who = f"the {role} role" if role else "your role"
    asg = f" for assignment {assignment_id}" if assignment_id else ""
    return (
        f"You are a FRESH incarnation continuing {who}{asg}. Your predecessor's "
        "context was full and has been cleared — NOTHING from it survives. Your "
        "ONLY memory is the role artifact your predecessor wrote at:\n"
        f"  {artifact_path}\n"
        "READ THAT FILE FIRST, in full, before doing anything else. It holds the "
        "complete forward-state (goal, what's done, what's in progress, the exact "
        "next steps, key paths/decisions/gotchas). Then continue the work from "
        "there — do not restart already-finished work."
    )


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


def _inject_handoff(sid: str, artifact_path: str, role: str | None = None) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid, "text": handoff_prompt(artifact_path, role)})


def _artifact_mtime(path: str | None) -> float:
    if not path:
        return 0.0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _resolve_role_artifact(cfg: Any, slug: str, rec: dict):
    """Resolve ``(artifact_path|None, role, assignment_id|None)`` for a session's
    role artifact — the compact destination. ``None`` path ⇒ no destination ⇒ the
    caller falls back to Claude's /compact."""
    role = rec.get("role") or ""
    task_id = rec.get("task_id")
    sid = rec.get("sid")
    art = assignment.role_artifact(cfg.data_dir, slug, role=role, sid=sid,
                                   task_id=task_id)
    if art is None:
        return (None, role, None)
    task_bound = bool(task_id) and task_id != "~"
    assignment_id = task_id if task_bound else (role or None)
    return (str(art.path), role, assignment_id)


def _suspend_session(cfg: Any, slug: str, sid: str) -> None:
    """CLEAR the context — close the over-ceiling pane. (T-0467 step 2.)"""
    from bot_squad_worker import sessions
    sessions.suspend(cfg, slug, sid, source="autocompact",
                     reason="context-full compact handoff")


def _alert_orphaned_handoff(cfg: Any, slug: str, sid: str, reason: str) -> None:
    """Best-effort operator alert when a handoff cleared the old pane but the
    relaunch failed — telemetry won't re-sample a paneless session, so without
    this the assignment would orphan silently (mirrors recovery.py park-notify)."""
    try:
        from bot_squad_worker import intersession
        intersession.send(
            cfg, slug, to="operator", from_sid="S-autocompact",
            text=(f"⚠️ compact handoff for {sid} cleared the pane but the relaunch "
                  f"failed ({reason}). The assignment needs a manual respawn — "
                  f"the forward-state artifact is intact."))
    except Exception:
        log.exception("autocompact: orphaned-handoff alert failed for %s", sid)


def _relaunch_from_artifact(cfg: Any, slug: str, rec: dict, artifact_path: str) -> None:
    """Relaunch a FRESH incarnation booting from the role artifact, re-bound to
    the SAME assignment (binding continuity — task_id/initiative/parent_sid/owner
    carried from the predecessor's session md so reconcilers don't see an orphan)."""
    from bot_squad_worker import sessions

    sid = rec.get("sid")
    meta = sessions._read_session_metadata(
        sessions._session_file(cfg.data_dir, slug, sid)) or {}

    def _clean(v):
        return v if (v and v != "~") else None

    task_id = _clean(rec.get("task_id") or meta.get("task_id"))
    role = rec.get("role") or meta.get("role") or ""
    assignment_id = task_id if task_id else (role or None)
    window = (_clean(meta.get("window")) or _clean(rec.get("window"))
              or f"recover-{assignment_id or role or 'session'}")
    prompt = boot_prompt_from_artifact(role=role, assignment_id=assignment_id,
                                       artifact_path=artifact_path)
    sessions.spawn(
        cfg, slug, window, prompt,
        task_id=task_id,
        initiative=_clean(meta.get("initiative")),
        parent_sid=_clean(meta.get("parent_sid")),
        owner=_clean(meta.get("owner")),
        owner_user=_clean(meta.get("owner_user")),
    )


# --- executor ---------------------------------------------------------------

def _do_claude_compact(cfg: Any, slug: str, rec: dict, now: float) -> bool:
    """Legacy fallback — send Claude's in-context ``/compact`` to an idle,
    composer-ready pane and stamp the per-session cooldown."""
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
    fired = rec.get("alert_fired_at") or {}
    fired["compact"] = now
    rec["alert_fired_at"] = fired
    log.info("autocompact: sent /compact to idle over-ceiling session %s", sid)
    return True


def _maybe_finalize(cfg: Any, slug: str, rec: dict, compact: dict, now: float) -> bool:
    """A handoff is in flight (``phase == writing``). Finalize once the session
    has written its artifact and the pane is idle; fall back on timeout."""
    sid = rec.get("sid")
    armed_at = float(compact.get("armed_at", now))
    arm_mtime = float(compact.get("arm_mtime", 0.0))
    artifact_path = compact.get("artifact_path")

    # Never wedge: the session didn't write its state in time → drop the handoff
    # and let Claude's /compact take the context back down.
    if now - armed_at > handoff_timeout_sec():
        rec["compact"] = {}
        log.warning("autocompact: handoff timed out for %s — falling back to "
                    "/compact", sid)
        return _do_claude_compact(cfg, slug, rec, now)

    # Wait until the session has actually written its forward-state.
    if _artifact_mtime(artifact_path) <= arm_mtime:
        return False

    # Only clear an idle, composer-ready pane (don't cut mid-turn).
    if not compact_safe(rec.get("activity", "")):
        return False
    pane = _pane_for(sid)
    if pane and not composer_ready(_capture_pane(pane)):
        return False

    # CLEAR + RELAUNCH: close the old pane, boot a fresh incarnation from the
    # artifact, re-bound to the same assignment.
    try:
        _suspend_session(cfg, slug, sid)
    except Exception:
        log.exception("autocompact: finalize suspend failed for %s — retry next "
                      "tick", sid)
        return False
    try:
        _relaunch_from_artifact(cfg, slug, rec, artifact_path)
    except Exception as e:
        # The old pane is already gone and won't be re-sampled — alert the
        # operator instead of orphaning the assignment silently.
        log.exception("autocompact: relaunch failed for %s after clear", sid)
        _alert_orphaned_handoff(cfg, slug, sid, repr(e))
        return False
    rec["compact"] = {}
    log.info("autocompact: compact handoff complete for %s — relaunched fresh "
             "from %s", sid, artifact_path)
    return True


def maybe_compact(cfg: Any, slug: str, rec: dict, level: str, now: float) -> bool:
    """Recycle ``rec``'s session if it's over-threshold AND safe.

    Two strategies (see module docstring): the T-0467 write-to-artifact handoff
    (default) and the legacy Claude ``/compact`` fallback. Returns True iff an
    action was taken this tick (handoff armed, handoff finalized, or /compact
    sent). Every gate fails closed — an unresolved pane / not-ready composer
    simply defers to the next tick rather than risk interrupting work.
    """
    if not autocompact_enabled():
        return False
    sid = rec.get("sid")
    # T-0563/T-0564/T-0616 (recycle-v2): never recycle a non-allowlisted
    # project, any of the human's own sessions (user-conversation role,
    # hand-launched user-session window, recycle_exempt md marker — the
    # telemetry rec doesn't carry the marker, so read the session md
    # best-effort), or a pane a human is currently attached to — even the
    # operator (T-0334) is subject to this gate.
    from bot_squad_worker import sessions as _sessions
    meta = {}
    if sid:
        try:
            meta = _sessions._read_session_metadata(
                _sessions._session_file(cfg.data_dir, slug, sid)) or {}
        except Exception:
            meta = {}
    if not recycle_gate.recycle_allowed(
        cfg, slug=slug, role=rec.get("role") or meta.get("role"),
        tmux_target=_pane_for(sid) if sid else None, now=now,
        window=rec.get("window") or meta.get("window"), meta=meta,
    ):
        return False
    fired = rec.get("alert_fired_at") or {}
    rec["alert_fired_at"] = fired

    # A handoff already in flight → drive its finalize half (independent of the
    # cooldown; the phase dict is its own guard).
    compact = rec.get("compact") or {}
    if compact.get("phase") == "writing":
        return _maybe_finalize(cfg, slug, rec, compact, now)

    # Otherwise decide whether to START a compact this tick.
    if not (compact_due(level, fired, now) and compact_safe(rec.get("activity", ""))):
        return False
    sid = rec.get("sid")
    if not sid:
        return False
    pane = _pane_for(sid)
    if not pane:
        return False
    if not composer_ready(_capture_pane(pane)):
        return False

    # Handoff mode + a resolvable role artifact → ARM the write-to-artifact flow.
    if compact_mode() == "handoff":
        artifact_path, role, assignment_id = _resolve_role_artifact(cfg, slug, rec)
        if artifact_path:
            try:
                _inject_handoff(sid, artifact_path, role)
            except Exception:
                log.exception("autocompact: handoff inject failed for %s (will "
                              "retry)", sid)
                return False
            rec["compact"] = {
                "phase": "writing",
                "armed_at": now,
                "arm_mtime": _artifact_mtime(artifact_path),
                "artifact_path": artifact_path,
                "role": role,
                "assignment_id": assignment_id,
            }
            log.info("autocompact: armed compact handoff for %s → %s", sid,
                     artifact_path)
            return True

    # No role artifact (or claude mode) → legacy /compact fallback.
    return _do_claude_compact(cfg, slug, rec, now)
