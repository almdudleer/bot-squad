"""Tests for app.task_body — three-section schema parse/compose/append."""
from __future__ import annotations

import pytest

from app.task_body import (
    append_progress,
    compose_body,
    parse_body,
    regraft_progress,
    regraft_verbatim,
)


def test_parse_full_body():
    body = (
        "## Verbatim request\n\n"
        "I want X.\n\n"
        "## Context\n\n"
        "TL clarification: X means foo.\n\n"
        "## Progress\n\n"
        "- 2026-05-12T16:00:00Z · S-x · started\n"
    )
    out = parse_body(body)
    assert out["verbatim"] == "I want X."
    assert out["context"] == "TL clarification: X means foo."
    assert out["progress"] == "- 2026-05-12T16:00:00Z · S-x · started"


def test_parse_legacy_no_headings():
    body = "Free-form legacy body\nwith multiple lines.\n"
    out = parse_body(body)
    assert out["verbatim"] == "Free-form legacy body\nwith multiple lines."
    assert out["context"] == ""
    assert out["progress"] == ""


def test_parse_missing_sections():
    body = "## Verbatim request\n\nonly this.\n"
    out = parse_body(body)
    assert out["verbatim"] == "only this."
    assert out["context"] == ""
    assert out["progress"] == ""


def test_parse_case_insensitive_and_order():
    body = (
        "## Progress\n\n- 2026-05-12T00:00:00Z · S-1 · x\n\n"
        "## verbatim request\n\nhi\n\n"
        "## CONTEXT\n\nctx\n"
    )
    out = parse_body(body)
    assert out["verbatim"] == "hi"
    assert out["context"] == "ctx"
    assert out["progress"] == "- 2026-05-12T00:00:00Z · S-1 · x"


# --- T-0729: ANY level-2 heading ends a section -----------------------------
# The green "what you asked for" block on the task page renders `verbatim`.
# Before T-0729 only `## Verbatim request` / `## Context` / `## Progress` (exact
# match) were boundaries, so `## DoD`, `## Observed`, … got absorbed into the
# stakeholder's words on 414 of 700 live tickets.

def test_parse_stops_verbatim_at_dod_heading():
    body = "## Verbatim request\n\nI want X.\n\n## DoD\n\n- ship it\n"
    out = parse_body(body)
    assert out["verbatim"] == "I want X."
    assert "DoD" not in out["verbatim"]


def test_parse_stops_verbatim_at_observed_heading():
    body = (
        "## Verbatim request\n\nI want X.\n\n"
        "## Observed\n\nit exploded\n\n## Expected\n\nit works\n"
    )
    assert parse_body(body)["verbatim"] == "I want X."


def test_parse_decorated_context_heading_is_context():
    """`## Context (WS-1 gap analysis)` is the Context section, not an unknown
    one — and it still terminates verbatim."""
    body = (
        "## Verbatim request\n\nI want X.\n\n"
        "## Context (WS-1 gap analysis)\n\nthe gap\n\n"
        "## DoD\n\n- ship it\n"
    )
    out = parse_body(body)
    assert out["verbatim"] == "I want X."
    assert out["context"] == "the gap"


def test_parse_decorated_verbatim_heading_is_verbatim():
    """60 live tickets head the ask `## Verbatim request — source of truth
    (human-only, do not edit)`. That must parse as verbatim (not vanish)."""
    body = (
        "## Verbatim request — source of truth (human-only, do not edit)\n\n"
        "I want X.\n\n## DoD\n\n- ship it\n"
    )
    out = parse_body(body)
    assert out["verbatim"] == "I want X."


def test_parse_non_canonical_sections_are_not_surfaced():
    """Excluded from `verbatim`, and not returned anywhere else — the md on
    disk stays the SSOT for agent-authored sections."""
    body = "## Verbatim request\n\nask\n\n## DoD\n\n- d\n\n## Scope\n\n- s\n"
    out = parse_body(body)
    assert set(out) == {"verbatim", "context", "progress"}
    assert not any("DoD" in v or "Scope" in v for v in out.values())


def test_parse_legacy_body_with_agent_headings_keeps_whole_body():
    """No `## Verbatim request` heading at all → today's whole-body behaviour
    is preserved (13 live planning tickets whose ask IS the whole body)."""
    body = "The plan.\n\n## Scope\n\n- a\n\n## DoD\n\n- b\n"
    out = parse_body(body)
    assert out["verbatim"] == body.strip()


