"""T-0994 — the enforced fleet gate for container work and image builds.

The convention it replaces (``flock data/_worker/suite.lock``) was
unenforceable as written: it lived only in agent prompts, the two MANDATED
ways to run a suite (``bsq verify-isolated`` and the deploy recipe) both
bypassed it, and a deploy's hold was measured at 14.01 minutes with no holder,
no kind and no queue position visible to a waiter. Six suite processes ran
across three lanes at 17:49Z while a TL held it by hand, and nobody was
defying it.

These tests drive ``scripts/cli/fleet_slot.py`` as REAL OS PROCESSES, because
every property that matters here is a property of process groups and of the
filesystem, and an in-process fake would assert the model rather than the
surface.

**Overlap is detected by RENDEZVOUS, never by comparing wall-clock stamps.**
The first version of this file inferred overlap from recorded start/end times
and passed against a deliberately broken gate whenever the host was loaded
enough to delay the second launch past the first one's exit — a green produced
by a degraded host, which is the exact instrument failure this ticket is
about. Here the first child BLOCKS waiting for the second to announce itself,
so "did these two run at the same time" is answered by the children rather
than inferred from timing.

The order below is deliberate: **the healthy case runs first.** A gate first
validated against its hazardous case reports a defect it manufactured — the
sweep-form gate that opened 2026-09-06 fired 398 strays on a correct tree and
was caught only because a clean control ran ahead of it.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SLOT = REPO / "scripts" / "cli" / "fleet_slot.py"

pytestmark = pytest.mark.skipif(
    not SLOT.exists(), reason="fleet_slot.py not present in this tree")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _env(state: Path, slots: int | None = None, dstate_max: str | None = "") -> dict:
    """Environment for a gate invocation.

    ``dstate_max=""`` DISARMS the D-state ceiling, which is the right default
    for every test that is not about the ceiling: this suite runs on a shared,
    frequently-saturated box, and a test whose verdict depends on the host's
    D-state at that second measures the host, not the gate.

    The run-queue and memory-stall ceilings added by T-1031 are disarmed here
    for exactly the same reason and always: a peer's build or a co-tenant's
    memory event would otherwise decide these tests. The suite that exercises
    THOSE gates is test_t1031_memory_admission.py, which injects readings
    rather than hoping for a condition.
    """
    env = dict(os.environ)
    env["BOT_SQUAD_FLEET_SLOTS_DIR"] = str(state)
    # Never contend for the REAL fleet's stopgap semaphore from a test: a suite
    # that takes live slots throttles seven lanes to prove a point about itself.
    env["BOT_SQUAD_FLEET_LEGACY_SLOT_DIR"] = ""
    if slots is not None:
        env["BOT_SQUAD_FLEET_SLOTS"] = str(slots)
    else:
        env.pop("BOT_SQUAD_FLEET_SLOTS", None)
    if dstate_max is None:
        env.pop("BOT_SQUAD_FLEET_DSTATE_MAX", None)
    else:
        env["BOT_SQUAD_FLEET_DSTATE_MAX"] = dstate_max
    env["BOT_SQUAD_FLEET_RSTATE_MAX"] = ""
    env["BOT_SQUAD_FLEET_PSI_MEM_MAX"] = ""
    return env


def _run(state: Path, *argv: str, slots: int | None = None,
         dstate_max: str | None = "", timeout: float = 90):
    return subprocess.run([sys.executable, str(SLOT), *argv],
                          env=_env(state, slots, dstate_max), capture_output=True,
                          text=True, timeout=timeout)


def _status(state: Path, slots: int | None = None) -> dict:
    out = _run(state, "status", "--json", slots=slots)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _rendezvous_first(mine: Path, theirs: Path, window: float) -> list[str]:
    """A child that announces itself, then WAITS ``window`` seconds for a peer.

    Writes ``overlap`` into its marker iff the peer announced while it ran. The
    verdict is therefore a fact the child observed, not a timing inference:

    * gate WORKING  — the peer cannot start, this child waits the full window
      and records ``overlap: false``;
    * gate BROKEN   — the peer starts, this child sees it and EXITS EARLY with
      ``overlap: true``.

    ⚠ The window has to be generous, and the early exit is what makes that
    free. A short window fails the OVERLAP-EXPECTED direction on a loaded box:
    the peer's wrapper needs to sample admission, acquire and spawn, and if
    that takes longer than the window the first child records ``false`` and a
    correct gate reads as broken. That is the same host-dependence that made
    the timestamp version of this file green against a deliberately broken
    gate, surviving in the opposite direction — caught when
    ``test_a_build_does_NOT_block_containerless_work`` went red at D-state 16
    under seven-way contention.
    """
    code = (
        "import json,os,sys,time\n"
        "mine,theirs,w = sys.argv[1],sys.argv[2],float(sys.argv[3])\n"
        "open(mine+'.live','w').write('1')\n"
        "seen=False; end=time.time()+w\n"
        "while time.time()<end:\n"
        "    if os.path.exists(theirs+'.live'): seen=True; break\n"
        "    time.sleep(0.05)\n"
        "json.dump({'overlap':seen},open(mine,'w'))\n"
        "os.unlink(mine+'.live')\n"
    )
    return [sys.executable, "-c", code, str(mine), str(theirs), str(window)]


def _rendezvous_second(mine: Path) -> list[str]:
    """The peer: announce, linger briefly so the first can see it, exit."""
    code = (
        "import json,os,sys,time\n"
        "mine=sys.argv[1]\n"
        "open(mine+'.live','w').write('1')\n"
        "time.sleep(1.0)\n"
        "json.dump({'ran':True},open(mine,'w'))\n"
        "os.unlink(mine+'.live')\n"
    )
    return [sys.executable, "-c", code, str(mine)]


def _sleeper(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _spawn_run(state: Path, kind: str, cmd: list[str], *, note: str,
               slots: int | None = None, wait: float = 90,
               dstate_max: str | None = "",
               cl_slots: str | None = None) -> subprocess.Popen:
    env = _env(state, slots, dstate_max)
    if cl_slots is not None:
        env["BOT_SQUAD_FLEET_CONTAINERLESS_SLOTS"] = cl_slots
    return subprocess.Popen(
        [sys.executable, str(SLOT), "run", "--kind", kind, "--note", note,
         "--wait", str(wait), "--", *cmd],
        env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True)


def _wait_until(pred, timeout=30.0, poll=0.1):
    end = time.time() + timeout
    while time.time() < end:
        val = pred()
        if val:
            return val
        time.sleep(poll)
    return None


# ---------------------------------------------------------------------------
# 1. THE HEALTHY CASE — run it first
# ---------------------------------------------------------------------------

def test_healthy_single_run_acquires_runs_and_releases(tmp_path):
    """One run on a free pool: it must not block, and must leave nothing behind.

    If this is not green, every red below is uninterpretable — a gate that
    fails on the correct configuration cannot tell you anything about the
    incorrect one.
    """
    state = tmp_path / "slots"
    out = _run(state, "run", "--kind", "container", "--note", "healthy",
               "--", sys.executable, "-c", "print('ok')")
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
    assert "granted container slot" in out.stderr
    assert "released" in out.stderr

    snap = _status(state)
    assert snap["holders"] == [], snap
    assert snap["waiters"] == [], snap
    # The report says what it CHECKED, not only what it concluded.
    assert "process group" in snap["method"]
    assert "/proc" in snap["method"]


def test_run_forwards_the_child_returncode(tmp_path):
    """The gate must be transparent to the run's own verdict.

    A wrapper that swallows a non-zero rc turns a red suite into a green one —
    the exact class of instrument this ticket exists to remove.
    """
    state = tmp_path / "slots"
    out = _run(state, "run", "--", sys.executable, "-c", "raise SystemExit(3)")
    assert out.returncode == 3, out.stderr


def test_legacy_kind_names_still_work(tmp_path):
    """``suite``/``exclusive`` were the fleet's words for a day. Renaming a
    verb out from under seven live lanes is its own outage."""
    state = tmp_path / "slots"
    for alias, real in (("suite", "container"), ("exclusive", "build")):
        out = _run(state, "run", "--kind", alias, "--note", alias,
                   "--", sys.executable, "-c", "pass")
        assert out.returncode == 0, out.stderr
        assert f"granted {real} slot" in out.stderr


# ---------------------------------------------------------------------------
# 2. THE RED TEST T-0968 ASKED FOR: two container runs must not overlap
# ---------------------------------------------------------------------------

def test_two_container_runs_do_not_overlap_at_one_slot(tmp_path):
    """DoD: RED when two governed runs overlap.

    Decided by rendezvous — A waits 8s for B to announce itself. With the gate
    working B cannot start, so A records ``overlap: false`` however slow the
    box is.
    """
    state = tmp_path / "slots"
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    p1 = _spawn_run(state, "container", _rendezvous_first(a, b, 15.0),
                    note="A", slots=1)
    assert _wait_until(lambda: _status(state, 1)["holders"]), "A never took the slot"
    p2 = _spawn_run(state, "container", _rendezvous_second(b), note="B", slots=1)

    assert p1.wait(timeout=90) == 0
    assert p2.wait(timeout=90) == 0
    assert json.loads(a.read_text())["overlap"] is False, (
        "two container runs were live at the same time — the gate did not "
        "serialise them")
    # And B must have said WHY it was waiting: an opaque block is what made
    # lanes route around the old lock.
    err = p2.stderr.read()
    assert "WAITING" in err and "queue position" in err, err


def test_two_container_runs_DO_overlap_at_two_slots(tmp_path):
    """The pool must actually be a pool — the guard-can-fail proof.

    Without this, the test above is satisfied by a gate that serialises
    unconditionally, which is indistinguishable from one that ignores the slot
    count entirely. A control pinned only to the forbidden state cannot reject
    a design.
    """
    state = tmp_path / "slots"
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    p1 = _spawn_run(state, "container", _rendezvous_first(a, b, 60.0),
                    note="A", slots=2)
    assert _wait_until(lambda: _status(state, 2)["holders"]), "A never took a slot"
    p2 = _spawn_run(state, "container", _rendezvous_second(b), note="B", slots=2)

    assert p1.wait(timeout=90) == 0
    assert p2.wait(timeout=90) == 0
    assert json.loads(a.read_text())["overlap"] is True, (
        "two slots did not run concurrently — the pool size is not honoured")


def test_a_build_excludes_container_work(tmp_path):
    """An image build is an exclusive event; container work queues behind it.

    This is the shape in the ticket title — a deploy holding the gate while
    lanes have test work — and the second collapse was a co-tenant build
    layered on everyone else's runs.
    """
    state = tmp_path / "slots"
    a, b = tmp_path / "deploy.json", tmp_path / "suite.json"
    p1 = _spawn_run(state, "build", _rendezvous_first(a, b, 15.0),
                    note="staging deploy", slots=2)
    assert _wait_until(lambda: _status(state, 2)["build_held"]), "no build hold"
    p2 = _spawn_run(state, "container", _rendezvous_second(b),
                    note="api suite", slots=2)

    # While the build runs, the waiter is VISIBLE with its kind and its note.
    snap = _wait_until(lambda: (lambda s: s if s["waiters"] else None)(_status(state, 2)))
    assert snap, "the queued container run was not visible in status"
    assert snap["waiters"][0]["kind"] == "container"
    assert snap["waiters"][0]["note"] == "api suite"
    assert snap["holders"][0]["note"] == "staging deploy"

    assert p1.wait(timeout=90) == 0
    assert p2.wait(timeout=90) == 0
    assert json.loads(a.read_text())["overlap"] is False, (
        "container work ran DURING a build")
    assert "WAITING" in p2.stderr.read(), (
        "the peer never queued, so this proved nothing about exclusion")


def test_a_build_waits_for_container_work_to_drain(tmp_path):
    """And the reverse: a deploy must not build on top of running containers."""
    state = tmp_path / "slots"
    a, b = tmp_path / "suite.json", tmp_path / "deploy.json"
    p1 = _spawn_run(state, "container", _rendezvous_first(a, b, 15.0),
                    note="api suite", slots=2)
    assert _wait_until(lambda: _status(state, 2)["holders"])
    p2 = _spawn_run(state, "build", _rendezvous_second(b), note="deploy", slots=2)

    assert p1.wait(timeout=90) == 0
    assert p2.wait(timeout=90) == 0
    assert json.loads(a.read_text())["overlap"] is False, (
        "a build started while container work was running")
    assert "WAITING" in p2.stderr.read(), (
        "the peer never queued, so this proved nothing about exclusion")


def test_containerless_admit_takes_no_slot_and_a_build_does_not_block_it(tmp_path):
    """The exemption that keeps the rule satisfiable.

    A deploy exclusive of EVERY kind of testing is a deploy that gets routed
    around — which is how this ticket was born. A local-venv pytest contends
    for no daemon, no image and no container start, so it takes no slot and a
    build does not block it. It still records an admission ticket.
    """
    state = tmp_path / "slots"
    p1 = _spawn_run(state, "build", _sleeper(300), note="staging deploy", slots=2)
    assert _wait_until(lambda: _status(state, 2)["build_held"]), "no build hold"

    out = _run(state, "admit", "--note", "worker suite (containerless)", slots=2)
    assert out.returncode == 0, out.stderr
    assert "no slot taken" in out.stdout
    # It did not become a holder or a waiter.
    snap = _status(state, 2)
    assert [h["note"] for h in snap["holders"]] == ["staging deploy"]
    assert snap["waiters"] == []

    rec = json.loads((state / "admissions.ndjson").read_text().strip().splitlines()[-1])
    assert rec["kind"] == "containerless" and rec["slot"] is False
    os.killpg(os.getpgid(p1.pid), signal.SIGKILL)


# ---------------------------------------------------------------------------
# 3. THE FLOCK FAILURE THIS REPLACES
# ---------------------------------------------------------------------------

def test_killing_the_wrapper_alone_leaves_the_slot_held(tmp_path):
    """``flock`` releases when the fd-holder dies — and the fd-holder is not
    the work. On 2026-09-06 pid 497164 (pytest) outlived its dead ``flock``
    parent and held the fleet lock for 4m23s while ``/proc/locks`` named the
    dead parent, so every lane queued behind a process the kernel could not
    even attribute.

    Here liveness is the WORK's process group. Killing the wrapper alone must
    leave the slot HELD — the correct answer, because the child is still
    running and still burning the IO the gate exists to serialise — and the
    slot must free once the GROUP is killed.
    """
    state = tmp_path / "slots"
    p = _spawn_run(state, "container", _sleeper(120), note="long", slots=1)
    assert _wait_until(lambda: _status(state, 1)["holders"]), "never took the slot"
    held_pgid = _status(state, 1)["holders"][0]["pgid"]
    assert held_pgid != os.getpgid(p.pid), (
        "the slot is attributed to the WRAPPER's group — a killed wrapper would "
        "then free a slot whose work is still running")

    # Kill the wrapper ONLY. Its child is in its own session and survives.
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=10)
    time.sleep(0.5)

    snap = _status(state, 1)
    assert len(snap["holders"]) == 1, (
        "the slot was released while the work was still running — this is "
        "exactly the flock defect, reproduced in its replacement")
    assert snap["holders"][0]["pgid"] == held_pgid

    # And a would-be run genuinely cannot get in.
    denied = _run(state, "run", "--kind", "container", "--wait", "2", "--",
                  sys.executable, "-c", "pass", slots=1)
    assert denied.returncode == 75, denied.stderr
    assert "NOT MEASURED" in denied.stderr

    # Killing the GROUP releases it — on the record.
    os.killpg(held_pgid, signal.SIGKILL)
    assert _wait_until(lambda: not _status(state, 1)["holders"], timeout=30), (
        "killing the work's process group did not free the slot")

    log = (state / "reclaims.log").read_text().strip().splitlines()
    assert log, "the reclaim was silent — a stale slot must be reclaimed ON THE RECORD"
    rec = json.loads(log[-1])
    assert rec["pgid"] == held_pgid
    assert rec["note"] == "long"
    assert rec["why"] == "holder process group gone"


def test_a_holder_whose_group_never_existed_is_reclaimed(tmp_path):
    """DoD 3: a lane that dies holding the gate must not deadlock the fleet.

    Planted by hand rather than by killing a real run, so the reclaim path is
    exercised even where no process can be made to die on cue.
    """
    state = tmp_path / "slots"
    holders = state / "holders"
    holders.mkdir(parents=True)
    dead = int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 1
    (holders / "zzz.json").write_text(json.dumps({
        "token": "zzz", "kind": "build", "pgid": dead,
        "lane": "S-gone", "note": "dead deploy", "since": time.time() - 900,
    }))

    assert _status(state, 1)["holders"] == [], "a dead holder was not reclaimed"
    rec = json.loads((state / "reclaims.log").read_text().strip().splitlines()[-1])
    assert rec["token"] == "zzz" and rec["held_for_s"] >= 900

    out = _run(state, "run", "--wait", "5", "--", sys.executable, "-c", "print('after')")
    assert out.returncode == 0, out.stderr
    assert "after" in out.stdout


# ---------------------------------------------------------------------------
# 4. ADMISSION — D-state is the gate; the disk metric was tested and rejected
# ---------------------------------------------------------------------------

def test_d_state_ceiling_refuses_and_prints_the_value(tmp_path):
    """D-state is the only one of the three proposed readings that moved with
    the condition: 3 quiet, 10-13 working, 76-89 during a known heavy build.

    A ceiling of -1 is unsatisfiable by construction, which is the point: the
    refusal path must be exercised, and a healthy box cannot exercise it. The
    refusal must print the VALUE it refused on — a lane told only "refused"
    cannot distinguish a real saturation from a gate it can never satisfy, and
    that is how the last three gates in this area came to be routed around
    instead of reported.
    """
    state = tmp_path / "slots"
    out = _run(state, "run", "--kind", "container", "--note", "blocked",
               "--wait", "3", "--", sys.executable, "-c", "print('MUST NOT RUN')",
               dstate_max="-1")
    assert out.returncode == 75, out.stderr
    assert "MUST NOT RUN" not in out.stdout, "the gate refused but ran anyway"
    assert "admission REFUSED" in out.stderr
    assert "over the ceiling of -1" in out.stderr
    assert "d_state=" in out.stderr, "refused without printing the reading"
    # It WAITED and re-sampled rather than refusing once and giving up: a rule
    # a lane has to hand-poll is a rule that becomes optional.
    assert "WAITING on admission" in out.stderr
    # And it tells the lane how to REPORT an unsatisfiable gate rather than
    # leaving it to route around one.
    assert "FINDING" in out.stderr and "bsq peer send" in out.stderr


def test_a_healthy_ceiling_admits(tmp_path):
    """The guard-can-fail proof's other half: with a satisfiable ceiling the
    same command runs. Run the healthy case, or a detector that refuses
    everything reads as a working gate."""
    state = tmp_path / "slots"
    out = _run(state, "run", "--kind", "container", "--note", "ok",
               "--", sys.executable, "-c", "print('RAN')", dstate_max="100000")
    assert out.returncode == 0, out.stderr
    assert "RAN" in out.stdout
    assert "within the ceiling of 100000" in out.stderr


def test_admission_ticket_is_recorded_with_every_run(tmp_path):
    """p651's practice, adopted as standard: the run carries its own admission
    ticket rather than the runner's assurance that conditions were fine."""
    state = tmp_path / "slots"
    out = _run(state, "run", "--note", "ticketed", "--", sys.executable, "-c", "pass")
    assert out.returncode == 0, out.stderr
    assert "admission: d_state=" in out.stderr
    assert "at-grant admission:" in out.stderr
    assert "load DESCRIBES, never decides" in out.stderr

    lines = (state / "admissions.ndjson").read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["granted"] is True and rec["slot"] is True
    assert isinstance(rec["d_state"], int)
    # The disqualified metric is NOT sampled on the run path: it costs five
    # seconds per run and buys nothing.
    assert "disk_queue_series" not in rec


