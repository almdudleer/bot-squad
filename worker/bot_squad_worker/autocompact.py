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

    ARM      over-ceiling + idle + composer-ready & a handoff destination
             resolves → inject the HANDOFF prompt (write your full forward-state
             THERE) + stamp ``rec['compact'] = {phase: writing}``.
    FINALIZE phase==writing & the forward-state was written & the pane is idle →
             ``suspend`` the old pane (CLEAR the context) + relaunch a FRESH
             incarnation that boots from what was written, re-bound to the SAME
             assignment (binding continuity). No session relies on retained
             in-context memory across the compact.

  **WHERE the forward-state goes is decided by whether the session owns a TASK**
  (T-0863, stakeholder 2026-08-11): *«он должен просто обновлять контекст по
  задаче, и всё, никаких файлов, никаких notes, на одну задачу один артефакт —
  контекст, он же на тикете в UI виден»*.

    task-bound  → the ticket's own ``## Context`` (T-0767's ``task_context_set``
                  writer, driven by the session as ``bsq ticket context <id>``).
                  ONE artifact per task, already rendered on the board. NO
                  ``artifacts/<task_id>.md``, NO ``bsq compact-save``, and NO
                  auto ``## Progress`` note.
    task-less   → the role artifact, unchanged: an operator's state-doc
                  (``artifacts/operator-state.md``, T-0473) or a per-assignment
                  ``artifacts/role-<role>-<window>.md``. These sessions have no
                  task to write a Context onto, so the artifact seam is what
                  still gives them a home (see :func:`_resolve_compact_target`).

  The reload paths (``boot_prompt_from_ticket`` / ``boot_prompt_from_artifact``,
  ``_relaunch_from_ticket`` / ``_relaunch_from_artifact``) mirror that split;
  the artifact half is factored for reuse by T-0471 (crash recovery).

* **claude (legacy, fallback)** — Claude's in-context ``/compact``. Used when no
  handoff destination resolves at all, when the handoff times out (never wedge),
  or when ``BOT_SQUAD_COMPACT_MODE=claude`` forces it. A per-session cooldown stops a
  re-/compact while a compaction is still in flight (it rotates the transcript,
  so context only resets a tick or two later).

Neither strategy above ever runs against the human's own exempt sessions
(:func:`recycle_gate.user_session_exempt`) — T-0649 (2026-07-18) gives them a
third path instead: :func:`_maybe_compact_stay_ceiling` reuses
:mod:`idle_timeout`'s T-0617 compact-in-place machine (``compact_stay_*`` md
fields), so the ceiling trigger built here and the idle-window trigger built
there share ONE anti-loop guard (``compact_stay_last_at``) and can never
double-compact the same session. Worker sessions (dev/TL/operator) are
unaffected — the stakeholder's "autocompact loop is worse" complaint never
targeted them, and the handoff/artifact model above IS their designed
continuity mechanism.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
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


def composer_ready(buf: str, *, sid: str | None = None,
                   now: float | None = None) -> bool:
    """True when the captured pane buffer shows the composer ready for input.

    The ``❯`` rune is rendered by Claude Code's input box only when it accepts
    keystrokes (T-0126). A mid-generation pane shows an "esc to interrupt"
    marker — never /compact then, even if a stale ``❯`` lingers in scrollback.

    T-0864: pass ``sid`` (and the tick's ``now``) from a RECYCLE path to get one
    debounced INFO line naming which of the two reasons deferred this tick — a
    permission dialog or a stale render can hold this gate closed for hours, and
    it used to be as silent as ``recycle_gate.is_attached``. Callers that are
    not a recycle gate — ``input_mux``, which reuses this predicate per keystroke
    batch to decide whether a pane can be typed into — pass no ``sid`` and log
    nothing; a "not ready" there is the normal case, not a deferred recycle.
    The return value is unchanged either way: this stays a pure predicate over
    ``buf``.
    """
    if not buf or "❯" not in buf:
        _log_not_composer_ready(sid, "no ❯ composer rune in the captured pane "
                                "(empty capture, or a dialog/alt-screen over it)",
                                now)
        return False
    if "esc to interrupt" in buf.lower():
        _log_not_composer_ready(sid, "pane is mid-generation "
                                "('esc to interrupt' on screen)", now)
        return False
    return True


