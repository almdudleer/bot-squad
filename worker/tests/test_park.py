"""T-0252 (WS-4 S5): 5h-usage-limit park + retry.

When the detector reports a session whose pane shows the 5h usage-limit prompt,
PARK it (suspend → frees the parallel slot + preserves claude_uuid) and RETRY
(resume) once the limit has reset. Blast-radius (suspends a LIVE session), so:
default-OFF gate (BOT_SQUAD_PARK_RETRY), a DEBOUNCE (the marker must persist
across N consecutive detector samples before we suspend — guards a transient
capture-pane false match), and re-admit only when a slot is free + pressure
cleared. resets-at is parsed from the pane when present, else a fallback delay.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import park as P


def _cfg(tmp_path):
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=tmp_path / "data")


# --- gate / tunables -------------------------------------------------------

def test_park_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_PARK_RETRY", raising=False)
    assert P.park_enabled() is False  # blast-radius → opt-in


def test_park_enabled_when_set(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_PARK_RETRY", "1")
    assert P.park_enabled() is True


def test_debounce_default(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_PARK_DEBOUNCE", raising=False)
    assert P.debounce_samples() == 2


# --- resets-at parsing (pure) ----------------------------------------------

def test_parse_resets_at_24h():
    # now = 12:00:00 UTC on an arbitrary day → "resets at 15:30" = +3h30m
    now = 1_000_000  # epoch
    out = P.parse_resets_at("usage limit reached — resets at 15:30", now,
                            now_hm=(12, 0))
    assert out == now + (3 * 3600 + 30 * 60)


def test_parse_resets_at_rolls_to_next_day_when_past():
    now = 1_000_000
    # "resets at 09:00" but it's already 12:00 → tomorrow 09:00 = +21h
    out = P.parse_resets_at("resets at 09:00", now, now_hm=(12, 0))
    assert out == now + (21 * 3600)


def test_parse_resets_at_absent_returns_none():
    assert P.parse_resets_at("still working, all good", 1_000_000, now_hm=(12, 0)) is None


# --- debounce decision (pure) ----------------------------------------------

def test_decide_parks_only_after_debounce():
    # seen counts BEFORE this sample; DEBOUNCE=2
    # first sighting (count 0→1): not yet
    to_park, counts = P.decide_park({"S-a-p1"}, prev_counts={}, debounce=2,
                                    already_parked=set())
    assert to_park == set()
    assert counts == {"S-a-p1": 1}
    # second consecutive sighting (1→2): park now
    to_park2, counts2 = P.decide_park({"S-a-p1"}, prev_counts={"S-a-p1": 1},
                                      debounce=2, already_parked=set())
    assert to_park2 == {"S-a-p1"}
    assert counts2 == {"S-a-p1": 2}


def test_decide_resets_count_when_marker_clears():
    to_park, counts = P.decide_park(set(), prev_counts={"S-a-p1": 1}, debounce=2,
                                    already_parked=set())
    assert to_park == set()
    assert counts == {}  # cleared → no longer accumulating


def test_decide_skips_already_parked():
    to_park, counts = P.decide_park({"S-a-p1"}, prev_counts={"S-a-p1": 5},
                                    debounce=2, already_parked={"S-a-p1"})
    assert to_park == set()  # already parked → don't re-suspend


# --- tick orchestration (injected side effects) ----------------------------

def test_tick_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_SQUAD_PARK_RETRY", raising=False)
    out = P.park_tick(_cfg(tmp_path))
    assert out["enabled"] is False


def test_tick_parks_after_debounce(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_SQUAD_PARK_RETRY", "1")
    monkeypatch.setenv("BOT_SQUAD_PARK_DEBOUNCE", "2")
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(P, "session_pressure",
                        lambda c, now_epoch=None: {"limit_blocked_sids": ["S-x-p9"]})
    monkeypatch.setattr(P, "_slug_for_sid", lambda c, sid: "p1")
    parked = []
    monkeypatch.setattr(P, "_do_park", lambda c, slug, sid: parked.append(sid))
    monkeypatch.setattr(P, "_retry_parked", lambda c, now_epoch=None: [])

    P.park_tick(cfg, now_epoch=1000)   # 1st sighting → count 1, no park
    assert parked == []
    P.park_tick(cfg, now_epoch=1060)   # 2nd → park
    assert parked == ["S-x-p9"]
