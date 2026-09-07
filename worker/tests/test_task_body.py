"""Tests for bot_squad_worker.task_body — mirror of api test_task_body."""
from __future__ import annotations

import pytest

import hashlib

from bot_squad_worker.task_body import (
    STAKEHOLDER_HEADING,
    append_progress,
    append_stakeholder_quote,
    compose_body,
    decode_progress_text,
    encode_progress_text,
    parse_body,
    SECTION_KEYS,
    SUMMARY_HEADING,
    set_context,
    set_summary,
)


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
    assert compose_body("v", "", "") == "## Stakeholder notes\n\nv\n"
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


def test_append_progress_long_note_roundtrips_byte_identical():
    # Regression for F-2026-07-05-bsq-30844bca41: notes used to be silently
    # CLIPPED — that is the defect, not the cap's value. T-0767 restored the
    # 240 the role contracts state; the >300-char sacred verbatim that
    # motivated the report is pinned on `append_stakeholder_quote` below.
    body = "## Stakeholder notes\n\nv\n"
    note = ("stakeholder verbatim word " * 8).strip()
    assert 200 < len(note) <= 240
    new = append_progress(body, "T1", "S1", note)
    parsed = parse_body(new)
    text_part = parsed["progress"].split(" · ", 2)[-1]
    assert text_part == note


def test_append_progress_overflow_raises():
    body = "## Verbatim request\n\nv\n"
    with pytest.raises(ValueError, match="cap"):
        append_progress(body, "T1", "S1", "x" * 5000)


def test_append_progress_single_line_prose_is_stored_unchanged():
    """GREEN CONTROL for the round-trip below (T-0835).

    A single-line prose note — the overwhelming majority of the feed — must be
    stored byte-identically, with NO escape sequence appearing. Without this
    arm, a red on the multi-line test could equally mean "structure is lost" or
    "everything changed"; with it, a red means exactly the first.
    """
    body = "## Verbatim request\n\nv\n"
    note = "measured on disk: line 2 is 4055 bytes and ends mid-word — see re.sub(r'\\s+', ' ')"
    new = append_progress(body, "T1", "S1", note)
    stored = parse_body(new)["progress"].split(" · ", 2)[-1]
    assert stored == note            # at rest: untouched, no escaping
    assert decode_progress_text(stored) == note   # and the read half is a no-op


def test_append_progress_multiline_note_round_trips_byte_identical():
    """T-0835 RED ARM — the test that goes red without the encode/decode pair.

    THE CLAIM, in the terms a caller cares about: a note can be READ BACK AS
    THE THING IT WAS WRITTEN AS. Not "did the write succeed" — `ticket note`
    already returned success while flattening, which is the whole defect — and
    not "are all the bytes there", which was ALSO true of the flattened
    `sweep.sh` that this pins the fix for: every byte survived and the script
    was inert.

    Against the pre-fix `re.sub(r"\\s+", " ", text)` this fails on the first
    assert of the hash pair; the physical-shape asserts above it stay green,
    which is the point — the storage contract is not what was broken.
    """
    body = "## Verbatim request\n\nv\n"
    note = (
        "M1-M10 sweep, verdicts:\n"
        "\n"
        "| id | mutation            | verdict |\n"
        "|----|---------------------|---------|\n"
        "| M1 | drop the hook       | RED     |\n"
        "\n"
        "```bash\n"
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "grep -c 'x' file\t# a tab lives here\n"
        "```"
    )
    new = append_progress(body, "T1", "S1", note)
    progress = parse_body(new)["progress"]

    # STILL one physical line: the `- <ts> · <sid> · <text>` contract that
    # TaskDetail.parseProgressList, `bsq session-search` and drift.py's two
    # regexes all parse by line is NOT widened by this fix.
    assert len(progress.splitlines()) == 1
    assert progress.startswith("- T1 · S1 · ")

    stored = progress.split(" · ", 2)[-1]
    read_back = decode_progress_text(stored)
    assert (
        hashlib.sha256(read_back.encode("utf-8")).hexdigest()
        == hashlib.sha256(note.encode("utf-8")).hexdigest()
    )
    assert read_back == note   # named explicitly so a red says WHAT differs