def _log_not_composer_ready(sid: str | None, reason: str,
                            now: float | None) -> None:
    """One debounced INFO line per session per window, matching
    ``recycle_gate.project_allowed``'s pattern (T-0864 DoD 3)."""
    if not sid:
        return
    if recycle_gate.should_log_skip(f"composer:{sid}", now):
        log.info("recycle: %s not composer-ready — %s; gate deferred this tick",
                 sid, reason)


# --- prompts (the "ask the session to write everything down" + the reload) --

def handoff_prompt(artifact_path: str, role: str | None = None, *,
                   relaunch: bool = True) -> str:
    """The COMPACT HANDOFF — ask a TASK-LESS session to dump its full
    forward-state into its role artifact, then signal done.

    (A task-bound session takes :func:`context_handoff_prompt` instead — T-0863
    routes its forward-state to the ticket's ``## Context``.)

    ``role`` grafts role-specific shape guidance onto the generic handoff (T-0473:
    an operator must write the future-focused state-doc schema, not a free dump).
    The graft is a PURE ADDITION — for any role with no
    :func:`assignment.role_compact_guidance` (dev/teamlead/None) the returned
    prompt is byte-identical to the role-agnostic T-0467 handoff.

    ``relaunch=False`` is the ~1h idle-window trigger, which STOPS the session
    rather than booting a successor from what it wrote. Only the closing
    sentence differs, and it has to: telling a session it is about to be
    relaunched when it is about to be ended is the kind of small lie that
    changes what it bothers to write down.
    """
    head = (
        "⏳ CONTEXT FULL — COMPACT HANDOFF. Per the process-paradigm lifecycle you "
        "are about to be relaunched as a FRESH incarnation with an EMPTY context. "
        if relaunch else
        "⏳ CACHE WINDOW EXPIRING — COMPACT HANDOFF. Per the process-paradigm "
        "lifecycle this session is about to be ENDED. "
    )
    tail = ("The system then relaunches you fresh." if relaunch else
            "The system then ends this session; a later incarnation boots from "
            "what you wrote.")
    base = (
        f"{head}"
        "NOTHING from this conversation survives EXCEPT what you write to your role "
        "artifact now.\n\n"
        "Write your COMPLETE forward-state so your successor continues seamlessly: "
        "the assignment/goal, what's DONE, what's IN PROGRESS, the EXACT next "
        "steps, key file paths + decisions + gotchas, and anything you'd need if "
        "you woke up fresh. Be exhaustive — this is your only memory.\n\n"
        "Run exactly:\n"
        '  bsq compact-save "<your full forward-state markdown>"\n\n'
        f"(It full-replaces your role artifact at {artifact_path}.) After it "
        f"returns ok, reply: HANDOFF WRITTEN. {tail}"
    )
    guidance = assignment.role_compact_guidance(role)
    return base + ("\n\n" + guidance if guidance else "")