def test_disk_series_is_available_on_demand_and_never_gates(tmp_path):
    """The metric was TESTED AND REJECTED for admission control — recorded here
    because an instrument that was checked and rejected is worth more than one
    nobody checked.

    Eight 12-sample series on this box: healthy medians 15-41.5, and during a
    known heavy build that drove D-state 3 -> 89 the medians were 22, 39, 34
    and the co-tenant's stage-tagged 28/18/25/30/19 — inside, and mostly below,
    the healthy range. A metric that can be flat while D-state goes to 89
    cannot authorise anything. So it stays sampleable, and gates nothing.
    """
    state = tmp_path / "slots"
    out = _run(state, "admit", "--note", "evidence", "--disk-series", "--json")
    assert out.returncode == 0, out.stderr
    rec = json.loads(out.stdout)
    assert isinstance(rec["disk_queue_series"], list)
    assert rec["disk_queue_median"] is not None
    assert "NOT GATED" in rec["disk_note"]

    # A huge disk reading cannot refuse anything, because nothing reads it as a
    # threshold. Three readings are enforced — D-state, r_state and PSI memory
    # (T-1031) — and this is not one of them.
    (state / "config.json").write_text(json.dumps({"disk_queue_max": 0, "slots": 1}))
    ran = _run(state, "run", "--note", "still runs", "--disk-series",
               "--", sys.executable, "-c", "print('RAN')")
    assert ran.returncode == 0, ran.stderr
    assert "RAN" in ran.stdout


