"""T-1028: the T-0977 "declare a block" guidance in dev.md / qa.md had no test.

WHAT WAS MISSING. `ff73b1a` (T-0977) taught the contracts that a block is the
SENDER'S DECLARATION (`bsq peer send --blocked <to> "..."`) and that a plain
report — READY, FYI, finding, handoff — marks nothing, whoever it goes to.
That guidance landed in ``dev.md`` and ``qa.md`` (byte-identical bullet, no
heading of its own — it sits inline in each file's lead/Coordination list) and
never got an assertion of its own.

WHY THAT READS AS COVERAGE IT ISN'T. ``test_role_doc_ship_without_permission.py``
is a green, well-known role-doc test file, and it is easy to assume "the
role docs are checked" once you've seen it pass. But it polices a DIFFERENT
rule (T-0903, the deploy/restart permission round-trip) over a DIFFERENT role
set (``operator``, ``teamlead``, ``prod-teamlead``) — and it excludes ``dev``
ON PURPOSE, because a dev never deploys; that file's own docstring says so and
gives dev a separate, narrower assertion
(`test_dev_contract_no_longer_routes_prod_deploys_through_him`) for the one
T-0903 claim that DOES apply to it (the stakeholder-permission gate is gone).
That exclusion is correct and this ticket does not touch it — it is simply
answering a different question than "does dev carry the T-0977 text", which
no test answered until this file.

SCOPE. This file reads exactly the two named contracts
(``api/app/resources/roles/{dev,qa}.md``) directly by path — it is not a
repo-wide scanner, so it cannot self-match its own fixture strings the way a
grep-based census can (measured elsewhere in this repo, three times in one
night, per the T-1028 brief).

Every check here is proven capable of failing (`test_the_checks_above_can_actually_fail`):
mutate a copy, watch each assertion go red, restore it. A guard that has
never been observed failing pins nothing (T-0740).
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROLES_DIR = Path(__file__).resolve().parents[1] / "app" / "resources" / "roles"

REQUIRED_ROLES = ("dev", "qa")

MARKER = "- **Declare a block; a report declares nothing (T-0977).**"

# Load-bearing claims inside the block, flat (whitespace-normalized) since the
# docs are hard-wrapped and wrap at different columns per file. Dropping any
# one of these silently would leave the bullet looking intact while the thing
# it actually fixes (the sender-declares-it, not the recipient's role) walks
# back out — the exact T-0977 defect.
REQUIRED_CLAIMS = (
    "Declare a block; a report declares nothing (T-0977).",
    "`bsq peer send --blocked <to>",
    "is what tells the stall-watchdog you are STOPPED until that message is answered",
    "it escalates upstream if nobody replies, and clears when they do",
    "A plain `bsq peer send` — a READY, an FYI, a finding, a handoff — marks nothing",
    "Until T-0977 the watchdog guessed from the recipient's role",
    "do not go back to relying on that.",
)


def read_role(stem: str) -> str:
    return (ROLES_DIR / f"{stem}.md").read_text(encoding="utf-8")


def flat(text: str) -> str:
    return " ".join(text.split())


def block_of(text: str) -> str:
    """The bullet itself: from the marker to the next bullet or heading.

    There is no ``##`` section for this rule — it's one bullet inside a
    larger list in each file — so the end is whichever comes first: the next
    top-level heading, or the next bold-led bullet at the same indent.
    """
    start = text.index(MARKER)
    candidates = [i for i in (text.find("\n## ", start + 1), text.find("\n- **", start + 1)) if i != -1]
    end = min(candidates) if candidates else len(text)
    # qa.md's bullet is followed by a blank line before the next heading;
    # dev.md's is followed immediately by the next bullet. Trim that
    # incidental trailing whitespace so the two extraction sites are
    # comparable — the bullet's own text is what must match, not the layout
    # of whatever comes after it.
    return text[start:end].rstrip()


@pytest.mark.parametrize("role", REQUIRED_ROLES)
def test_the_rule_is_present(role):
    assert MARKER in read_role(role), (
        f"{role}.md has no T-0977 declare-a-block bullet — a session reading "
        "this contract has no way to know a report does not mark a block."
    )


@pytest.mark.parametrize("role", REQUIRED_ROLES)
@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
def test_every_load_bearing_claim_is_present(role, claim):
    block = flat(block_of(read_role(role)))
    assert claim in block, f"{role}.md dropped from the T-0977 bullet: {claim!r}"


def test_the_two_copies_are_byte_identical():
    """One rule, two contracts. Prose elsewhere may differ per role; this
    bullet may not — a divergent copy is a second, unreviewed variant of the
    same instruction."""
    blocks = {r: block_of(read_role(r)) for r in REQUIRED_ROLES}
    distinct = set(blocks.values())
    assert len(distinct) == 1, (
        "the T-0977 declare-a-block bullet has diverged between role docs — "
        f"{len(distinct)} variants across {sorted(blocks)}."
    )


def test_the_checks_above_can_actually_fail():
    """Positive control (T-0740): drive the same extractors over deliberately
    drifted copies and require every assertion above to notice."""
    good = read_role("dev")

    # 1. The whole bullet removed.
    without = good.replace(block_of(good), "", 1)
    assert without != good, "fixture did not mutate — the control proves nothing"
    assert MARKER not in without

    # 2. One load-bearing claim dropped, the rest of the bullet intact — the
    #    exact "still reads plausible" drift a whole-block presence check
    #    would miss.
    no_escalation = good.replace(
        "it escalates upstream if nobody\n  replies, and clears when they do "
        "(or when your pane resumes work).",
        "and clears when they do (or when your pane resumes work).",
        1,
    )
    assert no_escalation != good, "escalation fixture did not mutate"
    assert "it escalates upstream if nobody replies, and clears when they do" \
        not in flat(block_of(no_escalation))

    # 3. The core inversion dropped — a plain report would again read as
    #    marking something.
    no_inversion = good.replace(
        "A plain\n  `bsq peer send` — a READY, an FYI, a finding, a handoff — "
        "marks nothing,\n  whoever the recipient is.",
        "A plain `bsq peer send` also marks a block.",
        1,
    )
    assert no_inversion != good, "inversion fixture did not mutate"
    assert "marks nothing" not in flat(block_of(no_inversion))

    # 4. Divergence between the two copies.
    blocks = {r: block_of(read_role(r)) for r in REQUIRED_ROLES}
    blocks["qa"] = blocks["qa"] + " Also: this only applies on Tuesdays."
    assert len(set(blocks.values())) == 2