def context_handoff_prompt(task_id: str, *, relaunch: bool) -> str:
    """The FINALIZE handoff for a TASK-BOUND session — write the forward-state
    into the ticket's own ``## Context`` (T-0863).

    Replaces the ``bsq compact-save`` → ``artifacts/<task_id>.md`` indirection
    for every session that owns a task, on BOTH triggers (context ceiling and
    the ~1h idle window). The stakeholder's correction, verbatim: *«bsq
    compact-save, ждёт файл — кажется, тут лишний шаг, устаревший, он должен
    просто обновлять контекст по задаче, и всё, никаких файлов, никаких notes,
    на одну задачу один артефакт — контекст»*.

    ``relaunch`` is the only difference between the two triggers, and the
    session is told which one it is because the two mean different things to
    the successor: the ceiling path clears this pane and boots a fresh
    incarnation immediately, the idle path just stops (a later re-drive picks
    the ticket up). Either way the ticket is the whole inheritance.

    The prompt names ``## Stakeholder notes`` as off-limits on purpose — this
    is a write against the same md that holds his words, and the one failure
    mode worth a sentence of prompt is a degraded, context-full session
    rewriting them (T-0729 shipped exactly that, on 414 tickets).
    """
    after = (
        "The system then clears this pane and relaunches you FRESH on the same "
        "ticket — the Context you just wrote is what you will wake up holding."
        if relaunch else
        "The system then ENDS this session. A later session re-drives "
        f"{task_id} starting from the Context you just wrote."
    )
    return (
        f"⏳ FINALIZE — WRITE YOUR FORWARD-STATE ONTO {task_id}. Per the "
        "process-paradigm lifecycle this incarnation is ending now. NOTHING "
        "from this conversation survives EXCEPT what you write onto the "
        "ticket.\n\n"
        "Run exactly:\n"
        f"  bsq ticket context {task_id} --file <file holding the new Context>\n\n"
        "That REPLACES the ticket's `## Context` — the shared working area, "
        "which is also what the board renders. So write the CURRENT state, not "
        "a diary: the goal, what is DONE, what is IN PROGRESS, the EXACT next "
        "steps, key file paths, decisions and gotchas. Carry over everything in "
        "the existing Context that is still true and drop what is not. Be "
        "exhaustive — this is your successor's only memory.\n\n"
        "One ticket, one artifact: do NOT write a handoff file, and do NOT file "
        "a progress note about this — the Context IS the handoff. Do NOT touch "
        "`## Stakeholder notes`; those are his words and are human-only.\n\n"
        f"After it returns ok, reply: CONTEXT WRITTEN. {after}"
    )


def boot_prompt_from_ticket(*, role: str | None, task_id: str,
                            task_md_path: str | None = None) -> str:
    """The reload prompt for a fresh incarnation of a TASK-BOUND session.

    Counterpart to :func:`boot_prompt_from_artifact` for the ticket-Context
    handoff: the predecessor wrote its forward-state into ``## Context``, so
    that is where the successor is pointed — not at an artifact file.
    """
    who = f"the {role} role" if role else "your role"
    where = f"\n  {task_md_path}" if task_md_path else ""
    return (
        f"You are a FRESH incarnation continuing {who} on {task_id}. Your "
        "predecessor's context was cleared — NOTHING from it survives. Your "
        f"ONLY memory is what it wrote into {task_id}'s `## Context`:{where}\n"
        f"READ {task_id} FIRST, in full, before doing anything else — "
        "`## Stakeholder notes` is the ask in his own words (the anchor) and "
        "`## Context` is the complete forward-state (what is done, what is in "
        "progress, the exact next steps, key paths/decisions/gotchas). Then "
        "continue from there — do not restart already-finished work."
    )


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


def _inject_handoff(sid: str, artifact_path: str, role: str | None = None, *,
                    relaunch: bool = True) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid,
                          "text": handoff_prompt(artifact_path, role,
                                                 relaunch=relaunch)})


def _inject_context_handoff(sid: str, task_id: str, *, relaunch: bool) -> None:
    from bot_squad_worker.actions import _action_inject_input
    _action_inject_input({"sid": sid,
                          "text": context_handoff_prompt(task_id, relaunch=relaunch)})


def _artifact_mtime(path: str | None) -> float:
    if not path:
        return 0.0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# The frontmatter split every ticket reader must use. NOT ``split("---", 2)``:
# a title containing ` --- ` (T-0286 has one) makes that read the status as
# empty and the body as a fragment.
_TICKET_FM_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


def task_md_path(cfg: Any, slug: str, task_id: str | None) -> str | None:
    """Absolute path of the ticket md a task-bound session finalizes INTO.

    ``None`` for an unbound session (``task_id`` absent or the ``~`` placeholder
    a session md carries when nothing is bound) and for an id that resolves to
    no file — both mean "no Context destination", and the caller falls back to
    the role artifact.
    """
    tid = (task_id or "").strip()
    if not tid or tid == "~":
        return None
    try:
        from bot_squad_worker import frontmatter as _fm
        p = _fm.resolve_id_file(Path(cfg.data_dir) / slug / "backlog", tid,
                                strict=True)
    except Exception:
        log.exception("autocompact: could not resolve ticket md for %s", tid)
        return None
    return str(p) if p is not None else None


