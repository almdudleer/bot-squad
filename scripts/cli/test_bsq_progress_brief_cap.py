"""Tests for T-0852: capping a ticket's ``## Progress`` feed before it is
inlined into a spawn brief.

The feed is append-only and never pruned (T-0238) — on a long-lived ticket it
grows without bound, and every fresh spawn used to get the WHOLE thing inlined
via `_assemble_prompt`. Measured live on T-0496: 49 entries / 144KB going into
every new session's brief for that one ticket. `_cap_progress_for_brief` trims
what gets INLINED (storage is untouched) to the last N entries, further
trimmed from the old end if even that many still exceed a char budget —
individual entries can themselves be huge (a whole compacted session summary
pasted as one physical line, per T-0835's one-line-per-note contract), so a
count-only cap under-delivers on its own.

`bsq` is an extensionless script loaded via SourceFileLoader.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_progress_cap", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_progress_cap", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _entry(n: int) -> str:
    return f"- 2026-08-0{1 + n % 9}T00:00:0{n % 10}Z · S-x-p{n} · did thing #{n}"


def test_short_progress_feed_is_byte_identical():
    """Positive control: a ticket under the cap must round-trip untouched —
    the function should never rewrite a body it has no reason to touch."""
    body = ("## Verbatim request\n\nfix the button\n\n"
            "## Progress\n\n" + "\n".join(_entry(i) for i in range(3)) + "\n")
    assert bsq._cap_progress_for_brief(body) == body


def test_no_progress_section_is_byte_identical():
    body = "## Verbatim request\n\nfix the button\n"
    assert bsq._cap_progress_for_brief(body) == body


def test_over_count_cap_keeps_only_the_most_recent_entries():
    entries = [_entry(i) for i in range(40)]
    body = "## Verbatim request\n\nx\n\n## Progress\n\n" + "\n".join(entries) + "\n"
    capped = bsq._cap_progress_for_brief(body, max_entries=15, char_budget=10_000)
    for old in entries[:25]:
        assert old not in capped
    for recent in entries[25:]:
        assert recent in capped
    assert "25 earlier entries omitted" in capped


def test_over_budget_trims_further_from_the_old_end_even_under_the_count_cap():
    """A handful of huge entries (e.g. a pasted compaction summary) must not
    survive the count cap unbounded — the char budget trims them too."""
    huge = [f"- 2026-08-01T00:00:0{i}Z · S-x-p{i} · " + ("x" * 5000) for i in range(5)]
    body = "## Verbatim request\n\nx\n\n## Progress\n\n" + "\n".join(huge) + "\n"
    capped = bsq._cap_progress_for_brief(body, max_entries=15, char_budget=10_000)
    assert len(capped) < len(body)
    assert huge[-1] in capped  # the most recent entry always survives
    assert huge[0] not in capped
    assert "earlier entries omitted" in capped


def test_other_sections_survive_the_progress_trim_untouched():
    entries = [_entry(i) for i in range(40)]
    body = ("## Verbatim request\n\nkeep me exactly\n\n"
            "## Progress\n\n" + "\n".join(entries) + "\n\n"
            "## DoD\n\n- ship it\n")
    capped = bsq._cap_progress_for_brief(body, max_entries=5, char_budget=10_000)
    assert "## Verbatim request\n\nkeep me exactly" in capped
    assert "## DoD\n\n- ship it" in capped


def test_fresh_spawn_brief_uses_the_capped_progress(tmp_path, monkeypatch):
    ticket = "T-9999"
    entries = [_entry(i) for i in range(40)]
    md = tmp_path / f"{ticket}-fixture.md"
    md.write_text(
        "---\nid: T-9999\ntitle: cap fixture\n---\n\n"
        "## Verbatim request\n\nfix the button\n\n"
        "## Progress\n\n" + "\n".join(entries) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bsq, "_collect_guidance", lambda *a, **k: ([], 0, None))
    brief = bsq._assemble_prompt(
        "bot-squad", [ticket], {ticket: md}, {ticket: {"id": ticket, "title": "cap fixture"}},
        "dev", "S-tl",
    )
    assert entries[-1] in brief
    assert entries[0] not in brief
    assert "earlier entries omitted" in brief
