"""Tests for bot_squad_worker.task_body — mirror of api test_task_body."""
from __future__ import annotations

import pytest

from bot_squad_worker.task_body import append_progress, compose_body, parse_body


def test_parse_full_body():
    body = (
        "## Verbatim request\n\n"
        "I want X.\n\n"
        "## Context\n\n"
        "TL clarification.\n\n"
        "## Progress\n\n"
        "- 2026-05-12T16:00:00Z · S-x · started\n"
    )
    out = parse_body(body)
    assert out["verbatim"] == "I want X."
    assert out["context"] == "TL clarification."
    assert out["progress"] == "- 2026-05-12T16:00:00Z · S-x · started"


def test_parse_legacy_no_headings():
    body = "Free-form legacy body."
    out = parse_body(body)
    assert out["verbatim"] == "Free-form legacy body."
    assert out["context"] == ""
    assert out["progress"] == ""


def test_compose_skips_empty():
    assert compose_body("v", "", "") == "## Verbatim request\n\nv\n"
    assert compose_body("", "", "") == ""


def test_append_progress_creates_section():
    body = "## Verbatim request\n\nI want X.\n"
    new = append_progress(body, "2026-05-12T16:42:11Z", "S-a-b-p1", "did the thing")
    assert "I want X." in new
    assert "- 2026-05-12T16:42:11Z · S-a-b-p1 · did the thing" in new


def test_append_progress_preserves_sections():
    body = (
        "## Verbatim request\n\nVERBATIM\n\n"
        "## Context\n\nCONTEXT\n\n"
        "## Progress\n\n- old line\n"
    )
    new = append_progress(body, "T1", "S1", "new line")
    parsed = parse_body(new)
    assert parsed["verbatim"] == "VERBATIM"
    assert parsed["context"] == "CONTEXT"
    assert "- old line" in parsed["progress"]
    assert "- T1 · S1 · new line" in parsed["progress"]


def test_append_progress_caps_at_240():
    body = "## Verbatim request\n\nv\n"
    new = append_progress(body, "T1", "S1", "x" * 500)
    parsed = parse_body(new)
    text_part = parsed["progress"].split(" · ", 2)[-1]
    assert len(text_part) == 240


def test_append_progress_collapses_newlines():
    body = "## Verbatim request\n\nv\n"
    new = append_progress(body, "T1", "S1", "line1\nline2\n\n  line3")
    parsed = parse_body(new)
    text_part = parsed["progress"].split(" · ", 2)[-1]
    assert text_part == "line1 line2 line3"


def test_append_progress_empty_text_raises():
    with pytest.raises(ValueError):
        append_progress("## Verbatim request\n\nv\n", "T1", "S1", "   ")