def context_digest(task_md_path_: str | None) -> str:
    """Digest of a ticket's ``## Context`` — the ARM-time snapshot FINALIZE
    compares against to know the session actually wrote its forward-state.

    Deliberately the SECTION's content, not the file's mtime (which is what the
    artifact handoff watches). The ticket md is a shared file: a peer session
    filing a progress note, a stakeholder quote, or a status change all bump its
    mtime, and any of those would have read as "the handoff is written" and
    terminated the session with nothing recorded — the exact loss this path
    exists to prevent. Comparing the Context itself asserts the claim.

    Unreadable / missing file → ``""``. An arm-time ``""`` that stays ``""``
    simply never satisfies FINALIZE, so the bounded timeout takes over rather
    than a missing ticket looking like a completed write.
    """
    if not task_md_path_:
        return ""
    try:
        text = Path(task_md_path_).read_text()
    except OSError:
        return ""
    m = _TICKET_FM_RE.match(text)
    body = m.group(2) if m else text
    from bot_squad_worker.task_body import parse_body
    ctx = parse_body(body).get("context", "")
    return hashlib.sha256(ctx.encode("utf-8")).hexdigest()


def _resolve_compact_target(cfg: Any, slug: str, rec: dict) -> dict:
    """Where this session's forward-state goes at finalize (T-0863).

    ``kind`` is the whole decision, and it turns on ONE fact — does the session
    own a task:

      ``context``  a task-bound session → that ticket's ``## Context``. One
                   task, one artifact; it is already on the board.
      ``artifact`` a task-LESS session (operator state-doc, or any other role's
                   per-assignment file) → the role artifact, unchanged. The
                   stakeholder's "никаких файлов" is scoped to *«контекст по
                   задаче»* — a session with no task has no Context to write, so
                   removing the artifact here would leave it with no home at all.
      ``none``     neither resolves → caller falls back to Claude's ``/compact``.
    """
    role = rec.get("role") or ""
    task_id = (rec.get("task_id") or "").strip()
    task_bound = bool(task_id) and task_id != "~"

    if task_bound:
        tmd = task_md_path(cfg, slug, task_id)
        if tmd:
            return {"kind": "context", "task_id": task_id, "task_md": tmd,
                    "role": role, "assignment_id": task_id}
        # Bound to an id whose md is gone/ambiguous. Falling through to the
        # role artifact would resolve it to `artifacts/<task_id>.md` — the
        # exact file this ticket removed — so take the /compact fallback
        # instead of resurrecting the retired destination.
        log.warning("autocompact: %s is bound to %s but its md did not resolve "
                    "— no Context destination, falling back to /compact",
                    rec.get("sid"), task_id)
        return {"kind": "none", "role": role, "assignment_id": task_id}

    artifact_path, role, assignment_id = _resolve_role_artifact(cfg, slug, rec)
    if artifact_path:
        return {"kind": "artifact", "artifact_path": artifact_path,
                "role": role, "assignment_id": assignment_id}
    return {"kind": "none", "role": role, "assignment_id": None}


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
        # T-0827: send_notice — this is the alert for a handoff that ALREADY
        # failed; losing it silently is how the assignment orphans unseen,
        # which is the exact thing this alert exists to prevent.
        intersession.send_notice(
            cfg, slug, to="operator", from_sid="S-autocompact",
            text=(f"⚠️ compact handoff for {sid} cleared the pane but the relaunch "
                  f"failed ({reason}). The assignment needs a manual respawn — "
                  f"the forward-state artifact is intact."))
    except Exception:
        log.exception("autocompact: orphaned-handoff alert failed for %s", sid)


