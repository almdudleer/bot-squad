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
from bot_squad_worker import pickup
from bot_squad_worker import sessions as S
from bot_squad_worker import dispatch
from bot_squad_worker.actions import SpawnBackpressure
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
    # T-0783a: the prompt is the standing directive PLUS the concrete pickup
    # queue. Asserting the prefix (rather than equality against a call with no
    # brief) keeps the SSOT pinned while letting the queue block ride along.
    assert spawns[0]["prompt"].startswith(dispatch.operator_standing_task())
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


# --- T-0929: pause is the "stop everything automatic" lever — it must also
# end a currently-running autopilot, not just gate future re-drive respawns.

def test_pause_also_stops_a_running_autopilot(cfg_slug):
    """A live autopilot (T-0153) is a separate mechanism from re-drive with its
    own stop condition — before this fix, pausing operator_redrive left it
    running untouched, matching the stakeholder's report of sessions still
    working after an explicit stop."""
    from bot_squad_worker import autopilot as AP

    cfg, slug, spawns = cfg_slug
    state = AP.AutopilotState(
        slug=slug, key="session-S-x", kind="session", ref="S-x",
        target_sid="S-x", prompt="keep driving",
    )
    AP.save_state(cfg, state)
    assert AP.list_states(cfg, slug)[0].status == "running"

    ord_.pause(cfg, slug, by="user", reason="stop and work with me")

    reloaded = AP.list_states(cfg, slug)[0]
    assert reloaded.status == "stopped"
    assert reloaded.enabled is False


def test_pause_with_no_autopilots_is_unaffected(cfg_slug):
    """No autopilot state on disk at all -> pause still succeeds (no crash on
    an absent _worker/autopilot/ dir)."""
    cfg, slug, spawns = cfg_slug
    meta = ord_.pause(cfg, slug, by="user")
    assert ord_.is_paused(cfg, slug) is True
    assert meta["paused_by"] == "user"


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

    # T-1007: this one stayed GREEN through the type change, because its
    # assertion sits outside the branch that changed — which makes a stale
    # stimulus here more dangerous than a red one, not less. Typed so the test
    # exercises the deferral branch it names.
    def _cap_spawn(c, s, window, **kw):
        raise SpawnBackpressure("capacity reached: 15/15 parallel sessions")

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


# --- T-0783a: the respawn brief carries the concrete pickup queue ------------
#
# The whole point of the ticket: a re-driven operator that has to re-derive which
# tickets are takeable is the operator that left a reopened P1 sitting until the
# stakeholder chased it by hand. So the queue has to be IN the prompt, and the
# empty case has to be stated in it rather than silently absent.

def test_respawn_prompt_names_the_takeable_ticket(cfg_slug, monkeypatch):
    cfg, slug, spawns = cfg_slug
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    (cfg.data_dir / slug / "backlog" / "T-0719-x.md").write_text(
        "---\nid: T-0719\ntitle: REGRESSION reply-by-sid routing\nstatus: reopened\n"
        "priority: P1\nupdated: 2099-01-01T00:00:00Z\n---\n\nbody\n"
    )

    assert ord_.tick(cfg, slug)["action"] == "respawned"
    prompt = spawns[0]["prompt"]
    assert "PICKUP QUEUE" in prompt
    assert "T-0719" in prompt
    assert "[reopened]" in prompt


# --- T-0829: and it names the drive SCOPE the queue was narrowed to ----------
#
# «нужно более чёткое понимание для меня, какой режим драйва щас стоит, я просил
# закончить всё что в опен, но видимо это не интерпретировалось как переключить
# режим драйва» — 2026-07-30T07:54:16Z. Half of why he could not tell whether his
# instruction landed is that no operator incarnation ever STATED the mode it was
# driving under; it inferred one from the tickets it was handed.

