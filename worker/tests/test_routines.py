"""T-0464 / M1-F1.1 — Routines (the SECOND assignment-interface implementer).

Source (SOURCE-VERBATIM Part A): "Routine -- a rule, spawns sessions doing work
according to instruction. Spawned according to some rule, generally any incoming
event the integrations allow, simplest and very distinct type of condition is
schedule."

A Routine is a declared rule (instruction + a trigger; simplest = a schedule)
that, when its trigger is DUE, spawns exactly ONE session per due tick to do the
work per its instruction. Built on the existing scheduler tick (routine_tick),
not a new daemon. ``sessions.spawn`` is mocked so no real tmux window opens.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot_squad_worker import routines as R
from bot_squad_worker import sessions as S
from bot_squad_worker.assignment import Artifact
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

UTC = timezone.utc


@pytest.fixture
def cfg_slug(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path)
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug).mkdir(parents=True, exist_ok=True)

    spawns: list[dict] = []

    def _fake_spawn(c, s, window, initial_prompt=None, owner=None, **kw):
        sid = f"S-rt-{len(spawns)}"
        spawns.append({"slug": s, "window": window, "prompt": initial_prompt,
                       "owner": owner, "sid": sid})
        return {"ok": True, "sid": sid}

    monkeypatch.setattr(S, "spawn", _fake_spawn)
    return cfg, slug, spawns


# --- Trigger layer (pluggable; schedule first) ----------------------------

def test_make_trigger_schedule_returns_schedule_trigger():
    trig = R.make_trigger("schedule", "* * * * *")
    assert isinstance(trig, R.ScheduleTrigger)
    assert trig.type == "schedule"


def test_make_trigger_unknown_type_raises():
    with pytest.raises(R.RoutineError, match="trigger"):
        R.make_trigger("carrier-pigeon", "* * * * *")


def test_make_trigger_bad_cron_raises():
    with pytest.raises(R.RoutineError):
        R.make_trigger("schedule", "not a cron")


def test_schedule_next_fire_inclusive_returns_now_if_matches():
    trig = R.make_trigger("schedule", "* * * * *")
    now = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
    # inclusive: a fresh declare wants the next fire AT or after `now`.
    assert trig.next_fire(now, inclusive=True) == now


def test_schedule_next_fire_strict_returns_future():
    trig = R.make_trigger("schedule", "* * * * *")
    now = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
    # strict: after firing at `now`, the next fire must be strictly later so the
    # same due-tick can't re-fire.
    nxt = trig.next_fire(now, inclusive=False)
    assert nxt == now + timedelta(minutes=1)


# --- declare + persist + list + load --------------------------------------

def test_declare_persists_routine_in_store(cfg_slug):
    cfg, slug, _ = cfg_slug
    now = datetime(2026, 6, 27, 12, 0, 30, tzinfo=UTC)
    res = R.declare(cfg, slug, instruction="summarize the day", schedule="0 9 * * *",
                    title="Daily digest", provenance="stakeholder:2026-06-27", now=now)
    assert res["id"] == "R-0001"
    p = Path(res["file_path"])
    assert p.exists()
    assert p.parent == cfg.data_dir / slug / "routines"

    from bot_squad_worker import frontmatter as fm
    meta, body = fm.parse(p.read_text())
    assert meta["id"] == "R-0001"
    assert meta["trigger"] == "schedule"
    assert meta["schedule"] == "0 9 * * *"
    assert meta["status"] == "active"
    assert meta["next_run_at"]  # computed
    assert "summarize the day" in body


def test_declare_requires_instruction_and_schedule(cfg_slug):
    cfg, slug, _ = cfg_slug
    with pytest.raises(R.RoutineError):
        R.declare(cfg, slug, instruction="", schedule="0 9 * * *",
                  provenance="stakeholder:2026-06-27")
    with pytest.raises(R.RoutineError):
        R.declare(cfg, slug, instruction="x", schedule="",
                  provenance="stakeholder:2026-06-27")


def test_declare_bad_cron_raises_before_writing(cfg_slug):
    cfg, slug, _ = cfg_slug
    with pytest.raises(R.RoutineError):
        R.declare(cfg, slug, instruction="x", schedule="nonsense",
                  provenance="stakeholder:2026-06-27")
    # nothing persisted
    assert R.list_routines(cfg, slug) == []


def test_list_routines(cfg_slug):
    cfg, slug, _ = cfg_slug
    R.declare(cfg, slug, instruction="a", schedule="* * * * *",
              provenance="stakeholder:2026-06-27")
    R.declare(cfg, slug, instruction="b", schedule="0 9 * * *",
              provenance="stakeholder:2026-06-27")
    listed = R.list_routines(cfg, slug)
    assert sorted(r["id"] for r in listed) == ["R-0001", "R-0002"]


def test_load_routine(cfg_slug):
    cfg, slug, _ = cfg_slug
    R.declare(cfg, slug, instruction="a", schedule="* * * * *",
              provenance="stakeholder:2026-06-27")
    r = R.load(cfg, slug, "R-0001")
    assert r is not None
    assert r.id == "R-0001"
    assert r.instruction.strip() == "a"
    assert R.load(cfg, slug, "R-9999") is None


# --- the tick: due -> spawn exactly one session ---------------------------

def test_tick_not_due_does_not_spawn(cfg_slug):
    cfg, slug, spawns = cfg_slug
    declared_at = datetime(2026, 6, 27, 8, 0, 0, tzinfo=UTC)
    R.declare(cfg, slug, instruction="x", schedule="0 9 * * *",
              provenance="stakeholder:2026-06-27", now=declared_at)
    # next_run is 09:00; tick at 08:30 -> not due.
    res = R.tick(cfg, slug, now=datetime(2026, 6, 27, 8, 30, 0, tzinfo=UTC))
    assert res["spawned"] == 0
    assert spawns == []


def test_tick_due_spawns_exactly_one_session(cfg_slug):
    cfg, slug, spawns = cfg_slug
    declared_at = datetime(2026, 6, 27, 8, 0, 0, tzinfo=UTC)
    R.declare(cfg, slug, instruction="summarize the day", schedule="0 9 * * *",
              provenance="stakeholder:2026-06-27", now=declared_at)
    res = R.tick(cfg, slug, now=datetime(2026, 6, 27, 9, 0, 5, tzinfo=UTC))
    assert res["spawned"] == 1
    assert len(spawns) == 1
    # the session is driven by the routine assignment: its prompt carries the id
    # + the instruction (the assignment manifested into the session).
    assert "R-0001" in spawns[0]["prompt"]
    assert "summarize the day" in spawns[0]["prompt"]


def test_tick_fires_exactly_once_per_due_tick(cfg_slug):
    """DoD core: a due routine spawns exactly ONE session per due tick — a second
    tick at the same instant must NOT re-fire (next_run_at advanced past now)."""
    cfg, slug, spawns = cfg_slug
    declared_at = datetime(2026, 6, 27, 8, 0, 0, tzinfo=UTC)
    R.declare(cfg, slug, instruction="x", schedule="* * * * *",
              provenance="stakeholder:2026-06-27", now=declared_at)
    now = datetime(2026, 6, 27, 9, 0, 0, tzinfo=UTC)
    R.tick(cfg, slug, now=now)
    R.tick(cfg, slug, now=now)  # same instant, immediately again
    assert len(spawns) == 1  # exactly one, not two


def test_tick_advances_next_run_at(cfg_slug):
    cfg, slug, _ = cfg_slug
    declared_at = datetime(2026, 6, 27, 8, 0, 0, tzinfo=UTC)
    R.declare(cfg, slug, instruction="x", schedule="* * * * *",
              provenance="stakeholder:2026-06-27", now=declared_at)
    now = datetime(2026, 6, 27, 9, 0, 0, tzinfo=UTC)
    R.tick(cfg, slug, now=now)
    r = R.load(cfg, slug, "R-0001")
    assert R._parse_iso(r.next_run_at) == now + timedelta(minutes=1)
    assert R._parse_iso(r.last_run_at) == now


def test_tick_paused_routine_is_skipped(cfg_slug):
    cfg, slug, spawns = cfg_slug
    declared_at = datetime(2026, 6, 27, 8, 0, 0, tzinfo=UTC)
    R.declare(cfg, slug, instruction="x", schedule="* * * * *",
              provenance="stakeholder:2026-06-27", now=declared_at)
    # pause it by rewriting status
    r = R.load(cfg, slug, "R-0001")
    R._set_status(cfg, slug, "R-0001", "paused")
    res = R.tick(cfg, slug, now=datetime(2026, 6, 27, 9, 0, 0, tzinfo=UTC))
    assert res["spawned"] == 0
    assert spawns == []


def test_routine_tick_iterates_projects(cfg_slug, monkeypatch):
    cfg, slug, spawns = cfg_slug
    declared_at = datetime(2026, 6, 27, 8, 0, 0, tzinfo=UTC)
    R.declare(cfg, slug, instruction="x", schedule="* * * * *",
              provenance="stakeholder:2026-06-27", now=declared_at)
    # routine_tick uses real now() — force the routine due by backdating next_run.
    R._write_run_times(cfg, slug, "R-0001",
                       last_run_at=None,
                       next_run_at=R._iso(datetime(2020, 1, 1, tzinfo=UTC)))
    R.routine_tick(cfg)
    assert len(spawns) == 1


# --- scheduler wiring -----------------------------------------------------

def test_scheduler_registers_routine_job(cfg_slug):
    cfg, _slug, _ = cfg_slug
    from bot_squad_worker.scheduler import build_scheduler

    # build_scheduler builds but does not start; just assert the job is wired.
    sched = build_scheduler(cfg)
    assert "routines" in {j.id for j in sched.get_jobs()}


# --- assignment write-result path for a routine session -------------------

def test_routine_session_can_write_its_result(cfg_slug):
    cfg, slug, _ = cfg_slug
    R.declare(cfg, slug, instruction="x", schedule="* * * * *",
              provenance="stakeholder:2026-06-27")
    from bot_squad_worker.assignment import for_routine
    art = for_routine(cfg.data_dir, slug, "R-0001").write_result("done", sid="S-x-rt-p1")
    assert isinstance(art, Artifact)
    assert "done" in art.read()
    assert art.path == cfg.data_dir / slug / "artifacts" / "R-0001.md"