def _relaunch(cfg: Any, slug: str, rec: dict, make_prompt) -> None:
    """Relaunch a FRESH incarnation re-bound to the SAME assignment (binding
    continuity — task_id/initiative/parent_sid/owner carried from the
    predecessor's session md so reconcilers don't see an orphan).

    ``make_prompt(role, task_id, assignment_id)`` supplies the boot prompt, so
    the ticket-Context and role-artifact relaunches share every line of the
    binding-continuity logic and differ only in what the successor is told to
    read.
    """
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
    sessions.spawn(
        cfg, slug, window, make_prompt(role, task_id, assignment_id),
        task_id=task_id,
        initiative=_clean(meta.get("initiative")),
        parent_sid=_clean(meta.get("parent_sid")),
        owner=_clean(meta.get("owner")),
        owner_user=_clean(meta.get("owner_user")),
    )


def _relaunch_from_ticket(cfg: Any, slug: str, rec: dict, task_md: str | None) -> None:
    """T-0863: relaunch a task-bound session pointed at its TICKET — the
    predecessor's forward-state is that ticket's ``## Context``, not a file."""
    _relaunch(cfg, slug, rec, lambda role, task_id, assignment_id:
              boot_prompt_from_ticket(
                  role=role, task_id=task_id or assignment_id or "",
                  task_md_path=task_md))


def _relaunch_from_artifact(cfg: Any, slug: str, rec: dict, artifact_path: str) -> None:
    """Relaunch a FRESH incarnation booting from the role artifact — the
    task-LESS half (operator state-doc / per-assignment role file), and the path
    T-0471 crash recovery reuses for any session that still has an artifact."""
    _relaunch(cfg, slug, rec, lambda role, task_id, assignment_id:
              boot_prompt_from_artifact(role=role, assignment_id=assignment_id,
                                        artifact_path=artifact_path))


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
    if not composer_ready(_capture_pane(pane), sid=sid, now=now):
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


def handoff_mark(target: dict) -> str:
    """A destination's current state folded into ONE string.

    The idle-window recycler keeps its in-flight state as FLAT scalar session-md
    fields (the line-based ``session_start.sh`` reader cannot hold a nested
    mapping), so it snapshots the destination as this single value at ARM and
    re-derives it at FINALIZE: a changed mark means the session wrote. The
    ceiling recycler keeps a nested ``rec['compact']`` dict instead and reads
    the same two signals directly — see :func:`handoff_written`.
    """
    if target.get("kind") == "context":
        return "ctx:" + context_digest(target.get("task_md"))
    if target.get("kind") == "artifact":
        return "mtime:%r" % _artifact_mtime(target.get("artifact_path"))
    return ""


def handoff_written(compact: dict) -> bool:
    """True once the in-flight handoff's forward-state has actually landed.

    The two destinations are checked by the signal that is honest for each:
    a ticket ``## Context`` by its own content (a shared md's mtime moves for
    reasons that are not this write — see :func:`context_digest`), a role
    artifact by mtime (a private file only its own session writes).
    """
    if compact.get("kind") == "context":
        armed = compact.get("arm_digest", "")
        now_digest = context_digest(compact.get("task_md"))
        return bool(now_digest) and now_digest != armed
    return _artifact_mtime(compact.get("artifact_path")) > float(
        compact.get("arm_mtime", 0.0))