def test_decode_leaves_unknown_and_literal_escapes_alone():
    """The decoder must not invent structure that was never written (T-0835).

    `\\s` is not an escape this codec defines — a note quoting
    `re.sub(r"\\s+", ...)` must read back as those two characters. And a note
    that literally contains a backslash followed by `n` survives as such,
    because encode doubles the backslash first and decode is a single scan
    rather than chained replaces.
    """
    assert decode_progress_text(r"re.sub(r'\s+', ' ')") == r"re.sub(r'\s+', ' ')"
    assert encode_progress_text(r"re.sub(r'\s+', ' ')") == r"re.sub(r'\s+', ' ')"
    literal = r"the two characters \n, written out"
    assert decode_progress_text(encode_progress_text(literal)) == literal
    assert "\n" not in encode_progress_text(literal)


def test_encode_decode_round_trip_is_exhaustive_over_the_colliding_alphabet():
    """Every string over the characters that can collide round-trips (T-0835).

    The escape rule is minimal — a backslash is protected only when it precedes
    one the decoder would claim — so "it works on my example" is not evidence.
    This brute-forces every string up to length 4 over the alphabet where a
    mistake could hide: backslash, the three escape letters, a real newline and
    a real tab. 1554 cases; a chained-replace decoder fails it.
    """
    import itertools
    alphabet = "\\nrt\n\ta"
    for size in range(1, 5):
        for combo in itertools.product(alphabet, repeat=size):
            s = "".join(combo)
            enc = encode_progress_text(s)
            assert "\n" not in enc and "\r" not in enc and "\t" not in enc
            assert decode_progress_text(enc) == s, (s, enc)


def test_append_progress_overflow_names_both_lengths_when_escaped():
    """The refusal must quote a number the CALLER can reconcile with its input.

    The cap applies to the STORED form (that is what has to fit), but a caller
    that passed 3990 characters and is told "4200" cannot act on it — so when
    escaping changed the length, the message carries both.
    """
    body = "## Verbatim request\n\nv\n"
    note = "\n".join(["x" * 39] * 100)   # 3999 raw, +99 escaped newlines
    with pytest.raises(ValueError) as exc:
        append_progress(body, "T1", "S1", note)
    msg = str(exc.value)
    assert f"{len(note)} chars" in msg
    assert "stored, newlines escaped" in msg
    assert "refusing to truncate" in msg


def test_append_progress_empty_text_raises():
    with pytest.raises(ValueError):
        append_progress("## Verbatim request\n\nv\n", "T1", "S1", "   ")


# --- T-0729: ANY level-2 heading ends a section (mirror of the api tests) ----

def test_parse_stops_verbatim_at_dod_heading():
    body = "## Verbatim request\n\nI want X.\n\n## DoD\n\n- ship it\n"
    assert parse_body(body)["verbatim"] == "I want X."


def test_parse_stops_verbatim_at_observed_heading():
    body = "## Verbatim request\n\nI want X.\n\n## Observed\n\nit exploded\n"
    assert parse_body(body)["verbatim"] == "I want X."


def test_parse_decorated_context_heading_is_context():
    body = (
        "## Verbatim request\n\nI want X.\n\n"
        "## Context (WS-1 gap analysis)\n\nthe gap\n\n## DoD\n\n- ship it\n"
    )
    out = parse_body(body)
    assert out["verbatim"] == "I want X."
    assert out["context"] == "the gap"


def test_parse_decorated_verbatim_heading_is_verbatim():
    body = (
        "## Verbatim request — source of truth (human-only, do not edit)\n\n"
        "I want X.\n\n## DoD\n\n- ship it\n"
    )
    assert parse_body(body)["verbatim"] == "I want X."


def test_parse_legacy_body_with_agent_headings_keeps_whole_body():
    body = "The plan.\n\n## Scope\n\n- a\n\n## DoD\n\n- b\n"
    assert parse_body(body)["verbatim"] == body.strip()


def test_append_progress_preserves_non_canonical_sections():
    """A progress note must not delete `## DoD` / `## Scope` — the worker is
    the writer for `task_progress_add`, so this is where the loss would land."""
    body = (
        "## Verbatim request\n\nask\n\n## DoD\n\n- ship it\n\n"
        "## Progress\n\n- T0 · S0 · old\n\n## Scope\n\n- only this\n"
    )
    new = append_progress(body, "T1", "S1", "new note")
    assert "## DoD\n\n- ship it" in new
    assert "## Scope\n\n- only this" in new
    assert parse_body(new)["verbatim"] == "ask"
    assert "- T0 · S0 · old" in parse_body(new)["progress"]
    assert "- T1 · S1 · new note" in parse_body(new)["progress"]


# --- T-0767: worker-side copies of the two-artifact guarantees --------------

