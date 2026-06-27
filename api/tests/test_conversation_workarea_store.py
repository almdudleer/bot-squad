"""T-0495 (M5 / F5.8+F5.9): per-(project, user) conversation WORKING-AREA doc
+ resume-from-history assembly.

The working-area doc is the conversation analog of the operator state-doc
(T-0473): a FUTURE-FOCUSED continuity artifact (intentions / next steps / open
threads — NOT an event log) that a conversational session writes so the next
(recycled) session picks up continuity. Keyed by (project_slug, global_user_id),
mirroring the conversation history store (T-0489).

``assemble_resume_context`` is the pure boot-context builder: working-area doc +
recent N records from the history store (T-0489) — what a fresh session reads to
resume the dialogue after a recycle.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import conversation_store as CS
from app import conversation_workarea_store as CWA


# --- path / segment safety -------------------------------------------------

def test_path_is_under_mothership_per_slug_and_user(tmp_path: Path):
    p = CWA.workarea_path(tmp_path, "proj", "gu_abc")
    # parallels the history store: mothership-level, project-scoped, per-user
    assert p == tmp_path / "_mothership" / "conversation_workareas" / "proj" / "gu_abc.md"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../x", ".hidden"])
def test_unsafe_segments_rejected(tmp_path: Path, bad: str):
    with pytest.raises(ValueError):
        CWA.workarea_path(tmp_path, bad, "gu_abc")
    with pytest.raises(ValueError):
        CWA.workarea_path(tmp_path, "proj", bad)


# --- read of a missing doc -------------------------------------------------

def test_read_missing_returns_empty_future_focused_skeleton(tmp_path: Path):
    out = CWA.read(tmp_path, "proj", "gu_abc")
    assert out["exists"] is False
    assert out["slug"] == "proj"
    assert out["global_user_id"] == "gu_abc"
    assert out["updated"] is None
    # every canonical future-focused section is present and empty
    assert set(out["sections"]) == set(CWA.WORKAREA_SECTIONS)
    assert all(v == "" for v in out["sections"].values())


# --- upsert / read round-trip ---------------------------------------------

def test_upsert_then_read_roundtrips_sections(tmp_path: Path):
    CWA.upsert(
        tmp_path, "proj", "gu_abc",
        sections={
            "intentions": "Ship the export feature.",
            "next_steps": "Confirm CSV column order with the user.",
            "open_threads": "Awaiting their timezone preference.",
        },
        timestamp="2026-06-27T00:00:00Z",
    )
    out = CWA.read(tmp_path, "proj", "gu_abc")
    assert out["exists"] is True
    assert out["updated"] == "2026-06-27T00:00:00Z"
    assert out["sections"]["intentions"] == "Ship the export feature."
    assert out["sections"]["next_steps"] == "Confirm CSV column order with the user."
    assert out["sections"]["open_threads"] == "Awaiting their timezone preference."


def test_upsert_writes_markdown_with_frontmatter(tmp_path: Path):
    CWA.upsert(tmp_path, "proj", "gu_abc",
               sections={"intentions": "hello"}, timestamp="2026-06-27T00:00:00Z")
    raw = CWA.workarea_path(tmp_path, "proj", "gu_abc").read_text(encoding="utf-8")
    assert raw.startswith("---\n")            # frontmatter fence
    assert "global_user_id: gu_abc" in raw
    assert "slug: proj" in raw
    assert "## Intentions" in raw             # human-readable heading in body
    assert "hello" in raw


# --- upsert is a PARTIAL merge --------------------------------------------

def test_upsert_partial_preserves_other_sections(tmp_path: Path):
    CWA.upsert(tmp_path, "proj", "gu_abc",
               sections={"intentions": "keep me", "next_steps": "old step"},
               timestamp="2026-06-27T00:00:00Z")
    # update only next_steps; intentions must survive untouched
    CWA.upsert(tmp_path, "proj", "gu_abc",
               sections={"next_steps": "new step"},
               timestamp="2026-06-27T01:00:00Z")
    out = CWA.read(tmp_path, "proj", "gu_abc")
    assert out["sections"]["intentions"] == "keep me"
    assert out["sections"]["next_steps"] == "new step"
    assert out["updated"] == "2026-06-27T01:00:00Z"


# --- idempotency -----------------------------------------------------------

def test_upsert_idempotent_no_change_keeps_updated_and_bytes(tmp_path: Path):
    CWA.upsert(tmp_path, "proj", "gu_abc",
               sections={"intentions": "stable"}, timestamp="2026-06-27T00:00:00Z")
    p = CWA.workarea_path(tmp_path, "proj", "gu_abc")
    first = p.read_text(encoding="utf-8")
    # re-upsert identical content with a LATER timestamp — must be a no-op
    CWA.upsert(tmp_path, "proj", "gu_abc",
               sections={"intentions": "stable"}, timestamp="2026-06-27T09:99:99Z")
    second = p.read_text(encoding="utf-8")
    assert second == first
    assert CWA.read(tmp_path, "proj", "gu_abc")["updated"] == "2026-06-27T00:00:00Z"


def test_upsert_rejects_unknown_section(tmp_path: Path):
    with pytest.raises(ValueError):
        CWA.upsert(tmp_path, "proj", "gu_abc", sections={"bogus": "x"})


# --- resume-context assembly ----------------------------------------------

def test_assemble_merges_workarea_and_recent_history(tmp_path: Path):
    # a working-area doc...
    CWA.upsert(tmp_path, "proj", "gu_abc",
               sections={"next_steps": "ask about deadline"},
               timestamp="2026-06-27T00:00:00Z")
    # ...and a conversation thread (T-0489)
    for i in range(5):
        CS.append(tmp_path, "proj", "gu_abc", author="user", text=f"m{i}",
                  timestamp=f"2026-06-27T00:00:0{i}Z")

    ctx = CWA.assemble_resume_context(tmp_path, "proj", "gu_abc", history_limit=3)
    assert ctx["slug"] == "proj"
    assert ctx["global_user_id"] == "gu_abc"
    # workarea is the read() shape
    assert ctx["workarea"]["sections"]["next_steps"] == "ask about deadline"
    # only the most recent N records, in chronological order
    assert [m["text"] for m in ctx["recent_messages"]] == ["m2", "m3", "m4"]
    assert ctx["history_total"] == 5


def test_assemble_with_no_workarea_and_no_history(tmp_path: Path):
    ctx = CWA.assemble_resume_context(tmp_path, "proj", "gu_abc")
    assert ctx["workarea"]["exists"] is False
    assert ctx["recent_messages"] == []
    assert ctx["history_total"] == 0


def test_assemble_history_limit_zero_yields_no_messages(tmp_path: Path):
    CS.append(tmp_path, "proj", "gu_abc", author="user", text="hi")
    ctx = CWA.assemble_resume_context(tmp_path, "proj", "gu_abc", history_limit=0)
    assert ctx["recent_messages"] == []
    assert ctx["history_total"] == 1


# --- recycle survival ------------------------------------------------------

def test_workarea_survives_a_simulated_recycle(tmp_path: Path):
    # session A writes its continuity intentions then "recycles" (process ends)
    CWA.upsert(tmp_path, "proj", "gu_abc",
               sections={"intentions": "resume the export thread"},
               timestamp="2026-06-27T00:00:00Z")
    # a fresh session (new call, same data_dir) reads it back intact
    revived = CWA.read(tmp_path, "proj", "gu_abc")
    assert revived["exists"] is True
    assert revived["sections"]["intentions"] == "resume the export thread"
