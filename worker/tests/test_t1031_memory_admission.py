"""T-1031 — the admission gate answers about MEMORY and the RUN QUEUE too.

At 2026-09-06 22:57Z ``fleet_slot.py`` printed ``admission PASSED —
d_state=7`` on a box at load1 77.73 with SwapFree at 0.7% of 8.00 GB. It would
have admitted a build.

**The D-state gate was not broken and these tests do not treat it as though it
were.** D-state was chosen under T-0994 against real competitors and it answers
about IO by construction; the 22:57Z state was not an IO event. What was
missing was a second and a third question.

Every constant in this file is a MEASURED value, and the two that matter most
are the ones that pass:

* the 22:57Z event readings, from the operator's own sample;
* the healthy/parked host measured two hours later, at 1.18% swap free and a
  commit ratio of 1.63 — HIGHER than the event's 1.617. A gate keyed on
  SwapFree or on Committed_AS would have refused this box all night.

The calibration series behind the ceilings is in the module docstring: 325
samples across five deliberately produced conditions.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SLOT = REPO / "scripts" / "cli" / "fleet_slot.py"

pytestmark = pytest.mark.skipif(
    not SLOT.exists(), reason="fleet_slot.py not present in this tree")


def _module():
    spec = importlib.util.spec_from_file_location("fleet_slot_t1031", SLOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Measured states. Not invented, not rounded to make a point.
# --------------------------------------------------------------------------

#: 2026-09-06T22:57Z — the state the gate admitted. r_state was NOT sampled;
#: load counts R+D, so load1 77.73 with d_state 7 puts ~70 in the run queue.
#: That inference is what this constant carries, and it is labelled as one.
EVENT_2257 = {
    "at": 0.0, "d_state": 7, "r_state": 70, "loadavg1": 77.73,
    "psi_memory_some_avg10": None, "psi_memory_full_avg10": None,
    "psi_cpu_some_avg10": None, "psi_io_full_avg10": None,
    "swap_free_pct": 0.75, "swap_total_gb": 8.0, "committed_ratio": 1.617,
    "mem_available_gb": 16.8, "pswpin": 6481598, "pswpout": 12795481,
}

#: 2026-09-07T00:37Z — the SAME box, healthy, and still at 1.18% swap free
#: with a HIGHER commit ratio than the event had.
HEALTHY_PARKED = {
    "at": 0.0, "d_state": 4, "r_state": 2, "loadavg1": 9.54,
    "psi_memory_some_avg10": 0.06, "psi_memory_full_avg10": 0.05,
    "psi_cpu_some_avg10": 3.20, "psi_io_full_avg10": 55.07,
    "swap_free_pct": 1.18, "swap_total_gb": 8.0, "committed_ratio": 1.63,
    "mem_available_gb": 17.04, "pswpin": 6482093, "pswpout": 12795486,
}

#: The swap-eviction control's peak sample: memory stall, run queue quiet.
SWAP_EVICTION_PEAK = dict(HEALTHY_PARKED, psi_memory_full_avg10=16.81,
                          psi_memory_some_avg10=20.81, swap_free_pct=1.68)


def _check(mod, tmp_path, readings, monkeypatch, **config):
    """Run the real :func:`admission_check` over a fixed set of readings."""
    state = tmp_path / "slots"
    state.mkdir(parents=True, exist_ok=True)
    (state / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(mod, "admission_readings",
                        lambda sd, **kw: dict(readings))
    for var in ("BOT_SQUAD_FLEET_DSTATE_MAX", "BOT_SQUAD_FLEET_RSTATE_MAX",
                "BOT_SQUAD_FLEET_PSI_MEM_MAX"):
        monkeypatch.delenv(var, raising=False)
    return mod.admission_check(state)


# --------------------------------------------------------------------------
# The two states, and the ceilings are the shipped defaults in both cases
# --------------------------------------------------------------------------

def test_the_2257_state_is_refused_and_the_refusal_carries_the_value(
        tmp_path, monkeypatch):
    """The defect, closed. And it is the RUN QUEUE that closes it, not a swap
    reading: every memory number in this state also holds on a healthy box."""
    mod = _module()
    ok, reason, _ = _check(mod, tmp_path, EVENT_2257, monkeypatch)
    assert not ok, f"still admitted the 22:57Z state: {reason}"
    assert "r_state=70" in reason, "refused without printing the reading"
    assert "over the ceiling of 24" in reason
    # And D-state, correctly, had nothing to say about it.
    assert "d_state=7 over" not in reason


def test_swap_full_but_parked_is_ADMITTED(tmp_path, monkeypatch):
    """The half of this ticket that a naive fix breaks.

    This box sat at 1.18% swap free, static, all night while healthy. A
    SwapFree floor or a Committed_AS ceiling would have refused every run on
    it — and the commit ratio here (1.63) is HIGHER than during the event
    (1.617), so no threshold on that reading separates the two states at all.
    """
    mod = _module()
    ok, reason, _ = _check(mod, tmp_path, HEALTHY_PARKED, monkeypatch)
    assert ok, f"refused a healthy host: {reason}"
    assert "d_state=4 within" in reason
    assert "r_state=2 within" in reason
    assert "psi_memory_full_avg10=0.05 within" in reason


def test_a_memory_stall_is_refused_where_swap_fullness_is_not(
        tmp_path, monkeypatch):
    """Swap FULL and swap STALLING are different states, and only the second
    one stops anybody. Same swap reading as the healthy case; different
    verdict, because the stall is what was measured."""
    mod = _module()
    ok, reason, _ = _check(mod, tmp_path, SWAP_EVICTION_PEAK, monkeypatch)
    assert not ok
    assert "psi_memory_full_avg10=16.81" in reason
    assert "over the ceiling of 10.0" in reason


# --------------------------------------------------------------------------
# Prove each guard can fail — one property at a time, from a passing state
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field,value,expect", [
    ("d_state", 21, "d_state=21 over the ceiling of 20"),
    ("r_state", 25, "r_state=25 over the ceiling of 24"),
    ("psi_memory_full_avg10", 10.1,
     "psi_memory_full_avg10=10.1 over the ceiling of 10.0"),
])
def test_each_gate_refuses_when_ITS_OWN_reading_crosses(
        tmp_path, monkeypatch, field, value, expect):
    """Mutate the healthy state in ONE property. If a gate cannot be made to
    fail by moving its own reading, the green above was measuring nothing."""
    mod = _module()
    ok, reason, _ = _check(mod, tmp_path, dict(HEALTHY_PARKED, **{field: value}),
                           monkeypatch)
    assert not ok, f"{field}={value} was admitted"
    assert expect in reason


@pytest.mark.parametrize("field,value", [
    ("d_state", 20), ("r_state", 24), ("psi_memory_full_avg10", 10.0),
])
def test_each_gate_admits_AT_its_ceiling(tmp_path, monkeypatch, field, value):
    """The other direction: the ceiling is a ceiling, not an exclusion. A
    guard that refuses at exactly its limit is a different guard from the one
    the calibration table describes."""
    mod = _module()
    ok, reason, _ = _check(mod, tmp_path, dict(HEALTHY_PARKED, **{field: value}),
                           monkeypatch)
    assert ok, reason


@pytest.mark.parametrize("field", ["d_state", "r_state"])
def test_a_blind_proc_gate_refuses(tmp_path, monkeypatch, field):
    """Both counts come from the same walk of /proc. If that walk cannot be
    done the host is broken, and a gate that admits on a missing reading is
    absent exactly when it is needed."""
    mod = _module()
    ok, reason, _ = _check(mod, tmp_path, dict(HEALTHY_PARKED, **{field: None}),
                           monkeypatch)
    assert not ok
    assert f"{field} NOT MEASURED" in reason


def test_a_kernel_without_psi_leaves_the_gate_SATISFIABLE(
        tmp_path, monkeypatch):
    """CONFIG_PSI=n is a normal kernel, not a broken host. Refusing every run
    on one would be a gate that can never pass — which is the failure mode
    this whole module was rewritten to remove, so the blind-gate rule above is
    deliberately NOT generalised to an optional instrument."""
    mod = _module()
    ok, reason, _ = _check(
        mod, tmp_path, dict(HEALTHY_PARKED, psi_memory_full_avg10=None),
        monkeypatch)
    assert ok, reason
    assert "NOT AVAILABLE" in reason and "INACTIVE" in reason


# --------------------------------------------------------------------------
# The rejected candidates are RECORDED and gate nothing
# --------------------------------------------------------------------------

def test_the_rejected_readings_cannot_refuse_anything(tmp_path, monkeypatch):
    """SwapFree, Committed_AS/CommitLimit and MemAvailable were each proposed,
    each measured, and each refuses a healthy host: a 5% swap floor and a 1.5
    commit ceiling refuse 100% of samples in EVERY condition sampled, and a
    4 GB MemAvailable floor refuses 0% even during real memory pressure.

    They stay in the ticket because the next calibration needs the values.
    Nothing reads them as a threshold — including a config that tries to.
    """
    mod = _module()
    ok, reason, rec = _check(
        mod, tmp_path,
        dict(HEALTHY_PARKED, swap_free_pct=0.0, committed_ratio=9.9,
             mem_available_gb=0.1),
        monkeypatch,
        swap_free_min=50, committed_ratio_max=1.0, mem_available_min_gb=99)
    assert ok, f"a demoted reading refused a run: {reason}"
    assert rec["swap_free_pct"] == 0.0
    assert rec["committed_ratio"] == 9.9
    assert rec["mem_available_gb"] == 0.1


def test_loadavg_still_only_describes(tmp_path, monkeypatch):
    """Measured in-condition: with 24 spinners already running, load1 read
    12.39 while r_state read 28. The gate does not wait a minute to be told
    what the run queue already says."""
    mod = _module()
    ok, _, _ = _check(mod, tmp_path, dict(HEALTHY_PARKED, loadavg1=200.0),
                      monkeypatch)
    assert ok


# --------------------------------------------------------------------------
# The instruments themselves, on the real host
# --------------------------------------------------------------------------

def test_the_run_queue_counter_actually_counts(tmp_path):
    """A counter stuck at zero would pass every assertion about r_state being
    an int. The process doing the walk is itself RUNNABLE while it walks, so
    a working counter cannot report zero."""
    mod = _module()
    d, r = mod.proc_state_counts()
    assert isinstance(d, int) and isinstance(r, int)
    assert r >= 1, "r_state read 0 while the reader itself was running"
    assert mod.d_state_count() is not None


def test_psi_is_read_from_the_file_on_this_kernel():
    """This box has PSI. If that ever stops being true the memory gate goes
    INACTIVE rather than silently reading zero, which the test above covers."""
    mod = _module()
    psi = mod.psi_readings()
    assert set(psi) == {"psi_memory_some_avg10", "psi_memory_full_avg10",
                        "psi_cpu_some_avg10", "psi_io_full_avg10"}
    if Path("/proc/pressure/memory").exists():
        assert psi["psi_memory_full_avg10"] is not None
        assert 0.0 <= psi["psi_memory_full_avg10"] <= 100.0


def test_swap_rates_are_a_rate_and_gate_nothing(tmp_path):
    """si/so need two points. The sample is off the run path and carries the
    span it was taken over, because a rate without its interval is a number
    nobody can re-judge."""
    mod = _module()
    out = mod.swap_rate_sample(span=0.2)
    assert out["swap_rate_span_s"] >= 0.2
    assert "si_pages_s" in out and "so_pages_s" in out


# --------------------------------------------------------------------------
# End to end, through the CLI a lane actually calls
# --------------------------------------------------------------------------

def _env(state: Path, **over) -> dict:
    env = dict(os.environ)
    env["BOT_SQUAD_FLEET_SLOTS_DIR"] = str(state)
    env["BOT_SQUAD_FLEET_LEGACY_SLOT_DIR"] = ""
    env["BOT_SQUAD_FLEET_DSTATE_MAX"] = ""
    env["BOT_SQUAD_FLEET_RSTATE_MAX"] = ""
    env["BOT_SQUAD_FLEET_PSI_MEM_MAX"] = ""
    env.update({k: str(v) for k, v in over.items()})
    return env


def test_the_admission_ticket_carries_every_reading(tmp_path):
    """The ticket is the calibration series for whoever tunes these next. It
    accrues without anyone remembering to sample, which is the only reason
    tonight's numbers exist at all."""
    state = tmp_path / "slots"
    out = subprocess.run([sys.executable, str(SLOT), "admit", "--note",
                          "T-1031 ticket contents", "--json"],
                         env=_env(state), capture_output=True, text=True,
                         timeout=90)
    assert out.returncode == 0, out.stderr
    rec = json.loads(out.stdout)
    for key in ("d_state", "r_state", "loadavg1", "psi_memory_full_avg10",
                "swap_free_pct", "committed_ratio", "mem_available_gb",
                "pswpin", "pswpout"):
        assert key in rec, f"{key} missing from the admission ticket"
    written = json.loads(
        (state / "admissions.ndjson").read_text().strip().splitlines()[-1])
    assert written["r_state"] == rec["r_state"]


