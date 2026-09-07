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

This module also hosts :func:`classify_drive_mode` (T-0656) — how much AUTHORITY
one message grants. It has no sibling: the drive-SCOPE recogniser that used to
sit beside it was RETIRED by T-0848; see the tombstone below the classifier for
why, and read it before proposing anything that infers a standing setting from
his prose.
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

def operator_standing_task(pickup_brief: str = "") -> str:
    """The operator's STANDING TASK — the one directive it ALWAYS has when on
    (clarification-03: "when on it always has a task to clear the backlog,
    orchestrating the sessions according to parallelism and token usage
    constraints").

    The operator is user-facing — the user checks in and corrects it — but does
    NOT rely on user input: when running it autonomously drives the backlog to
    empty, dispatching sessions within the parallelism + token/quota constraints.
    SSOT for the directive text so the spawn brief and the re-drive cadence
    (T-0474's scheduler tick) inject the identical standing task; the full how-to
    lives in the role contract (``api/app/resources/roles/operator.md`` — the
    git SSOT per D-0043, not the drifting per-project ``vision/roles/`` copy).

    ``pickup_brief`` (T-0783a) appends the CONCRETE pickup queue — see
    :func:`pickup.pickup_brief`. Until it existed this directive told a fresh
    operator to "triage and prioritise open tasks" and left it to re-derive the
    board from 57 mds every re-drive; the stakeholder ended up being the
    fallback dispatcher for a P1 he had reopened. Optional and empty-by-default
    so the pure directive text stays callable (and testable) without a project.
    """
    base = (
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
    brief = (pickup_brief or "").strip()
    if not brief:
        return base
    return base + "\n\n" + brief


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
    # Word chars + hyphen only: "?" and "/" must NOT be part of a token —
    # "on/off" needs to tokenize as two control verbs ("on", "off"), not one
    # unmatched blob, and a mid-sentence "?" must not glue onto the noun it
    # follows ("tasks?" needs to match the "tasks" system noun).
    words = _re.findall(r"[\w-]+", stripped.lower())
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


# ---------------------------------------------------------------------------
# T-0656 — drive-mode granularity: distinguish do-all / do-one-task /
# just-record-as-wish, ask when ambiguous.
#
# Sits downstream of classify_request: once a message is a long_request (real
# work, not an instant tweak), THIS classifies how much of the backlog it
# authorizes driving. Direct incident: T-0655 was filed from a stakeholder
# musing/note ("я просто дал заметку") and got driven plan->build->deploy->
# closed off one nudge — the operator treated a wish as a build directive.
#
# Unlike classify_request's silent long_request default (durable-but-safe: a
# mis-filed TWEAK is slow, mis-applied WORK is lost, so default toward
# filing), drive-mode's safe default runs the OTHER way: when scope is
# unclear, the answer is "ambiguous" (ask), never a silent guess-and-run —
# driving unscoped work is precisely the failure this ticket exists to stop.
# ``do_all`` is deliberately rare and requires an explicit, matchable signal;
# it is never the fallback.
# ---------------------------------------------------------------------------

# Record-only phrasing — the stakeholder is noting/wishing, not directing a
# build. Checked FIRST: an explicit "not yet" always wins over an
# accompanying work verb ("собери фичу, но это пока просто пожелание").
RECORD_ONLY_PHRASES = (
    # RU
    "просто заметка", "просто пометка", "это просто заметка",
    "это пожелание", "как пожелание", "просто пожелание",
    "просто дал заметку", "просто дала заметку", "дал заметку как",
    "пока не делай", "пока не начинай", "не начинай", "не делай пока",
    "запиши пожелание", "просто запиши", "на будущее", "пока не надо",
    "не сейчас", "пока рано", "выключи permanent drive",
    "выключи драйв", "выключи автопилот",
    # EN
    "just a note", "just a wish", "just noting", "just record",
    "don't build", "dont build", "not now", "no rush", "for later",
    "just file this", "just log this", "no need to build",
    "turn off permanent drive", "turn off drive",
)

# Whole-backlog phrasing — explicit, unscoped "drive everything" language.
# NOTE: deliberately does NOT include a bare "permanent drive"/"драйв" — that
# names the continuous-operation mode, not scope, and is trivially negated
# ("выключи permanent drive" = turn OFF do-all, not a do-all order). Every
# entry here names WHAT is in scope, not just that driving continues.
DO_ALL_PHRASES = (
    # RU
    "все задачи", "всех задач", "весь бэклог", "весь backlog",
    "закрой всё", "закрой все", "закрой весь", "все таски",
    "всего пришедшего фидбека", "весь фидбек",
    # EN
    "all tasks", "everything", "whole backlog", "entire backlog",
    "clear the backlog", "close everything", "all the feedback",
)

# Single/bounded-scope phrasing — points at one task or "just this".
BOUNDED_PHRASES = (
    # RU
    "эту задачу", "это задачу", "только это", "только эту",
    "один таск", "одну задачу", "этот тикет", "вот эту",
    # EN
    "this task", "just this task", "one task", "this ticket", "only this",
)


def classify_drive_mode(text: str) -> dict:
    """Classify one long-request's authorized drive scope (T-0656).

    Deterministic phrase ladder, ask-first default:

    1. blank text -> ValueError (same contract as :func:`classify_request`);
    2. a record-only phrase present -> ``record_only`` (checked first: an
       explicit "just a note" wins even alongside a work verb or entity ref);
    3. a whole-backlog phrase present -> ``do_all``;
    4. a bounded-scope phrase, or an entity-id reference (reusing
       :data:`_ENTITY_REF`), present -> ``bounded``;
    5. none of the above -> ``ambiguous`` — the caller must ask the
       stakeholder to pick a mode rather than guess and run.

    Returns ``{mode, signals}`` — ``signals`` names every cue that fired.
    """
    if not text or not text.strip():
        raise ValueError("classify_drive_mode: empty text")
    stripped = text.strip()
    lowered = stripped.lower()

    signals: list[str] = []
    record_hits = sorted({p for p in RECORD_ONLY_PHRASES if p in lowered})
    do_all_hits = sorted({p for p in DO_ALL_PHRASES if p in lowered})
    bounded_hits = sorted({p for p in BOUNDED_PHRASES if p in lowered})
    refs = sorted({m.group(0) for m in _ENTITY_REF.finditer(stripped)})

    if record_hits:
        signals.append("record-only-phrase:" + ",".join(record_hits))
        mode = "record_only"
    elif do_all_hits:
        signals.append("do-all-phrase:" + ",".join(do_all_hits))
        mode = "do_all"
    elif bounded_hits or refs:
        if bounded_hits:
            signals.append("bounded-phrase:" + ",".join(bounded_hits))
        if refs:
            signals.append("entity-refs:" + ",".join(refs))
        mode = "bounded"
    else:
        signals.append("no-scope-signal")
        mode = "ambiguous"

    return {"mode": mode, "signals": signals}


# ---------------------------------------------------------------------------
# RETIRED — the drive-SCOPE recogniser (T-0830 L3, shipped 2026-07-30 17:41Z,
# removed by T-0848 the same evening). ~380 lines lived here:
# classify_drive_scope / apply_drive_scope / drive_scope_confirmation, the
# directive-cue and status-target patterns, and STAKEHOLDER_AUTHOR.
#
# T-0830 WAS BUILT TO SPEC AND WORKED AS DESIGNED. Its DoD 6 named this exact
# hazard ("a ladder that fires on any status word will silently re-scope the
# project off a passing remark"), TL p534 flagged it during the build, and the
# lane answered it with a real rule: a switch needed a DIRECTIVE cue AND a
# STATUS target in the SAME sentence, not negated, not a question, exactly one
# value. That rule held against every passing remark the tests could invent, and
# on live traffic at 2026-07-30T20:19Z it correctly caught «закрыть всё в open».
#
# WHAT KILLED IT, 18 minutes later (T-0848). He sent a UI layout request:
#
#     «а вот верхняя плашка где IN PROGRESS LIVE SESSIONS DRIVE MODE карточки,
#      может быть покомпактнее,»
#
# and two seconds later the scope silently changed to in_progress. Every half of
# the co-occurrence rule was satisfied by the SUMMARY BAR'S OWN LABELS: "IN
# PROGRESS" is the status target, "DRIVE" is the English directive cue, and a
# comma is not a sentence terminator — deliberately, because «закончи всё, что в
# опен» had to survive. The classifier was not sloppy; it was correct and still
# wrong, because he types his product's vocabulary and his commands into the same
# channel and the words are the same words.
#
# ★ THE PART WORTH KEEPING. `bsq pace show` then rendered:
#
#     scope: in_progress — set 2026-07-30 20:37 by tg:gu_dc8262b6cea9098d98e04d7e
#            from «IN PROGRESS LIVE SESSIONS DRIVE»
#
# Every field TRUE. He is that TG id, he did type those words, the timestamp is
# right. T-0828's fix — provenance belongs to the REQUEST, not the RESULT — was
# working exactly as designed, and that is what gave a misclassification a veneer
# of auditability: a reader checking "who set this and on what basis" gets a
# clean sourced answer and stops. Correct provenance proves the WORDS are his; it
# cannot prove the CLASSIFICATION was right, and the surface presents the two
# claims identically. Any future feature that infers intent from his prose
# reproduces this trap — the provenance display will not catch it for you.
#
# HIS RULING, 2026-07-30T20:42:37Z, verbatim, replying to the misfire report:
#
#     «не надо срабатывать в контексте в целом на слова автоматически, я могу
#      явно попросить у user-сессии всё»
#
# So the remedy is REMOVAL, not a smarter classifier. NOTHING in this repo may
# infer a drive scope from his ordinary messages. The scope is set only by an
# EXPLICIT act against a named value — `bsq pace drive --scope … --source-text
# …` — which the attendant runs when he asks in so many words, recording HIS
# words in `source_text` and the executing session in `set_by`. That split keeps
# T-0828's guarantee without the inference: the provenance line still shows what
# he said, and it no longer claims a machine understood it.
#
# Held by tests, not by this comment (`prose is not a control`):
#   * test_dispatch.py::test_no_automatic_drive_scope_recogniser_exists
#   * test_tg_listener.py::test_his_ui_layout_message_does_not_touch_the_scope
#   * test_tg_listener.py::test_a_real_scope_command_also_no_longer_fires
#   * test_tg_listener.py::test_a_deliberate_setting_survives_both_messages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T-0855 — the scaling ladder's first rung: WHO dispatches the work, the
# user-conversation session DIRECTLY or an operator tier in between.
#
# Stakeholder, 2026-08-11, verbatim (T-0855):
#   «когда поток задач маленький, не устраивать цепочку из юзер-сессия ->
#    оператор -> дев-сессия, 80% времени такая длинная цепочка не нужна, она
#    нужна в оставшиеся 20% когда я прямо сижу и в потоке работаю над кучей
#    задач сразу»
#   «качество, скорее всего, вырастет из-за предотвращения глухого телефона, а
#    траты токенов сократятся из-за убирания затрат на координацию»
#
# WHY THIS IS CODE AND NOT A ROLE-DOC PARAGRAPH. The operator hop was
# unconditional, and not because any contract demanded it: ``operator_redrive``
# respawns an operator whenever the board holds ANY non-closed task, so the
# ladder's own «оператора может не быть, если низкий параллелизм на входе»
# (F-2026-08-06-bsq-6c16cca10b) was mechanically unreachable — the tier came
# back inside 60s however small the flow was. A user-conversation session
# reading a doc that says "you may dispatch directly" would still find an
# operator on the roster a minute later. So the rule lives here, one
# deterministic function, and the role contracts POINT at it rather than
# restating it.
#
# WHAT IS MEASURED — roster state, never his prose. T-0848 removed the last
# classifier that inferred a standing setting from his ordinary messages («не
# надо срабатывать в контексте в целом на слова автоматически»); this one reads
# no message text at all, so nothing he types can promote or demote a tier.
# Signals:
#   * tasks in flight — the distinct tasks held by LIVE dev sessions;
#   * live devs — live dev sessions holding this project (a dev with no task
#     bound is still load, and a bundled dev holding three tasks is more);
#   * live operators / attending user-conversation sessions — the roster.
#
# ★ WHY NOT THE BOARD'S ``in_progress`` COUNT, the obvious candidate (and what
# ``pace.max_in_progress`` throttles). Measured against the live install on
# 2026-08-11, before this was written:
#     bot-squad  board in_progress 3 — actually held by a live dev: 2
#     guestent   board in_progress 4 — actually held by a live dev: 0
#     watchrobot board in_progress 3 — actually held by a live dev: 0
# The status is a LABEL a dev leaves behind when it dies or forgets to move the
# ticket; it drifts up and never comes down on its own. Gating on it would have
# pinned all three projects to the operator tier permanently — the feature would
# have shipped, passed its tests, and never once fired. Sessions holding tasks
# are the thing that cannot lie: a dead dev's session md stops being a live
# holder. (Board ``in_progress`` is still REPORTED under ``counts`` so the drift
# stays visible to whoever reads `bsq route`; it does not gate.)
#
# THE RULE (defaults below, both env-tunable on a live worker):
#     tier = direct   iff  tasks_in_flight < max_tasks AND live_devs < max_devs
#            operator otherwise
# The comparison is STRICT, so the count after one more direct dispatch still
# honours the ceiling: at the default 3, one user session steers up to three
# dev sessions and the fourth dispatch promotes the operator tier — his 20%,
# «когда я прямо сижу и в потоке работаю над кучей задач сразу».
#
# Two answers come out of one read, because two callers ask different questions:
#   * ``operator_needed`` — "must this project have an operator?" — the gate
#     ``operator_redrive.tick`` consults. TRUE whenever the tier is ``operator``
#     OR no ATTENDING user-conversation session exists to drive directly: nobody
#     drives a backlog by itself, and the base state of an unattended project is
#     still the operator clearing it. "Attending" means status ``active`` — a
#     PAUSED attendant is a parked process, not a driver, and this install has
#     one such md sitting in the roster right now. Nothing here probes how
#     recently it spoke: ``gc_sessions`` keeps ``status`` synced to real panes
#     and the ~1h idle recycle removes an attendant that stopped attending, so
#     the de-escalation path already exists — a transcript-freshness probe would
#     add an instrument whose null reading silently disables the feature.
#   * ``route`` — "what should I, the asking user session, do with this?" —
#     ``direct`` (spawn/steer the dev yourself) only when the tier is direct AND
#     no operator is already live. A live operator keeps the request: it is
#     already driving the board and a second dispatcher would double-drive it
#     (the T-0472 invariant). That tier then de-escalates on its own — the next
#     re-drive after it recycles finds a direct tier and does not bring it back.
#
# T-0937 — THE SEAT SITS ABOVE BOTH. The rule above answers "how much load is
# there"; it cannot answer "does this board already have a driver", because it
# reads drivers only as sessions whose ROLE is operator. A root
# user-conversation session that has claimed the drive is one — «ты должен
# стать оператором одновременно с юзер-сессией» — and while it holds the seat
# (``operator_seat.seat_holder``) ``operator_needed`` is FALSE at any tier and
# ``route`` is ``direct``, with the reason naming the holder. ``tier`` is left
# telling the truth about the load, so the two never collapse into one number:
# "the load would justify an operator" and "we already have one" are different
# facts, and a surface that showed only their AND would hide the first.
# ---------------------------------------------------------------------------

# At most this many tasks held by live dev sessions under direct drive.
DEFAULT_DIRECT_MAX_TASKS = 3
# At most this many live dev sessions under direct drive. Both are ceilings on
# what ONE user-conversation session can steer without the coordination cost
# that pays for an operator; both read per-call so a live worker can be retuned
# without a redeploy.
DEFAULT_DIRECT_MAX_DEVS = 3


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        val = int(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def direct_max_tasks() -> int:
    """In-flight task ceiling for direct (operator-less) drive.
    ``BOT_SQUAD_DIRECT_MAX_TASKS``."""
    return _positive_env_int("BOT_SQUAD_DIRECT_MAX_TASKS", DEFAULT_DIRECT_MAX_TASKS)


def direct_max_devs() -> int:
    """Live-dev ceiling for direct (operator-less) drive.
    ``BOT_SQUAD_DIRECT_MAX_DEVS``."""
    return _positive_env_int("BOT_SQUAD_DIRECT_MAX_DEVS", DEFAULT_DIRECT_MAX_DEVS)


def direct_dispatch_enabled() -> bool:
    """False iff the kill switch ``BOT_SQUAD_DIRECT_DISPATCH`` is off — every
    project then routes through the operator exactly as it did before T-0855.
    One env var is the whole rollback."""
    raw = os.environ.get("BOT_SQUAD_DIRECT_DISPATCH")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def live_role_sids(
    cfg: Any, slug: str, role: str, *, active_only: bool = False,
) -> list[tuple[str, dict]]:
    """``(sid, meta)`` of LIVE sessions holding ``slug`` whose role is ``role``.

    Pure session-md scan, keyed off the same role SSOT (``sessions._role_of``,
    which honours a morph stamp) the rest of this module uses. Devs and
    user-conversation sessions are always spawn-registered, so — unlike the
    canonical operator (see :func:`live_operator_sids`) — no tmux scan is
    needed; for ``role == "operator"`` call ``live_operator_sids`` instead, it
    catches the unregistered pane too.

    ``active_only`` drops PAUSED sessions. ``_is_live_holder`` counts
    active+paused, which is right for "does this session still hold a task"
    and wrong for "is anyone driving": a paused attendant is a parked process.
    """
    out: list[tuple[str, dict]] = []
    sess_dir = cfg.data_dir / slug / "sessions"
    if not sess_dir.exists():
        return out
    for md in sorted(sess_dir.glob("*.md")):
        meta = S._read_session_metadata(md)
        if meta is None or not S._is_live_holder(meta):
            continue
        if active_only and str(meta.get("status", "")).strip().lower() != "active":
            continue
        if S._role_of(meta) == role:
            out.append((meta.get("sid", md.stem), meta))
    return out


def decide_topology(cfg: Any, slug: str) -> dict:
    """Does ``slug`` need an OPERATOR tier right now, or can a live
    user-conversation session drive dev sessions directly? (T-0855)

    Returns ``{ok, tier, route, operator_needed, may_dispatch_directly, counts,
    thresholds, live_operator_sids, operator_seat, live_user_session_sids,
    signals, reason}``.
    ``tier`` is ``direct``/``operator`` (the load verdict), ``route`` is
    ``direct``/``via_operator`` (what the ASKING user session should do) — see
    the section comment above for why they are two answers, not one.

    Pure read (board mds + session mds + one tolerant live-pane scan), advisory
    in the same sense as :func:`decide_dispatch`: it spawns nothing and files
    nothing. Raises ActionError on an unknown project slug.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker import operator_redrive as _ord

    if cfg.projects.get(slug) is None:
        raise ActionError(f"decide_topology: unknown project slug {slug!r}")

    max_tasks = direct_max_tasks()
    max_devs = direct_max_devs()
    enabled = direct_dispatch_enabled()

    devs = live_role_sids(cfg, slug, "dev")
    in_flight: set[str] = set()
    for _sid, meta in devs:
        in_flight |= {t for t in S._full_task_set(meta) if t and t != "~"}
    attending = [sid for sid, _m in
                 live_role_sids(cfg, slug, "user-conversation", active_only=True)]
    operators = live_operator_sids(cfg, slug)

    counts = {
        "tasks_in_flight": len(in_flight),
        "live_devs": len(devs),
        "live_operators": len(operators),
        "attending_user_sessions": len(attending),
        # Reported, never gated on — see the ★ note above: this is the number
        # that drifts, and seeing it beside the real one is how the drift stays
        # visible instead of silently steering the topology.
        "board_in_progress": _ord._in_progress_count(cfg, slug),
    }
    thresholds = {
        "max_tasks": max_tasks,
        "max_devs": max_devs,
        "enabled": enabled,
    }

    signals: list[str] = []
    if not enabled:
        tier = "operator"
        signals.append("direct-dispatch-disabled")
    elif len(in_flight) >= max_tasks:
        tier = "operator"
        signals.append(f"tasks-in-flight:{len(in_flight)}>=max:{max_tasks}")
    elif len(devs) >= max_devs:
        tier = "operator"
        signals.append(f"live-devs:{len(devs)}>=max:{max_devs}")
    else:
        tier = "direct"
        signals.append(f"tasks-in-flight:{len(in_flight)}<max:{max_tasks}")
        signals.append(f"live-devs:{len(devs)}<max:{max_devs}")

    if not attending:
        signals.append("no-attending-user-session")
    if operators:
        signals.append("operator-live:" + operators[0])

    # T-0937 — the operator SEAT: a live ROOT session can hold the board
    # DIRECTLY while keeping its user-conversation role («ты должен стать
    # оператором одновременно с юзер-сессией»). It is a prior question to the
    # tier: `tier` still answers honestly how much load there is, but a board
    # that already has a driver does not need a second one minted, however
    # heavy the load — which is precisely the case the ceilings alone got
    # wrong. Same read `operator_redrive.tick` makes, so the session and the
    # scheduler cannot disagree about who is driving.
    from bot_squad_worker import operator_seat as _seat
    seat = _seat.seat_holder(cfg, slug)
    if seat is not None:
        signals.append(f"operator-seat-held:{seat['sid']}:{seat['kind']}")

    operator_needed = (tier == "operator" or not attending) and seat is None
    may_direct = (tier == "direct" or seat is not None) and not operators
    route = "direct" if may_direct else "via_operator"

    if seat is not None and not operators:
        reason = (
            f"{seat['sid']} holds the operator SEAT (via {seat['kind']}"
            + (f", since {seat['since']}" if seat.get("since") else "")
            + f") — it is a {seat['role']} session driving this board itself "
            "(T-0937), so no operator is minted while it lives. If that is "
            "you, spawn/steer dev sessions yourself (`bsq spawn`); if it is "
            "not, route through it. It hands the wheel over with `bsq bud "
            "operator`, and the seat vacates by itself if it dies"
        )
        if tier == "operator":
            reason += (
                f" — note: the LOAD ({len(in_flight)} task(s) in flight, "
                f"{len(devs)} live dev(s); ceilings {max_tasks}/{max_devs}) "
                "would otherwise justify the operator tier"
            )
    elif route == "direct":
        reason = (
            f"small flow ({len(in_flight)} task(s) in flight < {max_tasks}, "
            f"{len(devs)} live dev(s) < {max_devs}) and no operator on the "
            "roster — spawn/steer the dev session yourself (`bsq spawn`) and "
            "do NOT spawn an operator to relay"
        )
        if not attending:
            # `route` answers the ASKING session (whoever runs `bsq route` is by
            # definition present, and a terminal stakeholder session bound by the
            # user-conversation contract has no `user-conversation` window to be
            # counted by). `operator_needed` answers the scheduler, which can
            # only see the roster. Saying so keeps the two from reading as a
            # contradiction when they diverge.
            reason += (
                " — note: no user-conversation session is REGISTERED as "
                "attending this project, so the re-drive still brings an "
                "operator onto the board; expect one shortly"
            )
    elif tier == "direct" and operators:
        reason = (
            f"load is small enough for direct drive, but operator "
            f"{operators[0]} is already driving this board — route through it "
            "(a second dispatcher double-drives the backlog, T-0472). The tier "
            "de-escalates by itself: once it recycles, the re-drive gate leaves "
            "it off while the flow stays small"
        )
    elif not enabled:
        reason = (
            "direct dispatch is disabled (BOT_SQUAD_DIRECT_DISPATCH) — route "
            "through the operator"
        )
    else:
        reason = (
            f"load justifies the operator tier ({len(in_flight)} task(s) in "
            f"flight, {len(devs)} live dev(s); ceilings {max_tasks}/"
            f"{max_devs}) — route through the operator"
            + (f" ({operators[0]})" if operators else
               " (none live — ensure/await one rather than steering the devs "
               "yourself)")
        )

    return {
        "ok": True,
        "tier": tier,
        "route": route,
        "operator_needed": operator_needed,
        "may_dispatch_directly": may_direct,
        "counts": counts,
        "thresholds": thresholds,
        "live_operator_sids": operators,
        "operator_seat": seat,          # T-0937; None = the seat is vacant
        "attending_user_session_sids": attending,
        "tasks_in_flight": sorted(in_flight),
        "signals": signals,
        "reason": reason,
    }


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

    # T-0656: a long_request also carries a drive-mode scope — how much of
    # the backlog this message authorizes driving. Computed unconditionally
    # (independent of task_id/dispatch) so callers always see it before
    # deciding whether to dispatch at all.
    dm = classify_drive_mode(text)
    out["drive_mode"] = dm["mode"]
    out["drive_mode_signals"] = dm["signals"]

    # T-0855: a long request also needs a DISPATCH ROUTE — does the asking user
    # session drive the dev itself, or hand it to the operator? Every intake
    # surface sees it here, so the 3-hop relay stops being the default the way
    # the classification stops being per-session judgement. Advisory extra: a
    # failed read reports itself (explicit unknown, never a silent None) rather
    # than sinking the placement answer.
    try:
        topo = decide_topology(cfg, slug)
    except Exception as exc:  # noqa: BLE001 — advisory, must not break placement
        out["topology"] = None
        out["topology_error"] = f"{type(exc).__name__}: {exc}"
    else:
        out["topology"] = {
            "tier": topo["tier"],
            "route": topo["route"],
            "may_dispatch_directly": topo["may_dispatch_directly"],
            "counts": topo["counts"],
            "thresholds": topo["thresholds"],
            "reason": topo["reason"],
        }

    # T-0848: there is deliberately NO `drive_scope` key here any more. This
    # reported one — read-only, never applied — and a read-only report is still
    # the inference his ruling removed: it hands the next caller a scope it did
    # not have to derive, and the write gate is one line away. The standing scope
    # comes from `bsq pace drive` alone. See the tombstone above.

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

    if dm["mode"] == "ambiguous":
        out["reason"] = (
            "drive-mode unclear — ask the stakeholder to pick one before "
            "driving: do all tasks / just this task / just record it. "
            + out["reason"]
        )
    elif dm["mode"] == "record_only":
        out["reason"] = (
            "record-only — file it, take NO build action without a further "
            "explicit go. " + out["reason"]
        )
    elif out.get("topology") and out["topology"]["route"] == "direct":
        # Only on a mode that authorizes building at all — a record_only /
        # ambiguous message must not read as "go dispatch it, just faster".
        out["reason"] += (
            " — and dispatch it DIRECTLY, no operator hop: "
            + out["topology"]["reason"]
        )

    return out
