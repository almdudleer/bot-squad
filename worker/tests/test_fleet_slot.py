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

def _env(state: Path, slots: int | None = None, dstate_max: str | None = "",
         per_lane: str | None = "") -> dict:
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

    ``per_lane=""`` DISARMS the one-suite-per-lane cap, and must stay the
    default for the same class of reason with a sharper edge: every process
    this suite spawns inherits the SAME ``$BOT_SQUAD_SID``, so to the gate they
    are all ONE LANE. Armed by default, the cap would refuse the second run in
    every multi-run test here and those tests would go red against a correct
    gate. The cap's own tests arm it explicitly and pass distinct ``--lane``
    values, which is the only way to fixture a per-lane rule honestly.
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
    if per_lane is None:
        env.pop("BOT_SQUAD_FLEET_PER_LANE", None)
    else:
        env["BOT_SQUAD_FLEET_PER_LANE"] = per_lane
    return env


def _run(state: Path, *argv: str, slots: int | None = None,
         dstate_max: str | None = "", timeout: float = 90,
         per_lane: str | None = ""):
    return subprocess.run([sys.executable, str(SLOT), *argv],
                          env=_env(state, slots, dstate_max, per_lane),
                          capture_output=True, text=True, timeout=timeout)


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
               cl_slots: str | None = None,
               per_lane: str | None = "",
               lane: str | None = None) -> subprocess.Popen:
    env = _env(state, slots, dstate_max, per_lane)
    if cl_slots is not None:
        env["BOT_SQUAD_FLEET_CONTAINERLESS_SLOTS"] = cl_slots
    lane_argv = ["--lane", lane] if lane is not None else []
    return subprocess.Popen(
        [sys.executable, str(SLOT), "run", "--kind", kind, "--note", note,
         *lane_argv, "--wait", str(wait), "--", *cmd],
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


def _wait_for_adopted_pgid(state: Path, wrapper_pid: int, *,
                           slots: int | None = None, timeout: float = 30.0) -> int:
    """The holder's pgid, but only once it is the WORK's — not the wrapper's.

    ``acquire()`` writes the holder record with the wrapper's own pgid (it has
    no other pgid yet); ``_cmd_run`` overwrites it via ``adopt()`` only after
    ``Popen`` returns. That gap is real (a process spawn), and a status query
    landing inside it hands back the wrapper's group — the exact wrong value
    this fix exists to stop killing. So poll until the reading diverges from
    the wrapper's own group rather than trusting the first non-empty holder.
    """
    wrapper_pgid = os.getpgid(wrapper_pid)
    end = time.time() + timeout
    last = None
    while time.time() < end:
        holders = _status(state, slots)["holders"]
        if holders:
            pgid = holders[0]["pgid"]
            last = pgid
            if pgid and pgid != wrapper_pgid:
                return pgid
        time.sleep(0.05)
    raise AssertionError(
        f"holder pgid never adopted away from the wrapper's group {wrapper_pgid} "
        f"(last read {last}) — adopt() did not land within {timeout}s")


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
    # Kill the WORK's group, read from the holder record — not the wrapper's
    # (_cmd_run spawns the work with its own start_new_session=True, so the
    # wrapper's group is a different, harmless one to kill).
    os.killpg(_wait_for_adopted_pgid(state, p1.pid, slots=2), signal.SIGKILL)


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
    # Kill the WORK's group, not the wrapper's — re-read rather than reuse
    # h["pgid"]: that first read can land before adopt() overwrites it (see
    # _wait_for_adopted_pgid).
    os.killpg(_wait_for_adopted_pgid(state, p.pid, slots=1), signal.SIGKILL)


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
    # Kill the WORK's group, not the wrapper's — see _wait_for_adopted_pgid.
    os.killpg(_wait_for_adopted_pgid(state, p.pid, slots=1), signal.SIGKILL)


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
    child OBSERVE its peers, so the ceiling is checked under genuine
    contention rather than in the serialised order the other tests arrange.

    ⚠ **WHAT THIS TEST DOES NOT ESTABLISH.** It proves the pool ceiling is not
    exceeded and that the check-and-claim is atomic. It says NOTHING about
    whether gating reduces contention for real work — that is a wall-to-CPU
    measurement on a real suite, not a property of this file, and this result
    must not be reported alongside one. A green here is a correctness result
    about the arbiter, and nothing else.

    ⚠⚠ **REPAIRED 2026-09-07, AND THE DEFECT IS WORTH KEEPING WRITTEN DOWN.**
    The anti-tautology assertion below used to be driven by a FIXED ONE-SECOND
    SAMPLING WINDOW: each child took 20 samples at 50ms and then exited, so
    "no two runs ever overlapped" got concluded whenever the host was slow
    enough that a peer had not started yet. Host-load-dependent BY
    CONSTRUCTION — and it is the SAME DEFECT as the timestamp-derived overlap
    detection replaced elsewhere in this file the day before, surviving in
    sampling form. It matters because it is silent in the dangerous
    direction: a fixed window can only ever UNDER-report overlap, so the
    CEILING assertion stays green while the assertion that gives it meaning
    goes red for reasons that have nothing to do with the code.

    The repair is the same one: the child WAITS to be joined, up to a generous
    deadline, and exits the moment it sees a peer.

    ⚠ **THE GENEROSITY IS ONLY FREE FOR A RACER THAT GETS JOINED**, which is
    not every racer and was measured rather than assumed: six racers over two
    slots do not pair up evenly, one ends up running alone, and on the first
    green run it paid 61s of a 62s test. So a racer also stops once ANY racer
    has recorded an overlap — the question the window exists to answer is
    already answered, and waiting longer cannot change the verdict. The full
    window survives only for the case where nothing has overlapped yet, which
    is the genuinely RED path and the one worth being patient about.

    ⚠⚠ **AND THE SHORT-CIRCUIT NEEDED A FLOOR, WHICH THE FIRST VERSION OF IT
    DID NOT HAVE.** Exiting the instant the verdict was settled took the test
    from 62s to 11s and simultaneously gutted it: five of six racers then
    exited at 0.0s, and the CEILING assertion can only witness a violation
    while children are alive TOGETHER. A runtime optimisation had quietly made
    the strongest assertion in this test sample almost nothing. Each racer now
    dwells at least a second before leaving, which keeps the pool genuinely
    occupied while still costing seconds rather than a minute.
    """
    state = tmp_path / "slots"
    live = tmp_path / "live"
    live.mkdir(parents=True)
    # Announce, then WAIT to be joined -- do not sample a fixed window and walk
    # away. saw_peer is reported as its own field rather than left for the
    # caller to infer from peak == 2: a verdict that has to be derived from a
    # number is a verdict that gets derived wrongly later.
    code = (
        "import json,os,sys,time\n"
        "d,out,w = sys.argv[1],sys.argv[2],float(sys.argv[3])\n"
        "me = os.path.join(d, str(os.getpid()))\n"
        "open(me,'w').write('1')\n"
        "flag = os.path.join(os.path.dirname(out), 'SEEN')\n"
        "peak = 0; saw = False; t0 = time.time(); end = t0+w\n"
        "while time.time() < end:\n"
        "    n = len(os.listdir(d))\n"
        "    peak = max(peak, n)\n"
        "    if n >= 2:\n"
        "        saw = True; open(flag,'w').write('1')\n"
        # DWELL. Do not leave the moment the verdict is settled: the CEILING
        # assertion can only observe a violation while children are alive
        # together, so a racer that exits instantly reduces the exposure of
        # the safety property to nearly nothing. Cutting the 61s tail is worth
        # doing; cutting it to 0.0s quietly turned the strongest assertion
        # here into one that barely samples. Stay a beat, then go.
        "    if (saw or os.path.exists(flag)) and time.time()-t0 >= 1.0:\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "os.unlink(me)\n"
        # waited_s is recorded but NOT asserted on. It is the evidence that the
        # old fixed 1.0s window was too short on THIS host, and asserting a
        # bound on it would reintroduce exactly the host-dependence being
        # removed. Measure it, report it, never gate on it.
        "json.dump({'peak':peak,'saw_peer':saw,\n"
        "           'waited_s':round(time.time()-t0,2),\n"
        "           'peer_seen_by_someone':os.path.exists(flag)}, open(out,'w'))\n"
    )
    procs, outs = [], []
    for i in range(6):
        o = tmp_path / f"peak{i}.json"
        outs.append(o)
        procs.append(_spawn_run(
            state, "containerless",
            [sys.executable, "-c", code, str(live), str(o), "60.0"],
            note=f"racer{i}", cl_slots="2", wait=300))
    for pr in procs:
        assert pr.wait(timeout=600) == 0, pr.stderr.read()

    recs = [json.loads(o.read_text()) for o in outs]
    peaks = [r["peak"] for r in recs]
    # THE CEILING. The safety property, and the one that must never be relaxed
    # to make the suite green.
    assert max(peaks) <= 2, (
        f"the pool was exceeded under a real race: peak concurrency {max(peaks)} "
        f"with a ceiling of 2 (per-child peaks {peaks})")
    # THE ANTI-TAUTOLOGY. A ceiling assertion is trivially satisfied by a run
    # in which nothing was ever concurrent, so at least one racer must have
    # OBSERVED a peer rather than merely failed to see one inside a window.
    assert any(r["saw_peer"] for r in recs), (
        f"no racer ever observed a peer, so the ceiling was never under "
        f"pressure and this proves nothing about atomicity (per-child peaks "
        f"{peaks}). Each racer waited up to 60s to be joined, so this is a "
        f"REAL absence of overlap rather than a sampling artefact: either the "
        f"pool granted one slot at a time, or the wrappers never got that far")


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


# ---------------------------------------------------------------------------
# 6. ONE SUITE PER LANE — T-0968 DoD 2
#
# The ceiling bounds the HOST; this bounds one lane's share of it. They are
# genuinely two controls: with three containerless slots, one lane running
# three suites never touches the ceiling and has still starved four peers to
# zero. Every test here arms the cap explicitly (``per_lane="1"``) and passes
# distinct ``--lane`` values, because a per-lane rule fixtured with one lane
# label cannot tell "the cap works" from "the ceiling works".
# ---------------------------------------------------------------------------

def _fleet_slot_module():
    sys.path.insert(0, str(SLOT.parent))
    try:
        import fleet_slot  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    return fleet_slot


def _holder(kind: str, lane: str, token: str = "t") -> dict:
    return {"token": token, "kind": kind, "lane": lane, "note": kind,
            "pgid": 1234, "since": time.time()}


def test_the_cap_does_NOT_serialise_DIFFERENT_lanes(tmp_path):
    """THE HEALTHY CASE, FIRST: two lanes still run at once under the cap.

    This is the control that makes the next test mean something. A per-lane cap
    implemented against the wrong key — the note, the pgid, an empty lane label
    — reduces the fleet to one suite and every "the cap blocks a second run"
    assertion below stays green while it does. So prove the fleet still
    parallelises BEFORE proving the cap bites.
    """
    state = tmp_path / "slots"
    a, b = tmp_path / "a.json", tmp_path / "b.json"

    # Two slots, two DIFFERENT lanes, cap of 1 each: both must run together.
    first = _spawn_run(state, "container", _rendezvous_first(a, b, 60.0),
                       note="lane-a", slots=2, per_lane="1", lane="LANE-A")
    second = _spawn_run(state, "container", _rendezvous_second(b),
                        note="lane-b", slots=2, per_lane="1", lane="LANE-B")
    try:
        assert first.wait(timeout=120) == 0
        assert second.wait(timeout=120) == 0
    finally:
        for p in (first, second):
            if p.poll() is None:
                p.kill()

    assert json.loads(a.read_text())["overlap"] is True, (
        "two DIFFERENT lanes did not overlap under a per-lane cap of 1 — the "
        "cap is keyed on something that is the same for both, which turns a "
        "per-lane rule into a fleet-wide serialiser")


def test_one_lane_cannot_take_two_slots_in_a_pool(tmp_path):
    """The clause itself: the SAME lane's second run WAITS, and says why.

    Two slots are FREE for the fleet the whole time this blocks, which is the
    point — the refusal has to come from the lane's own share, not from the
    ceiling. So the reason string is asserted too: a wait produced by the
    ceiling would be indistinguishable from this one by timing alone.
    """
    state = tmp_path / "slots"
    a, b = tmp_path / "a.json", tmp_path / "b.json"

    first = _spawn_run(state, "container", _rendezvous_first(a, b, 15.0),
                       note="suite-1", slots=2, per_lane="1", lane="LANE-A")
    second = _spawn_run(state, "container", _rendezvous_second(b),
                        note="suite-2", slots=2, per_lane="1", lane="LANE-A")
    try:
        assert first.wait(timeout=120) == 0
        assert second.wait(timeout=120) == 0
        err = second.stderr.read()
    finally:
        for p in (first, second):
            if p.poll() is None:
                p.kill()

    assert json.loads(a.read_text())["overlap"] is False, (
        "one lane held two slots at once — the per-lane clause did not bind")
    # It WAITED rather than skipping, and it SAID SO naming the clause. A gate
    # that blocks silently is the gate lanes routed around (T-0968 DoD 1).
    assert "one suite per lane" in err, err
    assert "LANE-A" in err, err
    # And it eventually ran: the cap is a queue, not a refusal.
    assert json.loads(b.read_text())["ran"] is True


def test_an_unlabelled_lane_is_exempt_and_the_reason_says_so(tmp_path):
    """The documented hole, pinned as a POSITIVE test.

    Every unlabelled run shares the label ``""``. Counting those as one lane
    would serialise the fleet to one suite the first time a caller forgot
    ``--lane``, so they are exempt — deliberately, and therefore worth a test
    that fails if someone "tightens" it without noticing what it costs.
    """
    fs = _fleet_slot_module()
    held = [_holder("containerless", "")]
    ok, reason = fs._grantable("containerless", held, [], "me", 2, 3,
                               lane="", per_lane=1)
    assert ok is True, reason
    assert "one suite per lane" not in reason


def test_the_cap_is_per_pool_not_global(tmp_path):
    """A lane holding containerless may still take a container slot.

    Derived, not preferred: a GLOBAL cap creates a starvation class this one
    does not have. A ``build`` waiter forms the FIFO barrier, so under a global
    cap a lane holding a 20-minute containerless slot could queue its own
    deploy and stall EVERY other lane's container work behind a barrier that
    cannot clear until its own unrelated run ends. Per-pool adds no such class:
    a build blocked by its own lane's container slot was already blocked by "a
    build drains the pool".
    """
    fs = _fleet_slot_module()
    held = [_holder("containerless", "LANE-A")]

    ok, reason = fs._grantable("container", held, [], "me", 2, 3,
                               lane="LANE-A", per_lane=1)
    assert ok is True, f"a containerless hold blocked container work: {reason}"

    # ...and the same lane is still capped WITHIN its own pool.
    ok2, reason2 = fs._grantable("containerless", held, [], "me", 2, 3,
                                 lane="LANE-A", per_lane=1)
    assert ok2 is False
    assert "one suite per lane" in reason2

    # A build is unaffected by the term — it was already exclusive of the pool.
    ok3, reason3 = fs._grantable("build", held, [], "me", 2, 3,
                                 lane="LANE-A", per_lane=1)
    assert ok3 is True, reason3


def test_the_cap_can_be_disarmed_and_status_says_which(tmp_path):
    """Disarming is a config flip, and the status line states it either way.

    A cap that is off is a policy decision a reader should SEE. The status
    header printing nothing when disarmed would make an armed and a disarmed
    fleet byte-identical to look at, which is the defect this suite already
    caught once on the admission pass line.
    """
    fs = _fleet_slot_module()
    held = [_holder("containerless", "LANE-A")]
    ok, reason = fs._grantable("containerless", held, [], "me", 2, 3,
                               lane="LANE-A", per_lane=None)
    assert ok is True, reason

    state = tmp_path / "slots"
    armed = _run(state, "status", per_lane="2")
    assert armed.returncode == 0, armed.stderr
    assert "per lane: 2 slot(s) per pool" in armed.stdout, armed.stdout

    off = _run(state, "status", per_lane="0")
    assert off.returncode == 0, off.stderr
    assert "per lane: DISARMED" in off.stdout, off.stdout


# ---------------------------------------------------------------------------
# 7. NESTING — a slot inside a slot must not queue behind its own parent
#
# The per-lane cap made this sharp, but it did not create it: "build in
# progress" already refused container work, so a gated suite inside a deploy's
# build slot would have hung before DoD 2 existed. It became worth closing when
# the AGENT_INSTRUCTIONS recipes started wrapping docker runs in the gate,
# because that is what turns a latent nesting hazard into a live one.
# ---------------------------------------------------------------------------

def test_covers_is_conservative_about_which_slot_excuses_which(tmp_path):
    """The coverage matrix, stated as a test so it cannot drift into folklore.

    The asymmetry is the point and it is not a preference: a ``build`` is
    EXCLUSIVE of the daemon pool, so a container run inside one consumes
    nothing that was not already reserved. A ``containerless`` slot reserves
    CPU and page cache and reserves NO container start, so letting it excuse
    one would exempt precisely the resource the gate exists to bound.
    """
    fs = _fleet_slot_module()

    # Reflexive.
    for k in ("container", "build", "containerless"):
        assert fs.covers(k, k) is True, k
    # A build owns the whole daemon pool, so it covers container work.
    assert fs.covers("build", "container") is True
    # Nothing else covers anything — especially not across pools.
    assert fs.covers("container", "build") is False
    assert fs.covers("containerless", "container") is False
    assert fs.covers("containerless", "build") is False
    assert fs.covers("container", "containerless") is False
    assert fs.covers("build", "containerless") is False
    # No outer slot at all, and a junk label, both fall through to "queue
    # normally" — the safe direction. A needless wait costs minutes; a wrongly
    # granted exemption costs the host.
    assert fs.covers(None, "container") is False
    assert fs.covers("", "container") is False
    assert fs.covers("not-a-kind", "container") is False
    # Aliases resolve, so a recipe saying --kind suite is not silently uncovered.
    assert fs.covers("deploy", "container") is True   # deploy == build
    assert fs.covers("suite", "container") is True    # suite  == container


def test_a_nested_run_does_not_take_a_second_slot(tmp_path):
    """The real shape: ``run`` inside ``run``, one lane, ONE slot held.

    Asserted from INSIDE the nested run rather than after it, because after it
    both slots are released and a leak and a correct release look identical.
    The innermost command writes the live holder count, so the evidence is a
    fact observed while the nesting existed.
    """
    state = tmp_path / "slots"
    seen = tmp_path / "seen.json"

    # innermost: record how many slots are held RIGHT NOW.
    probe = [sys.executable, "-c",
             "import json,subprocess,sys,os\n"
             "out = subprocess.run([sys.executable, sys.argv[1], 'status', '--json'],\n"
             "                     capture_output=True, text=True)\n"
             "snap = json.loads(out.stdout)\n"
             "json.dump({'holders': len(snap['holders']),\n"
             "           'kinds': [h['kind'] for h in snap['holders']],\n"
             "           'env_slot': os.environ.get('BOT_SQUAD_FLEET_SLOT'),\n"
             "           'env_kind': os.environ.get('BOT_SQUAD_FLEET_SLOT_KIND')},\n"
             "          open(sys.argv[2], 'w'))\n",
             str(SLOT), str(seen)]

    inner = [sys.executable, str(SLOT), "run", "--kind", "container",
             "--note", "inner", "--lane", "LANE-A", "--wait", "20", "--", *probe]

    out = subprocess.run(
        [sys.executable, str(SLOT), "run", "--kind", "container",
         "--note", "outer", "--lane", "LANE-A", "--wait", "60", "--", *inner],
        env=_env(state, slots=2, per_lane="1"), capture_output=True,
        text=True, timeout=180)

    assert out.returncode == 0, out.stderr
    rec = json.loads(seen.read_text())
    assert rec["holders"] == 1, (
        f"nesting took {rec['holders']} slots ({rec['kinds']}) for one lane's "
        f"one piece of work — the inner call did not recognise its parent")
    # It recognised the parent BECAUSE the token was inherited, not by luck.
    assert rec["env_slot"], "the outer slot's token never reached the child"
    assert rec["env_kind"] == "container"
    assert "already inside container slot" in out.stderr, out.stderr
    # And the outer slot really was released afterwards.
    assert _status(state, slots=2)["holders"] == []


def test_the_one_nesting_that_can_NEVER_succeed_is_refused_at_once(tmp_path):
    """container -> build cannot be granted, so it must not be queued.

    A build drains the daemon pool before it starts, and the pool is held by
    this run's own parent. No amount of waiting clears that, so queueing it
    spends the entire ``--wait`` to arrive at the same answer with less of the
    reason attached. This is the ONLY uncovered pair with that property, which
    is why it is the only one refused — the rest legitimately take a second
    slot in the other pool, and the warning for those says exactly that instead
    of crying deadlock at a case that works.
    """
    state = tmp_path / "slots"
    inner = [sys.executable, str(SLOT), "run", "--kind", "build",
             "--note", "inner-build", "--lane", "LANE-A", "--wait", "300",
             "--", sys.executable, "-c", "print('SHOULD NOT RUN')"]

    t0 = time.time()
    out = subprocess.run(
        [sys.executable, str(SLOT), "run", "--kind", "container",
         "--note", "outer", "--lane", "LANE-A", "--wait", "60", "--", *inner],
        env=_env(state, slots=2, per_lane="1"), capture_output=True,
        text=True, timeout=180)
    elapsed = time.time() - t0

    assert "NESTED DEADLOCK REFUSED" in out.stderr, out.stderr
    assert "NOT MEASURED" in out.stderr
    assert "SHOULD NOT RUN" not in out.stdout
    # Refused AT ONCE: nowhere near the inner --wait of 300s. The whole point
    # is not spending a wait on an answer that was knowable immediately.
    assert elapsed < 60, f"took {elapsed:.0f}s to refuse something unqueueable"


def test_an_uncovered_but_legal_nesting_takes_a_second_slot_and_says_so(tmp_path):
    """containerless -> container is fine: different pools, no deadlock.

    Included because the refusal above must not generalise. A guard that
    refuses every uncovered pair would break this one, which is legal, common
    (a containerless wrapper shelling out to a docker suite) and correct.
    """
    state = tmp_path / "slots"
    inner = [sys.executable, str(SLOT), "run", "--kind", "container",
             "--note", "inner", "--lane", "LANE-A", "--wait", "30",
             "--", sys.executable, "-c", "print('INNER_RAN')"]

    out = subprocess.run(
        [sys.executable, str(SLOT), "run", "--kind", "containerless",
         "--note", "outer", "--lane", "LANE-A", "--wait", "60", "--", *inner],
        env=_env(state, slots=2, per_lane="1"), capture_output=True,
        text=True, timeout=180)

    assert out.returncode == 0, out.stderr
    assert "INNER_RAN" in out.stdout
    assert "does NOT cover" in out.stderr, out.stderr
    assert "your lane will hold two" in out.stderr
    assert "DEADLOCK" not in out.stderr
