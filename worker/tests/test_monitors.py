"""T-0603 / D-0048 slice 1 — hybrid-trigger engine (monitor routines).

Source (stakeholder voice #2 2026-07-05, SSOT T-0592): "мы мониторим при помощи
какого-то кода, даже каждые пять секунд… какую-то метрику… когда она переходит
какой-то порог, срабатывает уже триггер, который подключает к этому процессу
искусственный интеллект".

A monitor routine = an existing Routine whose trigger is a MONITOR: a code-only
probe (shell subprocess) evaluated by ``monitor_tick`` at a 5s cadence, judged
against a threshold through a persist/cooldown state machine, and — only on a
confirmed breach — attaching AI via the EXISTING ``_spawn_for_routine`` path
with a TRIGGER EVENT section in the brief. Zero tokens while healthy.

``sessions.spawn`` and ``sessions.list_sessions`` are mocked (no tmux); probes
are REAL shell subprocesses against tmp files.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot_squad_worker import input_mux
from bot_squad_worker import routines as R
from bot_squad_worker import sessions as S
from bot_squad_worker.actions import ActionError
from tests.test_jobs import _make_config_with_project, _make_project_with_repo

UTC = timezone.utc
T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _spec(**overrides) -> dict:
    base = {
        "probe": "shell",
        "cmd": "echo 5",
        "interval_s": 5,
        "timeout_s": 3,
        "judge": "numeric_gt",
        "threshold": 10,
        "persist_s": 0,
        "cooldown_s": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def mcfg(tmp_path: Path, monkeypatch):
    project = _make_project_with_repo(tmp_path, slug="mon-test-proj")
    cfg = _make_config_with_project(tmp_path, project)
    slug = project.slug
    (cfg.data_dir / slug).mkdir(parents=True, exist_ok=True)

    spawns: list[dict] = []

    def _fake_spawn(c, s, window, initial_prompt=None, owner=None, **kw):
        sid = f"S-mon-{len(spawns)}"
        spawns.append({"slug": s, "window": window, "prompt": initial_prompt,
                       "owner": owner, "sid": sid})
        return {"ok": True, "sid": sid}

    monkeypatch.setattr(S, "spawn", _fake_spawn)
    # No live sessions by default — owner-dedup finds nothing, spawn proceeds.
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [])
    return cfg, slug, spawns


def _declare_file_monitor(cfg, slug, metric_file: Path, *, threshold=10,
                          persist_s=0, cooldown_s=0, interval_s=5,
                          judge="numeric_gt", now=None) -> str:
    res = R.declare(
        cfg, slug,
        instruction="investigate the metric breach per runbook",
        trigger="monitor",
        monitor=_spec(cmd=f"cat {metric_file}", threshold=threshold,
                      persist_s=persist_s, cooldown_s=cooldown_s,
                      interval_s=interval_s, judge=judge),
        provenance="T-0603", now=now or T0,
    )
    return res["id"]


# --- Trigger widening: make_trigger + uniform poll ------------------------

def test_make_trigger_monitor_returns_monitor_trigger():
    trig = R.make_trigger("monitor", _spec())
    assert isinstance(trig, R.MonitorTrigger)
    assert trig.type == "monitor"


def test_make_trigger_schedule_still_works():
    assert isinstance(R.make_trigger("schedule", "* * * * *"), R.ScheduleTrigger)


def test_monitor_trigger_never_schedules_a_next_fire():
    trig = R.make_trigger("monitor", _spec())
    assert trig.next_fire(T0, inclusive=True) is None


def test_schedule_trigger_poll_fires_when_due():
    trig = R.make_trigger("schedule", "* * * * *")
    ctx = {"next_run_at": R._iso(T0)}
    ev = trig.poll(T0, ctx)
    assert ev is not None and ev.kind == "fire"
    assert trig.poll(T0 - timedelta(seconds=30), ctx) is None


# --- Declare-time validation (RoutineError BEFORE any write) ---------------

@pytest.mark.parametrize("bad", [
    {"cmd": "   "},                       # empty/whitespace cmd
    {"interval_s": 4},                    # below the 5s tick floor
    {"timeout_s": None},                  # timeout is mandatory
    {"timeout_s": 0},
    {"judge": "vibes"},                   # unknown judge
    {"threshold": "not-a-number"},        # numeric judge needs numeric threshold
    {"judge": "regex_match", "threshold": "("},   # invalid regex
    {"judge": "regex_match", "threshold": ""},    # empty regex
    {"probe": "http"},                    # http probe takes url, not cmd (T-0605)
    {"probe": "grafana"},
    {"on_breach": "page-everyone"},       # unknown on_breach mode
    {"on_recover": "vibes"},              # unknown on_recover mode (T-0605)
    {"url": "http://x"},                  # http-probe key on a shell probe
    {"latency_budget_ms": 500},           # http-probe key on a shell probe
    {"persist_s": -1},
    {"cooldown_s": -1},
    {"nonsense_key": 1},                  # unknown keys refused (typo guard)
])
def test_monitor_spec_validation_rejects(bad):
    spec = _spec(**bad)
    if bad.get("timeout_s", "x") is None:
        spec.pop("timeout_s")
    with pytest.raises(R.RoutineError):
        R.MonitorTrigger(spec)


def test_monitor_spec_not_a_dict_raises():
    with pytest.raises(R.RoutineError):
        R.make_trigger("monitor", "cat /tmp/x")


def test_monitor_nonzero_exit_judge_needs_no_threshold():
    spec = _spec(judge="nonzero_exit")
    spec.pop("threshold")
    trig = R.MonitorTrigger(spec)
    assert trig.spec["judge"] == "nonzero_exit"


def test_declare_monitor_bad_spec_raises_before_write(mcfg):
    cfg, slug, _ = mcfg
    with pytest.raises(R.RoutineError):
        R.declare(cfg, slug, instruction="x", trigger="monitor",
                  monitor=_spec(judge="vibes"), provenance="T-0603")
    assert R.list_routines(cfg, slug) == []


def test_declare_monitor_requires_monitor_spec(mcfg):
    cfg, slug, _ = mcfg
    with pytest.raises(R.RoutineError):
        R.declare(cfg, slug, instruction="x", trigger="monitor",
                  provenance="T-0603")


# --- Declare + persist + load ----------------------------------------------

def test_declare_monitor_persists_spec_in_frontmatter(mcfg, tmp_path):
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("5")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=60,
                                cooldown_s=1800, interval_s=30)
    assert rid == "R-0001"

    from bot_squad_worker import frontmatter as fm
    path = Path(R.load(cfg, slug, rid).file_path)
    meta, _body = fm.parse(path.read_text())
    assert meta["trigger"] == "monitor"
    assert meta["next_run_at"] is None  # no time-based fire for monitors
    mon = meta["monitor"]
    assert mon["judge"] == "numeric_gt"
    assert mon["threshold"] == 10
    assert mon["interval_s"] == 30
    assert mon["persist_s"] == 60
    assert mon["cooldown_s"] == 1800

    r = R.load(cfg, slug, rid)
    assert r.trigger_type == "monitor"
    assert isinstance(r.monitor, dict)
    assert isinstance(r.trigger(), R.MonitorTrigger)


def test_schedule_tick_skips_monitor_routines(mcfg, tmp_path):
    """The 60s schedule tick keeps owning ONLY schedule triggers (D-0048 §4)."""
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("99")  # breaching — but the schedule tick must not care
    _declare_file_monitor(cfg, slug, metric)
    res = R.tick(cfg, slug, now=T0 + timedelta(hours=1))
    assert res["spawned"] == 0
    assert spawns == []


# --- Probe evaluation (judge layer) ----------------------------------------

def _pr(ok=True, exit_code=0, output="", error=""):
    return R.ProbeResult(ok=ok, exit_code=exit_code, output=output, error=error)


def test_evaluate_numeric_gt():
    spec = R.MonitorTrigger(_spec(judge="numeric_gt", threshold=10)).spec
    assert R.evaluate_probe(spec, _pr(output="42\n")) == ("breach", 42)
    assert R.evaluate_probe(spec, _pr(output="9")) == ("ok", 9)


def test_evaluate_numeric_lt_and_ne():
    lt = R.MonitorTrigger(_spec(judge="numeric_lt", threshold=10)).spec
    assert R.evaluate_probe(lt, _pr(output="3"))[0] == "breach"
    assert R.evaluate_probe(lt, _pr(output="30"))[0] == "ok"
    ne = R.MonitorTrigger(_spec(judge="numeric_ne", threshold=200)).spec
    assert R.evaluate_probe(ne, _pr(output="502"))[0] == "breach"
    assert R.evaluate_probe(ne, _pr(output="200"))[0] == "ok"


def test_evaluate_nonzero_exit():
    spec = _spec(judge="nonzero_exit")
    spec.pop("threshold")
    spec = R.MonitorTrigger(spec).spec
    assert R.evaluate_probe(spec, _pr(exit_code=1))[0] == "breach"
    assert R.evaluate_probe(spec, _pr(exit_code=0))[0] == "ok"


def test_evaluate_regex_match():
    spec = R.MonitorTrigger(_spec(judge="regex_match", threshold="ERROR|FATAL")).spec
    assert R.evaluate_probe(spec, _pr(output="12:00 FATAL boom"))[0] == "breach"
    assert R.evaluate_probe(spec, _pr(output="all quiet"))[0] == "ok"


def test_evaluate_probe_errors_are_not_breaches():
    spec = R.MonitorTrigger(_spec()).spec
    # probe did not run (timeout / exec error)
    assert R.evaluate_probe(spec, _pr(ok=False, error="timeout"))[0] == "error"
    # nonzero exit under a numeric judge: stdout is untrustworthy
    assert R.evaluate_probe(spec, _pr(exit_code=7, output="000"))[0] == "error"
    # unparseable stdout under a numeric judge
    assert R.evaluate_probe(spec, _pr(output="not a number"))[0] == "error"


def test_evaluate_probe_inf_nan_are_errors_not_crashes():
    """T-0603 review P3: inf/nan parse as floats, then int(value) raises
    OverflowError/ValueError OUTSIDE the non-numeric guard — must be the
    error verdict, never an escaping exception."""
    spec = R.MonitorTrigger(_spec()).spec
    for weird in ("inf", "-inf", "nan", "Infinity", "NaN", "+inf\n"):
        verdict, value = R.evaluate_probe(spec, _pr(output=weird))
        assert verdict == "error", weird
        assert "non-finite" in value


# --- Judge state machine (poll) ---------------------------------------------

def _poll(trig, now, st, **pr_kw):
    return trig.poll(now, {"state": st, "probe": _pr(**pr_kw)})


def test_poll_first_breach_sighting_stamps_and_waits_for_persist():
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=60))
    st: dict = {}
    ev = _poll(trig, T0, st, output="42")
    assert ev is None
    assert st["breach_first_seen"] == R._iso(T0)
    assert st["last_value"] == 42
    assert st["last_probe_at"] == R._iso(T0)


def test_poll_fires_after_persist_elapsed():
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=60))
    st: dict = {}
    assert _poll(trig, T0, st, output="42") is None
    assert _poll(trig, T0 + timedelta(seconds=30), st, output="42") is None
    ev = _poll(trig, T0 + timedelta(seconds=60), st, output="42")
    assert ev is not None and ev.kind == "fire"
    assert ev.value == 42
    assert ev.threshold == 10
    assert ev.judge == "numeric_gt"
    assert ev.breach_first_seen == R._iso(T0)


def test_poll_persist_zero_fires_on_first_sighting():
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=0))
    st: dict = {}
    ev = _poll(trig, T0, st, output="42")
    assert ev is not None and ev.kind == "fire"


def test_poll_does_not_stamp_cooldown_itself():
    """The CALLER stamps fired/last_fired_at only after a successful attach —
    a spawn deferred under capacity backpressure must retry next tick."""
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=0, cooldown_s=1800))
    st: dict = {}
    assert _poll(trig, T0, st, output="42").kind == "fire"
    assert not st.get("fired")
    assert st.get("last_fired_at") is None
    # not stamped -> the same breach polls as a fire again (retry semantics)
    assert _poll(trig, T0 + timedelta(seconds=5), st, output="42").kind == "fire"


def test_poll_cooldown_blocks_refire_then_allows():
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=0, cooldown_s=1800))
    st: dict = {}
    assert _poll(trig, T0, st, output="42").kind == "fire"
    st["fired"] = True
    st["last_fired_at"] = R._iso(T0)
    assert _poll(trig, T0 + timedelta(seconds=600), st, output="42") is None
    ev = _poll(trig, T0 + timedelta(seconds=1801), st, output="42")
    assert ev is not None and ev.kind == "fire"


def test_poll_recovery_clears_breach_state_and_reports():
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=0, cooldown_s=1800))
    st: dict = {"breach_first_seen": R._iso(T0), "fired": True,
                "last_fired_at": R._iso(T0)}
    ev = _poll(trig, T0 + timedelta(seconds=300), st, output="5")
    assert ev is not None and ev.kind == "recover"
    assert st["breach_first_seen"] is None
    assert st["fired"] is False


def test_poll_recovery_of_unfired_breach_is_silent():
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=600))
    st: dict = {}
    _poll(trig, T0, st, output="42")  # sighting, never fired
    ev = _poll(trig, T0 + timedelta(seconds=30), st, output="5")
    assert ev is None
    assert st["breach_first_seen"] is None


def test_poll_cooldown_spans_recovery_against_flap():
    """recover then re-breach inside cooldown_s must not fire again — persist
    absorbs boundary flap, cooldown gaps fires (min gap between fires)."""
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=0, cooldown_s=1800))
    st: dict = {}
    assert _poll(trig, T0, st, output="42").kind == "fire"
    st["fired"] = True
    st["last_fired_at"] = R._iso(T0)
    assert _poll(trig, T0 + timedelta(seconds=60), st, output="5").kind == "recover"
    assert _poll(trig, T0 + timedelta(seconds=120), st, output="42") is None
    ev = _poll(trig, T0 + timedelta(seconds=1801), st, output="42")
    assert ev is not None and ev.kind == "fire"


def test_poll_probe_error_counts_not_breaches():
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=0))
    st: dict = {"breach_first_seen": R._iso(T0)}  # ongoing breach…
    ev = _poll(trig, T0 + timedelta(seconds=5), st, ok=False, error="timeout")
    assert ev is None
    assert st["consecutive_errors"] == 1
    # …is NOT cleared by a blind probe (an error is not a recovery either)
    assert st["breach_first_seen"] == R._iso(T0)


def test_poll_inf_output_increments_errors_state_intact():
    """The P3 crash aborted the sweep step BEFORE save_state — with the fix
    the non-finite probe rides the normal error path: consecutive_errors
    increments, breach state untouched, no exception."""
    trig = R.MonitorTrigger(_spec(threshold=10, persist_s=0))
    st: dict = {"breach_first_seen": R._iso(T0)}
    for i, out in enumerate(("inf", "nan"), start=1):
        ev = _poll(trig, T0 + timedelta(seconds=5 * i), st, output=out)
        assert ev is None
        assert st["consecutive_errors"] == i
    assert st["breach_first_seen"] == R._iso(T0)


def test_poll_ok_probe_resets_consecutive_errors():
    trig = R.MonitorTrigger(_spec(threshold=10))
    st: dict = {"consecutive_errors": 7}
    _poll(trig, T0, st, output="5")
    assert st["consecutive_errors"] == 0


def test_poll_monitor_broken_alerts_once_at_bound():
    trig = R.MonitorTrigger(_spec(threshold=10))
    st: dict = {"consecutive_errors": R.MONITOR_ERROR_BOUND - 1}
    ev = _poll(trig, T0, st, ok=False, error="timeout")
    assert ev is not None and ev.kind == "monitor_broken"
    # past the bound: silent (alert once, not every 5s)
    ev2 = _poll(trig, T0 + timedelta(seconds=5), st, ok=False, error="timeout")
    assert ev2 is None
    assert st["consecutive_errors"] == R.MONITOR_ERROR_BOUND + 1


# --- Sidecar state io (atomic, corrupt = reseed-safe) -----------------------

def test_state_roundtrip_and_location(mcfg):
    cfg, slug, _ = mcfg
    st = {"last_value": 502, "fired": True, "consecutive_errors": 0}
    R.save_state(cfg, slug, "R-0001", st)
    p = R.state_path(cfg, slug, "R-0001")
    assert p == R.routines_dir(cfg, slug) / "state" / "R-0001.json"
    assert p.exists()
    loaded = R.load_state(cfg, slug, "R-0001")
    assert loaded["last_value"] == 502
    assert loaded["fired"] is True


def test_state_missing_or_corrupt_reseeds_safe(mcfg):
    cfg, slug, _ = mcfg
    fresh = R.load_state(cfg, slug, "R-0009")
    assert fresh["breach_first_seen"] is None
    assert fresh["fired"] is False
    assert fresh["consecutive_errors"] == 0
    p = R.state_path(cfg, slug, "R-0009")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{corrupt json!!")
    again = R.load_state(cfg, slug, "R-0009")
    assert again["fired"] is False  # reseeded, no raise


# --- Probe runner (subprocess: timeout + output cap) -------------------------

def test_run_probe_captures_numeric_stdout():
    pr = R._run_probe({"cmd": "echo 42", "timeout_s": 5})
    assert pr.ok and pr.exit_code == 0
    assert pr.output.strip() == "42"


def test_run_probe_timeout_is_an_error_not_a_crash():
    pr = R._run_probe({"cmd": "sleep 5", "timeout_s": 1})
    assert pr.ok is False
    assert "timeout" in pr.error


def test_run_probe_timeout_airtight_against_pipe_holding_grandchild():
    """T-0603 review P2: a backgrounded grandchild inherits the stdout pipe
    and survives a kill of the shell alone — pre-fix the drain blocked until
    IT exited (here ~30s), wedging the max_instances=1 monitor job. killpg
    on the probe's own process group must end the run at ~timeout_s."""
    start = time.monotonic()
    pr = R._run_probe({"cmd": "sleep 30 & sleep 30", "timeout_s": 1})
    elapsed = time.monotonic() - start
    assert pr.ok is False
    assert "timeout" in pr.error
    assert elapsed < 8, f"probe run blocked {elapsed:.1f}s past its 1s timeout"


