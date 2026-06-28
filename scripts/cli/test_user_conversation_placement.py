"""T-0510 (M11/F11.3): correct-placement guarantee.

The placement decision is made BY the transient user-conversation session, not
a hardcoded classifier (firehose paradigm). So the GUARANTEE lives in the
user-conversation ROLE CONTRACT (the git-tracked SSOT the spawn assembler /
`bsq brief` loads — see test_bsq_role_ssot.py / D-0043): the attendant must

  1. distinguish an INSTANT TWEAK (on/off, prioritize, write/correct a task or
     initiative) from a LONG REQUEST (real work: tasks/code/bugs),
  2. apply instant tweaks LIVE (no new ticket), and
  3. NEVER drop a long request — it becomes a task/initiative or reaches the
     right session.

This pins those directives as a content invariant so the contract can't
silently regress to "record everything as a task" (no triage) or lose the
no-drop guarantee. Markers are stakeholder vocabulary, kept loose enough not
to be brittle on rewording.
"""
from __future__ import annotations

from pathlib import Path

_ROLE_DOC = (
    Path(__file__).resolve().parents[2]
    / "api" / "app" / "resources" / "roles" / "user-conversation.md"
)


def _doc() -> str:
    return _ROLE_DOC.read_text(encoding="utf-8").lower()


def test_role_doc_exists():
    assert _ROLE_DOC.is_file(), f"missing role contract: {_ROLE_DOC}"


def test_contract_names_the_instant_tweak_vs_long_request_taxonomy():
    doc = _doc()
    assert "instant tweak" in doc, "contract must name the instant-tweak class"
    assert "long request" in doc, "contract must name the long-request class"


def test_contract_applies_instant_tweaks_live():
    doc = _doc()
    # An instant tweak is handled in-session, live — not turned into a ticket.
    assert "live" in doc
    # The stakeholder's concrete instant-tweak examples must be present so the
    # attendant can recognise them.
    for example in ("prioritize", "on/off"):
        assert example in doc, f"instant-tweak example missing: {example!r}"


def test_contract_guarantees_no_long_request_is_dropped():
    doc = _doc()
    # The no-drop guarantee: a long request always lands (task/initiative) or
    # reaches the right session.
    assert "drop" in doc, "contract must state the no-drop guarantee"
    assert ("task or initiative" in doc) or (
        "task/initiative" in doc
    ), "contract must say a long request becomes a task or initiative"


def test_thread_recipe_gives_concrete_base_and_bearer():
    """T-0543: the read/append recipes must spell out the API base + Bearer so an
    attendant doesn't waste turns trying :8080 / X-API-Key dead-ends (T-0542
    dogfood)."""
    doc = _doc()
    assert "127.0.0.1:8099" in doc, "recipe must give the concrete API base"
    assert "authorization: bearer" in doc, "recipe must show the Bearer auth header"
