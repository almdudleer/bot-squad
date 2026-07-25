"""Tests for T-0474 operator re-drive cadence (bot_squad_worker.operator_redrive).

Promotes the manual walkthrough in
``data/bot-squad/scenarios/T-0474-...md`` (written + walked first, per T-0158):
the operator is re-driven while there is pending backlog work AND the user has
not paused, and is NOT re-driven when paused or when it stalls out of its
time/quota budget.

``S.spawn`` is mocked (records calls instead of opening tmux) and
``dispatch.live_operator_sids`` is mocked to control whether an operator is
already on. ``_SPAWN_COOLDOWN_SEC`` is zeroed so back-to-back ticks in a test
aren't wall-clock-throttled.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker import operator_redrive as ord_
from bot_squad_worker import sessions as S
from bot_squad_worker import dispatch
from bot_squad_worker.actions import ActionError
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

# The real detector, captured before any per-test fixture stubs it — used by the
# T-0523 end-to-end no-dup test to exercise the live-pane scan for real.
_REAL_LIVE_OPERATOR_SIDS = dispatch.live_operator_sids


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug / "backlog").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / slug / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ord_, "_SPAWN_COOLDOWN_SEC", 0)

    spawns: list[dict] = []

    def _fake_spawn(c, s, window, initial_prompt=None, owner=None, model=None, **kw):
        sid = f"S-op-{len(spawns)}"
        spawns.append({"window": window, "prompt": initial_prompt, "owner": owner,
                       "model": model, "sid": sid})
        return {"ok": True, "sid": sid}

    monkeypatch.setattr(S, "spawn", _fake_spawn)
    # Default: no operator currently live (the re-drive path). Tests override.
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: [])
    return cfg, slug, spawns


def _write_task(cfg, slug, tid, *, status="open", archived=False):
    arch = "\narchived: true" if archived else ""
    (cfg.data_dir / slug / "backlog" / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: x\nstatus: {status}{arch}\n---\n\nbody\n"
    )


# --- DoD core: pending + not paused -> re-driven; paused -> not --------------

def test_pending_not_paused_redrives_operator(cfg_slug):
    """Backlog pending + not paused + no live operator -> spawn the operator
    with its standing 'clear the backlog' task."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    res = ord_.tick(cfg, slug)

    assert res["action"] == "respawned"
    assert len(spawns) == 1
    assert spawns[0]["window"] == "operator"
    assert spawns[0]["prompt"] == dispatch.operator_standing_task()
    assert spawns[0]["owner"] == "operator-redrive"


def test_paused_is_not_redriven(cfg_slug):
    """User-pause stops the re-drive even with pending backlog work."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    assert ord_.is_paused(cfg, slug) is False

    ord_.pause(cfg, slug, by="user", reason="test")
    assert ord_.is_paused(cfg, slug) is True

    res = ord_.tick(cfg, slug)
    assert res["action"] == "paused"
    assert spawns == []


def test_resume_reenables_redrive(cfg_slug):
    """After resume the operator is re-driven again."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    ord_.pause(cfg, slug)
    assert ord_.tick(cfg, slug)["action"] == "paused"

    assert ord_.resume(cfg, slug) is True
    res = ord_.tick(cfg, slug)
    assert res["action"] == "respawned"
    assert len(spawns) == 1


# --- continue vs respawn (T-0472 seam) --------------------------------------

def test_live_operator_continues_no_respawn(cfg_slug, monkeypatch):
    """A live operator means one is already driving -> continue (no second spawn,
    exactly one operator per project)."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: ["S-existing"])

    res = ord_.tick(cfg, slug)
    assert res["action"] == "continue"
    assert res["operator"] == "S-existing"
    assert spawns == []


def test_canonical_operator_no_md_continues_no_dup(cfg_slug, monkeypatch):
    """T-0523 end-to-end: the canonical operator runs in a live
    ``bot-squad-operator`` window with NO session md. The REAL (un-mocked)
    ``live_operator_sids`` must recognize it via the live-pane scan, so the
    re-drive tick CONTINUES instead of spawning a DUPLICATE operator."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    # Restore the real detector (the fixture stubs it to "no operator").
    monkeypatch.setattr(dispatch, "live_operator_sids", _REAL_LIVE_OPERATOR_SIDS)
    # A live canonical operator pane in THIS project's tmux session, no md.
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_proc_children_map", lambda: {})
    monkeypatch.setattr(S, "_pane_has_live_claude", lambda *a, **k: True)
    monkeypatch.setattr(S, "list_panes", lambda: [
        S.PaneInfo(pane_id="%5", window="bot-squad-operator", pid="999",
                   cwd="/tmp", command="claude", session=slug),
    ])

    res = ord_.tick(cfg, slug)
    assert res["action"] == "continue"
    assert res["operator"] == "S-u-bot-squad-operator-p5"
    assert spawns == []  # NO duplicate operator spawned