def test_run_probe_output_capped_at_8kb():
    pr = R._run_probe({"cmd": "python3 -c \"print('x' * 20000)\"", "timeout_s": 10})
    assert pr.ok
    assert len(pr.output) <= R.MONITOR_OUTPUT_CAP == 8192


# --- The sweep: walking skeleton + engine behaviors --------------------------

def test_walking_skeleton_file_metric_crossing_threshold_spawns_with_event(
        mcfg, tmp_path):
    """DoD skeleton proof (D-0048 §8): a shell probe watching a file-written
    number crosses the threshold -> after persist_s a session spawns whose
    brief carries the TRIGGER EVENT payload."""
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("5")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=30,
                                cooldown_s=1800, interval_s=5)

    # healthy: probe runs, no breach, no spawn — zero AI while green
    res = R.monitor_sweep(cfg, slug, now=T0)
    assert res["probed"] == 1 and res["fired"] == []
    assert spawns == []
    assert R.load_state(cfg, slug, rid)["last_value"] == 5

    # metric crosses the threshold: first sighting only stamps (anti-flap)
    metric.write_text("42")
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5))
    assert res["fired"] == [] and spawns == []

    # persist_s elapsed: FIRE -> one session through the existing spawn path
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=40))
    assert res["fired"] == [rid]
    assert len(spawns) == 1
    assert spawns[0]["owner"] == f"routine:{rid}"
    brief = spawns[0]["prompt"]
    assert "TRIGGER EVENT" in brief
    assert "42" in brief and "10" in brief and "numeric_gt" in brief
    assert rid in brief
    assert "investigate the metric breach per runbook" in brief

    # md stamped like a schedule fire (slow field; AI actually attached)
    r = R.load(cfg, slug, rid)
    assert r.last_run_at is not None
    # sidecar carries the fast state
    st = R.load_state(cfg, slug, rid)
    assert st["fired"] is True
    assert st["last_fired_at"] == R._iso(T0 + timedelta(seconds=40))