# ---------------------------------------------------------------------------
# 5. THE INSTRUMENTS
# ---------------------------------------------------------------------------

def test_pgid_is_parsed_past_a_comm_containing_spaces_and_parens(tmp_path):
    """``/proc/<pid>/stat`` field 2 is the command name, unescaped.

    Three lanes independently shipped the same column-index defect against
    ``/proc/locks`` on 2026-09-06, and the only thing that stopped it
    manufacturing kill targets was a numeric filter one lane had added for
    tidiness. Anchor on the LAST ``)``, never on whitespace.
    """
    sys.path.insert(0, str(SLOT.parent))
    try:
        import fleet_slot  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    assert fleet_slot._pgid_of(os.getpid()) == os.getpgrp()
    assert fleet_slot._pgid_of(2 ** 30) is None

    # A comm with a space and a close-paren, which a split()-based parser
    # would shift by two fields.
    raw = "4242 (my ) proc) S 1 9999 4242 0 -1 0 0 0 0\n"
    close = raw.rfind(")")
    assert raw[close + 2:].split()[2] == "9999", "the anchored parse is itself wrong"

    # And the median helper reports the SERIES, so a later reader can re-judge
    # the threshold rather than inheriting a verdict.
    s = fleet_slot.disk_queue_series(samples=3, span=0.1)
    assert s["n"] == len(s["series"])
    if s["series"]:
        assert s["min"] <= s["median"] <= s["max"]


