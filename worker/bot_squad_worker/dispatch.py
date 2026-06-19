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
            act = S._pane_activity_at(
                str(meta.get("cwd") or ""), meta.get("claude_uuid"), user_home
            )
            idle = act is None or (now - act) >= S.IDLE_AT_PROMPT_SECONDS
            init_match = bool(task_init) and task_init in S._full_initiative_set(meta)
            pct = _session_context_pct(cfg, slug, sid)

            reject: str | None = None
            if role != "dev":
                reject = "not-a-dev"
            elif not live:
                reject = "not-live"
            elif not idle:
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