def test_sweep_respects_probe_interval(mcfg, tmp_path):
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("5")
    _declare_file_monitor(cfg, slug, metric, interval_s=30)
    assert R.monitor_sweep(cfg, slug, now=T0)["probed"] == 1
    # 5s later: not due (interval 30) — the 5s tick must not re-probe
    assert R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5))["probed"] == 0
    assert R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=30))["probed"] == 1


def test_sweep_cooldown_blocks_second_spawn_then_refires(mcfg, tmp_path):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0,
                                cooldown_s=1800, interval_s=5)
    R.monitor_sweep(cfg, slug, now=T0)
    assert len(spawns) == 1
    # breach continues inside cooldown: no second process
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=600))
    assert len(spawns) == 1
    # cooldown elapsed, breach still on: re-fire
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=1805))
    assert res["fired"] == [rid]
    assert len(spawns) == 2


def test_sweep_recovery_clears_state_and_appends_event(mcfg, tmp_path):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0,
                                cooldown_s=1800, interval_s=5)
    R.monitor_sweep(cfg, slug, now=T0)
    assert len(spawns) == 1
    metric.write_text("3")
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=10))
    st = R.load_state(cfg, slug, rid)
    assert st["breach_first_seen"] is None and st["fired"] is False
    kinds = [e["kind"] for e in _events(cfg, slug)]
    assert kinds == ["fire", "recover"]