def test_legacy_and_new_stakeholder_headings_both_parse():
    assert parse_body("## Verbatim request\n\nask\n")["verbatim"] == "ask"
    assert parse_body("## Stakeholder notes\n\nask\n")["verbatim"] == "ask"


def test_sacred_verbatim_over_the_note_cap_roundtrips_as_a_stakeholder_quote():
    """The F-2026-07-05-bsq-30844bca41 case on the writer that now owns it."""
    quote = ("stakeholder verbatim word " * 40).strip()
    assert len(quote) > 300
    new = append_stakeholder_quote("## Stakeholder notes\n\nask\n",
                                   "T1", "2026-08-11", quote)
    assert "ask" in new
    assert parse_body(new)["verbatim"].split(" · ", 2)[-1] == quote


def test_set_context_replaces_and_keeps_the_feed():
    body = ("## Stakeholder notes\n\nv\n\n## Context\n\nOLD\n\n"
            "## Progress\n\n- a\n")
    out = set_context(body, "NEW")
    assert "OLD" not in out and "NEW" in out
    assert "- a" in parse_body(out)["progress"]


# --- T-1045: `set_context` must not silently drop a DoD or a stakeholder
# quote filed INSIDE `## Context` (mirror of the api tests). HEALTHY case
# first (DoD item 2): the guard must not make the verb unusable normally.

def test_set_context_healthy_case_with_no_dod_replaces_cleanly():
    body = "## Stakeholder notes\n\nv\n\n## Context\n\nold state\n"
    out = set_context(body, "new state")
    assert parse_body(out)["context"] == "new state"


def test_set_context_refuses_when_context_has_dod_and_replacement_drops_it():
    body = ("## Stakeholder notes\n\nv\n\n## Context\n\n"
            "### DoD\n\n1. ship it\n2. test it\n")
    with pytest.raises(ValueError, match="DoD"):
        set_context(body, "fresh session state, no DoD mentioned")
    assert parse_body(body)["context"] == "### DoD\n\n1. ship it\n2. test it"


def test_set_context_allows_when_replacement_carries_the_dod_forward():
    body = ("## Stakeholder notes\n\nv\n\n## Context\n\n"
            "### DoD\n\n1. ship it\n")
    out = set_context(body, "### DoD\n\n1. ship it\n\nsession notes here")
    ctx = parse_body(out)["context"]
    assert "### DoD" in ctx
    assert "session notes here" in ctx


def test_set_context_guard_ignores_a_dod_populated_normally_outside_context():
    """POSITIVE CONTROL (T-1045 DoD item 3): a normal top-level `## DoD`,
    never pasted into Context, must never trip the guard."""
    body = ("## Stakeholder notes\n\nv\n\n## DoD\n\n1. ship it\n\n"
            "## Context\n\nordinary working notes, no DoD in sight\n")
    out = set_context(body, "updated working notes")
    assert parse_body(out)["context"] == "updated working notes"
    assert "1. ship it" in out


def test_set_context_refuses_when_context_has_a_stakeholder_quote_and_replacement_drops_it():
    """The URGENT arm (DoD item 1b): a dropped quote is not recoverable in
    fidelity, so this refuses just as hard as the DoD arm."""
    body = ("## Stakeholder notes\n\nv\n\n## Context\n\n"
            "### Verbatim request\n\n(filed via task_new)\n\n"
            "### Stakeholder guidance\n\n- 2026-09-07 · his words here\n")
    with pytest.raises(ValueError, match="stakeholder"):
        set_context(body, "fresh session state, no quotes mentioned")


def test_set_context_allows_when_replacement_carries_the_quote_forward():
    body = ("## Stakeholder notes\n\nv\n\n## Context\n\n"
            "### Stakeholder notes\n\nhis words\n")
    out = set_context(body, "### Stakeholder notes\n\nhis words\n\nsession update")
    ctx = parse_body(out)["context"]
    assert "his words" in ctx
    assert "session update" in ctx


def test_set_context_guard_ignores_stakeholder_notes_outside_context():
    body = "## Stakeholder notes\n\nhis original ask\n\n## Context\n\nplain notes\n"
    out = set_context(body, "updated notes")
    assert parse_body(out)["context"] == "updated notes"
    assert "his original ask" in out


# --- T-0863: `## Executive summary`, the one-paragraph status ---------------
#
# Every arm below states the PROPERTY it pins in words first, because the
# defect class this section can carry is a well-formed result that no longer
# says what its author wrote (F-2026-07-05-bsq-30844bca41 / T-0835). A test
# asserting only "a summary came back" cannot see that.


