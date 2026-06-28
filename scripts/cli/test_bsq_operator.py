"""T-0522 (Process Paradigm M2): the `bsq operator pause/resume/status` verbs —
the user-facing toggle for T-0474's operator re-drive pause flag.

Pins that each verb sends EXACTLY the right params to its worker action (the
actions enforce a strict allowed-param set) and prints the paused-vs-driving
state. `post` is stubbed (no live worker socket — mid-deploy-freeze; the live
round-trip is the post-deploy verify).

`bsq` is extensionless, so it's loaded via SourceFileLoader (mirrors
test_bsq_access_points.py).
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

SLUG = "proj"


@pytest.fixture(autouse=True)
def _stub_slug(monkeypatch):
    monkeypatch.setattr(bsq, "resolve_slug", lambda *a, **k: SLUG)


def _record_post(returns):
    calls = []

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        return returns.get(action, {"ok": True})

    return fake_post, calls


# --- pause ------------------------------------------------------------------

def test_pause_sends_slug_and_reason(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_pause": {"ok": True, "was_already_paused": False,
                            "paused": {"reason": "away"}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_pause(SimpleNamespace(reason="away"))

    assert calls == [("operator_pause", {"slug": SLUG, "reason": "away"})]
    out = capsys.readouterr().out
    assert "paused" in out and "away" in out


def test_pause_omits_reason_when_absent(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_pause": {"ok": True, "was_already_paused": False, "paused": {}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_pause(SimpleNamespace(reason=None))

    # Strict allowed-param set: no `reason` key when not given.
    assert calls == [("operator_pause", {"slug": SLUG})]


def test_pause_reports_already_paused(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_pause": {"ok": True, "was_already_paused": True, "paused": {}}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_pause(SimpleNamespace(reason=None))

    assert "already paused" in capsys.readouterr().out


# --- resume -----------------------------------------------------------------

def test_resume_round_trips(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_resume": {"ok": True, "was_paused": True}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_resume(SimpleNamespace())

    assert calls == [("operator_resume", {"slug": SLUG})]
    assert "resumed" in capsys.readouterr().out


def test_resume_noop_when_not_paused(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_resume": {"ok": True, "was_paused": False}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_resume(SimpleNamespace())

    assert "not paused" in capsys.readouterr().out


# --- status -----------------------------------------------------------------

def test_status_paused(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_status": {"ok": True, "paused": True, "state": "paused",
                             "live_operators": [], "pending_backlog": 3}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_status(SimpleNamespace())

    assert calls == [("operator_status", {"slug": SLUG})]
    assert "PAUSED" in capsys.readouterr().out


def test_status_driving(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_status": {"ok": True, "paused": False, "state": "driving",
                             "live_operators": ["S-op-1"], "pending_backlog": 2}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_status(SimpleNamespace())

    out = capsys.readouterr().out
    assert "DRIVING" in out and "S-op-1" in out


def test_status_idle(monkeypatch, capsys):
    fake_post, calls = _record_post(
        {"operator_status": {"ok": True, "paused": False,
                             "state": "idle-empty-backlog",
                             "live_operators": [], "pending_backlog": 0}})
    monkeypatch.setattr(bsq, "post", fake_post)

    bsq.cmd_operator_status(SimpleNamespace())

    assert "idle" in capsys.readouterr().out