def test_sweep_owner_dedup_nudges_instead_of_second_spawn(mcfg, tmp_path,
                                                          monkeypatch):
    """A live session owning routine:<R> means NO second spawn — the new fire
    is appended to events.ndjson and a one-line nudge rides the input mux."""
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0,
                                cooldown_s=60, interval_s=5)
    R.monitor_sweep(cfg, slug, now=T0)
    assert len(spawns) == 1
    live_sid = spawns[0]["sid"]
    monkeypatch.setattr(S, "list_sessions", lambda c, s: [
        {"sid": live_sid, "status": "active", "archived": False,
         "owner": f"routine:{rid}"},
    ])
    # past cooldown, breach still on -> fire, but the owner is alive: nudge
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=65))
    assert res["fired"] == [rid]
    assert len(spawns) == 1  # NO second spawn
    queued = input_mux.read_queue(cfg.data_dir, live_sid)
    assert len(queued) == 1
    assert rid in queued[0]["text"]
    assert queued[0]["author"] == f"routine:{rid}"
    events = _events(cfg, slug)
    assert events[-1]["kind"] == "fire"
    assert events[-1]["sid"] == live_sid
    assert "dedup" in (events[-1]["note"] or "")
    # nudge counts as the fire: cooldown stamped, no 5s-spam
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=70))
    assert res["fired"] == []
    assert input_mux.read_queue(cfg.data_dir, live_sid) == queued


def test_sweep_capacity_deferral_retries_without_stamping_cooldown(
        mcfg, tmp_path, monkeypatch):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0,
                                cooldown_s=1800, interval_s=5)

    def _full(c, s, window, **kw):
        raise ActionError("session capacity reached (3/3)")

    monkeypatch.setattr(S, "spawn", _full)
    res = R.monitor_sweep(cfg, slug, now=T0)
    assert res["fired"] == [] and spawns == []
    st = R.load_state(cfg, slug, rid)
    assert st.get("fired") is False and st.get("last_fired_at") is None

    def _ok(c, s, window, initial_prompt=None, owner=None, **kw):
        spawns.append({"owner": owner, "sid": "S-mon-late", "prompt": initial_prompt})
        return {"ok": True, "sid": "S-mon-late"}

    monkeypatch.setattr(S, "spawn", _ok)
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5))
    assert res["fired"] == [rid]
    assert len(spawns) == 1


