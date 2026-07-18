"""T-0237 Layer-2 v1 — operator-invoked reuse-vs-spawn dispatch decision.

The "CPU cores with context" scheduler. Given an incomplete, unbound task, decide
whether to **reuse** an idle live dev session that already holds useful context
(same initiative + context headroom) or **spawn** a fresh one.

v1 is operator-invoked and *advisory*: ``decide_dispatch`` returns the
recommendation (reuse SID X, or spawn) + the candidates it weighed; the operator/
TL then acts on it via the existing ``spawn`` / ``resume(initial_prompt=…)``
paths. No autonomous spawning pass yet (deferred until the resource caps of
T-0239 land, per operator fork S2). Pure decision — reads session mds + telemetry
records only, no tmux — so it is cheap and fully unit-testable.

Heuristic (operator-confirmed fork S3): a candidate is reuse-eligible iff it is a
LIVE **dev** session (not an operator/TL interface process), **idle**, bound to
the **same initiative** as the task, and below the context-headroom cap
(``BOT_SQUAD_REUSE_MAX_CONTEXT_PCT``, default 80 — the telemetry warn ratio).
Among eligible candidates the one with the **most headroom** (lowest context %)
wins; with none, spawn fresh (a clean context is the safe default).
"""
from __future__ import annotations

import os
import re as _re
import time
from typing import Any

from bot_squad_worker import sessions as S
from bot_squad_worker import telemetry as T


# Default reuse context-headroom cap (%). Ties to the telemetry warn ratio (0.8×
# the ceiling): reusing a session already past 80% of its context window leaves
# too little room to load the new task, so it is not a good reuse target even if
# topically a fit. Read per-call so it is env-tunable on a live worker.
DEFAULT_REUSE_MAX_CONTEXT_PCT = 80.0


def reuse_max_context_pct() -> float:
    raw = os.environ.get("BOT_SQUAD_REUSE_MAX_CONTEXT_PCT")
    try:
        val = float(raw) if raw else DEFAULT_REUSE_MAX_CONTEXT_PCT
    except ValueError:
        val = DEFAULT_REUSE_MAX_CONTEXT_PCT
    return val if val > 0 else DEFAULT_REUSE_MAX_CONTEXT_PCT


# ---------------------------------------------------------------------------
# T-0472 — operator as a transient per-project DISPATCHER
#
# The operator rides the SAME universal lifecycle as every role (NOT a persistent
# session): user-facing (the user checks in + corrects) but never reliant on user
# input. When on it ALWAYS has one standing task — clear the backlog — and exactly
# one operator drives a project at a time. The two pure helpers below are the
# seams those invariants ride on: ``operator_standing_task`` is the SSOT for the
# directive text (spawn brief + T-0474 re-drive inject the SAME task), and
# ``live_operator_sids`` answers "is an operator already on?" for the
# one-per-project guard (T-0472) and the re-drive continue-vs-respawn (T-0474).
# ---------------------------------------------------------------------------

def operator_standing_task() -> str:
    """The operator's STANDING TASK — the one directive it ALWAYS has when on
    (clarification-03: "when on it always has a task to clear the backlog,
    orchestrating the sessions according to parallelism and token usage
    constraints").

    The operator is user-facing — the user checks in and corrects it — but does
    NOT rely on user input: when running it autonomously drives the backlog to
    empty, dispatching sessions within the parallelism + token/quota constraints.
    SSOT for the directive text so the spawn brief and the re-drive cadence
    (T-0474's scheduler tick) inject the identical standing task; the full how-to
    lives in the role contract (``vision/roles/operator.md``).
    """
    return (
        "Your standing task: clear the backlog autonomously. The user checks in "
        "and corrects you, but you do NOT wait on user input — when on, you "
        "always drive the backlog forward: triage and prioritise open tasks, and "
        "dispatch sessions to clear them, orchestrating per the parallelism + "
        "token/quota constraints. PACING (T-0475/F2.7): honor your pacing signals "
        "— stay within max_in_progress, THROTTLE (let in-flight drain, don't "
        "dispatch new) when at the cap or seeing rate-limit/429 pressure. When a "
        "weekly quota-utilization target is set, its verdict (under/on/over) is "
        "mechanically computed from best-effort spend-to-date: at/over target, or "
        "with no quota anchor set, it's ADVISORY (pace by judgement — the weekly "
        "total is not precisely knowable); when UNDER target the recommendation "
        "is RAMP with a concrete target_in_progress to admit toward (it already "
        "respects max_in_progress and the live backoff ceiling, so never dispatch "
        "past it). An empty backlog (nothing actionable left) is the only idle "
        "state; otherwise there is always a next move to make."
    )


