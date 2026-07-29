"""T-0777: the no-dramatising-preamble rule must stay IDENTICAL in six role docs.

Why this exists
---------------
The stakeholder asked (2026-07-29, T-0777) for one rule about how anything he
reads must open: at the fact, with no dramatising wrapper and no candour
intensifier. He named the banned openers as concrete quotable phrases, twice —
so the rule is a NAMED LIST, not a style preference, and the list is the part
that has to be checkable.

It lives inline in six role contracts because that is the only surface a
session is served automatically: ``bsq``'s ``_assemble_prompt`` inlines the
role doc verbatim into the spawn brief, and ``bsq brief`` re-prints it. A
pointer to a shared file would be read-on-demand, which is exactly the reach
problem the ticket was opened to fix.

Six copies of one list is a drift hazard — this repo's top bug class, and the
T-0774 lesson (three overlapping DROPS fixtures, each defending a different
reader, none reaching the source). This pin is what keeps the six honest: the
prose register per role may differ (an operator pages, a prod-TL reports an
outage, an attendant relays), the LIST may not.

Scope note (T-0784): ``qa.md`` WAS excluded here, on the stated ground that it
"has no call site that writes to the stakeholder". That claim was false and
checkable in one grep — ``qa.md``'s escalation line ("only TG the stakeholder
if there's no TL or you've been stuck") is the same sanctioned path as
``dev.md``'s, which is the line used to justify putting dev in scope. What made
it urgent rather than cosmetic: T-0778 gave qa sessions their own ``qa.md`` —
before that they were mis-routed to ``dev.md`` and INCIDENTALLY inherited the
rule — so the routing fix would otherwise have silently removed a coverage
nobody recorded as depending on it. This list asserts a required MINIMUM rather
than an exact set precisely so widening it is an addition, not a red build.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROLES_DIR = Path(__file__).resolve().parents[1] / "app" / "resources" / "roles"

HEADING = "## Writing to the stakeholder — START WITH THE FACT"

# The roles whose sessions compose prose the stakeholder reads — set on the
# call sites, not defensively (2026-07-29). `qa` added by T-0784: its call site
# is the same sanctioned "TG him if there's no TL or you've been stuck" line.
REQUIRED_ROLES = ("operator", "teamlead", "user-conversation", "dev", "prod-teamlead",
                  "qa")

# Every opener he named, both messages. Dropping one silently would leave the
# rule looking intact while the thing he pointed at walked out of it.
BANNED_OPENERS = (
    "одно изменение, о котором говорю сразу, а не молча",
    "поправка, и неприятная",
    "лучше скажу сразу, а не потом",
    "«честно»",
    "«честно говоря»",
    "«если честно»",
    '"I want to flag this before you find it"',
    '"being upfront here"',
    '"this is the uncomfortable part"',
    '"honestly"',
    '"to be honest"',
    '"frankly"',
)

# The scope limit. It travels with the rule in EVERY copy — on the ticket alone
# it would not reach the session that has to obey it. The obvious misreading
# ("he does not want to hear bad things") inverts a presentation rule into a
# licence to hide, so each doc names that inversion too.
LIMIT = ("**This is a PRESENTATION rule and it never licenses omitting, delaying "
         "or softening the fact.**")
INVERSION = '"he does not want to hear bad things" has inverted it'


def read_role(stem: str) -> str:
    return (ROLES_DIR / f"{stem}.md").read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Whitespace-normalized view, for PROSE checks only.

    The docs are hard-wrapped, so a sentence may break at any column and does
    so at different columns per role — `user-conversation.md` wraps mid-quote
    inside the inversion warning. Matching raw would fail on reflow, which is
    noise, not drift. The banned-opener LIST is compared unnormalized: there,
    an unexplained whitespace difference IS the drift.
    """
    return " ".join(text.split())


def block_of(text: str) -> str:
    """The rule's section: its heading through the next top-level heading."""
    start = text.index(HEADING)
    rest = text.find("\n## ", start + 1)
    return text[start:rest if rest != -1 else len(text)]


def banned_list_of(text: str) -> str:
    """The checkable part: the bullet lines naming the openers, verbatim.

    Compared byte-for-byte across the copies — this is what may not drift.
    """
    lines = block_of(text).splitlines()
    first = next(i for i, l in enumerate(lines) if l.startswith("- «"))
    last = max(i for i, l in enumerate(lines) if l.startswith("- ") or l.startswith("  "))
    return "\n".join(lines[first:last + 1])


@pytest.mark.parametrize("role", REQUIRED_ROLES)
def test_stakeholder_facing_role_carries_the_rule(role):
    """Each role that writes to him is served the rule in its own contract."""
    assert HEADING in read_role(role), (
        f"{role}.md has no T-0777 writing rule. Its session composes prose the "
        "stakeholder reads, so a pointer elsewhere would not reach it."
    )


@pytest.mark.parametrize("role", REQUIRED_ROLES)
def test_every_banned_opener_is_named(role):
    """The list is the enforceable part — 'write plainly' is not a rule."""
    # Against the LIST, not the whole block: «честно говоря» is also quoted in
    # the prose that explains why it is banned, so a block-wide check stays
    # green on a doc whose list quietly lost it (measured — that near-miss is
    # why this reads banned_list_of).
    listed = flat(banned_list_of(read_role(role)))
    missing = [o for o in BANNED_OPENERS if o not in listed]
    assert not missing, f"{role}.md dropped banned opener(s) from the list: {missing}"


def test_banned_opener_list_is_byte_identical_across_the_copies():
    """Six copies, one list. Register may differ per role; the list may not."""
    lists = {role: banned_list_of(read_role(role)) for role in REQUIRED_ROLES}
    distinct = set(lists.values())
    assert len(distinct) == 1, (
        "the banned-opener list has diverged between role docs — "
        f"{len(distinct)} variants across {sorted(lists)}. Fix the copies to "
        "match rather than teaching this test about a second variant."
    )


@pytest.mark.parametrize("role", REQUIRED_ROLES)
def test_the_presentation_limit_travels_with_the_rule(role):
    """Without the limit the rule reads as 'send him less bad news', which is
    strictly worse than the verbosity it replaces."""
    block = flat(block_of(read_role(role)))
    assert LIMIT in block, f"{role}.md states the ban without the presentation-only limit"
    assert INVERSION in block, f"{role}.md does not name the inversion the limit guards against"


def test_the_checks_above_can_actually_fail():
    """Positive control (T-0740): a guard that passes with the defect present
    pins nothing. Drive the same extractors over a deliberately drifted copy
    and require every assertion to notice."""
    good = read_role("operator")
    # One plausible drift: a paraphrase that reads fine and is not his word.
    # Replace it in the LIST only — the phrase also appears in the prose below
    # it, and a blanket replace would let a half-passing check look decisive.
    drifted = good.replace("«честно» · «честно говоря» · «если честно»",
                           "«честно» · «по правде говоря» · «если честно»", 1)
    assert drifted != good, "fixture did not mutate — the control proves nothing"

    assert banned_list_of(drifted) != banned_list_of(good)          # identity check bites
    assert "«честно говоря»" not in banned_list_of(drifted)         # opener check bites

    # And the limit check bites when the limit is dropped.
    without_limit = good.replace(
        "**This is a PRESENTATION rule and it never licenses omitting, delaying or\n"
        "softening the fact.**", "Be nice about it.", 1)
    assert without_limit != good, "limit fixture did not mutate"
    assert LIMIT not in flat(block_of(without_limit))