def test_sweep_paused_monitor_is_skipped(mcfg, tmp_path):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_file_monitor(cfg, slug, metric)
    R._set_status(cfg, slug, rid, "paused")
    res = R.monitor_sweep(cfg, slug, now=T0)
    assert res["probed"] == 0 and spawns == []


def test_sweep_probe_timeout_increments_errors_no_spawn(mcfg, tmp_path):
    cfg, slug, spawns = mcfg
    rid = R.declare(cfg, slug, instruction="x", trigger="monitor",
                    monitor=_spec(cmd="sleep 5", timeout_s=1),
                    provenance="T-0603", now=T0)["id"]
    res = R.monitor_sweep(cfg, slug, now=T0)
    assert res["fired"] == [] and spawns == []
    st = R.load_state(cfg, slug, rid)
    assert st["consecutive_errors"] == 1


def test_sweep_one_bad_monitor_never_kills_the_sweep(mcfg, tmp_path):
    cfg, slug, spawns = mcfg
    # hand-corrupt one monitor's spec on disk (bypasses declare validation)
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    bad = _declare_file_monitor(cfg, slug, metric)
    good = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0)
    from bot_squad_worker import frontmatter as fm
    bad_path = Path(R.load(cfg, slug, bad).file_path)
    meta, body = fm.parse(bad_path.read_text())
    meta["monitor"] = {"cmd": "", "judge": "vibes"}
    bad_path.write_text(fm.dump(meta, body))
    res = R.monitor_sweep(cfg, slug, now=T0)
    assert good in res["fired"]  # the good sibling still fired


def test_sweep_single_flight_skips_inflight_routine(mcfg, tmp_path):
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("5")
    rid = _declare_file_monitor(cfg, slug, metric)
    key = (slug, rid)
    R._INFLIGHT.add(key)
    try:
        assert R.monitor_sweep(cfg, slug, now=T0)["probed"] == 0
    finally:
        R._INFLIGHT.discard(key)
    assert R.monitor_sweep(cfg, slug, now=T0)["probed"] == 1


# --- Registry cache (mtime-gated so the 5s tick doesn't re-read every md) ---

def test_monitor_registry_cached_when_dir_quiet(mcfg, tmp_path, monkeypatch):
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("5")
    _declare_file_monitor(cfg, slug, metric)
    d = R.routines_dir(cfg, slug)
    past = T0.timestamp() - 3600
    os.utime(d, (past, past))  # dir quiet -> cacheable
    first = R._monitor_routines(cfg, slug)
    assert [r.id for r in first] == ["R-0001"]
    # md reads would now blow up — a second call must be served from cache
    monkeypatch.setattr(R, "_routine_from_md",
                        lambda p: (_ for _ in ()).throw(AssertionError("re-read!")))
    assert R._monitor_routines(cfg, slug) is first


def test_monitor_registry_rescans_on_dir_change(mcfg, tmp_path):
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("5")
    _declare_file_monitor(cfg, slug, metric)
    d = R.routines_dir(cfg, slug)
    past = T0.timestamp() - 3600
    os.utime(d, (past, past))
    assert len(R._monitor_routines(cfg, slug)) == 1
    _declare_file_monitor(cfg, slug, metric)  # dir mtime changes
    os.utime(d, (past + 10, past + 10))
    assert len(R._monitor_routines(cfg, slug)) == 2


# --- events.ndjson (observability seam, D-0048 §3.3) -------------------------

def _events(cfg, slug) -> list[dict]:
    p = R.events_path(cfg, slug)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def test_fire_event_appended_with_schema_fields(mcfg, tmp_path):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0)
    R.monitor_sweep(cfg, slug, now=T0)
    events = _events(cfg, slug)
    assert len(events) == 1
    ev = events[0]
    assert set(ev) == {"ts", "routine", "kind", "value", "threshold", "sid", "note"}
    assert ev["routine"] == rid
    assert ev["kind"] == "fire"
    assert ev["value"] == 42
    assert ev["threshold"] == 10
    assert ev["sid"] == spawns[0]["sid"]


def test_monitor_broken_appends_event_once(mcfg):
    cfg, slug, _ = mcfg
    rid = R.declare(cfg, slug, instruction="x", trigger="monitor",
                    monitor=_spec(cmd="false", judge="numeric_gt", threshold=1,
                                  interval_s=5, timeout_s=2),
                    provenance="T-0603", now=T0)["id"]
    for i in range(R.MONITOR_ERROR_BOUND + 2):
        R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5 * i))
    events = _events(cfg, slug)
    broken = [e for e in events if e["kind"] == "monitor_broken"]
    assert len(broken) == 1
    assert broken[0]["routine"] == rid


def test_fire_count_24h_counts_only_recent_fires_of_this_routine(mcfg):
    cfg, slug, _ = mcfg
    now = T0
    R.append_event(cfg, slug, ts=R._iso(now - timedelta(hours=30)),
                   routine="R-0001", kind="fire")
    R.append_event(cfg, slug, ts=R._iso(now - timedelta(hours=2)),
                   routine="R-0001", kind="fire")
    R.append_event(cfg, slug, ts=R._iso(now - timedelta(hours=1)),
                   routine="R-0002", kind="fire")
    R.append_event(cfg, slug, ts=R._iso(now - timedelta(minutes=5)),
                   routine="R-0001", kind="recover")
    assert R._fire_count_24h(cfg, slug, "R-0001", now=now) == 1


def test_second_fire_brief_reports_fire_count(mcfg, tmp_path):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0,
                          cooldown_s=60, interval_s=5)
    R.monitor_sweep(cfg, slug, now=T0)
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=65))
    assert len(spawns) == 2
    assert "fires in last 24h (incl. this one): 2" in spawns[1]["prompt"]


# --- monitor_tick: kill switch + project iteration ---------------------------