def test_status_reports_the_queue_and_its_method(tmp_path):
    """A waiter needs to know WHO holds the gate, of what KIND, and for HOW
    LONG. The bare flock published none of those, which is why a 14-minute
    deploy hold and a 90-second one were indistinguishable from behind it."""
    state = tmp_path / "slots"
    p = _spawn_run(state, "build", _sleeper(300),
                   note="T-0974 staging deploy", slots=1)
    snap = _wait_until(lambda: (lambda s: s if s["holders"] else None)(_status(state, 1)))
    assert snap
    h = snap["holders"][0]
    assert h["kind"] == "build"
    assert h["note"] == "T-0974 staging deploy"
    assert isinstance(h["pgid"], int) and h["pgid"] > 0
    assert h["age_s"] >= 0
    assert snap["build_held"] is True

    text = _run(state, "status", slots=1).stdout
    assert "HOLD" in text and "build" in text and "T-0974 staging deploy" in text
    assert "checked:" in text
    os.killpg(os.getpgid(p.pid), signal.SIGKILL)


def test_timeout_is_reported_as_not_measured(tmp_path):
    """A timeout is not a result. Print it as NOT MEASURED from the runner
    rather than remembering it at report time."""
    state = tmp_path / "slots"
    p = _spawn_run(state, "build", _sleeper(30.0), note="build", slots=1)
    assert _wait_until(lambda: _status(state, 1)["holders"])
    out = _run(state, "run", "--kind", "container", "--wait", "1", "--",
               sys.executable, "-c", "print('MUST NOT RUN')", slots=1)
    assert out.returncode == 75
    assert "NOT MEASURED" in out.stderr
    assert "MUST NOT RUN" not in out.stdout
    # A refusal names the holder, so the waiter can decide rather than guess.
    assert "build in progress" in out.stderr
    os.killpg(os.getpgid(p.pid), signal.SIGKILL)


