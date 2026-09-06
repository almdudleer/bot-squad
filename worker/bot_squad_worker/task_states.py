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
    "to_accept",
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
#
# T-0944: `totest` is the HUMAN's queue ("to test это для меня уже,
# человека" — stakeholder). `to_accept` is the operator's queue in between —
# a dev's delivery now lands there, not in `totest` directly, so a ticket
# only reaches the human's queue once an operator has accepted it. Hence
# `in_progress` no longer has a direct edge to `totest` (that WAS the bug:
# nothing gated what a human saw); the only way in is via `to_accept`.
TRANSITIONS: dict[str, frozenset[str]] = {
    # T-0591 (F3.3) already names "planned/open -> in_progress" as one
    # transition — real build work starting from either "not yet queued" or
    # "queued" — so both go straight to in_progress, not just through open.
    "planned": frozenset({"open", "in_progress", "closed"}),
    "open": frozenset({"in_progress", "planned", "closed"}),
    "in_progress": frozenset({"paused", "blocked_on_user", "to_accept", "open", "closed"}),
    "to_accept": frozenset({"totest", "reopened", "closed"}),
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
# T-0944: `to_accept` is left OUT of PARKED_STATES (so it falls into
# ACTIVE_STATES below) — it is a live demand signal, just aimed at the
# operator rather than a dev. Whether a session should actually be kept
# alive/spawned for it is the session-lifecycle question T-0945 owns; this
# predicate only answers "is this orchestration load", and undelivered
# operator review is.
PARKED_STATES: frozenset[str] = frozenset({"planned", "paused", "blocked_on_user", "closed"})
ACTIVE_STATES: frozenset[str] = frozenset(TICKET_STATUSES) - PARKED_STATES


def is_parked(status: str) -> bool:
    return status in PARKED_STATES


# --- T-0948: the session-lifecycle predicates -------------------------------
#
# `PARKED_STATES` above answers "is this ORCHESTRATION LOAD" (budding's spawn
# triggers). The two predicates below answer the two DIFFERENT questions the
# recycle/nudge/respawn path asks, and they live here — beside the graph whose
# edges they have to stay consistent with — rather than as a third and fourth
# local copy in `idle_timeout` / `recovery` / `graceful_exit`.
#
# The T-0948 defect was exactly that drift: `idle_timeout.task_alive` derived
# "alive" as `not DONE and not WAITING`, so every OTHER status read as live
# work BY OMISSION — `paused` among them, which this module's own
# `PARKED_STATES` defines as "no session is or should be engaged". A dev on a
# ticket an operator had deliberately paused was therefore nudged «продолжай»
# every five minutes forever, and the escape the nudge named
# (`blocked_on_user`) is not even an edge out of `paused` in TRANSITIONS above.
#
# `to_accept`/`totest`/`closed` are DELIVERED (someone else's queue since
# T-0944), `blocked_on_user` WAITS on the human, `paused` was STOPPED on
# purpose. None of them is work a bound session can move.
#
# `planned` is deliberately NOT in this set, though :data:`PARKED_STATES` above
# does contain it — and the difference is not an inconsistency, it is the two
# sets answering their two different questions.
#
# `PARKED_STATES` is asked by budding: "should I SPAWN a session for this?" A
# `planned` ticket with nothing bound to it is not orchestration load, so no.
# This set is asked about a ticket that ALREADY HAS a session bound to it, and
# there the binding IS the queueing act — `planned` then means only that
# nobody updated the label, not that the work was stood down.
#
# That distinction is load-bearing rather than theoretical. Measured on the
# live fleet 2026-09-06 while building this fix: FOUR of the seven live
# task-bound dev sessions were on `planned` tickets (T-0942, T-0948, T-0949,
# T-0959 — this very ticket among them). Treating `planned` as "no session
# should be engaged" would have marked most of the working fleet as holding no
# live work and handed off and exited them on the next tick — a fleet-wide dev
# wipe wearing the costume of a token-economy fix, which is precisely the
# class of defect T-0948 exists to remove.
#
# And the REASON they sat there is the part worth keeping, because the obvious
# reading of it ("devs forget to update the label") was measured and is wrong.
# Those sessions TRIED: `planned -> in_progress` is refused unless the ticket
# carries a real `## Verbatim request`, and they had been dispatched with the
# `task_new` placeholder still in it. That guard is correct and stays (T-0483).
# So a status label can be pinned by a control that knows nothing about whether
# work is happening — which is the general reason a liveness predicate must not
# be derived from the board label alone. See T-0973, and `dispatch`/
# `session_history_ts`, which already records when a session was bound and
# answers "is anything working this ticket" without consulting status at all.
#
# `paused` differs in kind and stays: it is an explicit human decision to STOP,
# taken about a ticket that usually DOES have a session on it, and overriding
# that decision every five minutes is the defect itself.
NO_OWN_SESSION_STATES: frozenset[str] = frozenset({
    "to_accept", "totest", "closed",     # delivered — the operator's/human's
    "blocked_on_user",                   # waiting on the human
    "paused",                            # stopped on purpose, by a person
})

# The COORDINATOR's question is a THIRD one: a task-less team-lead is bound to
# an INITIATIVE, and this set says which of that initiative's tickets are still
# its to drive. Delivered work is the operator's or the human's, a blocked
# ticket waits on the human, and a paused ticket was deprioritised on purpose —
# a TL cannot move any of them. `planned` counts: queueing and dispatching an
# unstarted subtask IS the coordination job.
#
# That makes it EQUAL to NO_OWN_SESSION_STATES as both sets stand today, and it
# is still a separate name on purpose. The two answer different questions about
# different things — one about a ticket a session is bound to, one about a
# ticket in an initiative a TL coordinates — and they have already diverged
# once: this counter sat on `closed`-only for the whole stretch T-0944 was
# redefining what `totest` and `to_accept` mean, which is half of what T-0948
# had to repair. Collapsing them to one name would make the next such
# divergence invisible instead of impossible.
NO_COORDINATOR_WORK_STATES: frozenset[str] = frozenset({
    "to_accept", "totest", "closed", "blocked_on_user", "paused",
})


def demands_own_session(status: str | None) -> bool:
    """True when a session BOUND to this ticket still has work it can move.

    The alive-signal behind `idle_timeout.task_alive` (keep nudging / keep the
    session) and `recovery` (respawn a crashed one). An unknown/absent status
    is NOT work: there is nothing for a "продолжай" to point at.

    Note this is NOT `not is_parked(status)` — see the comment on
    :data:`NO_OWN_SESSION_STATES` for why `planned` parts company with
    `PARKED_STATES` here, and what it cost to find out.
    """
    st = (status or "").strip().lower()
    if not st:
        return False
    return st not in NO_OWN_SESSION_STATES


def demands_coordinator(status: str | None) -> bool:
    """True when this ticket is still a task-less team-lead's to drive.

    The per-ticket half of `graceful_exit.count_pending_initiative_tasks`.
    """
    st = (status or "").strip().lower()
    if not st:
        return False
    return st not in NO_COORDINATOR_WORK_STATES


def legal_block_escape(status: str | None) -> tuple[str, ...]:
    """The transitions that actually EXIST out of ``status`` toward "stop
    driving me" — read off :data:`TRANSITIONS` rather than asserted in prose.

    T-0948: the worker nudge told every session to «set the ticket to
    blocked_on_user» to stop the nudges. From `in_progress` that is a real
    edge; from `open`, `reopened`, `paused`, `to_accept` and `totest` it is
    not, so the one escape the system offered a stuck session was a transition
    the write boundary refuses. Returning the real path (possibly two hops)
    keeps the nudge text honest by construction — if an edge is amended above,
    the text follows.
    """
    st = (status or "").strip().lower()
    if st not in TRANSITIONS:
        return ()
    if "blocked_on_user" in TRANSITIONS[st]:
        return ("blocked_on_user",)
    for mid in sorted(TRANSITIONS[st]):
        if "blocked_on_user" in TRANSITIONS.get(mid, frozenset()):
            return (mid, "blocked_on_user")
    return ()
