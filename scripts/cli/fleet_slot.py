#!/usr/bin/env python3
"""fleet_slot — the ENFORCED fleet gate for container work and image builds.

T-0994. It replaces the ``flock /home/www/bot-squad/data/_worker/suite.lock``
convention, which was unenforceable as written for four measured reasons:

1. **It existed only in prose.** ``grep -rn suite.lock`` over the whole repo on
   2026-09-06 returned ONE hit, in an unrelated test's message string. Not the
   deploy path, not ``bsq verify-isolated``, not a routine — nothing took it.
   The control held exactly where a session had read the prompt carefully.
2. **The two MANDATED ways to run a suite both bypassed it.**
   ``bsq verify-isolated`` — the path the fleet was told to certify on —
   ``Popen``s pytest with no lock and no process group of its own; the deploy
   recipe runs ``docker compose build`` from the worker with no lock either.
   A lane using the sanctioned tool was not defying the rule; the tool did not
   implement it. That is how six suite processes ran across three lanes at
   17:49Z while a TL held the lock by hand and nobody was defying anything.
3. **A deploy's hold is ~14 minutes and opaque.** Measured from the deploy
   queue records: the 2026-09-06T17:39:43Z staging deploy took **14.01 min**
   from ``queued_at`` to its rc file (14.39 incl. the worker restart), of which
   pickup latency is bounded by the 60s ``deploy_monitor_one`` interval — so
   **the hold is the ``docker build``, not the wrap.** Five other deploys in
   the same records took 1.26-2.60 min; the long one is the uncached
   ``npm ci`` + ``vite`` build. A waiter on a bare flock sees no holder, no
   kind, no elapsed time and no queue position: it cannot tell a 90-second
   wait from a 14-minute one, so it routes around.
4. **One mutex for two different weight classes.** A deploy exclusive of ALL
   testing gets routed around, because most lanes' test work does not touch
   the resource the deploy is contending for.

So this is not "a better flock". It is a gate whose HOLDER IS IDENTIFIED, whose
QUEUE IS READABLE, which is taken by the code that runs the work rather than by
the agent who remembered to, and **whose scope is the resource the work
actually consumes**.

WHAT IS GOVERNED, and why the line is drawn there
-------------------------------------------------
The operator's ruling (2026-09-06, revised the same hour after p651 showed the
first version contradicted itself): **the subject of the rule is IO cost, not
the word "suite".**

* ``container`` — a ``docker run`` / ``exec`` / ``compose up``. Costs a daemon
  round-trip, an image, a container start and a cold page cache. Takes one of
  ``slots``.
* ``build``     — an image build or a deploy. A different weight class: the
  second collapse was an ``npm ci`` + ``vite`` + ``tsc`` build across three
  buildkit executors on a rotating disk. Exclusive of all other CONTAINER
  work; drains the pool before it starts.
* **containerless** — a local-venv ``pytest``/``vitest``/``node`` run touches
  none of that, so **it does not take a slot at all** and a build does not
  block it. That is the constraint the replacement had to satisfy: a deploy
  that is exclusive of every kind of testing will be routed around again.
  Use :func:`admit` — it records the same admission readings without queueing.

The gate must not repeat the defect it was built for: the first version of the
operator's revised gate exempted containerless work on IO grounds and then
gated it on ``docker ps`` LATENCY, a daemon-health metric — blockable by
contention it cannot cause and does not add to. **Gate on the resource your
work actually consumes**, or the gate becomes unsatisfiable for exactly the
class it exempted and the next lane routes around it without saying so.

Design, and every clause is a measured failure from T-0968
----------------------------------------------------------
* **Liveness is a PROCESS GROUP, not a pid.** ``flock`` forks and the child
  inherits the locked fd, so killing the ``flock`` parent leaves the child
  running and the lock HELD, while ``/proc/locks`` names the dead parent — it
  names the WRONG process, not merely a stale one. Here a holder records the
  **pgid of the work**, and a slot is held while ANY process in that group
  lives. Killing the wrapper alone therefore leaves the slot held *correctly*
  (the work is still burning IO); killing the group releases it.
* **Every instrument reads a FILE.** ``/proc`` and ``/sys`` have no service
  behind them and cannot time out. During the 2026-09-06 collapse every docker
  CLI degraded to a timeout at the moment it was needed, while ``cgroup.freeze``
  answered in one read.
* **Stale holders are reclaimed ON THE RECORD** (``reclaims.log``). A silent
  repair teaches the next reader that the gate is unreliable.
* **Waiters are recorded before they block.** The queue is the artifact the
  bare flock never produced.
* **Every run carries its own admission ticket** (``admissions.ndjson``): the
  gate readings AT THE MOMENT THE RUN STARTED, recorded by the runner rather
  than remembered at report time. Adopted from p651.
* **D-STATE IS THE GATE. The disk-queue field and ``docker ps`` latency were
  both TESTED AND REJECTED for admission control** — recorded here because an
  instrument that was checked and rejected is worth more than one nobody
  checked.

  ``/proc/diskstats`` field 12 (IOs in flight) was proposed as the gauge for
  containerless work, being a file that cannot time out. Eight 12-sample
  series were taken on this box inside half an hour:

  ====================================  =========  ======  ========
  condition                             D-state    median  range
  ====================================  =========  ======  ========
  operator, ~15 of OUR pytest/docker    ~13        16.0    4-89
  p664                                  quiet      15      -
  p674                                  quiet      30.5    -
  p673, ~20 test procs + 53 node        10         35.5    2-97
  p651, own run STOPPED                 quiet      ~40     -
  watchrobot, 2 containers + baseline   6          41.5    7-94
  **operator, DURING the co-tenant**    **78**     **22**  9-72
  **p673, DURING the co-tenant build**  **82**     **39**  24-87
  ====================================  =========  ======  ========

  **The two build-window series sit INSIDE, and mostly BELOW, the healthy
  range.** An ``npm ci`` + ``vite`` build across buildkit executors — the exact
  workload that collapsed this box at 18:15 — drove D-state from 3 to 78 and
  moved the disk median from 15-41 to 22-39, i.e. not at all. Across a 1-to-8
  swing in the condition the metric stayed inside its own sample-to-sample
  noise, and no amount of extra sampling fixes that: the problem is not the
  statistic, it is that the quantity does not vary with the condition.

  On the healthy series alone the same conclusion was already visible: the
  medians order INVERSELY to our own load, separably so (Mann-Whitney on the
  raw series, operator-vs-watchrobot ``z = -2.40``, operator-vs-p673
  ``z = -2.31``, both beyond ±1.96; watchrobot-vs-p673 ``z = -0.20``). And the
  provisional "median under 30" ceiling would have refused a healthy host:
  four of the six healthy medians are already over it, and in the retired
  point-read form it rejects 25-67% of single draws depending on condition.

  The co-tenant sampled its own build STAGE BY STAGE, which is the strongest
  arm because D-state and the disk median move against each other within one
  event: ``D 9 -> 8 -> 33 -> 61 -> 89`` and load ``17 -> 92``, while the disk
  median sat ``28, 18, 25, 30, 19`` throughout — indistinguishable from idle.

  ⚠ **The limits on this ruling, stated so nobody over-quotes it:** it
  establishes that field 12 FAILED TO RISE during large known events, which is
  enough to disqualify it as a gate — a metric that can be flat while D-state
  goes to 89 cannot authorise anything. It is NOT a claim that the field
  measures nothing. And ``npm ci`` was CACHED in the sampled build, so what was
  measured is the vite/lib-builder WRITER class, not the npm-ci class that
  drove the 18:15 collapse.

  ⚠⚠ **And ``docker ps`` latency failed the same test from the other side:
  204ms at load 80 with the disk saturated** (p673, same instant). A
  docker-latency gate would have ADMITTED container work into that host. The
  refined ruling: **docker latency detects DAEMON saturation, not DISK
  saturation.** It earned its place at 18:20 when the daemon could not answer
  in 120s, and it is blind to a build. Keep it as a daemon-health check for
  container work; never read it as a load signal.

  **D-state discriminated where both others did not — 3 quiet, 10-13 working,
  76-82 building.** So it is the one reading this module ENFORCES
  (``d_state_max``, default 20, settable to ``null`` to disarm). The disk
  series is no longer sampled on the run path at all: it costs five seconds
  per run and buys nothing. ``--disk-series`` still takes one on demand, for
  whoever gathers the further evidence that would re-open the question.

Usage::

    fleet_slot.py run --kind container --note "T-0994 api suite" -- docker run ...
    fleet_slot.py run --kind build     --note "staging deploy"   -- ./deploy.sh
    fleet_slot.py admit --note "worker suite (containerless)"     # ticket only
    fleet_slot.py status [--json]

    # For a caller that spawns and supervises its own child (deploy.py):
    tok=$(fleet_slot.py acquire --kind build --note "staging deploy")
    fleet_slot.py adopt "$tok" --pgid 12345      # hand liveness to the work
    fleet_slot.py release "$tok"

Deliberately stdlib-only and importing nothing from ``bot_squad_worker``: it
runs from a frozen extract, from the install, and from any clone, and must not
add an import-provenance hazard to the very runs it gates.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

# --------------------------------------------------------------------------
# Paths and policy
# --------------------------------------------------------------------------

BOT_SQUAD = os.environ.get("BOT_SQUAD", "/home/www/bot-squad")

#: Fleet-wide, NOT per-project: the constraint is one rotating disk and one
#: docker daemon shared by every project and every lane on this box.
DEFAULT_STATE_DIR = Path(BOT_SQUAD) / "data" / "_worker" / "fleet_slots"

#: Concurrent CONTAINER runs allowed. The operator's binding number after the
#: collapse is 2-3 test containers fleet-wide; ~15 concurrent docker runs on a
#: rotating disk is what took the host down. Raising or lowering it is a
#: one-key edit to config.json, not a code change and not a deploy.
DEFAULT_SLOTS = 2

#: A build's measured hold is ~14 min, so a default wait shorter than that
#: would make every lane queued behind a deploy time out and route around —
#: which is the defect this module exists to remove.
DEFAULT_WAIT = 1800.0

#: Kinds, plus the names the fleet already had in its fingers.
_KINDS = ("container", "build", "containerless")
_KIND_ALIASES = {"suite": "container", "exclusive": "build", "deploy": "build"}

_POLL = 1.0

#: Print a "this rule may be unsatisfiable — SAY SO" hint after this long.
#: Making the asking cheap is the part that actually worked today: both of the
#: day's defects in this area were found by a lane asking rather than deciding.
_ASK_AFTER = 300.0


def state_dir() -> Path:
    return Path(os.environ.get("BOT_SQUAD_FLEET_SLOTS_DIR") or DEFAULT_STATE_DIR)


def normalise_kind(kind: str) -> str:
    k = _KIND_ALIASES.get(kind, kind)
    if k not in _KINDS:
        raise ValueError(f"kind must be one of {_KINDS} (or an alias), got {kind!r}")
    return k


def _holders_dir(sd: Path) -> Path:
    return sd / "holders"


def _waiters_dir(sd: Path) -> Path:
    return sd / "waiters"


def _ensure(sd: Path) -> None:
    _holders_dir(sd).mkdir(parents=True, exist_ok=True)
    _waiters_dir(sd).mkdir(parents=True, exist_ok=True)


def _config(sd: Path) -> dict:
    try:
        with open(sd / "config.json", "rb") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def containerless_slots(sd: Path) -> int | None:
    """Concurrency bound for CONTAINERLESS work, or None to leave it unbounded.

    ⚠ **This is the one number in this module with no measurement behind it,
    and it is labelled rather than dressed up.** What IS measured is that the
    exemption without a bound admits unbounded parallelism: p673 counted SEVEN
    concurrent containerless pytest process groups from five lanes at 19:04Z,
    every one of which had read a passing D-state and every one of which was
    right at the moment it read it. Its own run then burned 4m53s of wall for
    15s of CPU.

    That is the T-0994 shape one level down: **D-state is a lagging CONSEQUENCE
    of concurrency, not a RESERVATION**, so a rule whose satisfiable condition
    is a health reading cannot bound the thing it means to bound. A slot can;
    that is p673's proposal and this is it.

    ⚠⚠ **THIS NUMBER BOUNDS OUR ADMISSIONS. IT DOES NOT BUY THROUGHPUT, AND A
    READER WHO THINKS IT DOES WILL KEEP LOWERING IT.** Measured by p673 inside
    a HELD slot with buildkit at zero: 10m03s wall on 17s CPU, a 35x
    wall-to-CPU ratio, against 39x unslotted — the ceiling bought it almost
    nothing. The D-state composition says why: co-tenant **postgres outnumbers
    our python roughly 3 to 1**, with named backends in CREATE INDEX and
    COMMIT. **No ceiling we choose makes wall-clock predictable while another
    tenant's database shares this spindle.** The slot is a reservation against
    OUR OWN over-admission; it is not a performance control.

    (And part of the IO we gate on is generated by the thing being gated: the
    worker suite shells out to real ``git clone`` — p673 caught ours in
    D-state from `test_run_next_deploy_clone_pro0`.)

    **3** is the operator's in-force number (19:04Z) and is a BOUND, NOT A
    CALIBRATION — nobody has measured where the containerless knee is. Set
    ``containerless_slots`` in ``config.json``, or ``null`` to disarm, once
    somebody does. p673 proposed the series that would let them: the
    **wall-to-CPU ratio** of a run (20x at seven concurrent groups, i.e. ~95%
    waiting), which moves with concurrency in a way D-state does not. It is
    knowable only after the fact, so it describes rather than reserves — but it
    is a measurement OF OUR WORK rather than of a shared disk.
    """
    env = os.environ.get("BOT_SQUAD_FLEET_CONTAINERLESS_SLOTS")
    raw = env if env is not None else _config(sd).get("containerless_slots", 3)
    if raw is None or raw == "":
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 4


#: The operator's hand-made stopgap semaphore (19:04Z): one lock file per slot,
#: taken with `flock -n`. It is being REPLACED by this module — his words were
#: "a stopgap to be replaced, not the design" — but while both exist they are
#: TWO SEMAPHORES OVER ONE RESOURCE, which is the exact failure a semaphore was
#: introduced to remove: a lane using the broadcast snippet and a lane calling
#: this module cannot see each other, so the effective ceiling is 3 PLUS the
#: pool rather than 3. Found by p673.
#:
#: So a containerless run here ALSO takes one of those fds while it runs. The
#: two mechanisms then contend over the same files and the ceiling is one
#: number. Delete the directory (or set ``legacy_slot_dir: null``) to retire the
#: stopgap once no lane uses the raw snippet.
LEGACY_SLOT_DIR = Path(BOT_SQUAD) / "data" / "_worker" / "slots"


def legacy_slot_dir(sd: Path) -> Path | None:
    raw = _config(sd).get("legacy_slot_dir", "")
    if raw is None:
        return None
    env = os.environ.get("BOT_SQUAD_FLEET_LEGACY_SLOT_DIR")
    if env is not None:
        return Path(env) if env else None
    d = Path(raw) if raw else LEGACY_SLOT_DIR
    return d if d.is_dir() else None


def legacy_slot_mode(sd: Path) -> str:
    """``interop`` (take a legacy fd too) or ``refuse`` (the snippet is retired).

    The retirement criterion, stated by the operator so it is a criterion and
    not an intention: the directory goes when ``/proc/locks`` shows zero FLOCK
    entries on those three inodes AND no lane has used the raw snippet for a
    full cycle. Until then **do not delete the directory and do not disarm
    this** — a live run losing its reservation mid-flight is worse than the
    ambiguity. Flip to ``refuse`` after the snippet is withdrawn, so a
    stray legacy fd becomes an error rather than a warning.
    """
    return str(_config(sd).get("legacy_slot_mode", "interop"))


def fd_holders(path: Path) -> tuple[list[int], int]:
    """Pids holding an open fd on ``path``, and the uid the scan ran as.

    Attribution AND liveness in one read, and it is the right instrument for
    the legacy pool specifically, where this module is not the acquirer and so
    has no registry entry to consult.

    ``/proc/locks`` cannot do this: the broadcast idiom is ``exec 9>file``
    followed by ``flock -n 9``, and **the ``flock`` binary is a short-lived
    child that performs the syscall on the inherited fd and exits** — so the
    recorded pid is the helper, already a corpse, while the live fd sits in the
    PARENT SHELL. Measured on all three legacy slots: acquirers 1826624,
    1836423, 1914718 all absent from /proc, live holders 1826622, 1836421,
    1914715 — two apart, the shell's fork/exec of ``flock``. **The acquirer is
    a CHILD of the holder, not an ancestor**, so walking descendants of the
    /proc/locks pid finds an empty set and reads as a leak.

    ⚠⚠ **The scan sees only processes whose /proc is readable BY THIS UID, so
    under a different uid it silently under-reports — which looks exactly like
    a leak.** That is why the uid is returned rather than assumed: a pool that
    force-releases on an under-read takes a live holder's slot, which is worse
    than the problem it fixes. This module therefore REPORTS and never
    force-releases a legacy fd.
    """
    target = str(path.resolve())
    holders: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return holders, os.getuid()
    for name in entries:
        if not name.isdigit():
            continue
        fddir = f"/proc/{name}/fd"
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue  # not ours to read, or gone
        for fd in fds:
            try:
                if os.path.realpath(f"{fddir}/{fd}") == target:
                    holders.append(int(name))
                    break
            except OSError:
                continue
    return holders, os.getuid()


def legacy_report(sd: Path) -> list[dict]:
    """Per legacy slot file: is it held, by whom, and under which uid we looked.

    A leak becomes a POSITIVE test — held with no visible fd holder — rather
    than an inference from a pid that was never the holder. It is still only a
    SUSPECTED leak, because an unreadable uid produces the same reading.
    """
    d = legacy_slot_dir(sd)
    if d is None:
        return []
    out = []
    for path in sorted(d.glob("containerless-*.lock")):
        held = True
        try:
            fh = open(path, "a+")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = False          # we got it, so nobody held it
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                held = True
            finally:
                fh.close()
        except OSError:
            held = True
        pids, uid = fd_holders(path)
        out.append({
            "slot": path.name, "held": held, "fd_holders": pids,
            "scanned_as_uid": uid,
            "suspected_leak": bool(held and not pids),
        })
    return out


def take_legacy_slot(sd: Path):
    """Grab one ``containerless-*.lock`` fd non-blocking; None when none free.

    The fd is returned and must be kept open for the run: a HELD fd cannot be
    double-taken, whereas a count-then-start has a TOCTOU window two lanes can
    pass simultaneously. That property is the operator's, and it is why this
    interoperates with his files rather than counting them.

    ⚠⚠ **OCCUPANCY IS DECIDED BY THIS NON-BLOCKING ACQUIRE, NEVER BY READING
    ``/proc/locks``.** That row records the pid that MADE the syscall, and in
    the idiom in circulation (``exec 9>file; flock -n 9``) the ``flock`` binary
    is a short-lived child that exits immediately — so the row always names a
    corpse while the fd lives on in the parent shell. Measured 19:25Z: all
    three legacy slots named a dead acquirer, **and none was leaked** —
    ``flock -n`` refused every one of them, positive-control proven. **A pool
    that concludes "leaked" from a dead pid in /proc/locks will FORCE-RELEASE A
    LIVE HOLDER**, which is worse than the problem it fixes. This module reads
    neither: liveness comes from its own registry plus the holder's process
    group in ``/proc/<pid>/stat``.
    """
    d = legacy_slot_dir(sd)
    if d is None or legacy_slot_mode(sd) == "refuse":
        return None
    for path in sorted(d.glob("containerless-*.lock")):
        try:
            fh = open(path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            try:
                fh.close()
            except (OSError, UnboundLocalError):
                pass
    return None


def slots(sd: Path) -> int:
    """Pool size: env override, then ``config.json``, then the default.

    Re-read on every acquire so the operator can widen or narrow the fleet
    without restarting anything that is already waiting.
    """
    env = os.environ.get("BOT_SQUAD_FLEET_SLOTS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    try:
        return max(1, int(_config(sd).get("slots", DEFAULT_SLOTS)))
    except (ValueError, TypeError):
        return DEFAULT_SLOTS


# --------------------------------------------------------------------------
# Admission readings — files only, so they survive the collapse they describe
# --------------------------------------------------------------------------

def d_state_count() -> int | None:
    """Processes in uninterruptible sleep, from ``/proc/<pid>/stat``.

    D-state is the honest saturation gauge: load average LAGS BY CONSTRUCTION
    and inverted the attribution mid-incident on 2026-09-06 — at 14:37:39Z our
    load was ~0 while D-state was 602, which read as "not us"; one sample later
    D-state was 66, because the 602 was the BACKLOG DRAINING.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    n = 0
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        close = raw.rfind(")")
        if close < 0:
            continue
        fields = raw[close + 2:].split()
        if fields and fields[0] == "D":
            n += 1
    return n