def test_slots_config_file_is_read_without_a_restart(tmp_path):
    """The pool size is operator policy, not a code constant.

    The measured ceiling that collapsed the host was ~15 concurrent docker runs
    on a rotating disk; the binding number is 2-3 test containers fleet-wide.
    Whoever sets it must not need a deploy.
    """
    state = tmp_path / "slots"
    state.mkdir(parents=True)
    assert _status(state)["slots"] == 2
    (state / "config.json").write_text(json.dumps({"slots": 3}))
    assert _status(state)["slots"] == 3
    # A malformed config must not silently widen the fleet.
    (state / "config.json").write_text("{not json")
    assert _status(state)["slots"] == 2


# ---------------------------------------------------------------------------
# 6. THE EXEMPTION MUST STILL BE BOUNDED (p673, 19:03Z)
# ---------------------------------------------------------------------------

def test_containerless_work_is_bounded_by_its_own_pool(tmp_path):
    """An exemption without a reservation is not a rule.

    Measured: seven concurrent containerless pytest process groups from five
    lanes at 19:04Z, every one of which had read a passing D-state and every
    one of which was right at the moment it read it. **D-state is a lagging
    CONSEQUENCE of concurrency, not a reservation** — so a rule whose
    satisfiable condition is a health reading cannot bound the thing it means
    to bound. That is the T-0994 shape one level down.
    """
    state = tmp_path / "slots"
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    p1 = _spawn_run(state, "containerless", _rendezvous_first(a, b, 15.0),
                    note="suite A", cl_slots="1")
    assert _wait_until(lambda: _status(state)["containerless_held"] == 1)
    p2 = _spawn_run(state, "containerless", _rendezvous_second(b),
                    note="suite B", cl_slots="1")

    assert p1.wait(timeout=90) == 0
    assert p2.wait(timeout=90) == 0
    assert json.loads(a.read_text())["overlap"] is False, (
        "the containerless exemption admitted unbounded parallelism")
    assert "WAITING" in p2.stderr.read(), (
        "the peer never queued, so this proved nothing about the bound")


