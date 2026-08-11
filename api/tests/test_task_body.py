"""Tests for app.task_body — three-section schema parse/compose/append."""
from __future__ import annotations

import pytest

import hashlib

from app.task_body import (
    STAKEHOLDER_HEADING,
    append_progress,
    append_stakeholder_quote,
    decode_progress_text,
    encode_progress_text,
    compose_body,
    is_legacy_body,
    parse_body,
    regraft_progress,
    regraft_verbatim,
    set_context,
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


# ---------------------------------------------------------------------------
# T-0733 — is_legacy_body: which branch produced `verbatim`, so a consumer can
# label a whole-body planning doc as the ticket body instead of as the ask.


def test_is_legacy_body_true_when_no_verbatim_heading():
    assert is_legacy_body("The plan.\n\n## Scope\n\n- a\n")
    assert is_legacy_body("Legacy ask.\n\n## Progress\n\n- T1 · S1 · note\n")
    assert is_legacy_body("just prose, no headings at all")
    assert is_legacy_body("")
    assert is_legacy_body(None)  # type: ignore[arg-type]


def test_is_legacy_body_false_for_canonical_and_decorated_headings():
    assert not is_legacy_body("## Verbatim request\n\nI want X.\n")
    # Decorated heading — 60 live tickets carry this exact form.
    assert not is_legacy_body(
        "## Verbatim request — source of truth (human-only, do not edit)\n\nI want X.\n"
    )
    assert not is_legacy_body("## Progress\n\n- x\n\n## verbatim request\n\nask\n")


@pytest.mark.parametrize(
    "body,legacy,verbatim",
    [
        # Canonical: verbatim comes from the heading's section.
        ("## Verbatim request\n\nask\n\n## Progress\n\n- a\n", False, "ask"),
        # Legacy: verbatim is the leading text (up to the first CANONICAL heading).
        ("The plan.\n\n## Scope\n\n- a\n", True, "The plan.\n\n## Scope\n\n- a"),
        ("Legacy ask.\n\n## Context\n\nctx\n", True, "Legacy ask."),
        ("## DoD\n\n- d\n", True, "## DoD\n\n- d"),
        ("", True, ""),
    ],
)
def test_is_legacy_body_agrees_with_the_branch_parse_body_took(body, legacy, verbatim):
    """The flag must never disagree with which branch actually produced
    `verbatim` — that divergence is exactly what T-0729 cost us."""
    assert is_legacy_body(body) is legacy
    assert parse_body(body)["verbatim"] == verbatim


def test_parse_h3_is_not_a_boundary():
    body = "## Verbatim request\n\nI want X.\n\n### sub-point\n\nstill the ask\n"
    assert parse_body(body)["verbatim"] == "I want X.\n\n### sub-point\n\nstill the ask"


def test_compose_skips_empty():
    assert compose_body("v", "", "") == "## Stakeholder notes\n\nv\n"
    assert compose_body("", "", "") == ""
    out = compose_body("v", "c", "- line")
    assert STAKEHOLDER_HEADING in out
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


def test_append_progress_note_roundtrips_byte_identical():
    # Regression for F-2026-07-05-bsq-30844bca41: notes used to be silently
    # CLIPPED, which is the defect — not the cap's value. T-0767 restored the
    # 240 the role contracts state, so this exercises the no-silent-loss
    # guarantee at a length a note is allowed to be; the >300-char sacred
    # verbatim that motivated the report is pinned below on the writer that
    # now owns it.
    body = "## Stakeholder notes\n\nv\n"
    note = ("stakeholder verbatim word " * 8).strip()
    assert 200 < len(note) <= 240
    new = append_progress(body, "T1", "S1", note)
    parsed = parse_body(new)
    text_part = parsed["progress"].split(" · ", 2)[-1]
    assert text_part == note


def test_sacred_verbatim_over_the_note_cap_roundtrips_as_a_stakeholder_quote():
    """The F-2026-07-05-bsq-30844bca41 case itself, on its new writer (T-0767).

    The report was about a >300-char stakeholder verbatim being clipped while
    stored as a progress NOTE. T-0767's answer is that his words stop being
    stored as notes at all — so the guarantee has to hold HERE now, or the
    redesign silently re-opens the bug the 4000-char cap was raised to close.
    """
    body = "## Stakeholder notes\n\noriginal ask\n"
    quote = ("stakeholder verbatim word " * 40).strip()
    assert len(quote) > 300
    new = append_stakeholder_quote(body, "T1", "2026-08-11", quote)
    assert "original ask" in new          # the original formulation survives
    text_part = parse_body(new)["verbatim"].split(" · ", 2)[-1]
    assert text_part == quote


def test_append_progress_overflow_raises_and_names_the_working_area():
    body = "## Stakeholder notes\n\nv\n"
    with pytest.raises(ValueError, match="cap") as e:
        append_progress(body, "T1", "S1", "x" * 5000)
    # The refusal has to be actionable in one read, or a session just retries
    # and burns the tokens T-0767 exists to save.
    assert "context" in str(e.value).lower()


def test_stakeholder_quote_overflow_still_refuses_rather_than_truncating():
    body = "## Stakeholder notes\n\nv\n"
    with pytest.raises(ValueError, match="cap"):
        append_stakeholder_quote(body, "T1", "2026-08-11", "x" * 5000)


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


# ---------------------------------------------------------------------------
# T-0767: two authored artifacts (stakeholder notes + working-area Context),
# with `## Verbatim request` kept as a parsing alias so 809 live tickets need
# no migration.
# ---------------------------------------------------------------------------

def test_legacy_verbatim_heading_still_parses_as_the_stakeholder_section():
    """The alias is what makes the rename free — if this breaks, every live
    ticket loses the protection on the stakeholder's words at once."""
    body = "## Verbatim request\n\nI want X.\n\n## Progress\n\n- a\n"
    assert parse_body(body)["verbatim"] == "I want X."
    assert not is_legacy_body(body)


def test_new_stakeholder_heading_parses_as_the_same_section():
    body = "## Stakeholder notes\n\nI want X.\n\n## Progress\n\n- a\n"
    assert parse_body(body)["verbatim"] == "I want X."
    assert not is_legacy_body(body)


def test_a_ticket_carrying_BOTH_spellings_loses_neither():
    """A transitional ticket can hold both headings. Whichever way the parser
    resolves that, it must not DROP one — the original ask is the thing this
    schema exists to protect, and a silent replace is how it would go."""
    body = ("## Verbatim request\n\nTHE ORIGINAL ASK\n\n"
            "## Stakeholder notes\n\n- T1 · 2026-08-11 · A LATER QUOTE\n")
    parsed = parse_body(body)
    assert "THE ORIGINAL ASK" in parsed["verbatim"]
    assert "A LATER QUOTE" in parsed["verbatim"]


def test_regraft_protects_the_new_heading_too():
    """`regraft_verbatim` is the write-protection path. A caller must not be
    able to put words in the stakeholder's mouth through the new spelling."""
    on_disk = "## Stakeholder notes\n\nHIS WORDS\n\n## Context\n\nc\n"
    hijack = "## Stakeholder notes\n\nWORDS HE NEVER SAID\n\n## Context\n\nnew c\n"
    out = regraft_verbatim(on_disk, hijack)
    assert "HIS WORDS" in out
    assert "WORDS HE NEVER SAID" not in out
    assert "new c" in out            # the legitimate Context edit still lands


def test_regraft_removes_a_second_stakeholder_section_a_caller_invented():
    """Excise EVERY stakeholder span, not just the first: a caller that keeps
    the real section and ADDS a second one must not get the second through."""
    on_disk = "## Stakeholder notes\n\nHIS WORDS\n"
    hijack = ("## Stakeholder notes\n\nHIS WORDS\n\n"
              "## Verbatim request\n\nSMUGGLED\n")
    out = regraft_verbatim(on_disk, hijack)
    assert "HIS WORDS" in out
    assert "SMUGGLED" not in out


def test_set_context_replaces_rather_than_appends():
    """The working area records what is TRUE NOW — replacing is the point."""
    body = ("## Stakeholder notes\n\nv\n\n## Context\n\nOLD STATE\n\n"
            "## Progress\n\n- a\n")
    out = set_context(body, "NEW STATE")
    assert "NEW STATE" in out
    assert "OLD STATE" not in out
    parsed = parse_body(out)
    assert parsed["verbatim"] == "v"
    assert "- a" in parsed["progress"]


def test_set_context_creates_the_section_before_progress():
    body = "## Stakeholder notes\n\nv\n\n## Progress\n\n- a\n"
    out = set_context(body, "STATE")
    assert parse_body(out)["context"] == "STATE"
    # the machine feed stays last, so the authored artifacts read first
    assert out.index("## Context") < out.index("## Progress")


def test_set_context_preserves_non_canonical_sections():
    """Same hazard as T-0729: a recompose would delete `## DoD` on 414 live
    tickets the moment anyone edited the working area."""
    body = ("## Stakeholder notes\n\nv\n\n## Context\n\nold\n\n"
            "## DoD\n\n- ship it\n")
    out = set_context(body, "new")
    assert "## DoD\n\n- ship it" in out
    assert "new" in out


def test_set_context_demotes_h2_so_the_working_area_survives_a_reparse():
    """Found by using `set_context` on a real ticket, not by a unit test.

    Any `## ` heading ends a section (T-0729), so a working area written with
    the structure a working area wants would parse back as several bogus
    top-level sections with only the preamble surfacing as Context. The file on
    disk looks complete, which is what makes it dangerous — the loss is at the
    READ layer, exactly where the brief and the board look. Measured on the
    first real use: a 3.4KB handover parsed back as 182 chars.
    """
    body = "## Stakeholder notes\n\nv\n\n## Progress\n\n- a\n"
    written = ("preamble\n\n## What shipped\n\ndetails here\n\n"
               "## Open questions\n\nthe hard one\n")
    out = set_context(body, written)
    ctx = parse_body(out)["context"]
    # every part of the working area round-trips into ONE section
    assert "preamble" in ctx
    assert "details here" in ctx
    assert "the hard one" in ctx
    # headings survive as headings, one level down
    assert "### What shipped" in ctx
    assert "### Open questions" in ctx
    # and the ticket did not sprout new top-level sections
    assert out.count("\n## ") + out.startswith("## ") == 3  # stakeholder, Context, Progress
    assert parse_body(out)["progress"].strip() == "- a"
