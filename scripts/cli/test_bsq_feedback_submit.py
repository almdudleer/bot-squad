"""Tests for `bsq feedback submit` writing F-*.md directly (audit item 8, Fork-4).

Previously `bsq feedback submit` appended a line to `feedback/inbox.log`, which a
constant team auto-ticketed (the firehose the operator's Occam prune exists to
prevent) AND `list_feedback` re-materialized into F-*.md on every read (a
side-effecting GET). The Fork-4 spine cuts both: submit writes a first-class
`F-*.md` FeedbackFile directly, so the read becomes pure and there is a single
operator-owned promote/dismiss gate. `bsq` is an extensionless script loaded via
SourceFileLoader; `_render_feedback_md` is a pure (name, content) renderer.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_fb", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_fb", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

# the API's promotable-feedback name gate (kept byte-compatible)
import re
_FEEDBACK_NAME_RE = re.compile(r"^F-[A-Za-z0-9_.-]+\.md$")


def test_render_feedback_md_name_and_body():
    name, content = bsq._render_feedback_md(
        "2026-06-21T15:00:00Z", "S-u-w-p1", "[DOGFOOD] the board double-counts", ""
    )
    # a valid, promotable F-*.md name dated from the submission ts
    assert _FEEDBACK_NAME_RE.match(name)
    assert name.startswith("F-2026-06-21-")
    # H1 title (drops the [TAG] prefix) so promote derives a sensible title
    assert "# the board double-counts" in content
    # provenance frontmatter the UI/AN can parse
    assert content.startswith("---\n")
    assert "submitted_by: S-u-w-p1" in content
    assert "the board double-counts" in content


def test_render_feedback_md_is_deterministic_for_same_line():
    """Same (ts, sid, text) → same filename (content-hash keyed), so an
    accidental double-submit dedupes instead of spamming two F-files."""
    a = bsq._render_feedback_md("2026-06-21T15:00:00Z", "S-x", "same note", "")
    b = bsq._render_feedback_md("2026-06-21T15:00:00Z", "S-x", "same note", "")
    assert a[0] == b[0]


def test_render_feedback_md_undated_ts_falls_back():
    name, _ = bsq._render_feedback_md("not-a-date", "S-x", "note", "")
    assert _FEEDBACK_NAME_RE.match(name)


def test_submit_writes_feedback_file_not_inbox_log(tmp_path, monkeypatch):
    """End-to-end: cmd_feedback_submit writes an F-*.md into the feedback dir and
    does NOT create inbox.log (the cut store)."""
    fb_dir = tmp_path / "data" / "test-project" / "feedback"
    monkeypatch.setattr(bsq, "BOT_SQUAD", str(tmp_path))
    monkeypatch.setattr(bsq, "resolve_slug", lambda: "test-project")
    monkeypatch.setattr(bsq, "my_sid", lambda: "S-u-w-p1")
    monkeypatch.setattr(bsq, "_utc_now", lambda: "2026-06-21T15:00:00Z")

    import argparse
    args = argparse.Namespace(message=["the", "thing", "broke"], sid=None, usecase=None)
    bsq.cmd_feedback_submit(args)

    assert not (fb_dir / "inbox.log").exists()
    files = list(fb_dir.glob("F-*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "the thing broke" in body
