"""Closed-set invariants for the canonical 4-state mapping (T-0479).

The canonical layer is only coherent if EVERY internal status maps, maps to a
REAL canonical state, and the canonical states are exactly the stakeholder's
four. These are closed-set invariants — they live in their own file so a new
internal status can't slip in without a matching mapping entry going red.
"""
from app.canonical_status import (
    CANONICAL_LABELS,
    CANONICAL_STATE,
    CANONICAL_STATES,
    canonical_of,
)
from app.routes_backlog import _VALID_STATUSES


def test_mapping_domain_is_exactly_the_valid_statuses():
    # Every internal status maps; no mapping entry references a non-status.
    assert set(CANONICAL_STATE) == set(_VALID_STATUSES)


def test_mapping_range_is_exactly_the_canonical_states():
    assert set(CANONICAL_STATE.values()) == set(CANONICAL_STATES)


def test_canonical_states_are_the_stakeholders_four():
    # SOURCE-VERBATIM Part A: "backlog/in progress/validating/done".
    assert CANONICAL_STATES == ("backlog", "in-progress", "validating", "done")


def test_every_canonical_state_has_a_label():
    assert set(CANONICAL_LABELS) == set(CANONICAL_STATES)


def test_decided_mapping_is_stable():
    # The grounded T-0479 decision, pinned so a silent re-bucket goes red.
    assert CANONICAL_STATE == {
        "planned": "backlog",
        "open": "backlog",
        "reopened": "backlog",
        "in_progress": "in-progress",
        "totest": "validating",
        "closed": "done",
    }


def test_canonical_of_helper_and_unknown_raises():
    assert canonical_of("totest") == "validating"
    try:
        canonical_of("bogus")
    except KeyError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected KeyError for unknown status")