def test_respawn_prompt_states_the_configured_drive_scope(cfg_slug, monkeypatch):
    """The configured scope reaches the operator prompt AND narrows the queue in
    it — the two halves have to agree, or the brief describes a mode the list
    below it does not obey."""
    from bot_squad_worker import pace

    cfg, slug, spawns = cfg_slug
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    pace.set_drive(cfg, slug, scope="open_reopened", set_by="stakeholder",
                   source_text="закончить всё что в опен")
    _write_task(cfg, slug, "T-1", status="open")
    _write_task(cfg, slug, "T-2", status="planned")

    assert ord_.tick(cfg, slug)["action"] == "respawned"
    prompt = spawns[0]["prompt"]
    assert "DRIVE SCOPE: open_reopened" in prompt
    assert "закончить всё что в опен" in prompt
    assert "T-1" in prompt
    assert "T-2" not in prompt  # planned is out of this scope


def test_respawn_prompt_states_the_scope_even_when_none_is_set(cfg_slug, monkeypatch):
    """The unset case is the one he actually hit. An operator told nothing reads
    the absence as "no mode", which is indistinguishable from "the widest mode"
    — so the default is stated, and stated as the widest."""
    cfg, slug, spawns = cfg_slug
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    _write_task(cfg, slug, "T-1", status="open")

    assert ord_.tick(cfg, slug)["action"] == "respawned"
    prompt = spawns[0]["prompt"]
    assert "DRIVE SCOPE: all" in prompt
    assert "never set" in prompt and "WIDEST" in prompt
    assert "T-1" in prompt


def test_respawn_prompt_states_an_empty_queue_out_loud(cfg_slug, monkeypatch):
    """Pending backlog exists (so the tick fires) but nothing is TAKEABLE — a
    totest ticket is review work. The brief must say the queue is empty instead
    of omitting the section, which a reader takes as "not computed"."""
    cfg, slug, spawns = cfg_slug
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    _write_task(cfg, slug, "T-1", status="totest")

    assert ord_.tick(cfg, slug)["action"] == "respawned"
    prompt = spawns[0]["prompt"]
    assert pickup.EMPTY_PICKUP_LINE in prompt
    assert "T-1" not in prompt


def test_a_broken_pickup_computation_never_blocks_the_respawn(cfg_slug, monkeypatch):
    """Degrade to the plain directive: an operator with the old brief is what we
    had before this wiring, and is strictly better than no operator at all."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pickup, "pickup_queue", _boom)

    assert ord_.tick(cfg, slug)["action"] == "respawned"
    assert spawns[0]["prompt"] == dispatch.operator_standing_task()


def test_operator_tick_sweeps_all_projects_and_swallows_errors(cfg_slug, monkeypatch):
    """The scheduler entry point never raises even if a project tick blows up."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    def _boom(c, s):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ord_, "tick", _boom)
    # Must not raise.
    ord_.operator_tick(cfg)


# ---------------------------------------------------------------------------
# T-0855 — the scaling-ladder gate. THIS is the mechanism that made the
# operator hop unconditional: pending work + no live operator used to mean
# "spawn one", so a project with one open task and a live user-conversation
# session got an operator back inside 60s no matter what any role contract
# said. «когда поток задач маленький, не устраивать цепочку из юзер-сессия ->
# оператор -> дев-сессия» (stakeholder, 2026-08-11).
# ---------------------------------------------------------------------------

def _write_session(cfg, slug, sid, window, *, status="active", task_id="~"):
    S._write_session_metadata(
        cfg.data_dir / slug / "sessions" / f"{sid}.md",
        {"sid": sid, "status": status, "window": window, "cwd": "/tmp",
         "claude_uuid": "uuid-" + sid[-3:], "task_id": task_id, "initiative": "~",
         "started_at": "2026-08-11T00:00:00Z"},
    )


@pytest.fixture(autouse=True)
def _no_stray_panes(monkeypatch):
    """The topology read scans live panes for an unregistered operator — keep
    it hermetic, like test_dispatch's fixture does."""
    monkeypatch.setattr(S, "list_panes", lambda: [])


