"""T-0966 — ``pace.max_in_progress`` gates LIVE DEV SESSIONS, not the board label.

The defect, measured on the live install 2026-09-06 13:49Z with the
stakeholder's own ``max_in_progress = 7`` (set from «таргет параллелизма 7»):

    8 live dev sessions, holding 8 tickets between them.
    Exactly 1 of those tickets carried the ``in_progress`` label.

So the cap read **1 of 7** and would have permitted six more spawns while eight
sessions were already running. The cap was not broken — it was measuring a
PROXY, and the proxy is a field a session has to remember to stamp. Nothing
enforced the stamp, so the proxy's offset varied and a cap whose input drifts
reports a believable, wrong number instead of failing.

These tests pin the decision (DoD1): the cap counts live dev sessions, taken
from the process table + tmux roster on every read, because that is the one
quantity in the system no session can silently fail to maintain (DoD2).

``test_unstamped_lanes_refuse_the_next_spawn`` is the DoD3 red-before-fix test:
against the pre-fix code it does not raise at all, because the board label the
cap read was ``0``.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bot_squad_worker import automation as _automation
from bot_squad_worker import operator_redrive as _ord
from bot_squad_worker import pace as _pace
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError

SLUG = "p1"

# SID set backing the patched ``_live_agent_sids`` — a session counts only if
# its SID is here (a pane running a LIVE claude process), so ``pane=False``
# models a dev that died without clearing its binding.
_LIVE_AGENTS: set[str] = set()


@pytest.fixture(autouse=True)
def _no_panes(monkeypatch):
    _LIVE_AGENTS.clear()
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set(_LIVE_AGENTS))


@pytest.fixture
def cfg(tmp_path: Path):
    data = tmp_path / "data"
    (data / SLUG / "sessions").mkdir(parents=True)
    (data / SLUG / "backlog").mkdir(parents=True)
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    # No [caps] cap and no backoff pressure: whatever refuses below is the PACE
    # cap and nothing else. (0 = unlimited on both.)
    (cfgdir / "system_settings.toml").write_text(
        "[caps]\nmax_parallel_sessions = 0\nmax_total_tokens = 0\n", encoding="utf-8")
    return types.SimpleNamespace(
        projects={SLUG: object()}, data_dir=data, config_dir=cfgdir)


def _dev(cfg, sid: str, task_id: str = "~", *, pane: bool = True) -> None:
    """Register a live leaf-dev session holding ``task_id``. ``window="w"`` is
    the leaf-dev marker (an operator/TL window would not consume the cap)."""
    S._write_session_metadata(
        cfg.data_dir / SLUG / "sessions" / f"{sid}.md",
        {"sid": sid, "status": "active", "window": "w",
         "task_id": task_id, "initiative": "~"},
    )
    if pane:
        _LIVE_AGENTS.add(sid)
    else:
        _LIVE_AGENTS.discard(sid)


def _ticket(cfg, tid: str, status: str = "planned") -> None:
    (cfg.data_dir / SLUG / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: x\nstatus: {status}\n---\n\nbody\n", encoding="utf-8")


def _set_cap(cfg, n: int) -> None:
    _pace.set_max_in_progress(cfg, SLUG, n)


# ---------------------------------------------------------------------------
# DoD3 — the cap refuses the N+1th spawn while the tickets are UNSTAMPED
# ---------------------------------------------------------------------------

def test_unstamped_lanes_refuse_the_next_spawn(cfg):
    """RED BEFORE THE FIX. Three live devs hold three tickets, none of which is
    labelled ``in_progress``; at cap 3 the fourth spawn must be refused.

    Pre-fix the cap read the board label (0 of 3) and admitted every spawn."""
    _set_cap(cfg, 3)
    for i in range(3):
        _ticket(cfg, f"T-100{i}", status="planned")   # NOT in_progress
        _dev(cfg, f"S-u-dev-p{i}", f"T-100{i}")

    # The proxy the cap used to read says there is nothing in flight at all.
    assert _ord._in_progress_count(cfg, SLUG) == 0
    # The quantity it reads now sees all three lanes.
    assert S.count_live_dev_sessions(cfg, SLUG) == 3

    with pytest.raises(ActionError) as exc:
        S._enforce_parallel_cap(cfg, SLUG)
    assert "3/3 live dev sessions" in str(exc.value)
    assert "pace.max_in_progress" in str(exc.value)


def test_the_measured_install_shape_is_refused(cfg):
    """The exact numbers from the ticket: 8 live devs, 1 stamped ticket, cap 7."""
    _set_cap(cfg, 7)
    _ticket(cfg, "T-0929", status="in_progress")
    _dev(cfg, "S-u-dev-p0", "T-0929")
    for i in range(1, 7):
        _ticket(cfg, f"T-090{i}", status="planned")
        _dev(cfg, f"S-u-dev-p{i}", f"T-090{i}")
    _ticket(cfg, "T-0907", status="paused")
    _dev(cfg, "S-u-dev-p7", "T-0907")

    assert _ord._in_progress_count(cfg, SLUG) == 1     # what the cap used to read
    assert S.count_live_dev_sessions(cfg, SLUG) == 8   # what it reads now

    with pytest.raises(ActionError) as exc:
        S._enforce_parallel_cap(cfg, SLUG)
    assert "8/7 live dev sessions" in str(exc.value)


def test_room_below_the_cap_still_admits(cfg):
    _set_cap(cfg, 3)
    _dev(cfg, "S-u-dev-p0", "T-1000")
    _dev(cfg, "S-u-dev-p1", "T-1001")
    S._enforce_parallel_cap(cfg, SLUG)  # 2 < 3 → must NOT raise


def test_zero_is_unlimited(cfg):
    """Back-compat: an unset / 0 cap must throttle nothing, exactly as before."""
    _set_cap(cfg, 0)
    for i in range(9):
        _dev(cfg, f"S-u-dev-p{i}", f"T-10{i:02d}")
    S._enforce_parallel_cap(cfg, SLUG)  # no raise


def test_no_slug_skips_the_pace_cap(cfg):
    """``_enforce_parallel_cap(cfg)`` (the pre-T-0966 arity) still honours only
    the global caps — a caller with no project in hand cannot read a
    per-project cap, and must not be refused by one it never consulted."""
    _set_cap(cfg, 1)
    _dev(cfg, "S-u-dev-p0", "T-1000")
    _dev(cfg, "S-u-dev-p1", "T-1001")
    S._enforce_parallel_cap(cfg)  # no raise: global cap is 0 = unlimited


# ---------------------------------------------------------------------------
# DoD2 — the input cannot be silently un-maintained
# ---------------------------------------------------------------------------

def test_the_board_label_alone_cannot_throttle(cfg):
    """The inverse of the headline: eight tickets STAMPED ``in_progress`` with no
    live dev behind any of them must not refuse a spawn.

    A label left behind by a dev that died is not work in flight; gating on it
    would have wedged the board shut, which is the same defect with the sign
    flipped."""
    _set_cap(cfg, 7)
    for i in range(8):
        _ticket(cfg, f"T-11{i:02d}", status="in_progress")
    assert _ord._in_progress_count(cfg, SLUG) == 8
    S._enforce_parallel_cap(cfg, SLUG)  # 0 live lanes → no raise


def test_a_dev_that_died_without_clearing_its_binding_frees_its_lane(cfg):
    """DoD2, the survival clause. The session md still reads ``active`` and still
    names its task — only the claude pane is gone. The count is re-derived from
    the live-pane roster on every call, so the lane is free immediately rather
    than at the next reconcile tick."""
    _set_cap(cfg, 2)
    _ticket(cfg, "T-1200", status="planned")
    _ticket(cfg, "T-1201", status="planned")
    _dev(cfg, "S-u-dev-p0", "T-1200")
    _dev(cfg, "S-u-dev-p1", "T-1201")
    with pytest.raises(ActionError):
        S._enforce_parallel_cap(cfg, SLUG)

    # p1's claude died; its md is untouched (status active, task still bound).
    _dev(cfg, "S-u-dev-p1", "T-1201", pane=False)
    meta = S._read_session_metadata(cfg.data_dir / SLUG / "sessions" / "S-u-dev-p1.md")
    assert meta["status"] == "active" and meta["task_id"] == "T-1201"

    assert S.count_live_dev_sessions(cfg, SLUG) == 1
    S._enforce_parallel_cap(cfg, SLUG)  # lane freed → no raise


def test_the_count_is_scoped_to_the_project(cfg):
    """``pace`` is per-project, so its cap must be too: another board's live devs
    are counted by the GLOBAL cap, never by this project's parallelism target."""
    (cfg.data_dir / "p2" / "sessions").mkdir(parents=True)
    cfg.projects["p2"] = object()
    S._write_session_metadata(
        cfg.data_dir / "p2" / "sessions" / "S-u-dev-other.md",
        {"sid": "S-u-dev-other", "status": "active", "window": "w",
         "task_id": "T-9999", "initiative": "~"},
    )
    _LIVE_AGENTS.add("S-u-dev-other")

    _set_cap(cfg, 2)
    _dev(cfg, "S-u-dev-p0", "T-1000")
    assert S.count_live_dev_sessions(cfg, SLUG) == 1
    assert S.count_live_dev_sessions(cfg) == 2  # both projects, the global count
    S._enforce_parallel_cap(cfg, SLUG)  # 1 < 2 on p1 → no raise


