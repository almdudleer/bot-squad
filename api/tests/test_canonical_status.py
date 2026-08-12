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
    derive_parent_status,
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
        "paused": "in-progress",
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


# ---------------------------------------------------------------------------
# T-0512 (M9 / Part A): parent-abstract status derivation
# ---------------------------------------------------------------------------

def test_derive_no_children_is_none():
    # A task with no subtasks is NOT abstract — it was never split.
    assert derive_parent_status([]) is None
    # ...and ignoring garbage that leaves nothing usable behaves the same.
    assert derive_parent_status(["bogus", "also-bad"]) is None


def test_derive_all_done_is_done():
    assert derive_parent_status(["closed", "closed"]) == "done"


def test_derive_done_only_when_every_child_done():
    # SOURCE-VERBATIM Part A: dependent on actual work — one unfinished subtask
    # keeps the abstract parent out of "done".
    assert derive_parent_status(["closed", "open"]) == "in-progress"
    assert derive_parent_status(["closed", "in_progress"]) == "in-progress"


def test_derive_all_backlog_is_backlog():
    # Nothing started yet → the parent is still in backlog.
    assert derive_parent_status(["planned", "open", "reopened"]) == "backlog"


def test_derive_any_started_is_in_progress():
    assert derive_parent_status(["open", "in_progress"]) == "in-progress"
    # A started+finished mix is still in-progress (not all done).
    assert derive_parent_status(["open", "closed"]) == "in-progress"


def test_derive_validating_when_all_work_complete():
    # All subtasks are validating-or-done (build finished, >=1 still verifying).
    assert derive_parent_status(["totest", "totest"]) == "validating"
    assert derive_parent_status(["totest", "closed"]) == "validating"
    # ...but an in-progress sibling drops it back to in-progress.
    assert derive_parent_status(["totest", "in_progress"]) == "in-progress"


def test_derive_ignores_unknown_child_status_but_uses_the_rest():
    assert derive_parent_status(["closed", "garbage"]) == "done"


def test_derive_range_is_canonical_states():
    # Whatever the mix, the derived value is always a real canonical state.
    for combo in (
        ["open"], ["in_progress"], ["totest"], ["closed"],
        ["open", "closed"], ["totest", "closed"], ["planned", "in_progress"],
    ):
        assert derive_parent_status(combo) in CANONICAL_STATES