def _disk_queue_point(device: str) -> int | None:
    """One read of ``/proc/diskstats`` field 12 — IOs CURRENTLY IN PROGRESS.

    A FILE: no daemon, no socket, no CLI, and it cannot time out. That is why
    the instrument was chosen — during the 2026-09-06 collapse every docker
    client degraded to a timeout at the moment it was needed.

    ⚠ NEVER GATE ON ONE OF THESE. Field 12 is INSTANTANEOUS in-flight IO —
    what the queue holds at the microsecond of the read. Measured on this box
    within twelve minutes: 4-89 (median 16), 2-97 (median 35.5), 7-94 (median
    41.5). A single read is a coin flip, and whichever value a lane happens to
    draw becomes its authorisation. The "healthy is 1-2" baseline that opened
    the day was three microseconds of quiet published as a state.
    """
    try:
        with open("/proc/diskstats", "rb") as fh:
            for line in fh.read().decode("utf-8", "replace").splitlines():
                f = line.split()
                # 1 major, 2 minor, 3 name, then 11 stat fields; field 12
                # overall is "IOs currently in progress" == f[11].
                if len(f) >= 12 and f[2] == device:
                    return int(f[11])
    except (OSError, ValueError):
        return None
    return None


def disk_queue_series(device: str | None = None, samples: int = 10,
                      span: float = 5.0) -> dict:
    """Sample field 12 ``samples`` times over ``span`` seconds.

    Returns ``{"series": [...], "median": m, "min": lo, "max": hi, ...}``.

    **Publish the series, not the verdict.** A median alone cannot be
    re-judged by whoever calibrates the threshold later; the series can.
    """
    dev = device or "sda"
    interval = span / max(1, samples - 1) if samples > 1 else 0.0
    series: list[int] = []
    for i in range(max(1, samples)):
        v = _disk_queue_point(dev)
        if v is not None:
            series.append(v)
        if i + 1 < samples and interval:
            time.sleep(interval)
    if not series:
        return {"series": [], "median": None, "min": None, "max": None,
                "device": dev, "n": 0}
    ordered = sorted(series)
    n = len(ordered)
    median = (ordered[n // 2] if n % 2
              else (ordered[n // 2 - 1] + ordered[n // 2]) / 2)
    return {"series": series, "median": median, "min": ordered[0],
            "max": ordered[-1], "device": dev, "n": n}


def loadavg() -> float | None:
    try:
        with open("/proc/loadavg", "rb") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def admission_readings(sd: Path | None = None, *, disk_series: bool = False) -> dict:
    """The gate readings, sampled now. Recorded WITH the run, not remembered.

    Only two readings are taken by default, and the asymmetry between them is
    the whole finding of this ticket's calibration work:

    * ``d_state`` — **the gate.** It discriminated 3 quiet / 10-13 working /
      76-82 during a known heavy build.
    * ``loadavg1`` — **describes, never decides.** It lags by construction and
      inverted the attribution mid-incident on 2026-09-06.

    ``docker ps`` latency is deliberately NOT sampled: it is a daemon-health
    metric, this function is called for containerless work too, and it was
    measured at 204ms on a box at load 80 with the disk saturated — it would
    have admitted work into the very state the rule exists to exclude.

    ``disk_series=True`` adds a 10-sample /proc/diskstats series. Off by
    default: the field was tested and REJECTED for admission control, so
    spending five seconds on it per run buys nothing.
    """
    sd = sd or state_dir()
    cfg = _config(sd)
    rec = {
        "at": time.time(),
        "d_state": d_state_count(),
        "loadavg1": loadavg(),
    }
    if disk_series:
        disk = disk_queue_series(cfg.get("disk_device"),
                                 samples=int(cfg.get("disk_samples", 10)),
                                 span=float(cfg.get("disk_span", 5.0)))
        rec.update({"disk_queue_median": disk["median"],
                    "disk_queue_series": disk["series"],
                    "disk_device": disk["device"],
                    "disk_note": "RECORDED, NOT GATED — disqualified for admission "
                                 "control: flat while D-state went to 78"})
    return rec


def d_state_max(sd: Path) -> int | None:
    """The enforced admission ceiling, or None when disarmed.

    The operator's binding number is "under ~20". It is the only one of the
    three proposed gate readings that moved with the condition.
    """
    env = os.environ.get("BOT_SQUAD_FLEET_DSTATE_MAX")
    raw = env if env is not None else _config(sd).get("d_state_max", 20)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 20


def admission_check(sd: Path | None = None, *, disk_series: bool = False
                    ) -> tuple[bool, str, dict]:
    """Sample the readings and decide admission on the ONE that discriminates.

    Returns ``(ok, reason, readings)``. The reason is returned either way so a
    refusal can print the VALUE it refused on rather than a verdict — a lane
    that is told only "refused" has no way to tell a real saturation from a
    gate that cannot be satisfied, and that is how the last three gates in this
    area came to be routed around instead of reported.
    """
    sd = sd or state_dir()
    rec = admission_readings(sd, disk_series=disk_series)
    ceiling = d_state_max(sd)
    d = rec.get("d_state")
    if ceiling is None:
        return True, f"d_state={d} (ceiling disarmed)", rec
    if d is None:
        # Refuse to conclude from an instrument that did not report. A gate
        # that admits on a missing reading is a gate that is absent exactly
        # when /proc is unreadable.
        return False, "d_state NOT MEASURED — refusing to admit on a blind gate", rec
    if d > ceiling:
        return False, f"d_state={d} over the ceiling of {ceiling}", rec
    return True, f"d_state={d} within the ceiling of {ceiling}", rec


def _record_admission(sd: Path, rec: dict) -> None:
    """Append one admission ticket. This file IS the calibration series the
    disk-queue ceiling is missing; it accrues without anyone remembering to
    sample."""
    try:
        _ensure(sd)
        with open(sd / "admissions.ndjson", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _admission_line(rec: dict) -> str:
    """The admission ticket. Publish the value beside the verdict — a verdict
    dies with the method that produced it, and a number nobody printed cannot
    be re-judged by whoever calibrates this next."""
    def _v(k):
        v = rec.get(k)
        return "NOT MEASURED" if v is None else v
    line = f"admission: d_state={_v('d_state')} load1={_v('loadavg1')} (load DESCRIBES, never decides)"
    if rec.get("disk_queue_series"):
        line += (f" disk_queue_median={_v('disk_queue_median')} "
                 f"series={rec['disk_queue_series']} [RECORDED, NOT GATED]")
    return line


# --------------------------------------------------------------------------
# Liveness — /proc only, and by PROCESS GROUP
# --------------------------------------------------------------------------

def _pgid_of(pid: int) -> int | None:
    """The process group of ``pid``, read from ``/proc/<pid>/stat``.

    Field 5 (1-indexed) is pgrp. The comm field (2) may itself contain spaces
    and parentheses, so the split is anchored on the LAST ``')'`` rather than
    on whitespace — a naive ``split()[4]`` misreads any process whose name
    holds a space, which is the same column-index defect that manufactured
    phantom kill targets out of ``/proc/locks`` waiter rows on 2026-09-06.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2:].split()
    # fields[0] is state (field 3), so pgrp (field 5) is fields[2].
    if len(fields) < 3:
        return None
    try:
        return int(fields[2])
    except ValueError:
        return None


def pgid_alive(pgid: int) -> bool:
    """True while ANY process still belongs to process group ``pgid``.

    This is the whole point of the module: ``flock`` released on the death of
    the process holding the fd, and that process was not the work.

    Read from ``/proc`` rather than ``kill(-pgid, 0)`` because the latter
    answers EPERM for a group we do not own — indistinguishable from "gone" —
    and every lane on this box runs as the same unix user, so a wrong answer
    there would free a slot another project still holds.
    """
    if pgid <= 0:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        # We cannot see /proc: refuse to conclude "dead". A gate that frees a
        # slot when its instrument is blind is worse than one that stalls.
        return True
    for name in entries:
        if not name.isdigit():
            continue
        if _pgid_of(int(name)) == pgid:
            return True
    return False


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

class _Registry:
    """Short-lived exclusive access to the slot registry.

    Held for MILLISECONDS — long enough to read the holder files, reap the dead
    and write one record. It is never held for the duration of a run, which is
    precisely the mistake that made the old convention a 14-minute opaque
    block.
    """

    def __init__(self, sd: Path):
        self.sd = sd
        self._fh = None

    def __enter__(self):
        _ensure(self.sd)
        self._fh = open(self.sd / "registry.lock", "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
        return False


def _read_records(d: Path) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(d / name, "rb") as fh:
                rec = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        rec["_path"] = str(d / name)
        out.append(rec)
    out.sort(key=lambda r: r.get("since", 0.0))
    return out


def _write_record(path: Path, rec: dict) -> None:
    """Atomic: a torn record read by a concurrent acquirer is a phantom holder
    that blocks the fleet until someone deletes it by hand."""
    tmp = Path(str(path) + f".tmp-{os.getpid()}")
    with open(tmp, "w") as fh:
        json.dump(rec, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _log_reclaim(sd: Path, rec: dict, why: str) -> None:
    line = json.dumps({
        "at": time.time(),
        "why": why,
        "token": rec.get("token"),
        "kind": rec.get("kind"),
        "lane": rec.get("lane"),
        "pgid": rec.get("pgid"),
        "note": rec.get("note"),
        "held_for_s": round(time.time() - float(rec.get("since") or 0), 1),
    })
    try:
        with open(sd / "reclaims.log", "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def reap(sd: Path) -> tuple[list[dict], list[dict]]:
    """Drop holder/waiter records whose process group is gone.

    Returns ``(live_holders, live_waiters)``. Call INSIDE the registry lock.
    Every removal is appended to ``reclaims.log`` — a stale slot silently
    reclaimed teaches the next reader that the gate is unreliable; one
    reclaimed on the record teaches them what happened.
    """
    live_h, live_w = [], []
    for rec in _read_records(_holders_dir(sd)):
        if pgid_alive(int(rec.get("pgid") or 0)):
            live_h.append(rec)
        else:
            _log_reclaim(sd, rec, "holder process group gone")
            try:
                os.unlink(rec["_path"])
            except OSError:
                pass
    for rec in _read_records(_waiters_dir(sd)):
        if pgid_alive(int(rec.get("pgid") or 0)):
            live_w.append(rec)
        else:
            try:
                os.unlink(rec["_path"])
            except OSError:
                pass
    return live_h, live_w


def _grantable(kind: str, holders: list[dict], waiters: list[dict],
               token: str, n_slots: int, n_cl: int | None) -> tuple[bool, str]:
    """Whether ``token`` may take a slot now, and the reason either way.

    Two INDEPENDENT pools, because they are two different resources:

    * ``container`` and ``build`` contend for the docker daemon, image layers
      and container starts. A build is exclusive of both.
    * ``containerless`` contends for CPU and page cache only. **A build does
      not block it** — a deploy exclusive of every kind of testing is a deploy
      that gets routed around, which is how this ticket was born — but it is
      still bounded by its own slot count, because an unbounded exemption is
      not a rule.

    The reason is returned either way so a waiter can PRINT WHY it is waiting
    rather than blocking silently, which is the single missing property that
    made lanes route around the old lock.
    """
    if kind == "containerless":
        mine = [h for h in holders if h.get("kind") == "containerless"]
        if n_cl is None:
            return True, f"{len(mine)} containerless run(s), unbounded"
        if len(mine) >= n_cl:
            return False, f"{len(mine)}/{n_cl} containerless slots held"
        return True, f"{len(mine)}/{n_cl} containerless slots held"

    daemon = [h for h in holders if h.get("kind") in ("container", "build")]
    build = next((h for h in daemon if h.get("kind") == "build"), None)
    if build is not None:
        return False, (f"build in progress: {build.get('note') or build.get('lane') or build.get('token')}"
                       f" (pgid {build.get('pgid')}, "
                       f"{round(time.time() - float(build.get('since') or 0))}s elapsed)")

    # FIFO against builds only, so a stream of container runs cannot starve a
    # deploy. A build queued ahead of us blocks us even when a slot is free;
    # that is what a barrier is for.
    ahead = [w for w in waiters if w.get("token") != token]
    qbuild = next((w for w in ahead if w.get("kind") == "build"), None)
    if qbuild is not None:
        return False, (f"build queued ahead: "
                       f"{qbuild.get('note') or qbuild.get('lane') or qbuild.get('token')}")

    if kind == "build":
        if daemon:
            return False, (f"{len(daemon)} container slot(s) still held; a build "
                           f"drains the pool before it starts")
        return True, "pool empty"

    if len(daemon) >= n_slots:
        return False, f"{len(daemon)}/{n_slots} container slots held"
    return True, f"{len(daemon)}/{n_slots} container slots held"


# --------------------------------------------------------------------------
# Public operations
# --------------------------------------------------------------------------

def admit(note: str | None = None, lane: str | None = None,
          kind: str = "containerless", sd: Path | None = None,
          disk_series: bool = False) -> tuple[bool, str, dict]:
    """Check the gate and record a ticket, taking NO slot.

    ⚠ **This RESERVES NOTHING.** Prefer ``run --kind containerless``, which
    holds a containerless slot for the lifetime of the command: a health
    reading is a symptom of concurrency, a slot is a reservation against it,
    and seven lanes each reading a valid D-state is exactly how an exemption
    becomes unbounded. This verb exists for callers that cannot wrap their
    work.

    A local-venv pytest/vitest/node run does not contend for the daemon, an
    image pull or a container start, so the pool does not govern it and a build
    does not block it. That exemption is load-bearing: a deploy exclusive of
    every kind of testing is a deploy that gets routed around.

    It still records its readings, because the run should carry its own
    admission ticket rather than the runner's assurance that conditions were
    fine — and because that record is the series a future calibration needs.
    """
    sd = sd or state_dir()
    ok, reason, rec = admission_check(sd, disk_series=disk_series)
    rec.update({"kind": kind, "note": note or "",
                "lane": lane or os.environ.get("BOT_SQUAD_SID") or "",
                "granted": ok, "slot": False, "reason": reason})
    _record_admission(sd, rec)
    return ok, reason, rec


def acquire(kind: str, *, pgid: int, lane: str | None, note: str | None,
            wait: float, sd: Path | None = None,
            on_wait=None) -> tuple[str, dict]:
    """Block until a slot is granted; return ``(token, admission_readings)``.

    ``pgid`` is what liveness will be read from. A caller that has not spawned
    its work yet passes its OWN pgid and calls :func:`adopt` once the child
    exists — the slot is then held by the work, not by the supervisor.
    """
    kind = normalise_kind(kind)
    sd = sd or state_dir()
    _ensure(sd)
    token = uuid.uuid4().hex[:12]
    now = time.time()
    rec = {
        "token": token, "kind": kind, "pgid": int(pgid),
        "lane": lane or os.environ.get("BOT_SQUAD_SID") or "",
        "note": note or "", "since": now, "host_pid": os.getpid(),
        "cwd": os.getcwd(),
    }
    wpath = _waiters_dir(sd) / f"{token}.json"
    deadline = now + wait
    last_reason = ""
    try:
        with _Registry(sd):
            _write_record(wpath, rec)
        while True:
            with _Registry(sd):
                holders, waiters = reap(sd)
                n = slots(sd)
                ok, reason = _grantable(kind, holders, waiters, token, n,
                                        containerless_slots(sd))
                if ok:
                    hrec = dict(rec)
                    hrec["since"] = time.time()
                    hrec["waited_s"] = round(time.time() - now, 1)
                    hrec.pop("_path", None)
                    _write_record(_holders_dir(sd) / f"{token}.json", hrec)
                    try:
                        os.unlink(wpath)
                    except OSError:
                        pass
                    adm = admission_readings(sd)
                    adm.update({"kind": kind, "note": note or "",
                                "lane": rec["lane"], "token": token,
                                "granted": True, "slot": True,
                                "waited_s": hrec["waited_s"]})
                    _record_admission(sd, adm)
                    return token, adm
                pos = 1 + sum(1 for w in waiters
                              if w.get("token") != token
                              and float(w.get("since") or 0) < now)
            if reason != last_reason and on_wait:
                on_wait(reason, pos, time.time() - now)
                last_reason = reason
            if time.time() >= deadline:
                adm = admission_readings(sd)
                adm.update({"kind": kind, "note": note or "", "lane": rec["lane"],
                            "granted": False, "slot": False,
                            "waited_s": round(time.time() - now, 1),
                            "reason": reason})
                _record_admission(sd, adm)
                raise TimeoutError(
                    f"fleet slot not granted in {wait:.0f}s: {reason} "
                    f"(queue position {pos}). NOT MEASURED — the run did not start."
                )
            time.sleep(_POLL)
    except BaseException:
        try:
            os.unlink(wpath)
        except OSError:
            pass
        raise


def adopt(token: str, pgid: int, sd: Path | None = None) -> bool:
    """Point an existing holder's liveness at ``pgid``.

    For the supervise-your-own-child shape: acquire before the fork (so the
    slot is reserved), then hand the slot to the work's process group, so a
    supervisor that dies mid-run does NOT free a slot whose docker build is
    still running.
    """
    sd = sd or state_dir()
    path = _holders_dir(sd) / f"{token}.json"
    with _Registry(sd):
        try:
            with open(path, "rb") as fh:
                rec = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False
        rec["pgid"] = int(pgid)
        rec["adopted_at"] = time.time()
        _write_record(path, rec)
    return True


def release(token: str, sd: Path | None = None) -> bool:
    sd = sd or state_dir()
    path = _holders_dir(sd) / f"{token}.json"
    with _Registry(sd):
        try:
            os.unlink(path)
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return False
            raise


def snapshot(sd: Path | None = None) -> dict:
    sd = sd or state_dir()
    _ensure(sd)
    with _Registry(sd):
        holders, waiters = reap(sd)
        n = slots(sd)
    now = time.time()

    def _fmt(rec):
        return {
            "token": rec.get("token"), "kind": rec.get("kind"),
            "lane": rec.get("lane"), "note": rec.get("note"),
            "pgid": rec.get("pgid"),
            "age_s": round(now - float(rec.get("since") or now), 1),
            "waited_s": rec.get("waited_s"),
        }

    return {
        "state_dir": str(sd),
        "slots": n,
        "containerless_slots": containerless_slots(sd),
        "containerless_held": sum(1 for h in holders
                                  if h.get("kind") == "containerless"),
        "holders": [_fmt(h) for h in holders],
        "waiters": [_fmt(w) for w in waiters],
        "build_held": any(h.get("kind") == "build" for h in holders),
        "legacy_slots": legacy_report(sd),
        "readings": admission_readings(sd),
        # Say what was CHECKED, not only what was concluded. An honest report
        # from a query that does not cover the case is still a false report.
        "method": (
            "liveness = any process in the holder's process group, read from "
            "/proc/<pid>/stat field 5; dead groups are reclaimed and appended to "
            "reclaims.log. TWO INDEPENDENT POOLS: container+build share the daemon "
            "pool (a build is exclusive and drains it); containerless has its own "
            "pool and a build does NOT block it. The only ENFORCED health reading is "
            "D-state — /proc/diskstats field 12 and docker-ps latency were both "
            "tested and rejected for admission control (field 12 stayed 18-39 while "
            "D-state went 3->89; docker ps answered in 204ms at load 80). Reservations "
            "bound, health readings describe."),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _ask_hint(kind: str, note: str | None, waited: float,
              what: str = "waited") -> str:
    """The escape hatch that is NOT routing around the rule.

    Both of the day's defects in this area were found by a lane asking rather
    than deciding, and the lock broke silently because everyone answered for
    themselves. So the tool prints the report line rather than leaving the lane
    to invent one — a lane that argues its way past a bad rule leaves the rule
    bad and everyone after it worse off.
    """
    # A refusal is not a wait. Saying "still waiting after 0 min" for an
    # instant admission refusal misdescribes what happened, and a lane
    # reporting it would misdescribe it to the operator too.
    detail = (f"still waiting after {waited / 60:.0f} min" if what == "waited"
              else "was refused on admission")
    return (
        f"fleet-slot: {detail}. If this gate looks "
        f"UNSATISFIABLE for your work, that is a FINDING — send it, do not route "
        f"around it:\n"
        f"  bsq peer send S-almdudleer-operator-p640 \"fleet gate unsatisfiable: "
        f"{kind} '{note or ''}' — {detail} — <what you need>\"")


def _cmd_run(args) -> int:
    sd = state_dir()
    child: subprocess.Popen | None = None
    token: str | None = None
    hinted = [False]

    def _on_wait(reason, pos, waited):
        print(f"fleet-slot: WAITING ({args.kind}) — {reason}; queue position {pos}",
              file=sys.stderr, flush=True)
        if waited >= _ASK_AFTER and not hinted[0]:
            hinted[0] = True
            print(_ask_hint(args.kind, args.note, waited), file=sys.stderr, flush=True)

    t0 = time.time()
    # Admission first, then the queue: there is no point holding a slot for a
    # run the host cannot afford, and a lane refused here has learned the
    # reason before it spent 30 minutes in a queue.
    # Admission WAITS and re-samples, it does not refuse once and give up.
    # T-0968 DoD 1: a lane that cannot proceed WAITS and says so — it must not
    # skip or shorten the run. An instantly-terminal refusal forces the lane to
    # poll by hand, and hand-polling a rule is how a rule becomes optional.
    adm_deadline = time.time() + args.wait
    said = ""
    while True:
        ok, reason, pre = admission_check(sd, disk_series=args.disk_series)
        if ok or time.time() >= adm_deadline:
            break
        if reason != said:
            print(f"fleet-slot: {_admission_line(pre)}", file=sys.stderr, flush=True)
            print(f"fleet-slot: WAITING on admission — {reason}",
                  file=sys.stderr, flush=True)
            said = reason
        time.sleep(_POLL * 5)
    print(f"fleet-slot: {_admission_line(pre)}", file=sys.stderr, flush=True)
    # Say what the GREEN read, not only the red. A positive control is for
    # passes too: without the ceiling in the pass line, an ARMED gate that was
    # satisfied and a DISARMED gate that checked nothing are byte-identical in
    # the log, and nobody can tell whether the gate ran.
    print(f"fleet-slot: admission {'PASSED' if ok else 'REFUSED'} — {reason}",
          file=sys.stderr, flush=True)
    if not ok:
        print("fleet-slot: NOT MEASURED — the run did not start.",
              file=sys.stderr, flush=True)
        print(_ask_hint(args.kind, args.note, 0.0, what="refused"),
              file=sys.stderr, flush=True)
        return 75
    try:
        token, adm = acquire(args.kind, pgid=os.getpgrp(), lane=args.lane,
                             note=args.note, wait=args.wait, sd=sd, on_wait=_on_wait)
    except TimeoutError as exc:
        print(f"fleet-slot: {exc}", file=sys.stderr, flush=True)
        print(_ask_hint(args.kind, args.note, time.time() - t0),
              file=sys.stderr, flush=True)
        return 75  # EX_TEMPFAIL — distinguishable from the command's own rc
    waited = time.time() - t0
    print(f"fleet-slot: granted {normalise_kind(args.kind)} slot {token} after "
          f"{waited:.1f}s — {args.note or '(no note)'}", file=sys.stderr, flush=True)
    # The ticket is re-sampled AT THE GRANT, not at the request: a run that
    # waited 14 minutes behind a build was admitted against a host that no
    # longer exists.
    print(f"fleet-slot: at-grant {_admission_line(adm)}", file=sys.stderr, flush=True)

    def _forward(signum, _frame):
        # Kill the GROUP, never the child pid alone: the whole reason the old
        # lock outlived its run is that the survivor was a grandchild.
        if child and child.poll() is None:
            try:
                os.killpg(os.getpgid(child.pid), signum)
            except OSError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _forward)
        except (ValueError, OSError):
            pass

    # While the operator's hand-made stopgap semaphore still exists, hold one of
    # its fds too, so a lane using the broadcast snippet and a lane using this
    # module contend over ONE ceiling instead of two.
    legacy_fh = None
    if normalise_kind(args.kind) == "containerless":
        legacy_fh = take_legacy_slot(sd)
        if legacy_fh is not None:
            print(f"fleet-slot: also holding legacy {legacy_fh.name} "
                  f"(stopgap interop — one ceiling, not two)",
                  file=sys.stderr, flush=True)

    try:
        child = subprocess.Popen(args.cmd, start_new_session=True)
        # Liveness moves to the work. If THIS wrapper is SIGKILLed the slot
        # stays held while the child lives — correct, because the IO is still
        # happening — and is reclaimed on the record once the group is gone.
        adopt(token, os.getpgid(child.pid), sd=sd)
        rc = child.wait()
    finally:
        if token:
            release(token, sd=sd)
        if legacy_fh is not None:
            legacy_fh.close()
    print(f"fleet-slot: released {token} after {time.time() - t0:.1f}s (rc {rc})",
          file=sys.stderr, flush=True)
    return rc


def _cmd_admit(args) -> int:
    ok, reason, rec = admit(note=args.note, lane=args.lane,
                            disk_series=args.disk_series)
    if args.json:
        print(json.dumps(rec))
    else:
        print(f"fleet-slot: containerless — no slot taken. {_admission_line(rec)}")
        print(f"fleet-slot: admission {'PASSED' if ok else 'REFUSED'} — {reason}")
    if not ok:
        print(_ask_hint("containerless", args.note, 0.0, what="refused"),
              file=sys.stderr)
        return 75
    return 0


def _cmd_status(args) -> int:
    snap = snapshot()
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    # Count each pool against ITS OWN ceiling. Summing both against the daemon
    # ceiling printed "4/2", which reads as an overshoot of a limit that was
    # never exceeded — a status line that manufactures an incident.
    daemon = sum(1 for h in snap["holders"] if h["kind"] in ("container", "build"))
    cl = snap.get("containerless_held", 0)
    cl_cap = snap.get("containerless_slots")
    print(f"fleet slots: {daemon}/{snap['slots']} container+build, "
          f"{cl}/{cl_cap if cl_cap is not None else '∞'} containerless"
          + ("  [BUILD — exclusive]" if snap["build_held"] else ""))
    print(f"  state: {snap['state_dir']}")
    for h in snap["holders"]:
        print(f"  HOLD  {h['kind']:<10} {h['age_s']:>7.1f}s  pgid {h['pgid']:<8} "
              f"{h['lane'] or '-'}  {h['note'] or '-'}")
    for i, w in enumerate(snap["waiters"], 1):
        print(f"  WAIT#{i} {w['kind']:<10} {w['age_s']:>7.1f}s  pgid {w['pgid']:<8} "
              f"{w['lane'] or '-'}  {w['note'] or '-'}")
    if not snap["holders"] and not snap["waiters"]:
        print("  (free)")
    for ls in snap.get("legacy_slots") or []:
        state = "HELD" if ls["held"] else "free"
        note = (" ⚠ SUSPECTED LEAK (or an fd owned by a uid we cannot read)"
                if ls["suspected_leak"] else "")
        print(f"  legacy {ls['slot']:<22} {state:<4} fd holders {ls['fd_holders'] or '-'} "
              f"(scanned as uid {ls['scanned_as_uid']}){note}")
    print(f"  {_admission_line(snap['readings'])}")
    print(f"  checked: {snap['method']}")
    return 0


def _cmd_acquire(args) -> int:
    try:
        token, _adm = acquire(args.kind, pgid=args.pgid or os.getpgrp(),
                              lane=args.lane, note=args.note, wait=args.wait)
    except TimeoutError as exc:
        print(f"fleet-slot: {exc}", file=sys.stderr)
        return 75
    print(token)
    return 0


def _cmd_adopt(args) -> int:
    return 0 if adopt(args.token, args.pgid) else 1


def _cmd_release(args) -> int:
    return 0 if release(args.token) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fleet-slot",
        description="Enforced fleet gate for container work and image builds (T-0994).")
    sub = p.add_subparsers(dest="verb", required=True)
    kinds = (*_KINDS, *_KIND_ALIASES)

    r = sub.add_parser("run", help="hold a slot for the lifetime of a command")
    r.add_argument("--kind", choices=kinds, default="container")
    r.add_argument("--lane", default=None, help="owning SID (default $BOT_SQUAD_SID)")
    r.add_argument("--note", default=None, help="what this run is, shown to waiters")
    r.add_argument("--wait", type=float, default=DEFAULT_WAIT)
    r.add_argument("--disk-series", action="store_true",
                   help="also sample /proc/diskstats field 12 (evidence only — "
                        "the metric is disqualified for admission control)")
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    r.set_defaults(fn=_cmd_run)

    a2 = sub.add_parser("admit", help="record an admission ticket WITHOUT a slot "
                                      "(containerless work)")
    a2.add_argument("--note", default=None)
    a2.add_argument("--lane", default=None)
    a2.add_argument("--json", action="store_true")
    a2.add_argument("--disk-series", action="store_true",
                   help="also sample /proc/diskstats field 12 (evidence only)")
    a2.set_defaults(fn=_cmd_admit)

    s = sub.add_parser("status", help="who holds what, who is queued, and what was checked")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_status)

    a = sub.add_parser("acquire", help="take a slot and print its token")
    a.add_argument("--kind", choices=kinds, default="container")
    a.add_argument("--lane", default=None)
    a.add_argument("--note", default=None)
    a.add_argument("--wait", type=float, default=DEFAULT_WAIT)
    a.add_argument("--pgid", type=int, default=None)
    a.set_defaults(fn=_cmd_acquire)

    ad = sub.add_parser("adopt", help="hand a held slot's liveness to a process group")
    ad.add_argument("token")
    ad.add_argument("--pgid", type=int, required=True)
    ad.set_defaults(fn=_cmd_adopt)

    rl = sub.add_parser("release", help="release a held slot by token")
    rl.add_argument("token")
    rl.set_defaults(fn=_cmd_release)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "verb", None) == "run":
        cmd = list(args.cmd or [])
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("fleet-slot run: nothing to run — "
                  "`fleet-slot run --kind container -- docker run ...`", file=sys.stderr)
            return 2
        args.cmd = cmd
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