def test_a_run_queue_refusal_reaches_a_lane_with_the_value(tmp_path):
    """A ceiling of -1 is unsatisfiable by construction, which is the point:
    the refusal path has to be observable from outside without waiting for the
    host to have a bad night."""
    state = tmp_path / "slots"
    out = subprocess.run([sys.executable, str(SLOT), "admit", "--note", "refuse me"],
                         env=_env(state, BOT_SQUAD_FLEET_RSTATE_MAX="-1"),
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 75
    assert "admission REFUSED" in out.stdout
    assert "r_state=" in out.stdout and "over the ceiling of -1" in out.stdout


def test_status_swap_rates_prints_the_values_and_gates_nothing(tmp_path):
    state = tmp_path / "slots"
    out = subprocess.run([sys.executable, str(SLOT), "status", "--swap-rates"],
                         env=_env(state), capture_output=True, text=True,
                         timeout=90)
    assert out.returncode == 0, out.stderr
    assert "si=" in out.stdout and "so=" in out.stdout
    assert "RECORDED, NOT GATED" in out.stdout


def test_ceiling_is_re_checked_at_the_actual_grant_not_only_at_request(
        tmp_path, monkeypatch):
    """T-1070 — fleet_slot ADMITTED a suite at d_state 44 against a ceiling of
    20. The ceiling itself was never broken: ``admission_check()`` refuses
    d_state=44 on demand every time it is asked (see the parametrized guards
    above). What was missing is that :func:`acquire` only ever asked it ONCE,
    before queueing for a free SLOT — whatever the capacity wait then cost was
    a window nothing re-checked, and a grant that only re-SAMPLES the readings
    for the log, rather than re-DECIDING on them, admits on a pass that already
    expired.

    Here a single containerless slot is held by lane A while the readings are
    healthy; lane B's own admission_check() also passes at that instant, and
    it queues behind A for capacity — genuinely queued, proven by its waiter
    file existing. Only once B is behind the same slot does the host degrade
    to the 22:57Z event (T-1031's own r_state=70 sample) and A's slot free.
    The grant point must re-decide on THAT reading, not the one B queued on.
    """
    mod = _module()
    state = tmp_path / "slots"
    state.mkdir(parents=True)
    (state / "config.json").write_text(json.dumps({"containerless_slots": 1}))
    for var in ("BOT_SQUAD_FLEET_DSTATE_MAX", "BOT_SQUAD_FLEET_RSTATE_MAX",
                "BOT_SQUAD_FLEET_PSI_MEM_MAX"):
        monkeypatch.delenv(var, raising=False)

    box = [dict(HEALTHY_PARKED)]
    monkeypatch.setattr(mod, "admission_readings", lambda sd, **kw: dict(box[0]))
    monkeypatch.setattr(mod, "_POLL", 0.05)

    token_a, adm_a = mod.acquire("containerless", pgid=os.getpgid(0), lane="A",
                                 note="holderA", wait=5, sd=state)
    assert adm_a["r_state"] == HEALTHY_PARKED["r_state"]

    waiters_dir = mod._waiters_dir(state)
    outcome: dict = {}

    def _second():
        try:
            outcome["result"] = mod.acquire(
                "containerless", pgid=os.getpgid(0), lane="B",
                note="holderB", wait=2, sd=state)
        except BaseException as exc:  # crosses the thread boundary
            outcome["exc"] = exc

    th = threading.Thread(target=_second)
    th.start()
    deadline = time.time() + 5
    while not any(waiters_dir.glob("*.json")) and time.time() < deadline:
        time.sleep(0.02)
    assert any(waiters_dir.glob("*.json")), "B never entered the wait queue"

    # The host degrades to the 22:57Z event at the EXACT moment A's slot
    # frees — the only moment a grant decision actually happens.
    box[0] = dict(EVENT_2257)
    mod.release(token_a, sd=state)
    th.join(timeout=10)

    assert isinstance(outcome.get("exc"), TimeoutError), (
        f"acquire() granted a slot on a reading it never re-checked at the "
        f"grant point: {outcome.get('result')}")
    assert "r_state=70" in str(outcome["exc"])
    assert "over the ceiling of 24" in str(outcome["exc"])