def live_operator_sids(cfg: Any, slug: str) -> list[str]:
    """SIDs of LIVE operator sessions for ``slug``.

    The ONE operator-identity SSOT (T-0523) behind two M2 invariants: (1)
    exactly-one-operator-per-project enforcement at spawn time (T-0472 — a
    non-empty result blocks a second operator), and (2) the re-drive cadence's
    continue-vs-respawn decision (T-0474 — non-empty ⇒ an operator is already on;
    empty ⇒ the project has no operator driving the backlog, so re-drive spawns
    one). Both consumers read THIS function — no divergent identity logic.

    Detection is a UNION of two scans, both keyed off the same window→role SSOT
    (:func:`sessions._derive_role`, which matches BOTH the canonical
    ``bot-squad-operator`` and the newer ``operator`` window namings):

    1. **Registered sessions** — a live-holder (status active/paused, not
       archived) session md whose window derives the operator role.
    2. **Canonical / unregistered operator** (T-0523) — a LIVE claude pane in an
       operator window that has NO session md. The canonical operator predates
       spawn-registration and runs in a ``bot-squad-operator`` window with no md,
       so the md-only scan missed it and the re-drive gate misfired → a DUPLICATE
       operator. The tmux scan is scoped to THIS project's tmux session
       (``pane.session == slug``) so another project's operator never leaks in.
    """
    out: list[str] = []
    seen: set[str] = set()

    # (1) Registered operator sessions — pure read of session mds.
    sess_dir = cfg.data_dir / slug / "sessions"
    if sess_dir.exists():
        for md in sorted(sess_dir.glob("*.md")):
            meta = S._read_session_metadata(md)
            if meta is None or not S._is_live_holder(meta):
                continue
            # T-0509: honor a stored ``role`` (a user session morphed to
            # operator stamps it without renaming its window), so the singleton
            # guard sees morphed operators too.
            role = S._role_of(meta)
            if role == "operator":
                sid = meta.get("sid", md.stem)
                if sid not in seen:
                    seen.add(sid)
                    out.append(sid)

    # (2) Canonical / unregistered operator — live claude pane, no session md.
    out.extend(_live_operator_sids_from_tmux(slug, seen))
    return out


def _live_operator_sids_from_tmux(slug: str, seen: set[str]) -> list[str]:
    """Operator SIDs discovered from LIVE claude tmux panes for ``slug`` whose
    window derives the operator role (T-0523). Scoped to the project's tmux
    session (``pane.session == slug``); mutates ``seen`` for cross-scan dedup.

    Tolerant of a missing/broken tmux server (``list_panes`` returns ``[]``).
    """
    try:
        panes = S.list_panes()
    except Exception:  # noqa: BLE001 — never let a tmux hiccup break the gate
        return []
    if not panes:
        return []
    user = S._get_current_user()
    children = S._proc_children_map()
    out: list[str] = []
    for p in panes:
        if p.session != slug:
            continue
        if S._derive_role(p.window, None, None) != "operator":
            continue
        if not S._pane_has_live_claude(p.pid, children):
            continue
        try:
            sid = S.compute_sid(user, p.window, p.pane_id)
        except Exception:  # noqa: BLE001
            continue
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _task_initiative(matches) -> str | None:
    """The bound initiative of a task md (None if unbound / placeholder)."""
    meta = S._read_session_metadata(matches[0])
    init = (meta or {}).get("initiative")
    if not init or init == "~":
        return None
    return init


