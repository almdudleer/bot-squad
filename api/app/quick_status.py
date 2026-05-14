"""Project-level quick-status aggregation.

The canonical contract — see vision/multi-server/quick-status.md — derives a
single string per project from its session rows:

    working      = at least one session with status 'active'
    needs-input  = no active, at least one 'paused' (Ctrl-C'd pane awaits human)
    idle         = otherwise (only suspended, or zero sessions)

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
    if actives:
        return {
            "status": "working",
            "status_since": _max_ts(actives, "started_at"),
        }

    pauseds = [r for r in rows if r.get("status") == "paused"]
    if pauseds:
        return {
            "status": "needs-input",
            "status_since": _max_ts(pauseds, "paused_at"),
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