# --- T-0678: per-session model override carries across a full respawn ------

def test_respawn_carries_forward_last_operator_model(cfg_slug):
    """A full re-drive respawn mints a BRAND-NEW SID (unlike sessions.resume()'s
    in-place carry-forward), so without help a sticky per-session `model`
    override set via `bsq model set` would silently revert to the fleet/role
    default on every re-drive. _respawn_operator must look up the model the
    replaced operator incarnation carried and pass it forward explicitly."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    sessions_dir = cfg.data_dir / slug / "sessions"
    S._write_session_metadata(sessions_dir / "S-old-operator-p1.md", {
        "sid": "S-old-operator-p1", "status": "suspended", "window": "operator",
        "cwd": str(cfg.projects[slug].repo_path), "claude_uuid": "u-1",
        "archived": "true", "model": "claude-fable-5",
        "started_at": "2026-07-25T10:00:00Z",
    })

    res = ord_.tick(cfg, slug)
    assert res["action"] == "respawned"
    assert len(spawns) == 1
    assert spawns[0]["model"] == "claude-fable-5"


def test_respawn_omits_model_when_no_prior_operator_had_one(cfg_slug):
    """No prior operator md carries a `model` override -> respawn passes none,
    so the freshly spawned operator falls through to the role/fleet default
    exactly as before T-0678."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    res = ord_.tick(cfg, slug)
    assert res["action"] == "respawned"
    assert spawns[0]["model"] is None


# --- empty backlog is the only idle state -----------------------------------

def test_empty_backlog_is_idle(cfg_slug):
    """Only closed / archived tasks -> nothing actionable -> no re-drive."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="closed")
    _write_task(cfg, slug, "T-2", status="open", archived=True)

    res = ord_.tick(cfg, slug)
    assert res["action"] == "idle-empty-backlog"
    assert res["pending"] == 0
    assert spawns == []


def test_count_pending_backlog_counts_only_actionable(cfg_slug):
    cfg, slug, _ = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    _write_task(cfg, slug, "T-2", status="in_progress")
    _write_task(cfg, slug, "T-3", status="totest")
    _write_task(cfg, slug, "T-4", status="closed")        # terminal -> not counted
    _write_task(cfg, slug, "T-5", status="open", archived=True)  # off-board -> not counted
    assert ord_.count_pending_backlog(cfg, slug) == 3


# --- stops when it stalls out of time (capacity / quota backpressure) --------

def test_capacity_backpressure_defers_redrive(cfg_slug, monkeypatch):
    """When spawn admission is at capacity (out of slots/quota = 'stalls out of
    time') the tick defers quietly — no spawn, no crash — instead of respawning
    into a wall (NOT a never-recycled process)."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    def _cap_spawn(c, s, window, **kw):
        raise ActionError("capacity reached: 15/15 parallel sessions")

    monkeypatch.setattr(S, "spawn", _cap_spawn)
    res = ord_.tick(cfg, slug)
    assert res["action"] == "deferred"
    assert spawns == []


# --- cooldown + kill switch + sweep safety ----------------------------------

def test_cooldown_prevents_stampede(cfg_slug, monkeypatch):
    """Two ticks in a row within the cooldown window spawn only once."""
    cfg, slug, spawns = cfg_slug
    monkeypatch.setattr(ord_, "_SPAWN_COOLDOWN_SEC", 9999)
    _write_task(cfg, slug, "T-1", status="open")

    assert ord_.tick(cfg, slug)["action"] == "respawned"
    # operator now "exited" again (live still empty) -> second tick is cooled down
    res = ord_.tick(cfg, slug)
    assert res["action"] == "cooldown"
    assert len(spawns) == 1


def test_kill_switch_disables(cfg_slug, monkeypatch):
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    monkeypatch.setenv("BOT_SQUAD_OPERATOR_REDRIVE", "0")
    res = ord_.tick(cfg, slug)
    assert res["action"] == "disabled"
    assert spawns == []


def test_operator_tick_sweeps_all_projects_and_swallows_errors(cfg_slug, monkeypatch):
    """The scheduler entry point never raises even if a project tick blows up."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    def _boom(c, s):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ord_, "tick", _boom)
    # Must not raise.
    ord_.operator_tick(cfg)
