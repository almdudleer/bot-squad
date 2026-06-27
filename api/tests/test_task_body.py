"""Tests for app.task_body — three-section schema parse/compose/append."""
from __future__ import annotations

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


def test_append_progress_caps_at_240():
    body = "## Verbatim request\n\nv\n"
    long_text = "x" * 500
    new = append_progress(body, "T1", "S1", long_text)
    # The line itself is prefixed with "- T1 · S1 · " — the *text* portion is capped.
    parsed = parse_body(new)
    progress_line = parsed["progress"]
    text_part = progress_line.split(" · ", 2)[-1]
    assert len(text_part) == 240
    assert text_part == "x" * 240


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
