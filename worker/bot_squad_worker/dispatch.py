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
        "token/quota constraints. An empty backlog (nothing actionable left) is "
        "the only idle state; otherwise there is always a next move to make."
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
            role = S._derive_role(
                meta.get("window"), meta.get("task_id"), meta.get("initiative"),
            )
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
    if sess_dir.exists():
        for md in sorted(sess_dir.glob("*.md")):
            meta = S._read_session_metadata(md)
            if meta is None:
                continue
            sid = meta.get("sid", md.stem)
            # A session already holding this task is the binding itself, not a
            # reuse target — skip it.
            if task_id in S._full_task_set(meta):
                continue

            role = S._derive_role(
                meta.get("window"), meta.get("task_id"), meta.get("initiative"),
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

    return {
        "ok": True,
        "task_id": task_id,
        "task_initiative": task_init,
        "decision": decision,
        "target_sid": target,
        "reason": reason,
        "candidates": candidates,
    }