def _summary_body() -> str:
    return ("## Stakeholder notes\n\nask\n\n## Context\n\nwork\n\n"
            "## Progress\n\n- 2026-08-11T00:00:00Z · S-x-p1 · note\n")


def test_summary_is_parsed_as_its_own_section_not_absorbed():
    """The heading must be CANONICAL. An unrecognised `## ` heading is a
    section boundary, so a non-canonical `## Executive summary` would end the
    section above it and its own text would belong to nobody."""
    out = set_summary(_summary_body(), "Half done.")
    parsed = parse_body(out)
    assert parsed["summary"] == "Half done."
    assert parsed["verbatim"] == "ask"
    assert parsed["context"] == "work"
    assert "Half done." not in parsed["verbatim"]
    assert "Half done." not in parsed["context"]


def test_summary_sits_between_the_ask_and_the_working_area():
    """Ordering is a product requirement, not cosmetics: he reads it on the
    ticket, so it must not land under a long Context and a long feed."""
    out = set_summary(_summary_body(), "Half done.")
    assert (out.index("## Stakeholder notes")
            < out.index("## Executive summary")
            < out.index("## Context")
            < out.index("## Progress"))


def test_summary_is_inserted_before_progress_when_there_is_no_context():
    out = set_summary("## Stakeholder notes\n\nask\n\n## Progress\n\n- a\n", "S.")
    assert out.index("## Executive summary") < out.index("## Progress")
    assert parse_body(out)["progress"] == "- a"


def test_summary_replaces_rather_than_appends():
    """A status says what is true NOW. Two paragraphs of history is the feed."""
    once = set_summary(_summary_body(), "First.")
    twice = set_summary(once, "Second.")
    assert "First." not in twice
    assert parse_body(twice)["summary"] == "Second."
    assert twice.count("## Executive summary") == 1


def test_summary_write_leaves_every_other_section_byte_identical():
    body = _summary_body()
    out = set_summary(body, "Half done.")
    for key in ("verbatim", "context", "progress"):
        assert parse_body(out)[key] == parse_body(body)[key]


def test_empty_summary_clears_the_section():
    out = set_summary(set_summary(_summary_body(), "Half done."), "")
    assert "## Executive summary" not in out
    assert parse_body(out)["context"] == "work"


def test_two_paragraphs_are_REFUSED_not_joined():
    """The two available repairs — join with a space, or keep the first — both
    produce something well-formed that no longer says what was written, and
    neither tells anyone. Refusing costs one retry."""
    with pytest.raises(ValueError) as e:
        set_summary(_summary_body(), "First para.\n\nSecond para.")
    assert "ONE paragraph" in str(e.value)
    assert "blank line" in str(e.value)


def test_a_bullet_list_is_REFUSED():
    with pytest.raises(ValueError) as e:
        set_summary(_summary_body(), "Done so far:\n- a\n- b")
    assert "ONE paragraph" in str(e.value)


def test_a_heading_is_REFUSED():
    with pytest.raises(ValueError):
        set_summary(_summary_body(), "## Status\nall good")


def test_over_cap_summary_is_REFUSED_and_names_the_remedy():
    """Never truncated. The message must name the section that holds the long
    version, or the caller's only option is to guess."""
    with pytest.raises(ValueError) as e:
        set_summary(_summary_body(), "x" * 1201)
    msg = str(e.value)
    assert "1201 chars" in msg and "refusing to truncate" in msg
    assert "## Context" in msg


def test_a_wrapped_sentence_is_one_paragraph_and_survives_verbatim():
    """Single newlines render inside one paragraph, so rewrapping them would be
    exactly the pointless mangling the refusals exist to avoid."""
    text = "Shipped the writer and the CLI;\nthe board render is what remains."
    assert parse_body(set_summary(_summary_body(), text))["summary"] == text


def test_compose_body_emits_the_summary_second():
    out = compose_body("v", "c", "- p", summary="s")
    assert out.index(STAKEHOLDER_HEADING) < out.index(SUMMARY_HEADING)
    assert out.index(SUMMARY_HEADING) < out.index("## Context")
    assert compose_body("v", "", "") == "## Stakeholder notes\n\nv\n"


def test_parse_body_always_returns_every_declared_section_key():
    """A key added to the heading regex and forgotten in the parser's dict
    would KeyError every reader — or, worse, be silently absent."""
    for body in ("", "no headings at all", _summary_body()):
        assert set(parse_body(body)) == set(SECTION_KEYS)
