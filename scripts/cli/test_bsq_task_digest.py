"""``bsq task digest`` wrapper tests (T-0589).

The digest COMPOSITION is unit-tested in ``worker/tests/test_task_chat.py``;
this file covers the thin CLI layer: parser wiring, the exact action payload
(``task_digest`` + slug only), and text vs ``--json`` rendering. Automated
after the manual walkthrough (scenarios/T-0589) that drove the real
bsq→socket→action round-trip against a sandbox worker.
"""
from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_td", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_td", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

_DIGEST = {
    "ok": True,
    "text": "Бэклог demo: 1 in_progress (2 closed)\n• T-0001 P1 in_progress — Voice ask",
    "counts": {"in_progress": 1, "closed": 2},
}


def _run(monkeypatch, argv, digest=_DIGEST):
    """Drive the real argparse wiring end-to-end with post() recorded."""
    calls = []

    def _fake_post(action, params, **kw):
        calls.append((action, params))
        return dict(digest)

    monkeypatch.setattr(bsq, "post", _fake_post)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return calls


def test_task_digest_posts_slug_only(monkeypatch, capsys):
    calls = _run(monkeypatch, ["task", "digest"])
    assert calls == [("task_digest", {"slug": "demo"})]
    out = capsys.readouterr().out
    assert out.strip() == _DIGEST["text"]  # ready-to-paste, nothing added


def test_task_digest_json(monkeypatch, capsys):
    _run(monkeypatch, ["task", "digest", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["counts"]["in_progress"] == 1
    assert "T-0001" in out["text"]


def test_task_digest_dies_before_post_without_worker(monkeypatch):
    """No socket → post() dies (exit) BEFORE anything is printed as a digest —
    the CLI must never fabricate backlog state without the worker."""
    import pytest

    def _die(*a, **k):
        raise SystemExit(1)

    monkeypatch.setattr(bsq, "post", _die)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(["task", "digest"])
    with pytest.raises(SystemExit):
        args.func(args)