def test_coordinators_do_not_consume_the_parallelism_target(cfg):
    """Same T-0524 rule the global cap follows: the target governs the
    disposable leaf-dev workload, not the always-on coordination layer."""
    _set_cap(cfg, 2)
    for sid, window in (("S-u-op", "p1-operator"), ("S-u-tl", "p1-TL")):
        S._write_session_metadata(
            cfg.data_dir / SLUG / "sessions" / f"{sid}.md",
            {"sid": sid, "status": "active", "window": window,
             "task_id": "~", "initiative": "~"},
        )
        _LIVE_AGENTS.add(sid)
    _dev(cfg, "S-u-dev-p0", "T-1000")
    assert S.count_live_dev_sessions(cfg, SLUG) == 1
    S._enforce_parallel_cap(cfg, SLUG)  # no raise


# ---------------------------------------------------------------------------
# DoD4 — every surface states WHAT the cap counts and its LIVE value
# ---------------------------------------------------------------------------

def _quiet_quota(cfg, monkeypatch):
    """No weekly target and no telemetry, so `recommendation` is decided by the
    cap alone and not by a ramp/advisory verdict."""
    (cfg.config_dir / "system_settings.toml").write_text(
        "[caps]\nmax_parallel_sessions = 0\nmax_total_tokens = 0\n", encoding="utf-8")


