"""T-0931: `bsq ticket update` enforces the state-machine transition graph at
this write boundary too, not just the api PATCH — invalid transitions refused
loudly, citing the allowed targets, and the file left untouched on refusal."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_ticket_transitions", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_ticket_transitions", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(path: Path, *, status: str) -> None:
    fm = ["id: T-0001", "title: demo", f"status: {status}", "provenance: T-0001"]
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\n## Verbatim request\n\nreal words\n")


def _run_update(tmp_path, monkeypatch, status):
    monkeypatch.setattr(bsq, "backlog_dir", lambda slug: tmp_path)
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: "demo")
    parser = bsq.build_parser()
    args = parser.parse_args(["ticket", "update", "T-0001", status])
    args.func(args)


def test_invalid_transition_refused(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="totest")
    with pytest.raises(SystemExit):
        _run_update(tmp_path, monkeypatch, "open")
    # refused write leaves the file untouched
    assert bsq.read_frontmatter(p)["status"] == "totest"


def test_valid_transition_succeeds(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="in_progress")
    _run_update(tmp_path, monkeypatch, "blocked_on_user")
    assert bsq.read_frontmatter(p)["status"] == "blocked_on_user"


def test_blocked_on_user_resolves_back_to_in_progress(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="blocked_on_user")
    _run_update(tmp_path, monkeypatch, "in_progress")
    assert bsq.read_frontmatter(p)["status"] == "in_progress"


def test_noop_status_always_allowed(tmp_path, monkeypatch):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="totest")
    _run_update(tmp_path, monkeypatch, "totest")
    assert bsq.read_frontmatter(p)["status"] == "totest"


def test_legacy_unknown_status_is_correctable(tmp_path, monkeypatch):
    # A ticket predating this graph (or hand-edited to garbage) must still be
    # fixable to any real status, not locked out.
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="bogus-legacy")
    _run_update(tmp_path, monkeypatch, "open")
    assert bsq.read_frontmatter(p)["status"] == "open"


def test_error_message_cites_allowed_targets(tmp_path, monkeypatch, capsys):
    p = tmp_path / "T-0001-demo.md"
    _write(p, status="closed")
    with pytest.raises(SystemExit):
        _run_update(tmp_path, monkeypatch, "in_progress")
    err = capsys.readouterr().err
    assert "closed" in err and "in_progress" in err
    assert "reopened" in err  # the one allowed target from closed
