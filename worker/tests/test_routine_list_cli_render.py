"""T-0899: what ``bsq routine list`` actually PRINTS for a monitor row.

Before this fix, ``routines.py`` tracked ``consecutive_errors`` in the sidecar
state but no surface rendered it, and the error branch of ``MonitorTrigger.poll``
returned before updating ``last_value`` — so a routine blind on every probe
looked identical to a healthy one: ``last=`` showed the last GOOD reading with
no marker, and the error count was invisible outside the raw state json.

This pins the CLI RENDERING (not just the underlying dict — see
``test_list_routines_surfaces_error_state_T0899`` in ``test_monitors.py`` for
that leg): a healthy monitor prints unmarked (the pass leg, T-0899 DoD 4), a
monitor mid error-streak gets an error marker with a staleness timestamp, and
one past ``MONITOR_ERROR_BOUND`` reads BROKEN and keeps reading BROKEN — no
plausible input renders it as indistinguishable from healthy again (the fail
leg).

Loads ``scripts/cli/bsq`` by source path, same shape as
``test_pace_drive_cli_render.py`` — the render helper is pure (takes a dict,
prints), so no verb is called and nothing hits the live worker socket.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_BSQ = REPO_ROOT / "scripts" / "cli" / "bsq"

pytestmark = pytest.mark.skipif(
    not CLI_BSQ.is_file(),
    reason=f"scripts/cli/bsq not present under {REPO_ROOT} — needs a full checkout",
)


@pytest.fixture(scope="module")
def bsq():
    spec = importlib.util.spec_from_loader(
        "bsq_cli_render_under_test_routines",
        importlib.machinery.SourceFileLoader("bsq_cli_render_under_test_routines",
                                              str(CLI_BSQ)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(bsq, r: dict, capsys) -> str:
    bsq._routine_print_row(r)
    return capsys.readouterr().out


def _base(**monitor_overrides) -> dict:
    return {
        "id": "R-0012",
        "status": "active",
        "title": "grouping deciding",
        "monitor": {
            "probe": "shell",
            "interval_s": 30,
            "judge": "regex_match",
            "threshold": "^DEGRADED",
            "last_value": "OK",
            "last_value_at": "2026-08-31T10:00:00+00:00",
            "last_probe_at": "2026-08-31T10:00:00+00:00",
            "consecutive_errors": 0,
            # T-0984: `broken` is now a RATE over the last `window_probes`
            # probes, not a consecutive run, so a row can no longer come up
            # clean on the one tick in ten that happened to succeed.
            "window_errors": 0,
            "window_probes": 0,
            "window_size": 20,
            "broken": False,
            "breach": False,
            "last_fired_at": None,
            **monitor_overrides,
        },
    }


# --- Pass leg: a healthy routine is not marked -----------------------------

def test_healthy_routine_renders_unmarked(bsq, capsys):
    out = _row(bsq, _base(), capsys)
    assert "last=OK" in out
    assert "error" not in out
    assert "BROKEN" not in out
    assert "stale" not in out


# --- Fail leg: an error streak becomes visible ------------------------------

def test_mid_error_streak_shows_count_and_staleness(bsq, capsys):
    out = _row(bsq, _base(consecutive_errors=4, broken=False,
                          last_value="OK", last_value_at="2026-08-31T09:00:00+00:00"),
               capsys)
    assert "error×4" in out
    assert "2026-08-31T09:00:00+00:00" in out
    # the stale reading is still shown, not hidden or replaced
    assert "last=OK" in out
    assert "BROKEN" not in out


def test_broken_past_bound_reads_broken_and_never_unmarked(bsq, capsys):
    out = _row(bsq, _base(consecutive_errors=10, broken=True,
                          window_errors=10, window_probes=20,
                          last_value="OK", last_value_at="2026-08-31T09:00:00+00:00"),
               capsys)
    assert "BROKEN" in out
    # T-0984 changed this wording with the mechanism it describes: the bound is
    # counted over a WINDOW now, so "10 consecutive probe errors" would be a
    # sentence the code can no longer justify. The run is still shown, second.
    assert "10 of the last 20 probes failed" in out
    assert "10 in a row" in out
    assert "2026-08-31T09:00:00+00:00" in out
    # a healthy-looking row for the same last_value is impossible to confuse
    # with test_healthy_routine_renders_unmarked's output
    healthy = _row(bsq, _base(), capsys)
    assert out != healthy


def test_broken_stays_broken_regardless_of_error_count_growth(bsq, capsys):
    """The list marker must not fade just because the count moved past the
    bound. T-0984 changed only the WORDING here, with the mechanism: the count
    is over a window now, and the consecutive run is reported second rather
    than as the whole story. The claim this test exists for — a routine deep
    into a streak still reads BROKEN and still shows its numbers — is
    unchanged."""
    out = _row(bsq, _base(consecutive_errors=47, broken=True,
                          window_errors=20, window_probes=20), capsys)
    assert "BROKEN" in out
    assert "20 of the last 20 probes failed" in out
    assert "47 in a row" in out


def test_no_observation_yet_still_renders_pass_leg(bsq, capsys):
    """Pre-first-probe state (T-0604 contract, unaffected by T-0899): no
    error marker, no crash on missing last_value_at."""
    out = _row(bsq, _base(last_value=None, last_value_at=None,
                          last_probe_at=None), capsys)
    assert "last=—" in out
    assert "error" not in out
    assert "BROKEN" not in out


# --- T-1023: a project-wide drive-pause must not render like a healthy or a
# broken routine — its own marker, carrying the live paused state ----------

def test_default_call_renders_no_stand_down_marker(bsq, capsys):
    """Backward compat: every pre-T-1023 call site (and every test above)
    calls ``_routine_print_row(r)`` with no second argument. The default must
    keep rendering exactly as before — no marker invented from thin air."""
    out = _row(bsq, _base(), capsys)
    assert "STOOD DOWN" not in out


def test_healthy_monitor_under_paused_drive_gets_stood_down_not_silently_ok(bsq, capsys):
    """The defect itself: errors=0 + a paused drive used to render EXACTLY
    like a healthy, actively-probed monitor. The live paused flag (never
    inferred from staleness) must now say so, carrying last_probe_at — the
    freshness fact the pass leg alone doesn't surface."""
    r = _base(last_probe_at="2026-08-31T09:00:00+00:00")
    bsq._routine_print_row(r, automation_paused=True)
    out = capsys.readouterr().out
    assert "STOOD DOWN" in out
    assert "drive paused" in out
    assert "2026-08-31T09:00:00+00:00" in out
    assert "BROKEN" not in out