def test_pacing_status_names_the_unit_and_the_live_value(cfg, monkeypatch):
    _quiet_quota(cfg, monkeypatch)
    _set_cap(cfg, 7)
    _ticket(cfg, "T-0929", status="in_progress")
    _dev(cfg, "S-u-dev-p0", "T-0929")
    for i in range(1, 8):
        _ticket(cfg, f"T-090{i}", status="planned")
        _dev(cfg, f"S-u-dev-p{i}", f"T-090{i}")

    st = _ord.pacing_status(cfg, SLUG)
    assert st["cap_counts"] == "live_dev_sessions"
    assert st["live_dev_sessions"] == 8
    assert st["max_in_progress"] == 7
    # The board label is still REPORTED — that is how its drift stays visible —
    # but it is no longer what `at_cap` is computed from.
    assert st["in_progress"] == 1
    assert st["at_cap"] is True
    assert st["recommendation"] == "throttle"


def test_pacing_status_under_cap_is_not_throttled(cfg, monkeypatch):
    _quiet_quota(cfg, monkeypatch)
    _set_cap(cfg, 7)
    for i in range(3):
        _ticket(cfg, f"T-090{i}", status="planned")
        _dev(cfg, f"S-u-dev-p{i}", f"T-090{i}")
    st = _ord.pacing_status(cfg, SLUG)
    assert st["live_dev_sessions"] == 3 and st["in_progress"] == 0
    assert st["at_cap"] is False
    assert st["recommendation"] == "ok"


def test_automation_snapshot_publishes_the_unit_and_the_live_value(cfg, monkeypatch):
    """`bsq pace show` / `bsq pace status` / the web card all render this payload,
    so the number and its unit have to travel together on the wire."""
    _quiet_quota(cfg, monkeypatch)
    _set_cap(cfg, 7)
    for i in range(5):
        _dev(cfg, f"S-u-dev-p{i}", f"T-090{i}")
    snap = _automation.snapshot(cfg, SLUG)
    q = snap["quota"]
    assert q["max_in_progress"] == 7
    assert q["cap_counts"] == "live_dev_sessions"
    assert q["live_dev_sessions"] == 5


def test_live_dev_count_degrades_to_zero_not_an_exception(cfg, monkeypatch):
    """Pacing must never break the tick: an unreadable roster yields 0, and the
    dashboard still renders."""
    def _boom(*_a, **_k):
        raise RuntimeError("roster unreadable")
    monkeypatch.setattr(S, "count_live_dev_sessions", _boom)
    _quiet_quota(cfg, monkeypatch)
    _set_cap(cfg, 7)
    st = _ord.pacing_status(cfg, SLUG)
    assert st["live_dev_sessions"] == 0
    assert st["at_cap"] is False


def test_an_unreadable_pace_config_is_not_a_ceiling(cfg, monkeypatch):
    """A pace.json that cannot be read must admit, never refuse: a broken config
    file is not evidence of a cap, and failing closed here would wedge every
    spawn on the project."""
    def _boom(*_a, **_k):
        raise RuntimeError("pace.json unreadable")
    monkeypatch.setattr(_pace, "max_in_progress", _boom)
    for i in range(9):
        _dev(cfg, f"S-u-dev-p{i}", f"T-10{i:02d}")
    S._enforce_parallel_cap(cfg, SLUG)  # no raise


def test_stored_cap_value_is_unchanged_by_this_ticket(cfg):
    """The on-disk key keeps its name and its shape — only what it COUNTS moved.
    A project that already stored `max_in_progress: 7` keeps reading 7."""
    _set_cap(cfg, 7)
    raw = json.loads((cfg.data_dir / SLUG / "_worker" / "pace" / "pace.json").read_text())
    assert raw["max_in_progress"] == 7
    assert _pace.max_in_progress(cfg, SLUG) == 7
