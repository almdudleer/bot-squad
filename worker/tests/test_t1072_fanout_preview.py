"""T-1072 — the ticket fan-out preview reported "no live operator" while one
was live, and collapsed several causes into one empty list.

The specimen, 2026-09-07T14:12:26Z: a `bsq ticket context T-1082` write printed

    → nobody is bound to T-1082 and no live operator — this write nudges no one

while T-1082's frontmatter named the writing session in `session_history` and
`bsq team status` showed S-almdudleer-operator-p640 active as talking-operator
in the same minute. BOTH CLAUSES FALSE, and reproducible on demand: write to
any ticket your own session is bound to.

Root cause: `_ticket_fanout_preview` did `sids, _why = recipients_for(...)`,
DISCARDED `_why`, then filtered out the author. So an empty list meant any of
four different things and the caller could not tell which.

These tests are about the REASON, not the recipient list — the list was never
wrong. Each state must be distinguishable, because the remedies differ: relay
it / do nothing, it is normal / turn the watch on / go read the log.
"""
from __future__ import annotations

import pytest

from bot_squad_worker import actions as A


ME = "S-almdudleer-dev-p766"
SLUG = "bot-squad"


@pytest.fixture()
def watch(monkeypatch):
    """Steer `recipients_for` and the enabled flag; return a setter."""
    from bot_squad_worker import ticket_watch as tw
    state = {"enabled": True, "result": ([], "operator"), "raises": False}

    def _enabled():
        return state["enabled"]

    def _recipients(cfg, slug, task_id, *a, **k):
        if state["raises"]:
            raise RuntimeError("the store is on fire")
        return state["result"]

    monkeypatch.setattr(tw, "ticket_watch_enabled", _enabled)
    monkeypatch.setattr(tw, "recipients_for", _recipients)
    return state


def _fanout(sid=ME):
    return A._ticket_fanout(object(), SLUG, "T-1082", sid)


def test_the_specimen_says_the_writer_is_the_one_bound(watch):
    """THE 14:12:26Z CASE. The rung found somebody; the somebody was me. The
    old code answered this identically to "nobody is bound anywhere"."""
    watch["result"] = ([ME], "bound")

    targets, why = _fanout()

    assert targets == []                       # nobody ELSE to nudge — correct
    assert why == A.FANOUT_AUTHOR_ONLY         # ...and that is now sayable


def test_a_genuinely_empty_board_is_still_reported_as_such(watch):
    """The positive control for the case the original sentence was written
    for. Without it, `author_only` could just be what this always returns."""
    watch["result"] = ([], "operator")

    assert _fanout() == ([], A.FANOUT_NOBODY)


def test_a_bound_peer_is_named_and_the_author_is_not(watch):
    """The author exclusion itself is right and must not regress — nudging
    yourself about your own write is noise."""
    peer = "S-almdudleer-dev-p999"
    watch["result"] = ([ME, peer], "bound")

    targets, why = _fanout()

    assert targets == [peer]
    assert why == A.FANOUT_BOUND


def test_a_disabled_watch_is_not_reported_as_nobody_being_bound(watch):
    """Binding and operators are irrelevant when the fan-out is switched off,
    and the switch is the fact the reader needs. The old code returned [] here
    and the CLI blamed the board."""
    watch["enabled"] = False
    watch["result"] = ([ME, "S-almdudleer-dev-p999"], "bound")

    assert _fanout() == ([], A.FANOUT_WATCH_OFF)


def test_a_lookup_that_RAISED_is_unknown_and_never_nobody(watch):
    """An `except` that returns [] mints a negative fact out of an error.
    Absent is not known-empty."""
    watch["raises"] = True

    targets, why = _fanout()

    assert targets == []
    assert why == A.FANOUT_UNKNOWN
    assert why != A.FANOUT_NOBODY


def test_the_operator_rung_keeps_its_own_reason(watch):
    """`why` must survive the author filter when it did not empty the list —
    otherwise every non-empty result would read as `bound`."""
    op = "S-almdudleer-operator-p640"
    watch["result"] = ([op], "operator")

    assert _fanout() == ([op], A.FANOUT_OPERATOR)


def test_every_reason_is_a_distinct_string(watch):
    """A collapse would be invisible at the call site: two states sharing a
    token print the same sentence again, which IS this ticket."""
    reasons = [A.FANOUT_BOUND, A.FANOUT_OPERATOR, A.FANOUT_SEAT,
               A.FANOUT_AUTHOR_ONLY, A.FANOUT_NOBODY, A.FANOUT_WATCH_OFF,
               A.FANOUT_UNKNOWN]
    assert len(set(reasons)) == len(reasons)


def test_the_response_carries_both_fields(watch):
    """The CLI branches on `will_notify_why`; a writer that returns only
    `will_notify` sends it straight back to the collapsed sentence."""
    watch["result"] = ([ME], "bound")

    fields = A._fanout_fields(object(), SLUG, "T-1082", ME)

    assert fields == {"will_notify": [], "will_notify_why": A.FANOUT_AUTHOR_ONLY}