def test_monitor_tick_kill_switch(mcfg, tmp_path, monkeypatch):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0)
    monkeypatch.setenv("BOT_SQUAD_MONITORS", "0")
    R.monitor_tick(cfg)
    assert spawns == []
    assert not R.state_path(cfg, slug, rid).exists()  # not even probed


def test_monitor_tick_sweeps_projects(mcfg, tmp_path, monkeypatch):
    cfg, slug, spawns = mcfg
    monkeypatch.delenv("BOT_SQUAD_MONITORS", raising=False)
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    _declare_file_monitor(cfg, slug, metric, threshold=10, persist_s=0)
    R.monitor_tick(cfg)  # real now(); persist 0 fires immediately
    assert len(spawns) == 1


# --- scheduler wiring ---------------------------------------------------------

def test_scheduler_registers_monitor_job_at_5s(mcfg):
    cfg, _slug, _ = mcfg
    from bot_squad_worker.scheduler import build_scheduler
    sched = build_scheduler(cfg)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert "monitors" in jobs
    assert "0:00:05" in str(jobs["monitors"].trigger)


# ===========================================================================
# T-0605 / D-0048 slice 3 — alert parity (manual walkthrough passed first,
# scenario T-0605): on_breach notify (urgent TG, no AI) + on_recover ✅ +
# http probe + monitor-broken self-alert. All stakeholder pages ride the
# actions._send_stakeholder_dm SSOT (T-0394 guard) with urgent=True — the
# quiet-hours gate silently drops non-urgent sends 17-05 UTC.
# ===========================================================================

@pytest.fixture
def dm_capture(monkeypatch):
    """Capture-stub the _send_stakeholder_dm SSOT (the delivery boundary)."""
    calls: list[dict] = []

    def _fake(cfg, *, message, **kw):
        calls.append({"message": message, **kw})
        return {"ok": True, "sent": True, "channel": "test"}

    from bot_squad_worker import actions
    monkeypatch.setattr(actions, "_send_stakeholder_dm", _fake)
    return calls


def _declare_notify_monitor(cfg, slug, metric_file: Path, *, threshold=10,
                            persist_s=0, cooldown_s=0, interval_s=5,
                            on_recover=None, on_breach="notify") -> str:
    spec = _spec(cmd=f"cat {metric_file}", threshold=threshold,
                 persist_s=persist_s, cooldown_s=cooldown_s,
                 interval_s=interval_s, on_breach=on_breach)
    if on_recover:
        spec["on_recover"] = on_recover
    return R.declare(cfg, slug, instruction="alert-parity monitor",
                     trigger="monitor", monitor=spec,
                     provenance="T-0605", now=T0)["id"]


# --- spec validation: notify modes + http probe -----------------------------

def test_monitor_spec_accepts_on_breach_notify_and_on_recover():
    trig = R.MonitorTrigger(_spec(on_breach="notify", on_recover="notify"))
    assert trig.spec["on_breach"] == "notify"
    assert trig.spec["on_recover"] == "notify"
    # on_recover omitted -> absent from the normalized spec
    assert "on_recover" not in R.MonitorTrigger(_spec()).spec


def _http_spec(**overrides) -> dict:
    base = {"probe": "http", "url": "http://127.0.0.1:1/health",
            "interval_s": 5, "timeout_s": 3}
    base.update(overrides)
    return base


def test_http_spec_normalizes_self_judging_fields():
    trig = R.MonitorTrigger(_http_spec())
    assert trig.spec["judge"] == "http"
    assert trig.spec["expect_status"] == 200  # default
    assert trig.spec["threshold"] == "status==200"
    assert "cmd" not in trig.spec

    trig = R.MonitorTrigger(_http_spec(expect_status=302, latency_budget_ms=500))
    assert trig.spec["threshold"] == "status==302 & latency<=500ms"
    assert trig.spec["latency_budget_ms"] == 500


def test_http_normalized_spec_revalidates_on_reload():
    """The persisted normalized spec (judge='http', derived threshold) must
    round-trip through declare-time validation — Routine.trigger() re-runs it."""
    first = R.MonitorTrigger(_http_spec(latency_budget_ms=250)).spec
    again = R.MonitorTrigger(dict(first)).spec
    assert again == first


@pytest.mark.parametrize("bad", [
    {"url": None},                          # url mandatory
    {"url": "ftp://x/health"},              # not http(s)
    {"url": "127.0.0.1:80"},                # scheme missing
    {"judge": "numeric_gt"},                # http probe judges itself
    {"threshold": 200},                     # threshold is derived
    {"expect_status": 99},
    {"expect_status": 600},
    {"expect_status": "teapot"},
    {"latency_budget_ms": 0},
    {"latency_budget_ms": "fast"},
    {"cmd": "curl ..."},                    # http probe takes url, not cmd
])
def test_http_spec_validation_rejects(bad):
    spec = _http_spec(**bad)
    if bad.get("url", "x") is None:
        spec.pop("url")
    with pytest.raises(R.RoutineError):
        R.MonitorTrigger(spec)


def test_declare_http_monitor_persists_and_loads(mcfg):
    cfg, slug, _ = mcfg
    rid = R.declare(cfg, slug, instruction="watch the endpoint",
                    trigger="monitor",
                    monitor=_http_spec(expect_status=200, latency_budget_ms=800),
                    provenance="T-0605", now=T0)["id"]
    r = R.load(cfg, slug, rid)
    assert r.monitor["judge"] == "http"
    assert r.monitor["url"] == "http://127.0.0.1:1/health"
    assert isinstance(r.trigger(), R.MonitorTrigger)  # revalidates on load


# --- evaluate_probe: the http judge ------------------------------------------

def test_evaluate_http_judge_maps_exit_code_to_breach():
    spec = R.MonitorTrigger(_http_spec()).spec
    ok = _pr(exit_code=0, output="status=200 latency_ms=12")
    assert R.evaluate_probe(spec, ok) == ("ok", "status=200 latency_ms=12")
    bad = _pr(exit_code=1, output="status=502 latency_ms=40")
    assert R.evaluate_probe(spec, bad) == ("breach", "status=502 latency_ms=40")
    # probe machinery failure stays the error path
    assert R.evaluate_probe(spec, _pr(ok=False, error="boom"))[0] == "error"