def test_a_build_does_NOT_block_containerless_work(tmp_path):
    """The constraint that keeps the whole rule satisfiable.

    "Whatever replaces it cannot make a deploy exclusive of all testing, or it
    will be routed around again." A build drains the CONTAINER pool and leaves
    the containerless pool alone.
    """
    state = tmp_path / "slots"
    a, b = tmp_path / "deploy.json", tmp_path / "suite.json"
    p1 = _spawn_run(state, "build", _rendezvous_first(a, b, 60.0),
                    note="staging deploy", slots=2)
    assert _wait_until(lambda: _status(state, 2)["build_held"]), "no build hold"
    p2 = _spawn_run(state, "containerless", _rendezvous_second(b),
                    note="worker suite", slots=2, cl_slots="4")

    assert p1.wait(timeout=90) == 0
    assert p2.wait(timeout=90) == 0
    assert json.loads(a.read_text())["overlap"] is True, (
        "a build blocked containerless work — that exclusivity is what gets "
        "routed around")


def test_containerless_slots_can_be_disarmed(tmp_path):
    """The bound is a policy knob, and 3 is a bound rather than a calibration:
    nobody has measured where the containerless knee is. Whoever measures it
    must be able to change or remove it without a code change."""
    state = tmp_path / "slots"
    state.mkdir(parents=True)
    assert _status(state)["containerless_slots"] == 3
    (state / "config.json").write_text(json.dumps({"containerless_slots": None}))
    assert _status(state)["containerless_slots"] is None

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    p1 = _spawn_run(state, "containerless", _rendezvous_first(a, b, 60.0), note="A")
    assert _wait_until(lambda: _status(state)["containerless_held"] == 1)
    p2 = _spawn_run(state, "containerless", _rendezvous_second(b), note="B")
    assert p1.wait(timeout=90) == 0
    assert p2.wait(timeout=90) == 0
    assert json.loads(a.read_text())["overlap"] is True, "disarming had no effect"