def _maybe_finalize(cfg: Any, slug: str, rec: dict, compact: dict, now: float) -> bool:
    """A handoff is in flight (``phase == writing``). Finalize once the session
    has written its forward-state and the pane is idle; fall back on timeout.

    T-0863: ``compact['kind']`` selects the destination — ``context`` (the
    ticket's ``## Context``, for a task-bound session) or ``artifact`` (the role
    artifact, for a task-less one). A record with no ``kind`` is an in-flight
    handoff armed by the previous worker build and is finalized as ``artifact``,
    the only thing it can be.
    """
    sid = rec.get("sid")
    armed_at = float(compact.get("armed_at", now))
    kind = compact.get("kind") or "artifact"

    # Never wedge: the session didn't write its state in time → drop the handoff
    # and let Claude's /compact take the context back down.
    if now - armed_at > handoff_timeout_sec():
        rec["compact"] = {}
        log.warning("autocompact: handoff timed out for %s — falling back to "
                    "/compact", sid)
        return _do_claude_compact(cfg, slug, rec, now)

    # Wait until the session has actually written its forward-state.
    if not handoff_written(compact):
        return False

    # Only clear an idle, composer-ready pane (don't cut mid-turn).
    if not compact_safe(rec.get("activity", "")):
        return False
    pane = _pane_for(sid)
    if pane and not composer_ready(_capture_pane(pane), sid=sid, now=now):
        return False

    # CLEAR + RELAUNCH: close the old pane, boot a fresh incarnation from what
    # the predecessor wrote, re-bound to the same assignment.
    try:
        _suspend_session(cfg, slug, sid)
    except Exception:
        log.exception("autocompact: finalize suspend failed for %s — retry next "
                      "tick", sid)
        return False
    wrote_to = (compact.get("task_id") if kind == "context"
                else compact.get("artifact_path"))
    try:
        if kind == "context":
            _relaunch_from_ticket(cfg, slug, rec, compact.get("task_md"))
        else:
            _relaunch_from_artifact(cfg, slug, rec, compact.get("artifact_path"))
    except Exception as e:
        # The old pane is already gone and won't be re-sampled — alert the
        # operator instead of orphaning the assignment silently.
        log.exception("autocompact: relaunch failed for %s after clear", sid)
        _alert_orphaned_handoff(cfg, slug, sid, repr(e))
        return False
    rec["compact"] = {}
    log.info("autocompact: compact handoff complete for %s — relaunched fresh "
             "from %s", sid, wrote_to)
    return True


def _maybe_compact_stay_ceiling(sid: str, meta: dict, md_path, level: str, now: float,
                                pane: str | None) -> bool:
    """T-0649: the ceiling-trigger counterpart to :mod:`idle_timeout`'s T-0617
    compact-and-stay. Drives the SAME ``compact_stay_phase`` /
    ``compact_stay_armed_at`` / ``compact_stay_last_at`` session-md fields as
    the idle-window trigger — sharing that state (not forking it) is what
    makes ``compact_stay_last_at`` an effective anti-loop guard across BOTH
    triggers: whichever one compacts first blocks the other for the rest of
    the cache window. Only the ARM condition differs from idle_timeout's own
    :func:`idle_timeout._maybe_compact_and_stay`: this arms on the context
    ceiling (``level == 'urgent'``), not an idle window elapsing — a
    ceiling-triggered compact needs to fire immediately, same as it always has
    for worker sessions, not wait out an idle clock. NEVER calls
    ``sessions.suspend`` — the pane/tmux/process stay exactly where the human
    left them, same guarantee as T-0617.
    """
    from bot_squad_worker import idle_timeout, sessions as _sessions

    if meta.get("compact_stay_phase") == "compacting":
        return idle_timeout._finalize_compact_stay(sid, meta, md_path, now, pane)

    if level != "urgent":
        return False
    if not idle_timeout.compact_stay_due(meta.get("compact_stay_last_at"), now,
                                         idle_timeout.idle_timeout_sec()):
        return False  # already compacted-and-stayed this cache window
    if not pane or not composer_ready(_capture_pane(pane), sid=sid, now=now):
        return False

    try:
        _send_compact(sid)
    except Exception:
        log.exception("autocompact: ceiling compact-and-stay /compact send "
                      "failed for %s (will retry)", sid)
        return False
    meta["compact_stay_phase"] = "compacting"
    meta["compact_stay_armed_at"] = idle_timeout._now_iso()
    _sessions._write_session_metadata(md_path, meta, atomic=True)
    log.info("autocompact: ceiling compact-and-stay sent /compact to %s — "
             "session stays, no relaunch", sid)
    return True


