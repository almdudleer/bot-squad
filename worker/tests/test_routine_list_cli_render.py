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
                          last_value="OK", last_value_at="2026-08-31T09:00:00+00:00"),
               capsys)
    assert "BROKEN" in out
    assert "10 consecutive probe errors" in out
    assert "2026-08-31T09:00:00+00:00" in out
    # a healthy-looking row for the same last_value is impossible to confuse
    # with test_healthy_routine_renders_unmarked's output
    healthy = _row(bsq, _base(), capsys)
    assert out != healthy


def test_broken_stays_broken_regardless_of_error_count_growth(bsq, capsys):
    """The alert fires once at exactly MONITOR_ERROR_BOUND, but the routine
    keeps accumulating errors after that (T-0899 mechanism). The list marker
    must not fade just because the count moved past the bound."""
    out = _row(bsq, _base(consecutive_errors=47, broken=True), capsys)
    assert "BROKEN" in out
    assert "47 consecutive probe errors" in out


def test_no_observation_yet_still_renders_pass_leg(bsq, capsys):
    """Pre-first-probe state (T-0604 contract, unaffected by T-0899): no
    error marker, no crash on missing last_value_at."""
    out = _row(bsq, _base(last_value=None, last_value_at=None,
                          last_probe_at=None), capsys)
    assert "last=—" in out
    assert "error" not in out
    assert "BROKEN" not in out
