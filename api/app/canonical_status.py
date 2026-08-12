"""Canonical 4-state task model (T-0479, Process Paradigm M3).

The stakeholder's canonical task model has FOUR states:

    backlog → in-progress → validating → done

The live system stores SIX internal statuses (planned/open/in_progress/
totest/reopened/closed — see ``routes_backlog._VALID_STATUSES``). Those six
are a *refinement* of the canonical four, not a competing model: each internal
status rolls up into exactly one canonical state. This module is the
single-source-of-truth mapping. It is a non-destructive layer — no task is
renamed; the richer internal distinctions stay, and the canonical four are
*surfaced* (Board grouping, in-app docs) on top of them.

Mirrored in ``web/src/canonicalStatus.ts`` (keep both in lockstep). The
``test_canonical_status`` closed-set invariant asserts this mapping's domain is
exactly ``_VALID_STATUSES`` and its range is exactly ``CANONICAL_STATES``.
"""
from __future__ import annotations

# Display order — the user's stated lifecycle, left → right.
CANONICAL_STATES: tuple[str, ...] = ("backlog", "in-progress", "validating", "done")

CANONICAL_LABELS: dict[str, str] = {
    "backlog": "Backlog",
    "in-progress": "In progress",
    "validating": "Validating",
    "done": "Done",
}

# Internal 6-status → canonical 4-state. Decision (T-0479): planned/open/reopened
# all mean "not yet being worked / queued" → backlog; in_progress → in-progress;
# totest → validating; closed → done.
CANONICAL_STATE: dict[str, str] = {
    "planned": "backlog",
    "open": "backlog",
    "reopened": "backlog",
    "in_progress": "in-progress",
    # T-0889: work that was STARTED and has no live session on it any more —
    # distinct from planned (never started) and in_progress (actively worked).
    # Rolls up into in-progress because it is started work, and because his own
    # working-set sentence pairs them: «только in-progress/paused, without open».
    "paused": "in-progress",
    "totest": "validating",
    "closed": "done",
}


def canonical_of(status: str) -> str:
    """Map an internal status to its canonical state. Raises KeyError on an
    unknown status so callers fail loudly rather than silently mis-bucket."""
    return CANONICAL_STATE[status]


# T-0512 (Process Paradigm M9 / Part A). When a task is split into subtasks it
# becomes ABSTRACT: its progress is no longer set independently but DERIVED from
# the work being done in its children — "the task becomes more abstract and
# dependent on actual work being done in terms of its subtasks"
# (SOURCE-VERBATIM Part A). The derivation is a pure rollup over the children's
# canonical states, with this intuitive (epic-on-a-board) semantics:
#
#   - all children done             -> done       (every subtask finished)
#   - all children validating/done  -> validating (work complete, >=1 verifying)
#   - any subtask started           -> in-progress (some work underway)
#   - none started (all backlog)    -> backlog
#
# i.e. the parent is only "done" when ALL children are done, and only "backlog"
# when NONE has started; anything in between rolls up to in-progress (or
# validating once all the building is finished). A task with NO children is not
# abstract — it was never split — so the derivation returns ``None`` and the
# parent keeps its own on-disk status.
def derive_parent_status(child_statuses) -> str | None:
    """Roll a parent's canonical state up from its children's INTERNAL statuses.

    Unknown/garbage child statuses are ignored (defensive — the backlog parser
    does not validate status). Returns a canonical state, or ``None`` when there
    are no usable children.
    """
    canon = [CANONICAL_STATE[s] for s in child_statuses if s in CANONICAL_STATE]
    if not canon:
        return None
    if all(c == "done" for c in canon):
        return "done"
    if all(c in ("validating", "done") for c in canon):
        return "validating"
    if any(c in ("in-progress", "validating", "done") for c in canon):
        return "in-progress"
    return "backlog"
