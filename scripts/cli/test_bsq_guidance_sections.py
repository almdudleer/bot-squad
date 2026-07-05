"""T-0590: `bsq guidance search` indexes tickets' harvested-comments sections.

Steering routed onto an EXISTING ticket lands in its
"## Stakeholder comments (harvested at session close — T-0151)" section (or
via `bsq ticket note`) — not in `## Verbatim request`. Before T-0590 the
guidance corpus scanned only the Verbatim section of tickets, so routed
comments were unfindable by the next session. These tests pin the two-section
scan and that the section slicer keeps sections apart.

`bsq` is an extensionless script loaded via SourceFileLoader.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_guidance", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_guidance", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


TICKET_MD = (
    "---\n"
    "id: T-0901\n"
    'title: "sample"\n'
    "status: open\n"
    "updated: 2026-07-05T12:00:00Z\n"
    "---\n\n"
    "## Verbatim request\n\n"
    "> please build the flux capacitor\n\n"
    "## Context\n\n"
    "context body mentioning gravity\n\n"
    "## Stakeholder comments (harvested at session close — T-0151)\n\n"
    "- 2026-07-05T11:00:00Z: notifications must default to telegram only\n"
)


def _write_ticket(tmp_path: Path) -> Path:
    p = tmp_path / "T-0901-sample.md"
    p.write_text(TICKET_MD, encoding="utf-8")
    return p


def test_scan_finds_harvested_comment_section(tmp_path):
    p = _write_ticket(tmp_path)
    hits = bsq._scan_md_section(p, ["telegram"], "Stakeholder comments")
    assert len(hits) == 1
    assert "telegram" in hits[0]["text"]


def test_section_slice_does_not_leak_other_sections(tmp_path):
    p = _write_ticket(tmp_path)
    # "gravity" lives in ## Context only — neither scanned section may hit it.
    assert bsq._scan_md_section(p, ["gravity"], "Verbatim request") == []
    assert bsq._scan_md_section(p, ["gravity"], "Stakeholder comments") == []
    # and the verbatim scan still works
    assert len(bsq._scan_md_section(p, ["capacitor"], "Verbatim request")) == 1


def test_collect_guidance_surfaces_harvested_comments(tmp_path, monkeypatch):
    """End-to-end through _collect_guidance with the corpus dirs sandboxed."""
    backlog = tmp_path / "data" / "proj" / "backlog"
    backlog.mkdir(parents=True)
    _write_ticket(backlog)
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: backlog)
    # no sessions dir / no jsonl corpora in the sandbox
    monkeypatch.setattr(bsq, "BOT_SQUAD", str(tmp_path))
    monkeypatch.setattr(bsq, "_jsonl_search_dirs", lambda slug, ap: [])

    top, total, truncated = bsq._collect_guidance("proj", ["telegram"], 5)
    assert total == 1
    assert "telegram" in top[0]["text"]
    assert top[0]["source"].endswith("T-0901")