# --- _run_http_probe against a real local server ------------------------------

@pytest.fixture
def http_target():
    """A controllable local HTTP server: set ctl['code'] / ctl['delay']."""
    import http.server
    import threading

    ctl = {"code": 200, "delay": 0.0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(ctl["delay"])
            self.send_response(ctl["code"])
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # noqa: D102 — quiet test output
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", ctl
    finally:
        srv.shutdown()
        srv.server_close()


def test_run_http_probe_healthy(http_target):
    url, _ctl = http_target
    spec = R.MonitorTrigger(_http_spec(url=f"{url}/health")).spec
    pr = R._run_probe(spec)
    assert pr.ok and pr.exit_code == 0
    assert pr.output.startswith("status=200 latency_ms=")


def test_run_http_probe_status_mismatch_is_breach(http_target):
    url, ctl = http_target
    ctl["code"] = 500
    spec = R.MonitorTrigger(_http_spec(url=f"{url}/health")).spec
    pr = R._run_probe(spec)
    assert pr.ok and pr.exit_code == 1
    assert pr.output.startswith("status=500")
    assert "status 500 != 200" in pr.error


def test_run_http_probe_latency_budget_breach(http_target):
    url, ctl = http_target
    ctl["delay"] = 0.15
    spec = R.MonitorTrigger(_http_spec(url=f"{url}/", latency_budget_ms=1)).spec
    pr = R._run_probe(spec)
    assert pr.ok and pr.exit_code == 1
    assert "latency" in pr.error and "> 1ms" in pr.error


def test_run_http_probe_unreachable_is_breach_not_error():
    """Connection refused IS the outage for a health probe (T-0605 design
    note): it must alert as a breach within persist_s, not sit silent for 10
    intervals and then phrase itself as a broken monitor."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # port now closed
    spec = R.MonitorTrigger(_http_spec(url=f"http://127.0.0.1:{port}/")).spec
    pr = R._run_probe(spec)
    assert pr.ok is True and pr.exit_code == 1
    assert pr.output.startswith("unreachable (")


def test_run_http_probe_timeout_is_unreachable_breach(http_target):
    url, ctl = http_target
    ctl["delay"] = 3.0
    spec = R.MonitorTrigger(_http_spec(url=f"{url}/", timeout_s=1)).spec
    pr = R._run_probe(spec)
    assert pr.ok is True and pr.exit_code == 1
    assert pr.output.startswith("unreachable (")


# --- on_breach: notify — code-only alert, zero AI ----------------------------

def test_notify_breach_pages_urgent_without_spawning(mcfg, tmp_path, dm_capture):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_notify_monitor(cfg, slug, metric, cooldown_s=1800)
    res = R.monitor_sweep(cfg, slug, now=T0)
    assert res["fired"] == [rid]
    assert spawns == []                     # ZERO AI
    assert len(dm_capture) == 1
    dm = dm_capture[0]
    assert dm["urgent"] is True             # the quiet-hours trap
    assert dm["sid"] == f"routine:{rid}"
    assert dm["tg_chat_id"] == "TEST_CHAT_ID"
    assert rid in dm["message"] and "42" in dm["message"] and "10" in dm["message"]
    # events.ndjson records kind=notify (D-0048 §3.3)
    assert [e["kind"] for e in _events(cfg, slug)] == ["notify"]
    # cooldown stamped exactly like a spawn fire
    st = R.load_state(cfg, slug, rid)
    assert st["fired"] is True
    assert st["last_fired_at"] == R._iso(T0)
    # md untouched: no AI attached (last_run_at is a slow AI-attach field)
    assert R.load(cfg, slug, rid).last_run_at is None


def test_notify_breach_cooldown_gaps_realerts_then_refires(mcfg, tmp_path,
                                                           dm_capture):
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_notify_monitor(cfg, slug, metric, cooldown_s=1800)
    R.monitor_sweep(cfg, slug, now=T0)
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=600))
    assert len(dm_capture) == 1             # inside cooldown: no re-alert
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=1805))
    assert res["fired"] == [rid]
    assert len(dm_capture) == 2             # past cooldown, still breaching


def test_notify_delivery_failure_retries_next_tick(mcfg, tmp_path, monkeypatch):
    """A raised delivery (both channels down) must NOT stamp cooldown — the
    breach re-alerts on the next sweep, mirroring capacity-deferred spawns."""
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_notify_monitor(cfg, slug, metric, cooldown_s=1800)

    from bot_squad_worker import actions
    calls: list[dict] = []

    def _down(cfg, *, message, **kw):
        raise RuntimeError("MAX and TG both unreachable")

    monkeypatch.setattr(actions, "_send_stakeholder_dm", _down)
    res = R.monitor_sweep(cfg, slug, now=T0)
    assert res["fired"] == []
    st = R.load_state(cfg, slug, rid)
    assert st["fired"] is False and st["last_fired_at"] is None
    assert _events(cfg, slug) == []         # nothing delivered, nothing recorded

    def _up(cfg, *, message, **kw):
        calls.append({"message": message, **kw})
        return {"ok": True, "sent": True, "channel": "test"}

    monkeypatch.setattr(actions, "_send_stakeholder_dm", _up)
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5))
    assert res["fired"] == [rid]
    assert len(calls) == 1
    assert R.load_state(cfg, slug, rid)["fired"] is True


# --- on_recover: notify — ✅ only for breaches that actually fired ------------

def test_recover_notify_sends_checkmark_after_fired_breach(mcfg, tmp_path,
                                                           dm_capture):
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_notify_monitor(cfg, slug, metric, cooldown_s=1800,
                                  on_recover="notify")
    R.monitor_sweep(cfg, slug, now=T0)
    assert len(dm_capture) == 1
    metric.write_text("5")
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=10))
    assert spawns == []
    assert len(dm_capture) == 2
    dm = dm_capture[1]
    assert dm["urgent"] is True
    assert "✅" in dm["message"] and rid in dm["message"] and "5" in dm["message"]
    assert [e["kind"] for e in _events(cfg, slug)] == ["notify", "recover"]


def test_recover_without_on_recover_stays_silent(mcfg, tmp_path, dm_capture):
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    _declare_notify_monitor(cfg, slug, metric, cooldown_s=1800)  # no on_recover
    R.monitor_sweep(cfg, slug, now=T0)
    metric.write_text("5")
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=10))
    assert len(dm_capture) == 1             # breach alert only, no ✅
    # the recover event is still recorded (observability seam)
    assert [e["kind"] for e in _events(cfg, slug)] == ["notify", "recover"]


def test_recover_notify_silent_for_unfired_breach(mcfg, tmp_path, dm_capture):
    """A breach sighting that never fired (persist window) clears silently —
    the linza rule; no ✅ noise for a blip nobody was alerted to."""
    cfg, slug, _ = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    _declare_notify_monitor(cfg, slug, metric, persist_s=600,
                            on_recover="notify")
    R.monitor_sweep(cfg, slug, now=T0)      # sighting inside persist: no fire
    metric.write_text("5")
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=10))
    assert dm_capture == []
    assert _events(cfg, slug) == []


def test_spawn_mode_with_recover_notify_mixes(mcfg, tmp_path, dm_capture):
    """on_breach: spawn + on_recover: notify — AI attaches on breach, the ✅
    is still a code-only page."""
    cfg, slug, spawns = mcfg
    metric = tmp_path / "metric.txt"
    metric.write_text("42")
    rid = _declare_notify_monitor(cfg, slug, metric, cooldown_s=1800,
                                  on_breach="spawn", on_recover="notify")
    R.monitor_sweep(cfg, slug, now=T0)
    assert len(spawns) == 1 and dm_capture == []
    metric.write_text("5")
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=10))
    assert len(spawns) == 1
    assert len(dm_capture) == 1
    assert "✅" in dm_capture[0]["message"] and rid in dm_capture[0]["message"]


# --- monitor-broken self-alert (once, urgent) ---------------------------------

def test_monitor_broken_pages_once_urgent(mcfg, dm_capture):
    cfg, slug, spawns = mcfg
    rid = R.declare(cfg, slug, instruction="x", trigger="monitor",
                    monitor=_spec(cmd="false", judge="numeric_gt", threshold=1,
                                  interval_s=5, timeout_s=2),
                    provenance="T-0605", now=T0)["id"]
    for i in range(R.MONITOR_ERROR_BOUND + 3):
        R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5 * i))
    assert spawns == []                     # never fire AI with garbage
    assert len(dm_capture) == 1             # once, not per-tick
    dm = dm_capture[0]
    assert dm["urgent"] is True
    assert "BROKEN" in dm["message"] and rid in dm["message"]
    assert str(R.MONITOR_ERROR_BOUND) in dm["message"]


def test_monitor_broken_alert_failure_never_kills_the_sweep(mcfg, tmp_path,
                                                            monkeypatch):
    cfg, slug, _ = mcfg
    R.declare(cfg, slug, instruction="x", trigger="monitor",
              monitor=_spec(cmd="false", judge="numeric_gt", threshold=1,
                            interval_s=5, timeout_s=2),
              provenance="T-0605", now=T0)["id"]
    from bot_squad_worker import actions
    monkeypatch.setattr(actions, "_send_stakeholder_dm",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    for i in range(R.MONITOR_ERROR_BOUND + 1):
        R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5 * i))
    # the event is still on the observability seam despite the dead channel
    broken = [e for e in _events(cfg, slug) if e["kind"] == "monitor_broken"]
    assert len(broken) == 1


# --- http probe end-to-end through the sweep ----------------------------------

def test_http_monitor_sweep_end_to_end_notify(mcfg, http_target, dm_capture):
    """Walking-skeleton parity for slice 3: a REAL local endpoint goes 500 →
    the notify-mode http monitor pages urgent with the observation, zero AI."""
    cfg, slug, spawns = mcfg
    url, ctl = http_target
    rid = R.declare(cfg, slug, instruction="watch the endpoint",
                    trigger="monitor",
                    monitor={"probe": "http", "url": f"{url}/health",
                             "interval_s": 5, "timeout_s": 3,
                             "persist_s": 0, "cooldown_s": 1800,
                             "on_breach": "notify", "on_recover": "notify"},
                    provenance="T-0605", now=T0)["id"]
    R.monitor_sweep(cfg, slug, now=T0)
    assert dm_capture == [] and spawns == []    # healthy: zero noise
    assert str(R.load_state(cfg, slug, rid)["last_value"]).startswith("status=200")

    ctl["code"] = 500
    res = R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=5))
    assert res["fired"] == [rid] and spawns == []
    assert len(dm_capture) == 1
    assert dm_capture[0]["urgent"] is True
    assert "status=500" in dm_capture[0]["message"]
    assert "status==200" in dm_capture[0]["message"]

    ctl["code"] = 200
    R.monitor_sweep(cfg, slug, now=T0 + timedelta(seconds=10))
    assert len(dm_capture) == 2
    assert "✅" in dm_capture[1]["message"]


def test_http_monitor_spawn_mode_brief_carries_observation(mcfg, http_target):
    """on_breach: spawn (default) for an http monitor — the TRIGGER EVENT brief
    carries the http observation + derived threshold."""
    cfg, slug, spawns = mcfg
    url, ctl = http_target
    ctl["code"] = 502
    R.declare(cfg, slug, instruction="investigate the endpoint",
              trigger="monitor",
              monitor=_http_spec(url=f"{url}/health"),
              provenance="T-0605", now=T0)
    R.monitor_sweep(cfg, slug, now=T0)
    assert len(spawns) == 1
    brief = spawns[0]["prompt"]
    assert "TRIGGER EVENT" in brief
    assert "status=502" in brief and "status==200" in brief