def test_check_and_claim_is_atomic_under_a_real_race(tmp_path):
    """A count-before-start has a TOCTOU window two lanes can pass at once.

    The operator's point when he replaced the health reading with a semaphore:
    "a count-before-start has a TOCTOU race and two lanes can pass it
    simultaneously; a held fd cannot be double-taken." Here the equivalent
    property is that the reap-count-and-claim happens inside ONE registry
    flock. This test launches six wrappers with no stagger at all and has each
    child SAMPLE how many peers are live alongside it, so the ceiling is
    checked under genuine contention rather than in the serialised order the
    other tests arrange.
    """
    state = tmp_path / "slots"
    live = tmp_path / "live"
    live.mkdir(parents=True)
    code = (
        "import json,os,sys,time\n"
        "d,out = sys.argv[1],sys.argv[2]\n"
        "me = os.path.join(d, str(os.getpid()))\n"
        "open(me,'w').write('1')\n"
        "peak = 0\n"
        "for _ in range(20):\n"
        "    peak = max(peak, len(os.listdir(d)))\n"
        "    time.sleep(0.05)\n"
        "os.unlink(me)\n"
        "json.dump({'peak':peak}, open(out,'w'))\n"
    )
    procs, outs = [], []
    for i in range(6):
        o = tmp_path / f"peak{i}.json"
        outs.append(o)
        procs.append(_spawn_run(
            state, "containerless",
            [sys.executable, "-c", code, str(live), str(o)],
            note=f"racer{i}", cl_slots="2"))
    for pr in procs:
        assert pr.wait(timeout=120) == 0, pr.stderr.read()

    peaks = [json.loads(o.read_text())["peak"] for o in outs]
    assert max(peaks) <= 2, (
        f"the pool was exceeded under a real race: peak concurrency {max(peaks)} "
        f"with a ceiling of 2 (per-child peaks {peaks})")
    # And it is not trivially green because nothing ever ran concurrently:
    # with six racers and two slots, somebody must have shared.
    assert max(peaks) == 2, (
        f"no two runs ever overlapped, so this did not test the ceiling "
        f"(per-child peaks {peaks})")


