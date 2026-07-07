"""Tests for bsq's find_ticket id-collision resolution (T-0231).

`find_ticket` backs `bsq ticket status/update/note` and `bsq spawn`'s ticket
lookup. It used to be a plain ``sorted(glob(f"{id}-*.md"))[0]`` — the
alphabetically-FIRST filename match, chosen without reading the file's own
``id:`` frontmatter. Two real incidents (T-0030, T-0222) happened because a
second file (a genuine duplicate, or a renumbered ticket's tombstone) shared
the numeric filename prefix and sorted first, so every `bsq ticket
status/update/note <id>` silently resolved to the WRONG file.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_ft", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_ft", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(path: Path, task_id: str, **extra_fm) -> None:
    lines = [f"id: {task_id}"]
    for k, v in extra_fm.items():
        lines.append(f"{k}: {v}")
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n")


def test_find_ticket_single_match(tmp_path, monkeypatch):
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    p = tmp_path / "T-0001-only.md"
    _write(p, "T-0001")
    assert bsq.find_ticket("p", "T-0001") == p


def test_find_ticket_not_found_dies(tmp_path, monkeypatch):
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    with pytest.raises(SystemExit):
        bsq.find_ticket("p", "T-9999")


def test_find_ticket_skips_tombstone_sorting_first(tmp_path, monkeypatch):
    """Reproduces the T-0030 incident: tombstone sorts alphabetically BEFORE
    the real ticket. find_ticket must return the real one, not matches[0]."""
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    tombstone = tmp_path / "T-0030-aaa-tombstone.md"
    real = tmp_path / "T-0030-zzz-real-ticket.md"
    _write(tombstone, "T-0030-DUPLICATE-DO-NOT-USE", status="closed")
    _write(real, "T-0030", status="in_progress")
    assert sorted(tmp_path.glob("T-0030-*.md"))[0] == tombstone  # sanity
    assert bsq.find_ticket("p", "T-0030") == real


def test_find_ticket_skips_tombstone_sorting_last(tmp_path, monkeypatch):
    """The T-0222 incident: real ticket sorted first, tombstone last. Must
    still resolve correctly regardless of sort order in either direction."""
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    real = tmp_path / "T-0222-aaa-real-ticket.md"
    tombstone = tmp_path / "T-0222-zzz-tombstone.md"
    _write(real, "T-0222", status="closed")
    _write(tombstone, "T-0222-tombstone", status="closed")
    assert sorted(tmp_path.glob("T-0222-*.md"))[0] == real  # sanity
    assert bsq.find_ticket("p", "T-0222") == real


def test_find_ticket_genuine_collision_dies(tmp_path, monkeypatch):
    """Two files genuinely declaring the same id: — no way to disambiguate;
    must fail loudly instead of silently picking one."""
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    _write(tmp_path / "T-0030-a.md", "T-0030")
    _write(tmp_path / "T-0030-b.md", "T-0030")
    with pytest.raises(SystemExit):
        bsq.find_ticket("p", "T-0030")
