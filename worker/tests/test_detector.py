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

# Verbatim pane capture of the Claude Code promo banner (2026-07-04, T-0571).
# It mentions "usage limit" AND "hit your limit" while the session is perfectly
# healthy — informational copy like this must never read as pressure.
FABLE5_PROMO_BANNER = (
    " ▎ Until July 7, you can use up to 50% of your plan's weekly usage limit "
    "on Fable 5. If you hit your limit, you can continue on Fable 5 with usage "
    "credits. Fable 5 draws down usage faster than Opus 4.8. Learn more"
)


def test_text_has_limit_marker_matches_real_limit_banners():
    # older interactive prompt (with tmux decoration prefix)
    assert D.text_has_limit_marker("  ⎿  Claude usage limit reached. Your limit will reset at 4am (UTC).")
    # status-line variants
    assert D.text_has_limit_marker("✗ 5-hour limit reached ∙ resets 3am")
    assert D.text_has_limit_marker("Weekly limit reached ∙ resets Oct 9")
    assert D.text_has_limit_marker("Approaching usage limit · resets at 7pm")
    # case-insensitive
    assert D.text_has_limit_marker("USAGE LIMIT REACHED — RESETS AT 18:00")
    # banner buried in ordinary pane output still trips
    assert D.text_has_limit_marker("some output\n✗ 5-hour limit reached ∙ resets 3am\n❯ ")


def test_text_has_limit_marker_ignores_benign_text():
    assert not D.text_has_limit_marker("Running tests, all green")
    assert not D.text_has_limit_marker("")


def test_promo_banner_does_not_trip_marker():
    """T-0571: the Fable-5 promo banner tripped the bare 'usage limit' marker,
    clamping global concurrency to 2 on healthy idle panes."""
    assert not D.text_has_limit_marker(FABLE5_PROMO_BANNER)
    # ...even when embedded in ordinary composer output
    assert not D.text_has_limit_marker("some output\n" + FABLE5_PROMO_BANNER + "\n❯ ")


def test_quoted_marker_text_does_not_trip():
    """T-0571 (live incident): panes QUOTING marker phrases — a spawn brief, an
    env-override assignment, or the detector's own source in a diff — must not
    read as a limit hit; only a banner-shaped line (marker at line start +
    reset/retry context) counts."""
    # spawn brief discussing the markers, mid-prose
    assert not D.text_has_limit_marker(
        "❯ You are a DEV worker on T-0571: the bare 'usage limit' marker and "
        "'limit reached' phrases trip session_pressure when quoted"
    )
    # env-override assignment in a shell / .env pane, at line start
    assert not D.text_has_limit_marker(
        "BOT_SQUAD_LIMIT_MARKERS='limit reached,resets at,rate limit'"
    )
    # detector.py source shown in a diff pane: marker string starts the line
    # (after the +/quote decoration) but has no reset/retry context
    assert not D.text_has_limit_marker('+    "limit reached",\n+    "rate limited",')


def test_limit_markers_env_override(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_LIMIT_MARKERS", "foobar, baz")
    assert D.limit_markers() == ["foobar", "baz"]
    # custom markers use the same banner-shape rule: line start + context
    assert D.text_has_limit_marker("FOOBAR — resets at 5am")
    assert not D.text_has_limit_marker("a foobar appeared mid-prose, resets at 5am")
    assert not D.text_has_limit_marker("foobar bare on a line, no lift-time given")
    assert not D.text_has_limit_marker("usage limit reached — resets at 3pm")  # defaults overridden


def test_limit_markers_empty_env_disables_scan(monkeypatch):
    """unset → defaults; empty → pane scan OFF; csv → custom (operator knob)."""
    monkeypatch.setenv("BOT_SQUAD_LIMIT_MARKERS", "")
    assert D.limit_markers() == []
    assert not D.text_has_limit_marker("✗ 5-hour limit reached ∙ resets 3am")


def test_capture_pane_joins_wrapped_lines(monkeypatch):
    """-J unwraps long prose lines so a quoted marker phrase can't land at a
    visual line start via terminal wrapping."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(stdout="text")

    monkeypatch.setattr(D.subprocess, "run", fake_run)
    assert D._capture_pane("%1") == "text"
    assert "-J" in seen["cmd"]


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
