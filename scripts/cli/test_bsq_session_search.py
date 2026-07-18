"""``bsq session search`` — general history search over exited sessions
(T-0591, D-0047 gap: `bsq expert find` is ticket-scoped only; a stakeholder
asking "what happened in that old session" has no query that doesn't require
already knowing a ticket to anchor on).

Searches the one place a session's own narration of its work survives
archival: `## Progress` lines (`- <ts> · <sid> · <text>`) it appended across
the backlog, plus its window name.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_session_search", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_session_search", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write_ticket(path: Path, *, tid: str, progress: list[tuple[str, str, str]]) -> None:
    lines = "\n".join(f"- {ts} · {sid} · {text}" for ts, sid, text in progress)
    body = f"## Progress\n\n{lines}\n" if progress else ""
    path.write_text(f"---\nid: {tid}\ntitle: demo\nstatus: closed\n---\n\n{body}")


def _setup(tmp_path, monkeypatch, sessions):
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    monkeypatch.setattr(bsq, "post", lambda action, params, **kw: {"sessions": sessions})


def test_progress_lines_by_sid_indexes_and_sorts_chronologically(tmp_path, monkeypatch):
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    _write_ticket(tmp_path / "T-0001-a.md", tid="T-0001", progress=[
        ("2026-07-02T00:00:00Z", "S-alice-p1", "second event"),
        ("2026-07-01T00:00:00Z", "S-alice-p1", "first event"),
    ])
    _write_ticket(tmp_path / "T-0002-b.md", tid="T-0002", progress=[
        ("2026-07-03T00:00:00Z", "S-alice-p1", "third event, on another ticket"),
        ("2026-07-01T00:00:00Z", "S-bob-p2", "unrelated session"),
    ])
    idx = bsq._progress_lines_by_sid("demo")
    assert [r["text"] for r in idx["S-alice-p1"]] == [
        "first event", "second event", "third event, on another ticket",
    ]
    assert [r["ticket"] for r in idx["S-alice-p1"]] == ["T-0001", "T-0001", "T-0002"]
    assert len(idx["S-bob-p2"]) == 1


def test_sid_lookup_prints_session_and_progress_trail(tmp_path, monkeypatch, capsys):
    _write_ticket(tmp_path / "T-0001-a.md", tid="T-0001", progress=[
        ("2026-07-01T00:00:00Z", "S-alice-p1", "wired the widget"),
    ])
    _setup(tmp_path, monkeypatch, [
        {"sid": "S-alice-p1", "status": "suspended", "archived": False,
         "window": "widget-work", "task_id": None, "last_task_id": "T-0001",
         "started_at": "2026-07-01T00:00:00Z", "suspended_at": "2026-07-01T01:00:00Z"},
    ])
    parser = bsq.build_parser()
    args = parser.parse_args(["session", "search", "--sid", "S-alice-p1"])
    args.func(args)
    out = capsys.readouterr().out
    assert "S-alice-p1" in out
    assert "widget-work" in out
    assert "T-0001" in out
    assert "wired the widget" in out


def test_sid_lookup_with_no_record_or_progress_dies(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [])
    parser = bsq.build_parser()
    args = parser.parse_args(["session", "search", "--sid", "S-nobody"])
    with pytest.raises(SystemExit):
        args.func(args)


def test_query_mode_ranks_by_keyword_hits_in_progress_text(tmp_path, monkeypatch, capsys):
    _write_ticket(tmp_path / "T-0001-a.md", tid="T-0001", progress=[
        ("2026-07-01T00:00:00Z", "S-alice-p1", "migrated the payment gateway to stripe"),
    ])
    _write_ticket(tmp_path / "T-0002-b.md", tid="T-0002", progress=[
        ("2026-07-02T00:00:00Z", "S-bob-p2", "fixed a typo in the readme"),
    ])
    _setup(tmp_path, monkeypatch, [
        {"sid": "S-alice-p1", "status": "suspended", "archived": False,
         "window": "payments", "task_id": None, "last_task_id": "T-0001"},
        {"sid": "S-bob-p2", "status": "suspended", "archived": False,
         "window": "docs", "task_id": None, "last_task_id": "T-0002"},
    ])
    parser = bsq.build_parser()
    args = parser.parse_args(["session", "search", "payment gateway"])
    args.func(args)
    out = capsys.readouterr().out
    assert "S-alice-p1" in out
    assert "S-bob-p2" not in out


def test_query_mode_no_match_reports_none(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, [
        {"sid": "S-alice-p1", "status": "suspended", "archived": False,
         "window": "payments"},
    ])
    parser = bsq.build_parser()
    args = parser.parse_args(["session", "search", "nonexistent topic zzz"])
    args.func(args)
    out = capsys.readouterr().out
    assert "no exited session matched" in out


def test_query_mode_json_output(tmp_path, monkeypatch, capsys):
    _write_ticket(tmp_path / "T-0001-a.md", tid="T-0001", progress=[
        ("2026-07-01T00:00:00Z", "S-alice-p1", "wired the widget dashboard"),
    ])
    _setup(tmp_path, monkeypatch, [
        {"sid": "S-alice-p1", "status": "suspended", "archived": False,
         "window": "widget", "task_id": None, "last_task_id": "T-0001"},
    ])
    parser = bsq.build_parser()
    args = parser.parse_args(["session", "search", "widget", "--json"])
    args.func(args)
    out = capsys.readouterr().out
    import json
    data = json.loads(out)
    assert data[0]["sid"] == "S-alice-p1"
    assert data[0]["tickets"] == ["T-0001"]


def test_no_query_and_no_sid_dies(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [])
    parser = bsq.build_parser()
    args = parser.parse_args(["session", "search"])
    with pytest.raises(SystemExit):
        args.func(args)
