"""Tests for app.task_body — three-section schema parse/compose/append."""
from __future__ import annotations

from app.task_body import append_progress, compose_body, parse_body


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
