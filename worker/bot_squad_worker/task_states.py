"""T-0931: task state machine — names, transitions, and orchestration predicates.

The stakeholder's repeated ask ("я просил уже миллион раз") was for the task
statuses to be made explicit rather than implicit convention, with a new
``blocked_on_user`` status so a dev doesn't sit alive burning its cache window
repeating "still no answer from the user". This module is the shared home for
the pieces that read those states across the worker: the transition graph
(who may move a task from where to where) and the orchestration-demand
predicate (does this status count as a live session's work, or is it parked).

SSOT for VALUE membership is ``api/app/routes_backlog.py::_VALID_STATUSES``
(T-0889 lockstep-tested against 6 other copies — see
``api/tests/test_status_enum_lockstep.py``). :data:`TICKET_STATUSES` here is
an ADDITIONAL mirror for the same reason ``scripts/cli/bsq``'s copy exists:
so worker code can validate/reason about statuses without an API round-trip.
It is intentionally not added to the T-0889 lockstep set — that test asserts
equality of *complete* copies of the enum; :data:`PARKED_STATES` /
:data:`ACTIVE_STATES` are *predicates* (subsets), which T-0889 explicitly
calls a judgement call per consumer, not an invariant (see
``pickup.PICKUP_STATUSES``, ``recovery.ACTIVE_STATUSES`` — different
predicates over the same enum, answering different questions).
"""
from __future__ import annotations

TICKET_STATUSES: tuple[str, ...] = (
    "planned",
    "open",
    "in_progress",
    "paused",
    "blocked_on_user",
    "totest",
    "reopened",
    "closed",
)

# The write-boundary transition graph (T-0931 DoD: "invalid transitions
# refused loudly", not prose). Encoded as one data structure so amending an
# edge is a one-line change, not a redesign — see T-0931 ## Context for the
# rationale behind each edge (in short: every non-closed state can go
# straight to `closed` — a direct won't-fix/duplicate/cancelled is real and
# should not be forced through `totest`; `totest -> open` is deliberately NOT
# an edge because bouncing awaiting-review work back to "not started" would
# collide with `reopened`'s existing meaning of "was closed/totest, live
# again").
#
# This is a first cut, not yet stakeholder/TL sign-off on the exact edges —
# the DoD is the MECHANISM (enforced, data-driven graph), not these specific
# edges being final.
TRANSITIONS: dict[str, frozenset[str]] = {
    # T-0591 (F3.3) already names "planned/open -> in_progress" as one
    # transition — real build work starting from either "not yet queued" or
    # "queued" — so both go straight to in_progress, not just through open.
    "planned": frozenset({"open", "in_progress", "closed"}),
    "open": frozenset({"in_progress", "planned", "closed"}),
    "in_progress": frozenset({"paused", "blocked_on_user", "totest", "open", "closed"}),
    "paused": frozenset({"in_progress", "open", "closed"}),
    "blocked_on_user": frozenset({"in_progress", "closed"}),
    "totest": frozenset({"closed", "reopened"}),
    "reopened": frozenset({"in_progress", "open", "closed"}),
    "closed": frozenset({"reopened"}),
}


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """A no-op ``from == to`` is always allowed (re-saving unrelated fields on
    PATCH must not be rejected just because ``status`` was echoed back
    unchanged); every other move must be a real edge in :data:`TRANSITIONS`.

    A ``from_status`` this graph doesn't recognise (a legacy/garbage value
    from a direct file edit, predating this ticket) is permissive rather than
    a hard lock-out — any real status is accepted as the correction. Refusing
    the fix because the broken value itself isn't a known state would trap
    the ticket instead of letting it be repaired.
    """
    if from_status == to_status:
        return True
    if from_status not in TRANSITIONS:
        return to_status in TICKET_STATUSES
    return to_status in TRANSITIONS[from_status]


def invalid_transition_detail(from_status: str, to_status: str) -> str:
    """400-message helper mirroring ``routes_backlog._invalid_status_detail``:
    cite the allowed targets, not just echo the rejected input (T-0121)."""
    allowed = ", ".join(sorted(TRANSITIONS.get(from_status, frozenset())))
    return f"invalid transition: {from_status!r} -> {to_status!r} — from {from_status!r} may go to {{{allowed}}}"


# "Actively demanding a session right now" vs "parked" — for orchestration
# load (T-0932's budding triggers must read this rather than invent their own
# counters/hardcode a list).
#
# `open`/`reopened` count as ACTIVE though nothing is bound to them yet — an
# open task IS the demand signal budding exists to react to. `in_progress`/
# `totest` are obviously active (a session is or should be engaged).
# `planned` (not yet queued), `paused` (deliberately set aside),
# `blocked_on_user` (stuck on the stakeholder, no session can move it), and
# `closed` (done) are parked — none of them should count toward "how much
# work is waiting for a session".
PARKED_STATES: frozenset[str] = frozenset({"planned", "paused", "blocked_on_user", "closed"})
ACTIVE_STATES: frozenset[str] = frozenset(TICKET_STATUSES) - PARKED_STATES


def is_parked(status: str) -> bool:
    return status in PARKED_STATES