def test_parse_legacy_body_stops_at_first_canonical_heading():
    body = "Legacy ask.\n\n## Progress\n\n- T1 · S1 · note\n"
    out = parse_body(body)
    assert out["verbatim"] == "Legacy ask."
    assert out["progress"] == "- T1 · S1 · note"


def test_parse_h3_is_not_a_boundary():
    body = "## Verbatim request\n\nI want X.\n\n### sub-point\n\nstill the ask\n"
    assert parse_body(body)["verbatim"] == "I want X.\n\n### sub-point\n\nstill the ask"


def test_compose_skips_empty():
    assert compose_body("v", "", "") == "## Verbatim request\n\nv\n"
    assert compose_body("", "", "") == ""
    out = compose_body("v", "c", "- line")
    assert "## Verbatim request" in out
    assert "## Context" in out
    assert "## Progress" in out


def test_append_progress_creates_section():
    body = "## Verbatim request\n\nI want X.\n"
    new = append_progress(body, "2026-05-12T16:42:11Z", "S-a-b-p1", "did the thing")
    assert "I want X." in new
    assert "## Progress" in new
    assert "- 2026-05-12T16:42:11Z · S-a-b-p1 · did the thing" in new


def test_append_progress_preserves_other_sections():
    body = (
        "## Verbatim request\n\nVERBATIM\n\n"
        "## Context\n\nCONTEXT\n\n"
        "## Progress\n\n- old line\n"
    )
    new = append_progress(body, "T1", "S1", "new line")
    sections = parse_body(new)
    assert sections["verbatim"] == "VERBATIM"
    assert sections["context"] == "CONTEXT"
    assert "- old line" in sections["progress"]
    assert "- T1 · S1 · new line" in sections["progress"]


def test_append_progress_long_note_roundtrips_byte_identical():
    # Regression for F-2026-07-05-bsq-30844bca41: notes used to be silently
    # clipped at 240 chars, losing sacred stakeholder verbatims.
    body = "## Verbatim request\n\nv\n"
    note = ("stakeholder verbatim word " * 20).strip()
    assert len(note) > 300
    new = append_progress(body, "T1", "S1", note)
    parsed = parse_body(new)
    text_part = parsed["progress"].split(" · ", 2)[-1]
    assert text_part == note


def test_append_progress_overflow_raises():
    body = "## Verbatim request\n\nv\n"
    with pytest.raises(ValueError, match="cap"):
        append_progress(body, "T1", "S1", "x" * 5000)


def test_append_progress_collapses_newlines():
    body = "## Verbatim request\n\nv\n"
    new = append_progress(body, "T1", "S1", "line1\nline2\n\n  line3")
    parsed = parse_body(new)
    text_part = parsed["progress"].split(" · ", 2)[-1]
    assert text_part == "line1 line2 line3"


def test_append_progress_legacy_body_promotes_to_schema():
    body = "Legacy free-form body."
    new = append_progress(body, "T1", "S1", "first note")
    parsed = parse_body(new)
    assert parsed["verbatim"] == "Legacy free-form body."
    assert "- T1 · S1 · first note" in parsed["progress"]


def test_append_progress_preserves_non_canonical_sections():
    """T-0729 regression: a progress note must not delete `## DoD` / `## Scope`.

    `append_progress` used to parse→compose from the three canonical sections;
    once non-canonical sections stopped being absorbed into verbatim, that
    round-trip would have dropped them from 414 live tickets.
    """
    body = (
        "## Verbatim request\n\nask\n\n"
        "## DoD\n\n- ship it\n\n"
        "## Progress\n\n- T0 · S0 · old\n\n"
        "## Scope\n\n- only this\n"
    )
    new = append_progress(body, "T1", "S1", "new note")
    assert "## DoD\n\n- ship it" in new
    assert "## Scope\n\n- only this" in new
    assert parse_body(new)["verbatim"] == "ask"
    progress = parse_body(new)["progress"]
    assert "- T0 · S0 · old" in progress
    assert "- T1 · S1 · new note" in progress


def test_append_progress_empty_text_raises():
    import pytest
    with pytest.raises(ValueError):
        append_progress("## Verbatim request\n\nv\n", "T1", "S1", "   ")


# --- T-0335 item-14: regraft_progress (append-only Progress is on-disk SSOT) --

