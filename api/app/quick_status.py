"""Project-level quick-status aggregation.

The canonical contract — see docs/architecture/D-0018-quick-status.md — derives a
single string per project from its session rows:

    working      = at least one `active` session genuinely crunching
                   (active AND NOT active_at_prompt AND NOT awaiting_input)
    needs-input  = no working, at least one `paused` (Ctrl-C'd) OR
                   `awaiting_input` (the worker's PRECISE signal: sid in
                   tg_stall.blocked_sids — the agent peer_send'd the operator
                   and is blocked on a reply) — T-0375 Option A
    idle         = otherwise (only suspended, zero sessions, OR active sessions
                   merely idle-at-prompt but NOT blocked: an autonomous dev
                   parked at ❯ is idle/done + reapable, not awaiting a human)

    T-0375 supersedes T-0046: `active_at_prompt` no longer drives needs-input
    (it never decays, so a finished dev parked at the prompt left the project
    pill stuck on needs-input forever). It still excludes a session from
    "working". The precise `awaiting_input` flag is the canonical signal.

T-0025's mothership cache and T-0008's picker dropdown both consume this
contract verbatim. Mapping is pure over the list_sessions row shape, so it
fans out trivially across the per-user worker results.
"""
from __future__ import annotations

from typing import Iterable

# Canonical enum. Treated as a closed set by callers; downstream FE
# tolerates unknown strings (forward-compat) but won't render them as
# coloured pills — so don't extend without coordinating with T-0025.
STATUSES = ("working", "needs-input", "idle")


def _max_ts(rows: Iterable[dict], key: str) -> str | None:
    """Return the lexicographically-max ISO timestamp at `key`, or None.

    Session rows store timestamps as ISO 8601 UTC strings, which sort
    lexically the same as chronologically. Missing/None values are skipped.
    Numeric values (last_prompt_at can be a float mtime) are not used by
    callers here — only ISO string fields.
    """
    vals = []
    for r in rows:
        v = r.get(key)
        if v and isinstance(v, str):
            vals.append(v)
    if not vals:
        return None
    return max(vals)


def aggregate_project_status(rows: list[dict]) -> dict:
    """Compute {status, status_since} from a list of session rows.

    `rows` is the merged output of `list_sessions` across all user workers
    for a single project — same shape as the GET /api/projects/{slug}/sessions
    response.

    Returns: {"status": "working|needs-input|idle", "status_since": ISO|None}
    """
    actives = [r for r in rows if r.get("status") == "active"]
    # T-0046/T-0375: a session is "working" only if it's genuinely crunching —
    # active, NOT idle-at-prompt, and NOT blocked awaiting input. The idle-at-
    # prompt and blocked rows fall out of "working" below.
    workings = [
        r for r in actives
        if not r.get("active_at_prompt") and not r.get("awaiting_input")
    ]
    if workings:
        return {
            "status": "working",
            "status_since": _max_ts(workings, "started_at"),
        }

    # T-0375 (Option A — supersedes T-0046): needs-input is driven by the
    # PRECISE awaiting_input signal (sid in tg_stall.blocked_sids — the agent
    # actually peer_send'd the operator and is blocked on a reply), plus paused
    # (Ctrl-C'd) sessions. NOT the coarse `active_at_prompt` heuristic, which
    # never decays: an idle-at-prompt autonomous dev parked at ❯ is idle/done
    # (and reapable), not awaiting a human. See docs/architecture/D-0018.
    pauseds = [r for r in rows if r.get("status") == "paused"]
    blocked = [r for r in rows if r.get("awaiting_input")]
    if pauseds or blocked:
        # `paused_at` is the canonical needs-input timestamp; a blocked row that
        # isn't paused falls back to its `started_at`. _max_ts-style skip of
        # missing/non-string values.
        candidates: list[str] = []
        for r in pauseds:
            v = r.get("paused_at")
            if v and isinstance(v, str):
                candidates.append(v)
        for r in blocked:
            v = r.get("paused_at") or r.get("started_at")
            if v and isinstance(v, str):
                candidates.append(v)
        return {
            "status": "needs-input",
            "status_since": max(candidates) if candidates else None,
        }

    # Only suspended sessions left, or zero sessions at all — both render
    # the same way (no team currently engaged). Folding "empty" into "idle"
    # keeps the contract a closed three-string enum so T-0025 doesn't need a
    # null-branch in its renderer.
    suspendeds = [r for r in rows if r.get("status") == "suspended"]
    if suspendeds:
        return {
            "status": "idle",
            "status_since": _max_ts(suspendeds, "suspended_at"),
        }
    return {"status": "idle", "status_since": None}
