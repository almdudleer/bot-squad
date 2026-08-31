"""T-0855: the user-session -> dev DIRECT dispatch route is wired end to end.

The rule itself lives in code and is pinned there
(``worker/tests/test_dispatch.py``, ``worker/tests/test_operator_redrive.py``).
What this file guards is the JOIN: a live user-conversation session only uses
the rule if its contract points at a verb that exists, and the operator only
tolerates an operator-less board if its contract says so. Both are prose, and
prose drifts — so the pointers are checked by a machine, not by a reviewer
remembering that a CLI verb was renamed.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _repo_root() -> Path | None:
    """Locate the repo root (needs BOTH api/ and scripts/ — the api-only docker
    mount has no scripts/, hence the skip below)."""
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "api" / "app" / "resources" / "roles").is_dir() and (
                ancestor / "scripts" / "cli" / "bsq").exists():
            return ancestor
    return None


ROOT = _repo_root()

if ROOT is None:
    pytest.skip(
        "repo root with api/ + scripts/ not reachable from this mount",
        allow_module_level=True,
    )

ROLES = ROOT / "api" / "app" / "resources" / "roles"
CLI = (ROOT / "scripts" / "cli" / "bsq").read_text(encoding="utf-8")
USERCONV = (ROLES / "user-conversation.md").read_text(encoding="utf-8")
OPERATOR = (ROLES / "operator.md").read_text(encoding="utf-8")


def test_the_verb_the_user_contract_names_actually_exists():
    """`bsq route` is what the contract tells a live session to run. A renamed
    verb would leave the contract describing a capability the session cannot
    invoke — documented, not usable."""
    assert "bsq route" in USERCONV
    assert 'add_parser(\n        "route"' in CLI, "no `route` subcommand in the CLI"
    assert "def cmd_route(" in CLI
    assert "topology_decision" in CLI, "the verb must ask the worker for the rule"


def test_the_user_contract_offers_both_routes_not_just_the_hand_off():
    """Pre-T-0855 the contract said the operator dispatches the work, full
    stop. Both branches must be there — a contract that only names the direct
    route would drop the 20% case he explicitly kept."""
    assert "route: direct" in USERCONV and "route: via_operator" in USERCONV
    assert "spawn and steer the dev session yourself" in USERCONV
    assert "bsq peer send <operator-sid>" in USERCONV


def test_the_user_contract_does_not_restate_the_threshold_as_its_own():
    """The load ceilings live in `dispatch.decide_topology`. A number copied
    into prose is a second source of truth that cannot be updated by env and
    goes stale silently — the contract must defer to the verb."""
    assert "The threshold is the verb's, not yours" in USERCONV
    for numeric_restatement in (
            "at most 2 tasks", "fewer than 2 tasks", "up to two tasks"):
        assert numeric_restatement not in USERCONV.lower()


def test_the_operator_contract_knows_an_absent_operator_can_be_correct():
    """An operator that reads a quiet operator-less board as a fault spawns
    itself back in and undoes the whole ticket."""
    assert "not a fault" in OPERATOR
    assert "T-0855" in OPERATOR


# ---------------------------------------------------------------------------
# T-0937: the operator SEAT — the root drives the board itself. Same join, same
# reason: a capability the contract does not name is code no session invokes,
# and a contract naming a verb the CLI does not have is a session that cannot
# act on what it was told.
# ---------------------------------------------------------------------------

def test_the_seat_verbs_the_user_contract_names_actually_exist():
    assert "bsq operator seat claim" in USERCONV
    assert "bsq operator seat release" in USERCONV
    assert "bsq operator seat status" in USERCONV
    assert 'add_parser(\n        "seat"' in CLI, "no `operator seat` subcommand"
    for fn in ("cmd_operator_seat_claim", "cmd_operator_seat_release",
               "cmd_operator_seat_status"):
        assert f"def {fn}(" in CLI
    # ...and the verbs must reach the WORKER, which owns the rule — a CLI-local
    # claim would suppress nothing, since the 60s tick never reads the CLI.
    assert "operator_seat_claim" in CLI and "operator_seat_release" in CLI


def test_the_user_contract_says_the_seat_is_a_hat_not_a_morph():
    """The whole reason the seat exists rather than a role change: morphing the
    root to `operator` breaks its attendance/routing. A contract that lost this
    would have sessions morphing themselves and re-opening the bug."""
    assert "HAT, not a morph" in USERCONV
    assert "T-0937" in USERCONV


def test_the_operator_contract_knows_a_held_seat_is_not_a_fault():
    """An operator that reads a seat-held board as an operator-less one takes
    the board back and undoes the ticket — the same failure T-0855 guarded."""
    assert "T-0937" in OPERATOR
    assert "SEAT" in OPERATOR


def test_both_contracts_keep_the_one_dispatcher_invariant():
    """Direct dispatch narrows WHEN an operator exists; it never allows two
    dispatchers on one board (T-0472)."""
    assert "T-0472" in OPERATOR
    assert "second dispatcher" in USERCONV.lower()