def test_containerless_run_also_holds_a_legacy_stopgap_fd(tmp_path):
    """One ceiling, not two.

    The operator created ``containerless-{1,2,3}.lock`` by hand and broadcast a
    raw ``flock -n`` snippet over them. A lane using that snippet and a lane
    using this module hold DIFFERENT files, so the effective ceiling becomes
    3 + pool rather than 3 — two semaphores over one resource, which is the
    failure the semaphore was introduced to remove (p673). So a containerless
    run here takes one of those fds too, until the stopgap is retired.
    """
    state = tmp_path / "slots"
    legacy = tmp_path / "legacy"
    legacy.mkdir(parents=True)
    for i in (1, 2):
        (legacy / f"containerless-{i}.lock").touch()

    env = _env(state)
    env["BOT_SQUAD_FLEET_LEGACY_SLOT_DIR"] = str(legacy)
    out = subprocess.run(
        [sys.executable, str(SLOT), "run", "--kind", "containerless",
         "--note", "interop", "--", sys.executable, "-c", "print('RAN')"],
        env=env, capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr
    assert "RAN" in out.stdout
    assert "also holding legacy" in out.stderr
    assert "containerless-1.lock" in out.stderr

    # Retiring the stopgap is a config change, not a code change.
    env["BOT_SQUAD_FLEET_LEGACY_SLOT_DIR"] = ""
    out2 = subprocess.run(
        [sys.executable, str(SLOT), "run", "--kind", "containerless",
         "--note", "no-interop", "--", sys.executable, "-c", "print('RAN')"],
        env=env, capture_output=True, text=True, timeout=90)
    assert out2.returncode == 0, out2.stderr
    assert "also holding legacy" not in out2.stderr