def test_small_flow_with_a_live_user_session_gets_no_operator(cfg_slug):
    """The ticket's core claim, at the layer that decides it: one open task, a
    live user session to drive it, no operator spawned."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    _write_session(cfg, slug, "S-u-gu_x-user-conversation-p9",
                   "gu_x-user-conversation")

    res = ord_.tick(cfg, slug)

    assert res["action"] == "direct-tier"
    assert spawns == [], "an operator was spawned for a one-task flow"
    assert res["user_sessions"] == ["S-u-gu_x-user-conversation-p9"]


def test_unattended_project_still_gets_its_operator(cfg_slug):
    """No user session = no direct driver. The backlog must never be left with
    no dispatcher at all — this is the pre-T-0855 behaviour, preserved."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")

    res = ord_.tick(cfg, slug)

    assert res["action"] == "respawned"
    assert len(spawns) == 1


def test_real_parallel_load_promotes_the_operator(cfg_slug):
    """The 20% case, in his words — «когда я прямо сижу и в потоке работаю над
    кучей задач сразу». An attending user session does NOT suppress the tier
    when the load is what the tier is for. Load is LIVE DEVS holding tasks, not
    the board's in_progress labels — see the ★ note in dispatch.py."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="in_progress")
    _write_session(cfg, slug, "S-u-gu_x-user-conversation-p9",
                   "gu_x-user-conversation")
    for n in (1, 2, 3):
        _write_session(cfg, slug, f"S-u-d{n}-dev-p{n}", f"d{n}-dev",
                       task_id=f"T-{n}")

    res = ord_.tick(cfg, slug)

    assert res["action"] == "respawned"
    assert len(spawns) == 1


def test_a_broken_topology_gate_never_strands_the_backlog(cfg_slug, monkeypatch):
    """Fail-safe direction: any error in the new gate falls through to the
    respawn, i.e. to exactly what the tick did before this ticket."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    _write_session(cfg, slug, "S-u-gu_x-user-conversation-p9",
                   "gu_x-user-conversation")

    def _boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(dispatch, "decide_topology", _boom)
    res = ord_.tick(cfg, slug)

    assert res["action"] == "respawned"
    assert len(spawns) == 1


def test_kill_switch_restores_the_unconditional_operator_hop(cfg_slug, monkeypatch):
    """One env var is the whole rollback."""
    cfg, slug, spawns = cfg_slug
    monkeypatch.setenv("BOT_SQUAD_DIRECT_DISPATCH", "0")
    _write_task(cfg, slug, "T-1", status="open")
    _write_session(cfg, slug, "S-u-gu_x-user-conversation-p9",
                   "gu_x-user-conversation")

    res = ord_.tick(cfg, slug)

    assert res["action"] == "respawned"
    assert len(spawns) == 1


def test_a_live_operator_still_just_continues(cfg_slug, monkeypatch):
    """The gate sits BELOW the T-0472 one-operator check: a live operator is
    left alone to finish, and de-escalation happens on its next recycle, not by
    killing it mid-work."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    _write_session(cfg, slug, "S-u-gu_x-user-conversation-p9",
                   "gu_x-user-conversation")
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: ["S-op-live"])

    res = ord_.tick(cfg, slug)

    assert res["action"] == "continue"
    assert spawns == []


def test_the_tier_deescalates_after_that_operator_exits(cfg_slug, monkeypatch):
    """The other half of the same story: the same project, one tick later with
    the operator gone, does NOT bring it back."""
    cfg, slug, spawns = cfg_slug
    _write_task(cfg, slug, "T-1", status="open")
    _write_session(cfg, slug, "S-u-gu_x-user-conversation-p9",
                   "gu_x-user-conversation")
    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: ["S-op-live"])
    assert ord_.tick(cfg, slug)["action"] == "continue"

    monkeypatch.setattr(dispatch, "live_operator_sids", lambda c, s: [])
    assert ord_.tick(cfg, slug)["action"] == "direct-tier"
    assert spawns == []