def _resume_hint(meta: dict) -> dict | None:
    """The T-0468 exit-resume hint a graceful exit stamped on a session md, or
    None when absent. Flat keys mirror the ``graceful_exit._stamp_resume_hint``
    stamp (a nested dict is unsafe in the line-oriented frontmatter). Presence is
    keyed on ``resume_recommended`` — the one field every stamp writes."""
    if meta is None or "resume_recommended" not in meta:
        return None
    return {
        "resume_recommended": bool(meta.get("resume_recommended")),
        "reason": meta.get("resume_hint_reason") or "",
        "when": meta.get("resume_hint_when") or "",
        "last_work_summary": meta.get("last_work_summary") or "",
    }


def _session_context_pct(cfg: Any, slug: str, sid: str) -> float:
    """A session's last-sampled context-usage %, or 0.0 when no telemetry record
    exists yet (a brand-new session is assumed to have full headroom)."""
    rec = T._read_json(T._record_path(cfg, slug, sid))
    if not rec:
        return 0.0
    pct = (rec.get("context") or {}).get("pct")
    return float(pct) if isinstance(pct, (int, float)) else 0.0


def decide_dispatch(cfg: Any, slug: str, task_id: str, *, now_epoch: float | None = None) -> dict:
    """Recommend reuse-vs-spawn for ``task_id``. See module docstring.

    Returns ``{ok, task_id, task_initiative, decision: 'reuse'|'spawn',
    target_sid, reason, candidates: [{sid, role, live, idle, initiative_match,
    context_pct, eligible, reject}]}``. Raises ActionError on unknown project /
    missing task.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"decide_dispatch: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    backlog = data_dir / slug / "backlog"
    matches = sorted(backlog.glob(f"{task_id}-*.md"))
    if not matches:
        raise ActionError(f"decide_dispatch: task not found: {task_id}")
    task_init = _task_initiative(matches)

    threshold = reuse_max_context_pct()
    user_home = S._get_user_home()
    now = now_epoch if now_epoch is not None else time.time()

    sess_dir = data_dir / slug / "sessions"
    candidates: list[dict] = []
    resume_hints: list[dict] = []
    if sess_dir.exists():
        for md in sorted(sess_dir.glob("*.md")):
            meta = S._read_session_metadata(md)
            if meta is None:
                continue
            sid = meta.get("sid", md.stem)

            # T-0468: surface the exit-resume hint from EXITED (non-live)
            # sessions whose history bears on this task — same initiative, or
            # they ran this exact task (a reopened task's best resume target is
            # the session that did it). This is how the reuse-vs-spawn decision
            # "consumes the hint": the operator sees, per relevant exited
            # session, whether/when resuming it beats a fresh start. Computed
            # BEFORE the holds-this-task skip below (else a same-task exited
            # session would be dropped — and it is the most relevant hint).
            hint = _resume_hint(meta)
            if hint is not None and not S._is_live_holder(meta):
                held_task = task_id in S._full_task_set(meta)
                init_match = bool(task_init) and task_init in S._full_initiative_set(meta)
                if held_task or init_match:
                    resume_hints.append({
                        "sid": sid, "initiative_match": init_match,
                        "held_task": held_task, **hint,
                    })

            # T-0575: a recycle-v2 remembered session (idle_timeout compact-
            # terminate-remember, T-0566) is a resume candidate too — its md
            # carries ``resumable``/``recycled_at``/``resume_hint`` instead of
            # the graceful-exit stamp above. Recommend --resume when it ran
            # THIS task and its remembered context fits the <50k budget
            # (stakeholder 2026-07-04).
            if (hint is None and meta.get("resumable")
                    and not S._is_live_holder(meta)):
                held_task = task_id in S._full_task_set(meta)
                init_match = bool(task_init) and task_init in S._full_initiative_set(meta)
                if held_task or init_match:
                    ok, tokens = S.recycled_resume_eligible(meta.get("claude_uuid"))
                    resume_hints.append({
                        "sid": sid, "initiative_match": init_match,
                        "held_task": held_task,
                        "resume_recommended": bool(ok and held_task),
                        "reason": meta.get("resume_hint") or "recycled (resumable)",
                        "when": meta.get("recycled_at") or "",
                        "last_work_summary": "",
                        "context_tokens": tokens,
                    })

            # A session already holding this task is the binding itself, not a
            # reuse target — skip it.
            if task_id in S._full_task_set(meta):
                continue

            role = S._role_of(  # T-0509: honor a morph stamp (non-dev ⇒ not a reuse target)
                meta,
                extra_task_ids=[t for t in (meta.get("extra_task_ids") or []) if t and t != "~"],
                extra_initiatives=[i for i in (meta.get("extra_initiatives") or []) if i and i != "~"],
            )
            live = S._is_live_holder(meta)
            # T-0430: short-circuit the expensive per-session work
            # (_pane_activity_at + the _session_context_pct disk JSON read) for
            # candidates that can NEVER be an eligible reuse target — only a LIVE
            # dev qualifies. A cheap rejected-stub keeps the (FE-ignored) rejected
            # list semantically intact; role-before-live matches the original
            # ladder precedence. Collapses O(all-sessions-ever) → O(live).
            if role != "dev" or not live:
                candidates.append({
                    "sid": sid, "role": role, "live": live, "idle": False,
                    "initiative_match": False, "context_pct": 0,
                    "eligible": False,
                    "reject": "not-a-dev" if role != "dev" else "not-live",
                })
                continue

            act = S._pane_activity_at(
                str(meta.get("cwd") or ""), meta.get("claude_uuid"), user_home
            )
            idle = act is None or (now - act) >= S.IDLE_AT_PROMPT_SECONDS
            init_match = bool(task_init) and task_init in S._full_initiative_set(meta)
            pct = _session_context_pct(cfg, slug, sid)

            reject: str | None = None
            if not idle:
                reject = "busy"
            elif not init_match:
                reject = "initiative-mismatch" if task_init else "task-has-no-initiative"
            elif pct >= threshold:
                reject = f"context-{pct}%>=cap-{threshold}%"

            candidates.append({
                "sid": sid, "role": role, "live": live, "idle": idle,
                "initiative_match": init_match, "context_pct": pct,
                "eligible": reject is None, "reject": reject,
            })

    eligible = [c for c in candidates if c["eligible"]]
    if eligible:
        winner = min(eligible, key=lambda c: c["context_pct"])
        decision = "reuse"
        target = winner["sid"]
        reason = (
            f"reuse {target}: idle dev on initiative {task_init} with "
            f"{winner['context_pct']}% context (< {threshold}% cap)"
        )
    else:
        decision = "spawn"
        target = None
        if not task_init:
            reason = "spawn: task has no initiative to match a reusable session"
        else:
            reason = (
                f"spawn: no idle same-initiative ({task_init}) dev with context "
                f"headroom (< {threshold}%)"
            )
        # T-0468: when no LIVE reuse target exists but an EXITED session
        # recommends resuming it (work was in flight, not documented-done), point
        # the operator at `claude --resume` instead of a fresh spawn — the one
        # case where resuming an exited session genuinely beats starting fresh.
        recommend = [h for h in resume_hints if h.get("resume_recommended")]
        if recommend:
            sids = ", ".join(h["sid"] for h in recommend)
            reason += (
                f"; but exited session(s) {sids} recommend --resume "
                "(see resume_hints)"
            )

    return {
        "ok": True,
        "task_id": task_id,
        "task_initiative": task_init,
        "decision": decision,
        "target_sid": target,
        "reason": reason,
        "candidates": candidates,
        "resume_hints": resume_hints,
    }


# ---------------------------------------------------------------------------
# T-0576 (M11/F11.3) — correct-placement guarantee: instant-tweak vs
# long-request routing as SYSTEM logic, not prompt convention.
#
# The concept (clarification-03 / voice-14): the user firehoses messages at the
# system through any access point — TG mail, attach-and-write, or a dedicated
# user session — and the SYSTEM guarantees each one lands in the right place.
# **Instant tweaks** (on/off, prioritize, write/correct task/initiative) are
# applied LIVE on the control plane; **long requests** (real work — code,
# bugs, features) are filed as tasks and offloaded to the software factory.
# Before T-0576 that split lived only in role-doc prose, so placement rested
# on each receiving session's judgment (D-0047 F11.3: PARTIAL/MISSING).
#
# ``classify_request`` is the classification SSOT: one deterministic,
# unit-testable heuristic every access point consults, instead of N sessions
# re-deriving the split from their prompts. ``decide_placement`` turns the
# classification into a routing decision, chaining the existing
# ``decide_dispatch`` seam for the long-request side. Both are pure reads
# (session mds only, no tmux writes) — advisory-shaped like decide_dispatch,
# with the intake call sites adopting them per the wiring plan on T-0576.
# ---------------------------------------------------------------------------

# Control-plane verbs — the concept's enumerated instant tweaks (on/off,
# prioritize, write/correct task/initiative) plus the end-state's firehose
# levers (session spawning, operator/TL/dev on-off, resource constraints).
# A control verb alone is NOT enough to classify instant: it must target a
# system entity (id reference or system noun below), else "close the security
# hole" would read as a tweak. Ambiguity falls through to long_request — the
# durable default (a tweak mis-filed as a task is slow; real work mis-applied
# live is lost).
INSTANT_TWEAK_VERBS = frozenset({
    "on", "off", "enable", "disable", "pause", "unpause", "resume", "stop",
    "start", "restart", "kill", "throttle", "cap",
    "prioritize", "prioritise", "deprioritize", "deprioritise",
    "reprioritize", "reprioritise", "bump", "promote", "demote",
    "close", "reopen", "cancel", "hold", "unhold", "park", "unpark",
    "postpone", "archive", "unarchive", "suspend",
    "rename", "retitle", "reword", "correct", "amend", "edit", "update",
    "assign", "reassign", "unassign", "bind", "unbind", "spawn",
})

# Work verbs — real product work that must land in the backlog. Checked FIRST:
# a work verb anywhere makes the whole request long ("pause deploys and
# refactor the pipeline" files a task; the durable route loses nothing).
LONG_REQUEST_VERBS = frozenset({
    "build", "implement", "fix", "add", "create", "refactor", "rewrite",
    "investigate", "research", "debug", "diagnose", "design", "develop",
    "migrate", "integrate", "rework", "audit", "optimize", "optimise",
    "port", "ship", "test", "verify", "document", "prototype",
})

# System nouns — the entities instant tweaks operate on (M3 tasks/initiatives,
# M1 sessions, M2 roles, resource levers). Grounds a control verb: "pause the
# operator" is a tweak, "pause the video" is not our control plane.
SYSTEM_NOUNS = frozenset({
    "task", "tasks", "ticket", "tickets", "initiative", "initiatives",
    "backlog", "priority", "session", "sessions", "operator", "teamlead",
    "team-lead", "tl", "dev", "devs", "worker", "workers", "team", "teams",
    "deploy", "deploys", "quota", "pacing", "parallelism", "autopilot",
    "routine", "routines", "recycle", "everything",
})

# Entity-id references (T-0450, INI-04, D-0047, P1, a SID) — the strongest
# instant-tweak grounding: the user is pointing AT a system object.
_ENTITY_REF = _re.compile(
    r"\b(?:(?:T|D|UC|F|FL|INI)-\d+|P[0-3]|S-[\w-]+-p\d+)\b", _re.IGNORECASE
)

# Above this many words a message is never an instant tweak — a paragraph is
# work (or at least deserves durable placement). Env-tunable on a live worker
# like BOT_SQUAD_REUSE_MAX_CONTEXT_PCT.
DEFAULT_TWEAK_MAX_WORDS = 40


def tweak_max_words() -> int:
    raw = os.environ.get("BOT_SQUAD_TWEAK_MAX_WORDS")
    try:
        val = int(raw) if raw else DEFAULT_TWEAK_MAX_WORDS
    except ValueError:
        val = DEFAULT_TWEAK_MAX_WORDS
    return val if val > 0 else DEFAULT_TWEAK_MAX_WORDS


def classify_request(text: str) -> dict:
    """Classify one inbound user request as ``instant_tweak`` vs
    ``long_request`` (F11.3). Deterministic ladder, most-durable default:

    1. blank text → ValueError (degenerate input is a caller bug, never
       silently routed);
    2. longer than ``tweak_max_words()`` → long_request;
    3. any work verb → long_request (work wins over a co-present control verb);
    4. control verb grounded by an entity ref or system noun → instant_tweak;
    5. a pure question (ends with ``?``, no work verb) → instant_tweak — F11.4
       transparency: answered live from system state, never filed as a task;
    6. everything else → long_request (the safe default: a mis-filed tweak is
       slow, mis-applied work is lost).

    Returns ``{kind, signals, word_count}`` — ``signals`` names every cue that
    fired so callers (and the user) can see WHY, not just what.
    """
    if not text or not text.strip():
        raise ValueError("classify_request: empty text")
    stripped = text.strip()
    words = _re.findall(r"[\w?/-]+", stripped.lower())
    word_count = len(words)
    wordset = set(words)

    signals: list[str] = []
    work = sorted(wordset & LONG_REQUEST_VERBS)
    control = sorted(wordset & INSTANT_TWEAK_VERBS)
    nouns = sorted(wordset & SYSTEM_NOUNS)
    refs = sorted({m.group(0) for m in _ENTITY_REF.finditer(stripped)})
    if work:
        signals.append("work-verbs:" + ",".join(work))
    if control:
        signals.append("control-verbs:" + ",".join(control))
    if nouns:
        signals.append("system-nouns:" + ",".join(nouns))
    if refs:
        signals.append("entity-refs:" + ",".join(refs))

    max_words = tweak_max_words()
    if word_count > max_words:
        signals.append(f"length:{word_count}>{max_words}-words")
        kind = "long_request"
    elif work:
        kind = "long_request"
    elif control and (refs or nouns):
        kind = "instant_tweak"
    elif stripped.endswith("?"):
        signals.append("query")
        kind = "instant_tweak"
    else:
        signals.append("default-durable")
        kind = "long_request"

    return {"kind": kind, "signals": signals, "word_count": word_count}


def decide_placement(
    cfg: Any, slug: str, text: str, *,
    task_id: str | None = None, now_epoch: float | None = None,
) -> dict:
    """The F11.3 placement decision for one inbound request, whichever access
    point carried it.

    - ``instant_tweak`` → ``route: apply_live``: apply on the control plane
      NOW. ``target_sid`` is the live operator (the control-plane surface,
      via :func:`live_operator_sids`); ``None`` when no operator is on — the
      caller then ensures one (operator re-drive / ensure) rather than filing
      a task for a tweak.
    - ``long_request`` → ``route: file_task``: the request must land durably
      in the backlog. With ``task_id`` (caller already minted the task) the
      result chains the T-0237/T-0575 ``decide_dispatch`` seam verbatim under
      ``dispatch`` — reuse/spawn/resume for THAT task, one hop from intake to
      factory. Without it, ``reason`` names the next verbs (task_new with
      verbatim provenance, then dispatch_decision).

    Raises ActionError on unknown project / blank text. Pure read, no tmux
    writes — same advisory contract as :func:`decide_dispatch`.
    """
    from bot_squad_worker.actions import ActionError

    if cfg.projects.get(slug) is None:
        raise ActionError(f"decide_placement: unknown project slug {slug!r}")
    try:
        cls = classify_request(text)
    except ValueError as exc:
        raise ActionError(str(exc)) from None

    out: dict[str, Any] = {
        "ok": True,
        "kind": cls["kind"],
        "signals": cls["signals"],
        "word_count": cls["word_count"],
    }

    if cls["kind"] == "instant_tweak":
        ops = live_operator_sids(cfg, slug)
        out["route"] = "apply_live"
        out["target_sid"] = ops[0] if ops else None
        out["reason"] = (
            f"instant tweak — apply live via operator {ops[0]}"
            if ops else
            "instant tweak — no live operator; ensure one (re-drive), do NOT "
            "file a task for a tweak"
        )
        return out

    out["route"] = "file_task"
    if task_id:
        dispatch = decide_dispatch(cfg, slug, task_id, now_epoch=now_epoch)
        out["task_id"] = task_id
        out["dispatch"] = dispatch
        out["target_sid"] = dispatch.get("target_sid")
        out["reason"] = (
            f"long request — already filed as {task_id}; "
            f"dispatch: {dispatch['reason']}"
        )
    else:
        out["target_sid"] = None
        out["reason"] = (
            "long request — file durably first (task_new with verbatim "
            "provenance), then dispatch_decision for reuse-vs-spawn"
        )
    return out
