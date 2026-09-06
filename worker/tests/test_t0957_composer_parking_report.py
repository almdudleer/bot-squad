"""T-0957 DoD 1 — a composer parked past its own bound is DETECTED and
REPORTED, even on a session no gate happens to act on this tick.

``composer_watch.observe`` already ages content to STALE after
``STALE_AFTER_SEC`` — but it is only ever called by a gate about to act on
the pane (a compact-due check, a delivery attempt). A session nobody is
trying to do anything with is never sampled, so text parked on an otherwise-
idle session's composer can sit unreported for days (the measured symptom:
four sessions held unsubmitted work instructions for days, found only by
hand). ``composer_parking_report.report_parked_composers`` is the periodic
sampler that closes that gap.
"""
from __future__ import annotations

import types

import pytest

from bot_squad_worker import composer_parking_report as CPR
from bot_squad_worker import composer_watch as CW


def _pane(composer: str = "") -> str:
    """An idle pane whose composer holds ``composer`` (same shape as a real
    Claude Code TUI — mirrors test_t0954_composer_watch._pane)."""
    return ("● did some work\n"
            "─────────────────────────────────────────\n"
            f"❯ {composer}\n"
            "─────────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · ← for agents\n")


@pytest.fixture
def cpr_cfg(tmp_path):
    return types.SimpleNamespace(data_dir=tmp_path / "data"), "bot-squad"


def _seams(*, rows, pane_map, panes_by_id):
    """Build the three injectable seams ``report_parked_composers`` takes."""
    return {
        "list_sessions": lambda cfg, slug: rows,
        "pane_map": lambda: pane_map,
        "capture": lambda pane_id: panes_by_id[pane_id],
    }


def test_stale_composer_on_an_untouched_session_is_reported(cpr_cfg):
    """The core gap: nobody is polling %528's compact/delivery gate, so
    without this sampler its parked text is never even OBSERVED, let alone
    reported."""
    cfg, slug = cpr_cfg
    rows = [{"sid": "S-routine-handler-p528", "status": "active"}]
    panes = {"%528": _pane("file backlog tickets for the three blind signals")}
    seams = _seams(rows=rows, pane_map={"S-routine-handler-p528": "%528"},
                   panes_by_id=panes)

    first = CPR.report_parked_composers(cfg, slug, now=1000.0, **seams)
    assert first == []  # first sighting: TYPING, not yet stale

    late = CPR.report_parked_composers(
        cfg, slug, now=1000.0 + CW.STALE_AFTER_SEC, **seams)
    assert len(late) == 1
    assert late[0]["sid"] == "S-routine-handler-p528"
    assert late[0]["pane"] == "%528"
    assert late[0]["state"] == CW.STATE_STALE
    assert "file backlog tickets" in late[0]["text"]


def test_typing_within_the_window_is_not_reported(cpr_cfg):
    cfg, slug = cpr_cfg
    rows = [{"sid": "S-x", "status": "active"}]
    seams = _seams(rows=rows, pane_map={"S-x": "%1"},
                   panes_by_id={"%1": _pane("half a sent")})

    out = CPR.report_parked_composers(
        cfg, slug, now=1000.0 + CW.STALE_AFTER_SEC - 1, **seams)
    assert out == []


def test_empty_and_dialog_composers_are_not_reported_as_parked(cpr_cfg):
    """An empty composer has nothing parked; a dialog is a different state
    (T-0954) and reporting it here would duplicate that gate's own handling,
    not this one's job (unsubmitted TEXT)."""
    cfg, slug = cpr_cfg
    dialog = (" Do you want to proceed?\n"
              " ❯ 1. Yes\n"
              "   2. Yes, and don't ask again for: mail\n"
              "   3. No\n"
              " Esc to cancel · Tab to amend\n")
    rows = [{"sid": "S-empty", "status": "active"},
            {"sid": "S-dialog", "status": "active"}]
    seams = _seams(rows=rows,
                   pane_map={"S-empty": "%1", "S-dialog": "%2"},
                   panes_by_id={"%1": _pane(""), "%2": dialog})

    out = CPR.report_parked_composers(
        cfg, slug, now=1000.0 + CW.STALE_AFTER_SEC, **seams)
    assert out == []


def test_a_session_with_no_live_pane_is_skipped(cpr_cfg):
    """A row list_sessions returns for a paused/suspended session must not
    crash the sweep just because no pane is mapped for it."""
    cfg, slug = cpr_cfg
    rows = [{"sid": "S-gone", "status": "active"}]
    seams = _seams(rows=rows, pane_map={}, panes_by_id={})

    out = CPR.report_parked_composers(cfg, slug, now=1000.0, **seams)
    assert out == []


def test_inactive_rows_are_never_sampled(cpr_cfg):
    cfg, slug = cpr_cfg
    rows = [{"sid": "S-x", "status": "paused"}]
    seams = _seams(rows=rows, pane_map={"S-x": "%1"},
                   panes_by_id={"%1": _pane("still here")})

    out = CPR.report_parked_composers(
        cfg, slug, now=1000.0 + CW.STALE_AFTER_SEC, **seams)
    assert out == []


def test_one_bad_pane_does_not_blank_the_whole_sweep(cpr_cfg):
    cfg, slug = cpr_cfg
    rows = [{"sid": "S-bad", "status": "active"},
            {"sid": "S-good", "status": "active"}]

    def capture(pane_id):
        if pane_id == "%bad":
            raise RuntimeError("tmux capture-pane failed")
        return _pane("parked payload")

    kwargs = dict(
        list_sessions=lambda cfg, slug: rows,
        pane_map=lambda: {"S-bad": "%bad", "S-good": "%good"},
        capture=capture,
    )
    CPR.report_parked_composers(cfg, slug, now=1000.0, **kwargs)  # seed S-good
    out = CPR.report_parked_composers(
        cfg, slug, now=1000.0 + CW.STALE_AFTER_SEC, **kwargs)
    assert len(out) == 1
    assert out[0]["sid"] == "S-good"


# --- negative control: pin the pre-fix behaviour this closes ---------------

def test_gap_this_closes_composer_watch_alone_never_self_samples(cpr_cfg):
    """The regression this module exists to prevent: without a periodic
    sampler, `composer_watch.observe` is simply never called for a session
    nothing is trying to act on — so nothing about a stale composer is ever
    known, let alone reported. Pinned here so a future change that makes
    `observe` itself lazy/self-polling doesn't silently make this module
    redundant without anyone noticing."""
    cfg, slug = cpr_cfg
    # No call into composer_watch.observe happened for this sid — its state
    # file was never created — which is exactly the "never sampled" gap.
    assert not CW.state_path(cfg, slug, "S-never-touched").exists()
