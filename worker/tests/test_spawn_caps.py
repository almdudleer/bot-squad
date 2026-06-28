"""T-0239 slice 2: worker spawn-time enforcement of the parallel-sessions cap.

The cap is set via the API (system_settings.toml [caps].max_parallel_sessions);
the worker refuses to spawn once that many sessions are already live. 0 =
unlimited. At/over cap is a capacity-reached refusal (mirrors the T-0237 S4
admission contract — not a silent drop).
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from bot_squad_worker.sessions import (
    _count_live_sessions,
    _enforce_parallel_cap,
    _live_agent_sids,  # real ref — autouse fixture stubs S._live_agent_sids
    _read_caps,
    _write_session_metadata,
)


def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    (tmp_path / "config").mkdir()
    data_dir = tmp_path / "data"
    (data_dir / "p1" / "sessions").mkdir(parents=True)
    return types.SimpleNamespace(
        projects={"p1": object()},
        data_dir=data_dir,
        config_dir=tmp_path / "config",
    )


def _set_caps(cfg, *, max_parallel=0, max_tokens=0) -> None:
    (cfg.config_dir / "system_settings.toml").write_text(
        f"[caps]\nmax_parallel_sessions = {max_parallel}\nmax_total_tokens = {max_tokens}\n"
    )


# SID set backing the patched ``_live_agent_sids`` (T-0397): a session counts
# toward the cap only if its SID is here (a pane with a LIVE claude process), so
# ``_live(..., pane=False)`` models a phantom — either a vanished pane OR a
# dead-claude pane that fell back to a bash shell (both consume no agent slot).
_LIVE_AGENTS: set[str] = set()


def _live(cfg, sid, *, status="active", archived=None, pane=True, window="w") -> None:
    # window drives the derived role (T-0524): "w" → leaf-dev; an operator/TL
    # window marker → an always-on coordinator that does NOT consume the dev cap.
    meta = {"sid": sid, "status": status, "window": window, "task_id": "~",
            "initiative": "~"}
    if archived is not None:
        meta["archived"] = archived
    _write_session_metadata(cfg.data_dir / "p1" / "sessions" / f"{sid}.md", meta)
    if pane:
        _LIVE_AGENTS.add(sid)
    else:
        _LIVE_AGENTS.discard(sid)


@pytest.fixture(autouse=True)
def _no_panes(monkeypatch):
    _LIVE_AGENTS.clear()
    monkeypatch.setattr(S, "list_panes", lambda: [])
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set(_LIVE_AGENTS))


def test_read_caps_defaults_zero_when_missing(tmp_path):
    cfg = _make_cfg(tmp_path)  # no system_settings.toml
    caps = _read_caps(cfg.config_dir)
    # T-0408 added idle_suspend_sec to the caps dict; all default to 0 when the
    # system_settings.toml file is missing.
    assert caps == {
        "max_parallel_sessions": 0,
        "max_total_tokens": 0,
        "idle_suspend_sec": 0,
    }


def test_count_live_excludes_suspended_and_archived(tmp_path):
    cfg = _make_cfg(tmp_path)
    _live(cfg, "S-u-a-p1", status="active")
    _live(cfg, "S-u-b-p2", status="paused")
    _live(cfg, "S-u-c-p3", status="suspended")             # not counted
    _live(cfg, "S-u-d-p4", status="active", archived="true")  # not counted
    assert _count_live_sessions(cfg) == 2


def test_count_live_excludes_coordinators(tmp_path):
    """T-0524 (DoD a): the parallel cap governs the disposable LEAF-DEV workload,
    not the always-on coordination layer. An operator + N team-leads ride the
    universal lifecycle but must NOT consume the dev cap, so only leaf devs count."""
    cfg = _make_cfg(tmp_path)
    _live(cfg, "S-u-op-p1", window="p1-operator")        # coordinator → not counted
    _live(cfg, "S-u-tl-p2", window="p1-TL")              # coordinator → not counted
    _live(cfg, "S-u-tl-p3", window="p1_teamlead")        # coordinator → not counted
    _live(cfg, "S-u-ptl-p4", window="p1-prod-tl")        # coordinator → not counted
    _live(cfg, "S-u-dev-p5", window="feature-x")         # leaf dev → counted
    _live(cfg, "S-u-dev-p6", window="w")                 # leaf dev → counted
    assert _count_live_sessions(cfg) == 2


def test_enforce_admits_coordinator_cluster_with_few_devs(tmp_path):
    """T-0524 (DoD a, the headline false-full): a healthy org (1 operator + 3 TLs
    + 1 dev) all live must NOT false-full at cap=5 — only the single leaf dev
    counts (1 < 5), so the F2.7 ramp can keep spawning devs."""
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=5)
    _live(cfg, "S-u-op-p1", window="p1-operator")
    _live(cfg, "S-u-tl-p2", window="p1-TL")
    _live(cfg, "S-u-tl-p3", window="p1-TL")
    _live(cfg, "S-u-tl-p4", window="p1-TL")
    _live(cfg, "S-u-dev-p5", window="feature-x")
    _enforce_parallel_cap(cfg)  # 1 leaf dev < 5 → must NOT raise


def test_count_live_archived_churn_does_not_false_full(tmp_path):
    """T-0524 (DoD b): under rapid spawn/exit churn the reaper lags, so
    archived/suspended/dead dev mds linger. They must NOT inflate the count at
    enforcement time — only LIVE-holder leaf devs (live pane) count, regardless
    of reaper lag."""
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=3)
    _live(cfg, "S-u-live-p1", window="feature-a")                       # counts
    for i in range(6):  # churn of dead/archived/suspended dev rows the reaper hasn't reaped
        _live(cfg, f"S-u-arch-{i}", window="feature-b", archived="true")
    _live(cfg, "S-u-susp-p9", window="feature-c", status="suspended")   # not counted
    _live(cfg, "S-u-dead-p10", window="feature-d", pane=False)          # dead pane → phantom
    assert _count_live_sessions(cfg) == 1
    _enforce_parallel_cap(cfg)  # 1 live leaf dev < 3 → must NOT raise


def test_hard_ceiling_holds_for_leaf_devs(tmp_path):
    """T-0524 (DoD c): the hard ceiling still bounds the LEAF-DEV load — no
    over-spawn regression. At cap=2 with 2 live devs (plus uncounted
    coordinators), admission still refuses."""
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=2)
    _live(cfg, "S-u-op-p1", window="p1-operator")   # uncounted
    _live(cfg, "S-u-tl-p2", window="p1-TL")         # uncounted
    _live(cfg, "S-u-dev-p3", window="feature-x")    # counts
    _live(cfg, "S-u-dev-p4", window="feature-y")    # counts → 2/2
    with pytest.raises(ActionError, match="capacity reached"):
        _enforce_parallel_cap(cfg)


def test_count_live_drops_phantom_active_without_pane(tmp_path):
    """T-0397: a session marked ``status: active`` whose tmux pane is DEAD (no
    live pane for its SID) is a phantom — it must NOT count toward the parallel
    cap. Trusting md status alone let ~6 dead-pane sessions inflate
    ``live_sessions`` to 15/15 and silently refuse spawns at a false ceiling."""
    cfg = _make_cfg(tmp_path)
    _live(cfg, "S-u-live-p1", status="active")                  # genuine live pane
    _live(cfg, "S-u-paused-p2", status="paused")                # genuine live pane
    _live(cfg, "S-u-phantom-p3", status="active", pane=False)   # dead pane → phantom
    assert _count_live_sessions(cfg) == 2                       # phantom dropped


def test_live_agent_sids_excludes_dead_claude_bash_pane(tmp_path, monkeypatch):
    """T-0397: when claude exits, its tmux pane routinely lingers as a bash
    shell. That dead-claude pane still EXISTS (so ``live_pane_map`` would count
    it) but holds no agent — it must NOT yield a live-agent SID, else it keeps
    consuming a parallel-cap slot. A pane with a live claude process IS
    retained, including one briefly running a Bash *tool* child (claude is still
    alive in its /proc subtree)."""
    panes = [
        S.PaneInfo(pane_id="%1", window="alive", pid="111", cwd="/r", command="claude"),
        S.PaneInfo(pane_id="%2", window="busy", pid="222", cwd="/r", command="bash"),
        S.PaneInfo(pane_id="%3", window="dead", pid="333", cwd="/r", command="bash"),
    ]
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: panes)
    # %1 (claude foreground) and %2 (claude running a bash tool) have a live
    # claude in their subtree; %3 (claude exited → bash) does not.
    # T-0416: _live_agent_sids now passes a prebuilt children-map as a 2nd arg.
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda pid, children=None: pid in {"111", "222"})
    # call the REAL function (the autouse fixture stubs S._live_agent_sids); it
    # resolves list_panes / _pane_has_live_claude from the patched module.
    assert _live_agent_sids() == {"S-u-alive-p1", "S-u-busy-p2"}


def test_live_agent_sids_builds_proc_map_once(tmp_path, monkeypatch):
    """T-0416: the /proc children-map is built ONCE per live-count pass, not
    rebuilt per pane — caps_utilization is FE-polled per project, so a per-pane
    full-/proc rescan was projects × panes × scan. Assert one build for N panes."""
    panes = [
        S.PaneInfo(pane_id="%1", window="a", pid="111", cwd="/r", command="claude"),
        S.PaneInfo(pane_id="%2", window="b", pid="222", cwd="/r", command="claude"),
        S.PaneInfo(pane_id="%3", window="c", pid="333", cwd="/r", command="claude"),
    ]
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "list_panes", lambda: panes)
    builds = {"n": 0}

    def _counting_map():
        builds["n"] += 1
        return {}

    monkeypatch.setattr(S, "_proc_children_map", _counting_map)
    _live_agent_sids()
    assert builds["n"] == 1, f"expected ONE /proc map build for {len(panes)} panes, got {builds['n']}"


def test_enforce_admits_when_phantom_below_cap(tmp_path):
    """The FUNCTIONAL bug: a phantom dead-pane ``active`` md must not consume a
    cap slot. With cap=2, one real-live + one phantom is 1 real < 2 → admit."""
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=2)
    _live(cfg, "S-u-live-p1", status="active")
    _live(cfg, "S-u-phantom-p2", status="active", pane=False)
    _enforce_parallel_cap(cfg)  # 1 real live < 2 → must NOT raise


def test_enforce_refuses_at_cap(tmp_path):
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=1)
    _live(cfg, "S-u-a-p1", status="active")
    with pytest.raises(ActionError, match="capacity reached"):
        _enforce_parallel_cap(cfg)


def test_enforce_allows_under_cap(tmp_path):
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=3)
    _live(cfg, "S-u-a-p1", status="active")
    _enforce_parallel_cap(cfg)  # 1 < 3 -> no raise


def test_enforce_unlimited_when_zero(tmp_path):
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=0)
    for i in range(5):
        _live(cfg, f"S-u-x-p{i}", status="active")
    _enforce_parallel_cap(cfg)  # 0 = unlimited -> no raise


def test_spawn_refused_at_cap_before_side_effects(tmp_path):
    """The cap check fires at the top of spawn(), before any tmux work."""
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=1)
    _live(cfg, "S-u-a-p1", status="active")
    with pytest.raises(ActionError, match="capacity reached"):
        S.spawn(cfg, "p1", "newdev")


# --- T-0335 items 7 + 22: surface the ENFORCED caps utilization -------------

def test_caps_utilization_surfaces_enforced_numbers(tmp_path):
    """The meter measures what spawn actually enforces: live count vs the
    effective limit (item 22) + output_since_anchor vs max_total_tokens (item 7)."""
    from bot_squad_worker.sessions import caps_utilization
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=15, max_tokens=1_000_000)
    _live(cfg, "S-u-a-p1", status="active")
    _live(cfg, "S-u-b-p2", status="paused")
    out = caps_utilization(cfg)
    assert out["max_parallel_sessions"] == 15
    assert out["live_sessions"] == 2
    # backoff disabled/cold → effective_limit == the ceiling
    assert out["effective_limit"] == 15
    assert out["max_total_tokens"] == 1_000_000
    assert out["output_since_anchor"] == 0  # no telemetry samples yet


def test_caps_utilization_unlimited_maps_effective_to_zero(tmp_path):
    """With no parallel cap (0 = unlimited) and no pressure, effective_limit
    reports 0 (unlimited) rather than the internal 10_000 sentinel."""
    from bot_squad_worker.sessions import caps_utilization
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=0, max_tokens=0)
    out = caps_utilization(cfg)
    assert out["max_parallel_sessions"] == 0
    assert out["effective_limit"] == 0  # sentinel 10_000 → 0=unlimited on the wire


def test_caps_utilization_reflects_backoff_throttle(tmp_path, monkeypatch):
    """A depressed AIMD limit surfaces as effective_limit < ceiling (the
    "12/15, throttled to 8" strip)."""
    from bot_squad_worker import sessions as _S, backoff as _backoff
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=15, max_tokens=0)
    monkeypatch.setattr(_backoff, "effective_limit", lambda c: 8)
    out = _S.caps_utilization(cfg)
    assert out["effective_limit"] == 8
    assert out["max_parallel_sessions"] == 15


# ── T-0448 (#5): caps_utilization passes the backoff explainer through ────────

def test_caps_utilization_surfaces_backoff_reason_and_timing(tmp_path, monkeypatch):
    """Under pressure, the meter carries the ALREADY-persisted backoff
    reason + pressure/ramp timestamps so the 'throttled to N' badge can
    explain WHY (pure passthrough — no new computation)."""
    from bot_squad_worker import sessions as _S, backoff as _backoff
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=15, max_tokens=0)
    _backoff.save_state(cfg, {
        "effective_limit": 8,
        "last_pressure_at": 1_700_000_000.0,
        "last_ramp_at": 1_700_000_050.0,
        "reason": "pressure: 2 session(s) limited -> decrease to 8",
    })
    out = _S.caps_utilization(cfg)
    assert out["backoff_reason"] == "pressure: 2 session(s) limited -> decrease to 8"
    assert out["backoff_last_pressure_at"] == 1_700_000_000.0
    assert out["backoff_last_ramp_at"] == 1_700_000_050.0


def test_caps_utilization_backoff_fields_graceful_on_coldstart(tmp_path):
    """No backoff state yet (cold start) → the explainer fields are present
    and None, never a KeyError."""
    from bot_squad_worker.sessions import caps_utilization
    cfg = _make_cfg(tmp_path)
    _set_caps(cfg, max_parallel=15, max_tokens=0)
    out = caps_utilization(cfg)
    assert out["backoff_reason"] is None
    assert out["backoff_last_pressure_at"] is None
    assert out["backoff_last_ramp_at"] is None
