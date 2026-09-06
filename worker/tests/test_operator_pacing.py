"""T-0475 / M2-F2.6 — operator pacing status (parallelism + best-effort weekly
quota target). Unit-tests the signal composition + recommendation in
``operator_redrive.pacing_status``; the parallelism cap value itself is owned by
``pace.py`` (TL-B / T-0482) so we monkeypatch it and test OUR logic on top.

T-0966: the LOAD the cap is measured against is now live dev sessions, not the
``in_progress`` board label — so the tests that build load do it with ``_dev``
(a live session) and the ones that build board rows with ``_task`` are asserting
about the REPORTED label, which gates nothing. See
``test_t0966_pace_cap_counts_sessions.py`` for why."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import operator_redrive as ord_
from bot_squad_worker import pace as _pace
from bot_squad_worker import sessions as _sessions
from bot_squad_worker import telemetry as _telemetry

SLUG = "bot-squad"

#: SIDs the patched ``_live_agent_sids`` reports as running a live claude pane.
_LIVE_AGENTS: set[str] = set()


@pytest.fixture(autouse=True)
def _no_panes(monkeypatch):
    _LIVE_AGENTS.clear()
    monkeypatch.setattr(_sessions, "list_panes", lambda: [])
    monkeypatch.setattr(_sessions, "_live_agent_sids", lambda: set(_LIVE_AGENTS))


@pytest.fixture
def cfg(tmp_path: Path):
    data = tmp_path / "data"
    (data / SLUG / "backlog").mkdir(parents=True, exist_ok=True)
    (data / SLUG / "sessions").mkdir(parents=True, exist_ok=True)
    cfgdir = tmp_path / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(data_dir=data, config_dir=cfgdir, projects={SLUG: object()})


def _dev(cfg, sid, task_id="~"):
    """A live leaf-dev session — one unit of the load the cap gates (T-0966).
    ``window="w"`` is the leaf-dev marker; a coordinator window would not count."""
    _sessions._write_session_metadata(
        cfg.data_dir / SLUG / "sessions" / f"{sid}.md",
        {"sid": sid, "status": "active", "window": "w",
         "task_id": task_id, "initiative": "~"},
    )
    _LIVE_AGENTS.add(sid)


def _task(cfg, tid, status="open", archived=False):
    arch = f"\narchived: {str(archived).lower()}" if archived else ""
    (cfg.data_dir / SLUG / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: x\nstatus: {status}{arch}\n---\n\nbody\n", encoding="utf-8")


def _set_target(cfg, pct):
    (cfg.config_dir / "system_settings.toml").write_text(
        f"[operator]\nweekly_quota_target_pct = {pct}\n", encoding="utf-8")


def _no_target(cfg):
    """Write a ``system_settings.toml`` with no ``[operator]`` section into the
    tmp config_dir — the explicit "no weekly target set" fixture. T-0697:
    ``_system_settings_path`` now honors an explicit ``cfg.config_dir`` even
    when the file is absent there, so these tests must not lean on that
    absence to get a None target; they write their own isolated file instead,
    same as ``_set_target`` does for the "target set" case."""
    (cfg.config_dir / "system_settings.toml").write_text("", encoding="utf-8")


def _set_quota(cfg, *, burn=None, remaining=None, r429=0, budget=None):
    data = {
        "burn_tokens_per_hr": burn, "remaining_tokens": remaining,
        "rate_limit_429": {"count": r429, "last_at": None},
    }
    if budget is not None:
        data["anchor"] = {"budget_tokens": budget, "set_at": "2026-07-14T00:00:00Z"}
    q = _telemetry._quota_path(cfg, SLUG)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(json.dumps(data), encoding="utf-8")


def test_fresh_project_degrades_to_ok(cfg, monkeypatch):
    """No cap, no target, no telemetry -> a usable all-defaults dashboard, 'ok'."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _no_target(cfg)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["max_in_progress"] == 0 and st["in_progress"] == 0
    assert st["live_dev_sessions"] == 0 and st["cap_counts"] == "live_dev_sessions"
    assert st["at_cap"] is False
    assert st["weekly_target_pct"] is None
    assert st["burn_tokens_per_hr"] is None and st["remaining_tokens"] is None
    assert st["recommendation"] == "ok"


