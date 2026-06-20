"""T-0251 (WS-4 S4): auto stall-recovery — dead/exited respawn-or-park slice.

Safe slice first: a dev session whose tmux pane is DEAD while its task still
needs work → auto-respawn (bounded), then PARK + notify once the bound is hit.
A dead pane carries no risk of killing live work (the aggressive idle-wedge
SIGTERM of LIVE sessions is a separate, day-1-tuned slice). Conservative recs
(G1-G5): dev-only, respawn bound 2 then park, ``BOT_SQUAD_RECOVERY`` kill-switch
(default OFF so it can't surprise a live run until the operator enables it).
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from bot_squad_worker import recovery as R

ACTIVE = "in_progress"
DONE = "closed"


def _cfg(tmp_path):
    return types.SimpleNamespace(projects={"p1": object()}, data_dir=tmp_path / "data")


# --- kill-switch -----------------------------------------------------------

def test_recovery_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_RECOVERY", raising=False)
    assert R.recovery_enabled() is False  # default OFF — opt-in for a live run


def test_recovery_enabled_when_set(monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_RECOVERY", "1")
    assert R.recovery_enabled() is True


# --- pure classify ---------------------------------------------------------

def test_classify_non_dev_is_none():
    assert R.classify(role="teamlead", pane_live=False, task_status=ACTIVE,
                      respawn_count=0, bound=2) == "none"


def test_classify_live_pane_is_none():
    # this slice never touches a LIVE pane (wedge-SIGTERM is a separate slice)
    assert R.classify(role="dev", pane_live=True, task_status=ACTIVE,
                      respawn_count=0, bound=2) == "none"


def test_classify_dead_pane_active_task_respawns():
    assert R.classify(role="dev", pane_live=False, task_status=ACTIVE,
                      respawn_count=0, bound=2) == "respawn"
    assert R.classify(role="dev", pane_live=False, task_status="reopened",
                      respawn_count=1, bound=2) == "respawn"


def test_classify_dead_pane_respawn_bound_parks():
    assert R.classify(role="dev", pane_live=False, task_status=ACTIVE,
                      respawn_count=2, bound=2) == "park"


def test_classify_dead_pane_done_task_is_none():
    # deliverable exists → leave to T-0233 stale-archive, don't respawn
    assert R.classify(role="dev", pane_live=False, task_status=DONE,
                      respawn_count=0, bound=2) == "none"
    assert R.classify(role="dev", pane_live=False, task_status="totest",
                      respawn_count=0, bound=2) == "none"


def test_classify_dead_pane_planned_task_is_none():
    # never started → normal dispatch picks it up, nothing in-flight to recover
    assert R.classify(role="dev", pane_live=False, task_status="planned",
                      respawn_count=0, bound=2) == "none"


# --- tick dispatch (injected handlers, no real side effects) ---------------

def test_tick_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_SQUAD_RECOVERY", raising=False)
    called = []
    monkeypatch.setattr(R, "_gather", lambda cfg: [{"sid": "S-d-p1", "slug": "p1",
        "role": "dev", "pane_live": False, "task_id": "T-1", "task_status": ACTIVE}])
    monkeypatch.setattr(R, "_do_respawn", lambda *a, **k: called.append("respawn"))
    out = R.recovery_tick(tmp_path and _cfg(tmp_path))
    assert called == []
    assert out["enabled"] is False


def test_gather_derives_role_from_window_and_reads_status(monkeypatch, tmp_path):
    """Integration: _gather must call _derive_role with the real (window,
    task_id, initiative) signature and read live status — the mock-based tick
    tests don't exercise this path."""
    from bot_squad_worker import recovery as R
    from bot_squad_worker import sessions as S
    cfg = _cfg(tmp_path)
    sess = tmp_path / "data" / "p1" / "sessions"
    sess.mkdir(parents=True)
    backlog = tmp_path / "data" / "p1" / "backlog"
    backlog.mkdir(parents=True)
    # a marker-less window → dev; bound to an in_progress task; dead pane
    S._write_session_metadata(sess / "S-u-feat-p9.md", {
        "sid": "S-u-feat-p9", "status": "active", "window": "feat",
        "task_id": "T-1", "initiative": "~", "pane_id": "%9"})
    (backlog / "T-1-feature.md").write_text(
        "---\nid: T-1\nstatus: in_progress\n---\n# feature\n")
    # pane_live is now resolved via live_pane_map (real panes); force "no live
    # pane" deterministically so this dead-pane case is independent of tmux.
    monkeypatch.setattr("bot_squad_worker.sessions.list_panes", lambda: [])

    rows = R._gather(cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "dev"
    assert row["pane_live"] is False
    assert row["task_status"] == "in_progress"
    assert row["task_id"] == "T-1"


def test_tick_routes_respawn_then_park(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_SQUAD_RECOVERY", "1")
    monkeypatch.setenv("BOT_SQUAD_RESPAWN_MAX", "1")
    cfg = _cfg(tmp_path)
    rows = [{"sid": "S-d-p1", "slug": "p1", "role": "dev", "pane_live": False,
             "task_id": "T-1", "task_status": ACTIVE}]
    monkeypatch.setattr(R, "_gather", lambda c: rows)
    actions = []
    monkeypatch.setattr(R, "_do_respawn", lambda c, row: actions.append(("respawn", row["sid"])))
    monkeypatch.setattr(R, "_do_park", lambda c, row, reason: actions.append(("park", row["sid"])))

    # first tick: respawn_count 0 < bound 1 -> respawn (count becomes 1)
    R.recovery_tick(cfg)
    # second tick: respawn_count 1 >= bound 1 -> park
    R.recovery_tick(cfg)
    assert actions == [("respawn", "S-d-p1"), ("park", "S-d-p1")]
