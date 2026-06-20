"""T-0248 (WS-4 S1): pressure detector.

Two pressure sources for the run-survival backoff governor:
  (A) Claude 429 rate-limit — ALREADY sampled by telemetry into each session's
      record as ``rate_limited``; we surface the sids whose flag is set within a
      recency window.
  (B) the interactive per-session 5-hour usage-limit PANE prompt — NOT a jsonl
      429, so we scan ``tmux capture-pane`` text for a small marker set.

``session_pressure(cfg)`` is the single read the governor consults; it is
pure-ish (reads telemetry record files + an injectable pane-capture) and never
mutates state.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import detector as D


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    data_dir = tmp_path / "data"
    (data_dir / "p1" / "_worker" / "telemetry").mkdir(parents=True)
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=data_dir)


def _write_record(cfg, sid: str, *, rate_limited: bool, sampled_at: str) -> None:
    rec = {"sid": sid, "rate_limited": rate_limited, "sampled_at": sampled_at}
    p = cfg.data_dir / "p1" / "_worker" / "telemetry" / f"{sid}.json"
    p.write_text(json.dumps(rec))


# --- marker matching -------------------------------------------------------

def test_text_has_limit_marker_matches_known_phrases():
    assert D.text_has_limit_marker("Claude usage limit reached. resets at 3pm")
    assert D.text_has_limit_marker("You've hit the 5-hour limit")
    assert D.text_has_limit_marker("RATE LIMIT — retrying")  # case-insensitive


def test_text_has_limit_marker_ignores_benign_text():
    assert not D.text_has_limit_marker("Running tests, all green")
    assert not D.text_has_limit_marker("")


def test_limit_markers_env_override(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_LIMIT_MARKERS", "foobar, baz")
    assert D.limit_markers() == ["foobar", "baz"]
    assert D.text_has_limit_marker("a FOOBAR appeared")
    assert not D.text_has_limit_marker("usage limit reached")  # default list overridden


# --- recency window --------------------------------------------------------

def test_pressure_window_default_and_override(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_PRESSURE_WINDOW", raising=False)
    assert D.pressure_window_sec() == 180
    monkeypatch.setenv("BOT_SQUAD_PRESSURE_WINDOW", "60")
    assert D.pressure_window_sec() == 60


# --- (A) 429 from telemetry records ----------------------------------------

def test_recent_rate_limited_window(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    now = 1_000_000
    monkeypatch.setattr(D, "_parse_iso", lambda s: {"fresh": now - 10, "stale": now - 999, "off": now - 5}.get(s))
    _write_record(cfg, "S-u-fresh-p1", rate_limited=True, sampled_at="fresh")
    _write_record(cfg, "S-u-stale-p2", rate_limited=True, sampled_at="stale")   # too old
    _write_record(cfg, "S-u-off-p3", rate_limited=False, sampled_at="off")      # not limited
    got = D._recent_rate_limited(cfg, now_epoch=now)
    assert got == {"S-u-fresh-p1"}


# --- (B) 5h-limit pane scan ------------------------------------------------

def test_limit_blocked_panes_scans_capture(monkeypatch, tmp_path):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(D, "_live_panes", lambda c: [("S-u-a-p1", "%1"), ("S-u-b-p2", "%2")])
    monkeypatch.setattr(D, "_capture_pane", lambda pane: {
        "%1": "normal composer output",
        "%2": "Claude usage limit reached — resets at 18:00",
    }[pane])
    got = D._limit_blocked_panes(cfg)
    assert got == {"S-u-b-p2"}


# --- combined --------------------------------------------------------------

def test_session_pressure_combines_both(monkeypatch, tmp_path):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(D, "_recent_rate_limited", lambda c, now_epoch=None: {"S-rl-p1"})
    monkeypatch.setattr(D, "_limit_blocked_panes", lambda c: {"S-lim-p2"})
    out = D.session_pressure(cfg, now_epoch=123)
    assert set(out["rate_limited_sids"]) == {"S-rl-p1"}
    assert set(out["limit_blocked_sids"]) == {"S-lim-p2"}
    assert out["any"] is True


def test_session_pressure_none_when_clear(monkeypatch, tmp_path):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(D, "_recent_rate_limited", lambda c, now_epoch=None: set())
    monkeypatch.setattr(D, "_limit_blocked_panes", lambda c: set())
    out = D.session_pressure(cfg)
    assert out["rate_limited_sids"] == []
    assert out["limit_blocked_sids"] == []
    assert out["any"] is False