def test_regraft_progress_restores_on_disk_feed():
    """A body edit that rewrites Progress is forced back to the on-disk feed;
    other sections (Context) keep the caller's edit."""
    original = (
        "## Verbatim request\n\nV\n\n## Context\n\nold\n\n"
        "## Progress\n\n- T1 · S1 · real note\n"
    )
    edited = (
        "## Verbatim request\n\nV\n\n## Context\n\nNEW context\n\n"
        "## Progress\n\n- forged · fake · agent rewrite\n"
    )
    out = regraft_progress(original, edited)
    sections = parse_body(out)
    assert "real note" in sections["progress"]
    assert "forged" not in sections["progress"]
    assert sections["context"] == "NEW context"


def test_regraft_progress_reappends_when_caller_drops_it():
    """A body that drops the Progress heading must not lose the feed."""
    original = "## Verbatim request\n\nV\n\n## Progress\n\n- T1 · S1 · keep me\n"
    edited = "## Verbatim request\n\nV\n\n## Context\n\nc\n"
    out = regraft_progress(original, edited)
    assert "keep me" in parse_body(out)["progress"]
    assert "c" in parse_body(out)["context"]


def test_regraft_progress_noop_when_original_has_none():
    """No on-disk Progress → nothing to protect, caller's body is untouched."""
    original = "## Verbatim request\n\nV\n\n## Context\n\nc\n"
    edited = "## Verbatim request\n\nV\n\n## Progress\n\n- new · feed · x\n"
    out = regraft_progress(original, edited)
    assert "new · feed · x" in parse_body(out)["progress"]


# --- T-0481 / M3+M8: regraft_verbatim (Verbatim is human-only — read-only on
# every body write). These mirror the regraft_progress unit tests and pin the
# function's branches directly (previously covered only via PATCH integration).

def test_regraft_verbatim_restores_original():
    """A body edit that rewrites Verbatim is forced back to the on-disk words;
    other sections (Context) keep the caller's edit."""
    original = "## Verbatim request\n\nSTAKEHOLDER WORDS\n\n## Context\n\nold\n"
    edited = "## Verbatim request\n\nHIJACKED by an agent\n\n## Context\n\nNEW context\n"
    out = regraft_verbatim(original, edited)
    sections = parse_body(out)
    assert sections["verbatim"] == "STAKEHOLDER WORDS"
    assert "HIJACKED" not in out
    assert sections["context"] == "NEW context"


def test_regraft_verbatim_reappends_when_caller_drops_it():
    """A body that drops the Verbatim heading must not lose it — the original
    section is re-prepended."""
    original = "## Verbatim request\n\nKEEP THIS\n\n## Context\n\nc\n"
    edited = "## Context\n\nonly context now\n"
    out = regraft_verbatim(original, edited)
    sections = parse_body(out)
    assert sections["verbatim"] == "KEEP THIS"
    assert sections["context"] == "only context now"


def test_regraft_verbatim_noop_when_original_has_none():
    """No on-disk Verbatim → nothing to protect, caller's body is untouched
    (legacy/QA tickets without a canonical Verbatim section)."""
    original = "## Context\n\nc\n"
    edited = "## Verbatim request\n\nadded by caller\n\n## Context\n\nc\n"
    out = regraft_verbatim(original, edited)
    assert parse_body(out)["verbatim"] == "added by caller"


def test_regraft_verbatim_preserves_non_canonical_sections():
    """Re-grafting Verbatim must not mangle non-canonical sections (Finding/DoD
    on QA tickets): the DoD edit lands, only Verbatim is forced back."""
    original = "## Verbatim request\n\nSACRED\n\n## Finding\n\nbug\n\n## DoD\n\nold dod\n"
    edited = "## Verbatim request\n\nTAMPER\n\n## Finding\n\nbug\n\n## DoD\n\nnew dod\n"
    out = regraft_verbatim(original, edited)
    assert "SACRED" in out and "TAMPER" not in out
    assert "new dod" in out and "## Finding" in out


def test_regraft_verbatim_protects_decorated_heading():
    """T-0729: the write-protection path recognises the same decorated headings
    the read-parse path does — 60 live tickets had NO verbatim protection
    because their heading carried a suffix."""
    head = "## Verbatim request — source of truth (human-only, do not edit)"
    original = f"{head}\n\nSACRED\n\n## DoD\n\nold dod\n"
    edited = f"{head}\n\nTAMPER\n\n## DoD\n\nnew dod\n"
    out = regraft_verbatim(original, edited)
    assert "SACRED" in out and "TAMPER" not in out
    assert "new dod" in out
