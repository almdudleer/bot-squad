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
    "totest": "validating",
    "closed": "done",
}


def canonical_of(status: str) -> str:
    """Map an internal status to its canonical state. Raises KeyError on an
    unknown status so callers fail loudly rather than silently mis-bucket."""
    return CANONICAL_STATE[status]