def test_in_progress_counts_only_in_progress_nonarchived(cfg, monkeypatch):
    """The board-label count is still REPORTED (that is how its drift stays
    visible); T-0966 only stopped it GATING. This pins the count itself."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _task(cfg, "T-1", status="in_progress")
    _task(cfg, "T-2", status="in_progress")
    _task(cfg, "T-3", status="open")          # not in_progress
    _task(cfg, "T-4", status="in_progress", archived=True)  # archived
    assert ord_.pacing_status(cfg, SLUG)["in_progress"] == 2


def test_at_cap_throttles(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 2)
    _dev(cfg, "S-u-dev-p1", "T-1")
    _dev(cfg, "S-u-dev-p2", "T-2")
    st = ord_.pacing_status(cfg, SLUG)
    assert st["live_dev_sessions"] == 2
    assert st["at_cap"] is True
    assert st["recommendation"] == "throttle"


def test_at_cap_ignores_the_board_label(cfg, monkeypatch):
    """T-0966: the label is neither necessary nor sufficient for the throttle.

    Two live devs whose tickets are UNSTAMPED still throttle at cap 2 (the
    measured defect: 7 of 8 live devs had not stamped theirs); two STAMPED
    tickets with no live dev behind them do not."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 2)
    _no_target(cfg)
    _task(cfg, "T-1", status="planned")
    _task(cfg, "T-2", status="planned")
    _dev(cfg, "S-u-dev-p1", "T-1")
    _dev(cfg, "S-u-dev-p2", "T-2")
    st = ord_.pacing_status(cfg, SLUG)
    assert st["in_progress"] == 0 and st["live_dev_sessions"] == 2
    assert st["at_cap"] is True

    _LIVE_AGENTS.clear()  # both devs died; their tickets stay on the board
    _task(cfg, "T-1", status="in_progress")
    _task(cfg, "T-2", status="in_progress")
    st = ord_.pacing_status(cfg, SLUG)
    assert st["in_progress"] == 2 and st["live_dev_sessions"] == 0
    assert st["at_cap"] is False


def test_under_cap_is_ok(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 3)
    _no_target(cfg)
    _dev(cfg, "S-u-dev-p1", "T-1")
    st = ord_.pacing_status(cfg, SLUG)
    assert st["at_cap"] is False
    assert st["recommendation"] == "ok"


def test_target_set_is_advisory(cfg, monkeypatch):
    """A weekly target with no cap pressure -> advisory (pace by judgement, since
    the weekly TOTAL is not queryable)."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_target(cfg, 20)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["weekly_target_pct"] == 20.0
    assert st["recommendation"] == "advisory"


def test_rate_limit_429_throttles_over_advisory(cfg, monkeypatch):
    """429 pressure forces throttle even when only a target is set."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_target(cfg, 20)
    _set_quota(cfg, burn=12345.6, remaining=None, r429=3)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["burn_tokens_per_hr"] == 12345.6
    assert st["rate_limit_429"] == 3
    assert st["recommendation"] == "throttle"


