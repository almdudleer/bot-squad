"""T-0998: `bsq drift status --sid <sid>` accepted ANY string and answered
"ON (active)" (exit 0) even when no SessionMd for that SID existed — a green
verdict about nothing. Root cause: `cmd_drift_status` defaulted
`paused = False` when the md was missing, printed the fabricated verdict
anyway, and put the ONLY hint (a "no SessionMd" line) on stderr where a
caller checking stdout/exit-code would never see it.

`drift on`/`drift off` (`set_drift_paused`, worker/bot_squad_worker/sessions.py)
already refuse a missing SessionMd with an ActionError instead of writing one
— verified live before this fix, unchanged here. This pins `status` to the
same contract: refuse, don't fabricate.
"""
from __future__ import annotations

import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_drift_status", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_drift_status", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


@pytest.fixture
def sessions_dir_for(tmp_path, monkeypatch):
    """Point `sessions_dir(slug)` at an empty tmp dir for any slug."""
    monkeypatch.setattr(bsq, "resolve_slug", lambda: "test-slug")
    monkeypatch.setattr(bsq, "sessions_dir", lambda slug: tmp_path)
    return tmp_path


def test_a_sid_with_no_sessionmd_refuses_rather_than_answering_on(sessions_dir_for, capsys):
    """The bug: ANY string used to print "ON (active)" with exit 0."""
    args = argparse.Namespace(sid="S-totally-bogus-nonexistent-sid-xyz123")
    with pytest.raises(SystemExit) as exc:
        bsq.cmd_drift_status(args)
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert "ON (active)" not in out.out
    assert "OFF (paused)" not in out.out
    assert "not a known session" in out.err


def test_a_real_session_with_no_drift_paused_field_still_reports_on(sessions_dir_for, capsys):
    """Positive control: an existing, un-paused session still answers ON."""
    md = sessions_dir_for / "S-real-p1.md"
    md.write_text("---\nsid: S-real-p1\n---\n")
    bsq.cmd_drift_status(argparse.Namespace(sid="S-real-p1"))
    out = capsys.readouterr()
    assert "S-real-p1: ON (active)" in out.out


def test_a_real_session_with_drift_paused_reports_off(sessions_dir_for, capsys):
    """Positive control: the OFF (paused) branch is untouched by the fix."""
    md = sessions_dir_for / "S-real-p2.md"
    md.write_text("---\nsid: S-real-p2\ndrift_paused: true\n---\n")
    bsq.cmd_drift_status(argparse.Namespace(sid="S-real-p2"))
    out = capsys.readouterr()
    assert "S-real-p2: OFF (paused)" in out.out
