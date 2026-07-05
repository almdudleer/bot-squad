"""T-0604 (D-0048 slice 2) — the bsq CLI surface for monitor routines:
`routine declare --trigger monitor` flag composition, `routine list` monitor
columns, `routine mute` wiring + duration parsing.

The worker side (allowlist pass-through, engine mute semantics) is covered in
worker/tests/{test_assignment,test_monitors}.py; these tests pin the CLIENT
contract: which params each verb posts and how validation dies before any
socket call.

`bsq` is extensionless, so it's loaded via SourceFileLoader (mirrors
test_bsq_expert.py).
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_routines", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_routines", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

SLUG = "proj"


@pytest.fixture(autouse=True)
def _stub_slug(monkeypatch):
    monkeypatch.setattr(bsq, "resolve_slug", lambda: SLUG)


def _record_post(monkeypatch, result=None):
    calls = []

    def fake_post(action, params, timeout=35.0, fatal=True):
        calls.append((action, params))
        return result or {"ok": True, "id": "R-0001", "file_path": "/x.md",
                          "next_run_at": None, "muted_until": None}

    monkeypatch.setattr(bsq, "post", fake_post)
    return calls


_DECLARE_DEFAULTS = dict(
    instruction="investigate", trigger="schedule", schedule=None,
    probe=None, cmd=None, url=None, expect_status=None, latency_budget_ms=None,
    interval=None, timeout=None, judge=None, threshold=None,
    persist=None, cooldown=None, on_breach=None, on_recover=None,
    title=None, provenance=None,
)


def _declare_args(**kw):
    base = dict(_DECLARE_DEFAULTS)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# parse_duration_s
# ---------------------------------------------------------------------------
def test_parse_duration_units():
    assert bsq.parse_duration_s("30s") == 30
    assert bsq.parse_duration_s("15m") == 900
    assert bsq.parse_duration_s("2h") == 7200
    assert bsq.parse_duration_s("1d") == 86400
    assert bsq.parse_duration_s("45") == 45  # bare seconds
    assert bsq.parse_duration_s("0") == 0


@pytest.mark.parametrize("bad", ["", "soon", "5w", "-5m", "1.5h", "m5"])
def test_parse_duration_bad_dies(bad):
    with pytest.raises(SystemExit):
        bsq.parse_duration_s(bad)


# ---------------------------------------------------------------------------
# routine declare --trigger monitor
# ---------------------------------------------------------------------------
def test_declare_monitor_posts_composed_spec(monkeypatch, capsys):
    calls = _record_post(monkeypatch)
    bsq.cmd_routine_declare(_declare_args(
        trigger="monitor", probe="shell", cmd="cat /m", interval="30s",
        timeout="20s", judge="numeric_gt", threshold="499", persist="60s",
        cooldown="30m", on_breach="spawn", on_recover="notify",
        provenance="T-0604"))
    action, params = calls[0]
    assert action == "routine_declare"
    assert params["slug"] == SLUG and params["trigger"] == "monitor"
    assert "schedule" not in params
    assert params["monitor"] == {
        "probe": "shell", "cmd": "cat /m", "interval_s": 30, "timeout_s": 20,
        "judge": "numeric_gt", "threshold": 499, "persist_s": 60,
        "cooldown_s": 1800, "on_breach": "spawn", "on_recover": "notify",
    }
    assert "R-0001" in capsys.readouterr().out


def test_declare_monitor_omits_unset_flags_for_engine_defaults(monkeypatch):
    """Only flags the user set enter the spec — engine defaults own the rest."""
    calls = _record_post(monkeypatch)
    bsq.cmd_routine_declare(_declare_args(
        trigger="monitor", cmd="cat /m", timeout="20s",
        judge="nonzero_exit"))
    assert calls[0][1]["monitor"] == {
        "cmd": "cat /m", "timeout_s": 20, "judge": "nonzero_exit"}


def test_declare_monitor_threshold_coercion(monkeypatch):
    calls = _record_post(monkeypatch)
    bsq.cmd_routine_declare(_declare_args(
        trigger="monitor", cmd="c", timeout="3s", judge="numeric_lt",
        threshold="0.5"))
    assert calls[0][1]["monitor"]["threshold"] == 0.5
    bsq.cmd_routine_declare(_declare_args(
        trigger="monitor", cmd="c", timeout="3s", judge="regex_match",
        threshold="ERROR|FATAL"))
    assert calls[1][1]["monitor"]["threshold"] == "ERROR|FATAL"


def test_declare_monitor_rejects_schedule(monkeypatch):
    calls = _record_post(monkeypatch)
    with pytest.raises(SystemExit):
        bsq.cmd_routine_declare(_declare_args(
            trigger="monitor", cmd="c", timeout="3s", judge="nonzero_exit",
            schedule="* * * * *"))
    assert calls == []  # died before any socket call


def test_declare_schedule_requires_schedule(monkeypatch):
    calls = _record_post(monkeypatch)
    with pytest.raises(SystemExit):
        bsq.cmd_routine_declare(_declare_args())
    assert calls == []


def test_declare_schedule_rejects_monitor_flags(monkeypatch):
    calls = _record_post(monkeypatch)
    with pytest.raises(SystemExit):
        bsq.cmd_routine_declare(_declare_args(schedule="0 9 * * *", cmd="c"))
    assert calls == []


def test_declare_schedule_unchanged_contract(monkeypatch):
    """Pre-T-0604 schedule declares post the same params as before."""
    calls = _record_post(monkeypatch)
    bsq.cmd_routine_declare(_declare_args(schedule="0 9 * * *", title="daily"))
    action, params = calls[0]
    assert action == "routine_declare"
    assert params == {"slug": SLUG, "instruction": "investigate",
                      "trigger": "schedule", "schedule": "0 9 * * *",
                      "title": "daily"}


# ---------------------------------------------------------------------------
# routine list — monitor columns
# ---------------------------------------------------------------------------
def test_list_renders_monitor_columns_and_mute(monkeypatch, capsys):
    _record_post(monkeypatch, result={"ok": True, "routines": [
        {"id": "R-0001", "status": "active", "trigger": "monitor",
         "title": "disk watch", "muted_until": "2026-07-05T20:00:00+00:00",
         "mute_reason": "known flap",
         "monitor": {"probe": "shell", "interval_s": 30, "judge": "numeric_gt",
                     "threshold": 85, "last_value": 91, "breach": True,
                     "last_fired_at": "2026-07-05T19:00:00+00:00"}},
        {"id": "R-0002", "status": "active", "trigger": "schedule",
         "schedule": "0 9 * * *", "next_run_at": "2026-07-06T09:00:00+00:00",
         "title": "daily digest"},
    ]})
    bsq.cmd_routine_list(SimpleNamespace())
    out = capsys.readouterr().out
    mon_line, sched_line = out.strip().splitlines()
    assert "monitor(shell 30s numeric_gt/85)" in mon_line
    assert "last=91" in mon_line and "breach=YES" in mon_line
    assert "last_fire=2026-07-05T19:00:00+00:00" in mon_line
    assert "MUTED until 2026-07-05T20:00:00+00:00 (known flap)" in mon_line
    assert "schedule:0 9 * * *" in sched_line and "MUTED" not in sched_line


def test_list_monitor_absent_state_renders_dashes(monkeypatch, capsys):
    _record_post(monkeypatch, result={"ok": True, "routines": [
        {"id": "R-0001", "status": "active", "trigger": "monitor", "title": "t",
         "monitor": {"probe": "shell", "interval_s": 5, "judge": "nonzero_exit",
                     "threshold": None, "last_value": None, "breach": False,
                     "last_fired_at": None}},
    ]})
    bsq.cmd_routine_list(SimpleNamespace())
    out = capsys.readouterr().out
    assert "last=—" in out and "breach=no" in out and "last_fire=—" in out


# ---------------------------------------------------------------------------
# routine mute
# ---------------------------------------------------------------------------
def test_mute_posts_parsed_duration_and_reason(monkeypatch, capsys):
    calls = _record_post(monkeypatch, result={
        "ok": True, "id": "R-0001",
        "muted_until": "2026-07-05T20:00:00+00:00", "reason": "flap"})
    bsq.cmd_routine_mute(SimpleNamespace(rid="R-0001", duration="45m",
                                         reason="flap"))
    assert calls[0] == ("routine_mute", {
        "slug": SLUG, "rid": "R-0001", "duration_s": 2700, "reason": "flap"})
    assert "muted R-0001 until 2026-07-05T20:00:00+00:00" in capsys.readouterr().out


def test_mute_without_reason_dies_before_post(monkeypatch):
    calls = _record_post(monkeypatch)
    with pytest.raises(SystemExit):
        bsq.cmd_routine_mute(SimpleNamespace(rid="R-0001", duration="45m",
                                             reason=None))
    assert calls == []


def test_unmute_zero_needs_no_reason(monkeypatch, capsys):
    calls = _record_post(monkeypatch, result={
        "ok": True, "id": "R-0001", "muted_until": None})
    bsq.cmd_routine_mute(SimpleNamespace(rid="R-0001", duration="0",
                                         reason=None))
    assert calls[0] == ("routine_mute", {
        "slug": SLUG, "rid": "R-0001", "duration_s": 0})
    assert "unmuted R-0001" in capsys.readouterr().out
