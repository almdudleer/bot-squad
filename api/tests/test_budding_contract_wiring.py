"""T-0932: gradual budding is wired end to end — contract → verb → worker.

The ladder itself is pinned in code (``worker/tests/test_budding.py``). What
this file guards is the JOIN, in the shape T-0855's sibling
(``test_direct_dispatch_contract_wiring.py``) established: a root session only
buds if its contract names a verb that exists, and the verb only works if it
asks the worker for the rule. Both halves are prose on one side of the seam,
and prose drifts — so a machine checks the pointers rather than a reviewer
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

CLI = (ROOT / "scripts" / "cli" / "bsq").read_text(encoding="utf-8")
USERCONV = (ROOT / "api" / "app" / "resources" / "roles"
            / "user-conversation.md").read_text(encoding="utf-8")


def test_every_verb_the_contract_names_exists_in_the_cli():
    """A contract naming a verb the session cannot invoke is documentation of a
    capability, not a capability."""
    for verb, handler in (
        ("bsq bud dev", "def cmd_bud_dev("),
        ("bsq bud operator", "def cmd_bud_operator("),
        ("bsq bud absorb", "def cmd_bud_absorb("),
    ):
        assert verb in USERCONV, f"the contract stopped naming `{verb}`"
        assert handler in CLI, f"`{verb}` has no handler in the CLI"
    assert 'add_parser(\n        "bud"' in CLI, "no `bud` subcommand in the CLI"
    assert "def cmd_bud_status(" in CLI


def test_the_verb_asks_the_WORKER_for_the_rule():
    """The trigger must be the worker's read, not a threshold the CLI (or the
    session) re-derives — that is how a session and the scheduler end up
    disagreeing about which rung the project is on."""
    assert "budding_decision" in CLI
    assert "bud_operator" in CLI


def test_the_contract_carries_all_three_rungs_not_just_the_climb():
    """Deflation is half the ask («путешествие по разным энергетическим
    уровням» goes both ways). A contract that only describes growing the tree
    would leave every project it ever inflated permanently inflated."""
    for rung in ("L0", "L1", "L2"):
        assert rung in USERCONV, f"the contract lost the {rung} rung"
    assert "bsq bud absorb" in USERCONV, "no deflation path in the contract"


def test_the_contract_states_that_budding_only_ever_suggests():
    """The conservative half of the design, and the stakeholder's T-0929 line:
    a contract that read as "the system will bud you" would invite a session to
    wait for it instead of deciding."""
    low = USERCONV.lower()
    assert "never fires on its own" in low or "suggests" in low
    assert "bsq bud" in USERCONV


def test_the_contract_does_not_still_call_parallel_user_requests_theoretical():
    """The pre-T-0932 contract told a session under exactly this pressure that
    nothing could be done about it ("currently theoretical … flag it to the
    stakeholder"). Shipping the capability without retiring that sentence would
    leave the contract steering sessions away from the verb that now exists."""
    assert "currently theoretical" not in USERCONV
