"""Integration test for the ``bsq task search`` wrapper's backlog-reading path
(Process Paradigm M4 / F4.2, T-0486).

The ranking logic itself is unit-tested in ``test_task_search.py`` against pure
``Ticket`` records. This file covers the thin ``bsq`` wrapper layer that the pure
test does NOT exercise: ``_load_backlog_tickets`` reading real ticket .md files
off disk (frontmatter id/title/status + body = md minus frontmatter) and feeding
them to ``rank()``. Together they prove a dup input surfaces as a clarification
end-to-end through the CLI's file path, not just the ranker.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_ts", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_ts", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write_ticket(d: Path, tid: str, title: str, status: str, body: str) -> None:
    (d / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: {title!r}\nstatus: {status}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _backlog_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "demo" / "backlog"
    d.mkdir(parents=True)
    _write_ticket(d, "T-0010", "staging deploy pre-gate on dirty shared dev clone",
                  "open", "the deploy precheck should refuse when the clone is dirty")
    _write_ticket(d, "T-0011", "render task cards with a status pill",
                  "closed", "frontend polish for the board")
    return d


def test_load_backlog_tickets_reads_frontmatter_and_body(tmp_path, monkeypatch):
    d = _backlog_dir(tmp_path)
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: d)
    tickets = bsq._load_backlog_tickets("demo")
    by_id = {t.id: t for t in tickets}
    assert set(by_id) == {"T-0010", "T-0011"}
    assert by_id["T-0010"].title == "staging deploy pre-gate on dirty shared dev clone"
    assert by_id["T-0010"].status == "open"
    assert "precheck" in by_id["T-0010"].body          # body parsed (frontmatter stripped)
    assert "---" not in by_id["T-0010"].body            # frontmatter block removed


def test_wrapper_flags_duplicate_input_as_clarification(tmp_path, monkeypatch):
    """End-to-end through the wrapper: a restatement of T-0010 loaded off disk
    ranks T-0010 top and flags it dup=True (→ clarify the existing task)."""
    d = _backlog_dir(tmp_path)
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: d)
    rank = bsq._load_task_search().rank
    cands = rank("staging deploy pre-gate dirty shared dev clone",
                 bsq._load_backlog_tickets("demo"))
    assert cands[0].id == "T-0010"
    assert cands[0].dup is True
