"""``bsq task aging`` wrapper tests (T-0950 redesign, 2026-09-06).

The aging-report COMPOSITION is unit-tested in
``worker/tests/test_status_deadlines.py``; this file covers the thin CLI
layer: parser wiring, the exact action payload (``deadline_aging_report``
+ slug, optional ``statuses``), and text vs ``--json`` rendering. Mirrors
``test_bsq_task_digest.py``'s shape for the sibling pull-only verb.
"""
from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_ta", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_ta", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

_REPORT = {
    "ok": True,
    "slug": "demo",
    "total": 1,
    "breaches": [{"id": "T-0001", "status": "totest", "age_sec": 90000}],
    "text": "⏳ 1 ticket(s) past deadline:\n⏳ T-0001 totest for 25.0h (since updated) — x",
}


def _run(monkeypatch, argv, report=_REPORT):
    """Drive the real argparse wiring end-to-end with post() recorded."""
    calls = []

    def _fake_post(action, params, **kw):
        calls.append((action, params))
        return dict(report)

    monkeypatch.setattr(bsq, "post", _fake_post)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return calls


def test_task_aging_posts_slug_only_by_default(monkeypatch, capsys):
    calls = _run(monkeypatch, ["task", "aging"])
    assert calls == [("deadline_aging_report", {"slug": "demo"})]
    out = capsys.readouterr().out
    assert out.strip() == _REPORT["text"]  # ready-to-paste, nothing added


def test_task_aging_status_filter_forwarded(monkeypatch):
    calls = _run(monkeypatch, ["task", "aging", "--status", "totest"])
    assert calls == [("deadline_aging_report", {"slug": "demo", "statuses": ["totest"]})]


def test_task_aging_status_filter_repeatable(monkeypatch):
    calls = _run(
        monkeypatch,
        ["task", "aging", "--status", "totest", "--status", "to_accept"],
    )
    assert calls[0][1]["statuses"] == ["totest", "to_accept"]


def test_task_aging_json(monkeypatch, capsys):
    _run(monkeypatch, ["task", "aging", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["total"] == 1
    assert out["breaches"][0]["id"] == "T-0001"


def test_task_aging_dies_before_post_without_worker(monkeypatch):
    """No socket -> post() dies (exit) BEFORE anything is printed — the CLI
    must never fabricate an aging picture without the worker."""
    import pytest

    def _die(*a, **k):
        raise SystemExit(1)

    monkeypatch.setattr(bsq, "post", _die)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(["task", "aging"])
    with pytest.raises(SystemExit):
        args.func(args)