def test_burn_signal_surfaced_when_present(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_quota(cfg, burn=999.0, remaining=500000, r429=0)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["burn_tokens_per_hr"] == 999.0
    assert st["remaining_tokens"] == 500000


def test_burn_signal_reads_telemetrys_real_quota_path(cfg, monkeypatch):
    """T-0646 regression: _burn_signal must read the SAME file telemetry's own
    writer (_update_quota) targets — ``_worker/telemetry/_quota.json`` via the
    shared ``telemetry._quota_path`` helper — not a hand-duplicated path string.
    Writing through the real writer primitive (``_write_json`` at
    ``_quota_path``) and reading back through ``_burn_signal`` catches any
    future divergence between the two, which a bare ``_telemetry/`` path would
    silently fail (FileNotFoundError -> all-None signal)."""
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    q = _telemetry._quota_path(cfg, SLUG)
    assert q == cfg.data_dir / SLUG / "_worker" / "telemetry" / "_quota.json"
    _telemetry._write_json(q, {
        "burn_tokens_per_hr": 42.0, "remaining_tokens": 100,
        "anchor": {"budget_tokens": 1000}, "rate_limit_429": {"count": 0},
    })
    signal = ord_._burn_signal(cfg, SLUG)
    assert signal["burn_tokens_per_hr"] == 42.0
    assert signal["remaining_tokens"] == 100
    assert signal["spend_pct"] == pytest.approx(90.0)


def test_target_unreadable_degrades_to_none(cfg, monkeypatch):
    """No system_settings.toml -> target None, never raises (graceful)."""
    monkeypatch.setattr(ord_, "_system_settings_path", lambda c: None)
    assert ord_.weekly_quota_target_pct(cfg) is None


def test_pace_failure_never_breaks_status(cfg, monkeypatch):
    """If pace.max_in_progress raises, pacing_status still returns a dashboard."""
    def boom(c, s): raise RuntimeError("pace exploded")
    monkeypatch.setattr(_pace, "max_in_progress", boom)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["max_in_progress"] == 0  # degraded
    assert "recommendation" in st


# ---------------------------------------------------------------------------
# F2.7 / T-0579 — mechanized ramp-UP when spend-to-date runs under the weekly
# target's pace line. Today (T-0475) a target with no cap pressure always
# degrades to the same static "advisory" text regardless of under/on/over —
# these tests cover the new pace_verdict + ramp-to-target_in_progress behavior.
# ---------------------------------------------------------------------------

def _no_backoff_clamp(monkeypatch):
    """A wide-open backoff ceiling — the AIMD governor is not currently
    suppressing concurrency, so it never blocks a ramp in these tests."""
    from bot_squad_worker import backoff as _backoff
    monkeypatch.setattr(_backoff, "effective_limit", lambda c: 1_000)


def test_spend_pct_computed_from_anchor_and_remaining(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_quota(cfg, budget=1000, remaining=300)  # 700/1000 spent = 70%
    st = ord_.pacing_status(cfg, SLUG)
    assert st["spend_pct"] == pytest.approx(70.0)


def test_spend_pct_none_without_anchor(cfg, monkeypatch):
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_quota(cfg, remaining=300)  # no budget anchor set
    st = ord_.pacing_status(cfg, SLUG)
    assert st["spend_pct"] is None
    assert st["pace_verdict"] is None


def test_under_pace_ramps_above_baseline(cfg, monkeypatch):
    """spend-to-date (30%) under the 70% target -> RAMP, target_in_progress
    admits above the current in-progress baseline (F2.7 DoD)."""
    _no_backoff_clamp(monkeypatch)
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)  # unlimited board cap
    _set_target(cfg, 70)
    _set_quota(cfg, budget=1000, remaining=700)  # 300/1000 = 30% spent
    _dev(cfg, "S-u-dev-p1", "T-1")
    st = ord_.pacing_status(cfg, SLUG)
    assert st["pace_verdict"] == "under"
    assert st["recommendation"] == "ramp"
    assert st["target_in_progress"] is not None
    assert st["target_in_progress"] > st["live_dev_sessions"]


def test_on_pace_no_ramp(cfg, monkeypatch):
    _no_backoff_clamp(monkeypatch)
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_target(cfg, 70)
    _set_quota(cfg, budget=1000, remaining=300)  # 700/1000 = 70% spent = on target
    st = ord_.pacing_status(cfg, SLUG)
    assert st["pace_verdict"] == "on"
    assert st["recommendation"] == "advisory"
    assert st["target_in_progress"] is None


def test_over_pace_no_ramp(cfg, monkeypatch):
    _no_backoff_clamp(monkeypatch)
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_target(cfg, 20)
    _set_quota(cfg, budget=1000, remaining=300)  # 700/1000 = 70% spent > 20% target
    st = ord_.pacing_status(cfg, SLUG)
    assert st["pace_verdict"] == "over"
    assert st["recommendation"] == "advisory"
    assert st["target_in_progress"] is None


def test_ramp_never_exceeds_max_in_progress_cap(cfg, monkeypatch):
    """Under pace with headroom, but the board cap is already saturated by
    in-progress load -> ramp collapses to advisory (never overrides the cap)."""
    _no_backoff_clamp(monkeypatch)
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 1)
    _set_target(cfg, 70)
    _set_quota(cfg, budget=1000, remaining=700)  # 30% spent, well under target
    _dev(cfg, "S-u-dev-p1", "T-1")  # already at the cap (1)
    st = ord_.pacing_status(cfg, SLUG)
    assert st["pace_verdict"] == "under"
    assert st["at_cap"] is True
    assert st["recommendation"] == "throttle"  # at_cap wins over ramp
    assert st["target_in_progress"] is None