def maybe_compact(cfg: Any, slug: str, rec: dict, level: str, now: float) -> bool:
    """Recycle ``rec``'s session if it's over-threshold AND safe.

    Two strategies for WORKER (dev/TL/operator) sessions (see module
    docstring): the T-0467 write-to-artifact handoff (default) and the legacy
    Claude ``/compact`` fallback. The human's own exempt sessions (T-0616)
    never ride either — T-0649 routes their ceiling trigger to T-0617's
    compact-in-place mechanism instead (see :func:`_maybe_compact_stay_ceiling`).
    Returns True iff an action was taken this tick (handoff armed, handoff
    finalized, compact-and-stay armed/finalized, or /compact sent). Every gate
    fails closed — an unresolved pane / not-ready composer simply defers to the
    next tick rather than risk interrupting work.
    """
    if not autocompact_enabled():
        return False
    sid = rec.get("sid")
    # T-0563/T-0564/T-0616 (recycle-v2): never recycle a non-allowlisted
    # project, or touch a pane a human is currently attached to — even the
    # operator (T-0334) is subject to this gate. The telemetry rec doesn't
    # carry the recycle_exempt md marker, so read the session md best-effort.
    from bot_squad_worker import sessions as _sessions
    meta: dict = {}
    md_path = None
    if sid:
        try:
            md_path = _sessions._session_file(cfg.data_dir, slug, sid)
            meta = _sessions._read_session_metadata(md_path) or {}
        except Exception:
            meta = {}
            md_path = None
    if not recycle_gate.project_allowed(cfg, slug, now):
        return False
    pane = _pane_for(sid) if sid else None
    if recycle_gate.is_attached(pane, sid=sid, now=now):
        return False

    role = rec.get("role") or meta.get("role")
    window = rec.get("window") or meta.get("window")

    # T-0649: the human's own sessions (user-conversation role, hand-launched
    # user-session window, recycle_exempt md marker) never ride the
    # handoff/terminate machinery below — their ceiling trigger reuses
    # idle_timeout's T-0617 compact-in-place state instead, sharing its
    # compact_stay_last_at anti-loop guard so the ceiling and idle-window
    # triggers can never double-compact the same session. Worker sessions
    # (dev/TL/operator) are not exempt and fall through to the unchanged flow
    # below.
    if recycle_gate.user_session_exempt(role=role, window=window, meta=meta):
        if md_path is None:
            return False
        return _maybe_compact_stay_ceiling(sid, meta, md_path, level, now, pane)

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
    if not composer_ready(_capture_pane(pane), sid=sid, now=now):
        return False

    # Handoff mode + a resolvable destination → ARM the write-it-down flow.
    if compact_mode() == "handoff":
        target = _resolve_compact_target(cfg, slug, rec)
        if target["kind"] == "context":
            # T-0863: a task-bound session hands off through its TICKET.
            task_id, task_md = target["task_id"], target["task_md"]
            try:
                _inject_context_handoff(sid, task_id, relaunch=True)
            except Exception:
                log.exception("autocompact: context handoff inject failed for "
                              "%s (will retry)", sid)
                return False
            rec["compact"] = {
                "phase": "writing",
                "kind": "context",
                "armed_at": now,
                "task_id": task_id,
                "task_md": task_md,
                "arm_digest": context_digest(task_md),
                "role": target["role"],
                "assignment_id": task_id,
            }
            log.info("autocompact: armed context handoff for %s → %s ## Context",
                     sid, task_id)
            return True

        if target["kind"] == "artifact":
            artifact_path, role = target["artifact_path"], target["role"]
            try:
                _inject_handoff(sid, artifact_path, role)
            except Exception:
                log.exception("autocompact: handoff inject failed for %s (will "
                              "retry)", sid)
                return False
            rec["compact"] = {
                "phase": "writing",
                "kind": "artifact",
                "armed_at": now,
                "arm_mtime": _artifact_mtime(artifact_path),
                "artifact_path": artifact_path,
                "role": role,
                "assignment_id": target["assignment_id"],
            }
            log.info("autocompact: armed compact handoff for %s → %s", sid,
                     artifact_path)
            return True

    # No destination (or claude mode) → legacy /compact fallback.
    return _do_claude_compact(cfg, slug, rec, now)