def test_broken_monitor_under_paused_drive_shows_both_facts_not_merged(bsq, capsys):
    """BROKEN (a fact about past probes) and STOOD DOWN (a fact about right
    now) are independent — the ticket's own point is that a deliberate
    stand-down and a dead mechanism must NOT render the same, so when both
    are true, both words must appear rather than one swallowing the other."""
    r = _base(consecutive_errors=10, broken=True,
              last_value_at="2026-08-31T09:00:00+00:00",
              last_probe_at="2026-08-31T09:00:00+00:00")
    bsq._routine_print_row(r, automation_paused=True)
    out = capsys.readouterr().out
    assert "BROKEN" in out
    assert "STOOD DOWN" in out


def test_paused_status_routine_is_not_double_marked(bsq, capsys):
    """A routine already ``[paused]`` (its OWN status, a different and already
    visible fact) is not additionally flagged STOOD DOWN just because the
    project drive also happens to be paused — that would conflate two
    distinct pause concepts into one marker."""
    r = _base()
    r["status"] = "paused"
    bsq._routine_print_row(r, automation_paused=True)
    out = capsys.readouterr().out
    assert "STOOD DOWN" not in out
    assert "[paused]" in out


def test_schedule_row_under_paused_drive_gets_stood_down(bsq, capsys):
    """The schedule (non-monitor) branch gets the same treatment, carrying
    last_run_at rather than last_probe_at."""
    r = {"id": "R-0002", "status": "active", "trigger": "schedule",
         "schedule": "0 9 * * *", "next_run_at": "2026-07-06T09:00:00+00:00",
         "last_run_at": "2026-07-05T09:00:00+00:00", "title": "daily digest"}
    bsq._routine_print_row(r, automation_paused=True)
    out = capsys.readouterr().out
    assert "STOOD DOWN" in out
    assert "2026-07-05T09:00:00+00:00" in out


# --- T-0984: the row must not come up clean on the tick that worked ---------

def test_blind_routine_is_marked_on_the_tick_that_SUCCEEDED_T0984(bsq, capsys):
    """The reader half of T-0984's F2. A routine blind 9 ticks in 10 rendered a
    FULLY CLEAN row on the tick that worked: `consecutive_errors` had just
    reset to 0 and `last_value_at` was freshly stamped, so a human checking the
    board by hand at that moment was confirmed in the wrong belief. The row now
    carries the window, which does not reset."""
    out = _row(bsq, _base(consecutive_errors=0, broken=True,
                          window_errors=18, window_probes=20,
                          last_value="OK",
                          last_value_at="2026-08-31T10:00:00+00:00"), capsys)
    assert "BROKEN" in out
    assert "18 of the last 20 probes failed" in out
    assert out != _row(bsq, _base(), capsys), (
        "a 90%-blind routine rendered identically to a healthy one")


def test_sub_bound_blindness_shows_the_window_beside_the_streak_T0984(bsq, capsys):
    """Below the bound the row is not BROKEN, but the blindness is still
    visible — `error×1` alone understates a probe that failed 8 of its last 20
    times, and understating it is how it stays unnoticed."""
    out = _row(bsq, _base(consecutive_errors=1, broken=False,
                          window_errors=8, window_probes=20,
                          last_value="OK",
                          last_value_at="2026-08-31T09:00:00+00:00"), capsys)
    assert "error×1" in out
    assert "blind 8/20" in out
    assert "BROKEN" not in out


def test_healthy_row_carries_no_window_noise_T0984(bsq, capsys):
    """The healthy control for the render: a routine with a clean window is
    marked exactly as it was before this ticket — no blindness clause at all."""
    out = _row(bsq, _base(window_errors=0, window_probes=20), capsys)
    assert "blind" not in out
    assert "error" not in out
    assert "BROKEN" not in out