def test_ramp_bounded_by_max_in_progress_when_headroom_exists(cfg, monkeypatch):
    """Cap allows some headroom -> ramp climbs toward it but never past it."""
    _no_backoff_clamp(monkeypatch)
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 3)
    _set_target(cfg, 70)
    _set_quota(cfg, budget=1000, remaining=700)  # 30% spent
    _dev(cfg, "S-u-dev-p1", "T-1")  # 1 live lane, cap=3
    st = ord_.pacing_status(cfg, SLUG)
    assert st["recommendation"] == "ramp"
    assert st["live_dev_sessions"] < st["target_in_progress"] <= 3


def test_backoff_clamped_suppresses_ramp(cfg, monkeypatch):
    """An active AIMD backoff clamp (effective_limit already at/below the current
    load) suppresses the ramp even though spend is under target (F2.7 DoD:
    'backoff-clamped => no ramp')."""
    from bot_squad_worker import backoff as _backoff
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)  # no board cap
    _set_target(cfg, 70)
    _set_quota(cfg, budget=1000, remaining=700)  # 30% spent, well under target
    _dev(cfg, "S-u-dev-p1", "T-1")
    _dev(cfg, "S-u-dev-p2", "T-2")  # 2 live lanes
    monkeypatch.setattr(_backoff, "effective_limit", lambda c: 2)  # clamped at current load
    st = ord_.pacing_status(cfg, SLUG)
    assert st["pace_verdict"] == "under"
    assert st["recommendation"] == "advisory"
    assert st["target_in_progress"] is None


def test_backoff_unreadable_does_not_block_ramp(cfg, monkeypatch):
    """A backoff-signal failure degrades gracefully — it must not crash, and (with
    no other bound) doesn't suppress the ramp either."""
    from bot_squad_worker import backoff as _backoff
    def boom(c):
        raise RuntimeError("backoff exploded")
    monkeypatch.setattr(_backoff, "effective_limit", boom)
    monkeypatch.setattr(_pace, "max_in_progress", lambda c, s: 0)
    _set_target(cfg, 70)
    _set_quota(cfg, budget=1000, remaining=700)  # 30% spent
    st = ord_.pacing_status(cfg, SLUG)
    assert st["recommendation"] == "ramp"
    assert st["target_in_progress"] is not None


def test_pace_verdict_helper_thresholds():
    assert ord_.pace_verdict(70, 30) == "under"
    assert ord_.pace_verdict(70, 70) == "on"
    assert ord_.pace_verdict(70, 90) == "over"
    assert ord_.pace_verdict(None, 30) is None
    assert ord_.pace_verdict(70, None) is None
