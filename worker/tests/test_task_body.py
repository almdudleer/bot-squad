"""Tests for bot_squad_worker.task_body — mirror of api test_task_body."""
from __future__ import annotations

import pytest

import hashlib

from bot_squad_worker.task_body import (
    append_progress,
    compose_body,
    decode_progress_text,
    encode_progress_text,
    parse_body,
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
