#!/usr/bin/env python3
"""T-0968 DoD 5: wall-to-CPU ratio of a suite, GATED versus UNGATED.

WHY THIS METRIC. The harm the fleet suite lock prevents is a suite being
STARVED, and wall-to-CPU measures starvation directly: it is per-suite,
attributable to the run being measured, and does not move because another
linux tenant is busy. A ratio of 1.0 is the floor by construction — a run that
never waits. 35x was measured on this box during the incident that opened
T-0968.

WHY IT INTERLEAVES RATHER THAN TAKING TWO BLOCKS. The claim is a COMPARISON,
and a comparison does not need a quiet host — it needs both arms to see the
SAME host. Two blocks taken hours apart on a box whose load has swung by two
orders of magnitude in one evening are two different experiments wearing one
label. Alternating arms minutes apart share whatever the box is doing, and the
difference between them survives it. So the output is a DISTRIBUTION over
pairs, never a point value per arm.

⚠ CPU IS MEASURED INSIDE THE CONTAINER, NOT OUTSIDE IT. For a docker suite the
`docker run` client does almost no work; the suite's CPU lives in the
container. Timing the client would measure the wrong process and report a
wall-to-CPU ratio of several hundred that means nothing. The command is
therefore wrapped so the process that actually runs the suite reports its own
RUSAGE_CHILDREN.

⚠ io PSI IS SAMPLED ACROSS EACH ARM'S OWN WINDOW, AS A COVARIATE — NOT AS A
GATE, AND NEVER AS A POINT READ. Measured on this box 2026-09-07: a single read
of io PSI full avg10 gave 15.74 while a 54-second window over the same period
ranged 33.41–68.30, i.e. the point read sat below EVERY sample in the window.
The gauge that decides whether other gauges are trustworthy is itself
untrustworthy as a point read. If the two arms ran under materially different
pressure, that is reportable and possibly disqualifying, and it is knowable
only if it was sampled.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SLOT = REPO / "scripts" / "cli" / "fleet_slot.py"

#: Reports the CPU actually burned by the suite, from inside wherever it runs.
_TIMER = (
    "import json,os,resource,subprocess,sys,time\n"
    "t0=time.time()\n"
    "rc=subprocess.call(sys.argv[1:])\n"
    "ru=resource.getrusage(resource.RUSAGE_CHILDREN)\n"
    "sys.stderr.write('@@RATIO@@'+json.dumps({\n"
    "    'wall_s':round(time.time()-t0,3),\n"
    "    'cpu_s':round(ru.ru_utime+ru.ru_stime,3),\n"
    "    'rc':rc})+'\\n')\n"
    "sys.exit(rc)\n"
)


def _psi_full_avg10() -> float | None:
    """``full avg10`` from /proc/pressure/io, or None where PSI is absent."""
    try:
        for line in Path("/proc/pressure/io").read_text().splitlines():
            if line.startswith("full "):
                for field in line.split():
                    if field.startswith("avg10="):
                        return float(field.split("=", 1)[1])
    except (OSError, ValueError):
        return None
    return None


class _PsiWindow:
    """Sample io PSI for as long as an arm runs. A window, never a point."""

    def __init__(self, every: float = 2.0):
        self.every = every
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                v = _psi_full_avg10()
                if v is not None:
                    self.samples.append(v)
                self._stop.wait(self.every)
        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)
        return False

    def summary(self) -> dict:
        # A single sample is reported AS a single sample. Calling one reading a
        # median would launder exactly the mistake this class exists to stop.
        s = sorted(self.samples)
        if not s:
            return {"samples": 0, "note": "PSI unavailable on this host"}
        return {"samples": len(s), "median": statistics.median(s),
                "max": s[-1], "min": s[0],
                "over_50_pct": round(100.0 * sum(1 for v in s if v > 50) / len(s), 1)}


def _drop_pyc(root: Path) -> int:
    """Remove __pycache__ so a later arm cannot inherit an earlier warm compile."""
    n = 0
    for d in root.rglob("__pycache__"):
        try:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            n += 1
        except OSError:
            pass
    return n


def _run_arm(cmd: list[str], *, gated: bool, kind: str, note: str,
             wait: float, timeout: float, wrap: bool = True) -> dict:
    """One arm. Returns its wall/cpu/ratio plus the PSI window it ran under.

    ``wrap=False`` means the CALLER has already placed the timer where the work
    actually happens. That is mandatory for a docker suite: wrapping here would
    time the ``docker run`` CLIENT, which does almost no work, and report a
    ratio of several hundred for a perfectly healthy run. Use ``--print-timer``
    to get the script and splice it in as the container's own command.
    """
    inner = [sys.executable, "-c", _TIMER, *cmd] if wrap else list(cmd)
    argv = ([str(SLOT), "run", "--kind", kind, "--note", note,
             "--wait", str(wait), "--"] + inner) if gated else inner

    env = dict(os.environ)
    # An outer slot would exempt the GATED arm from taking one -- correct
    # behaviour, and fatal to this measurement, since both arms would then be
    # ungated and the comparison would read as "gating changes nothing".
    env.pop("BOT_SQUAD_FLEET_SLOT", None)
    env.pop("BOT_SQUAD_FLEET_SLOT_KIND", None)

    t0 = time.time()
    with _PsiWindow() as psi:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env)
    outer_wall = time.time() - t0

    inner_rec = None
    for line in proc.stderr.splitlines():
        if line.startswith("@@RATIO@@"):
            inner_rec = json.loads(line[len("@@RATIO@@"):])
    granted_after = None
    for line in proc.stderr.splitlines():
        if "granted" in line and " after " in line:
            try:
                granted_after = float(line.split(" after ")[1].split("s")[0])
            except (IndexError, ValueError):
                pass

    rec = {
        "arm": "gated" if gated else "ungated",
        "outer_wall_s": round(outer_wall, 3),
        "rc": proc.returncode,
        "psi_io_full_avg10": psi.summary(),
        # The gate's own queueing is NOT part of the suite's ratio: it is the
        # price of the gate, reported separately so neither number hides in
        # the other. Occupation time is what the fleet forecast needs.
        "queue_wait_s": granted_after,
    }
    if inner_rec is None:
        rec.update({"measured": False,
                    "why": "the timer never reported — the suite did not start"})
        return rec
    wall, cpu = inner_rec["wall_s"], inner_rec["cpu_s"]
    rec.update({
        "measured": True,
        "occupation_s": wall,          # what the slot was actually held FOR
        "cpu_s": cpu,
        "wall_to_cpu": round(wall / cpu, 2) if cpu > 0 else None,
        "suite_rc": inner_rec["rc"],
    })
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", type=int, default=3,
                    help="ungated/gated pairs to interleave (default 3)")
    ap.add_argument("--kind", default="container")
    ap.add_argument("--wait", type=float, default=1800.0)
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--pyc-root", default=None,
                    help="drop __pycache__ under here between arms")
    ap.add_argument("--out", default=None, help="write the JSON record here")
    ap.add_argument("--no-wrap", action="store_true",
                    help="the command ALREADY emits the @@RATIO@@ line — "
                         "required for a docker suite, where the CPU that "
                         "matters is burned inside the container and timing "
                         "the client would measure the wrong process")
    ap.add_argument("--print-timer", action="store_true",
                    help="print the timer script and exit, to splice into a "
                         "container command")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- the suite command to measure")
    args = ap.parse_args()

    if args.print_timer:
        sys.stdout.write(_TIMER)
        return 0

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        ap.error("nothing to measure — pass the suite after `--`")

    pyc_root = Path(args.pyc_root) if args.pyc_root else None
    runs = []
    for i in range(args.pairs):
        for gated in (False, True):
            if pyc_root:
                dropped = _drop_pyc(pyc_root)
            else:
                dropped = None
            rec = _run_arm(cmd, gated=gated, kind=args.kind,
                           note=f"T-0968 DoD5 {'gated' if gated else 'ungated'} #{i + 1}",
                           wait=args.wait, timeout=args.timeout,
                           wrap=not args.no_wrap)
            rec["pair"] = i + 1
            rec["pycache_dirs_dropped"] = dropped
            runs.append(rec)
            print(f"pair {i + 1} {rec['arm']:<8} "
                  f"ratio {rec.get('wall_to_cpu')} "
                  f"occupation {rec.get('occupation_s')}s "
                  f"queue {rec['queue_wait_s'] if rec.get('queue_wait_s') is not None else 'n/a'} "
                  f"psi {rec['psi_io_full_avg10']}", flush=True)

    def ratios(arm):
        return [r["wall_to_cpu"] for r in runs
                if r["arm"] == arm and r.get("wall_to_cpu") is not None]

    ung, gat = ratios("ungated"), ratios("gated")
    report = {
        "runs": runs,
        "ungated_ratios": ung,
        "gated_ratios": gat,
        "ungated_median": statistics.median(ung) if ung else None,
        "gated_median": statistics.median(gat) if gat else None,
        # ⚠ The floor is 1.0 BY CONSTRUCTION, so a healthy baseline is not
        # needed to interpret the direction of this comparison — only to say
        # how much of the remaining gap is this suite's irreducible overhead.
        "floor": 1.0,
        "caveats": [
            "A COMPARISON OF DISTRIBUTIONS, NOT OF TWO POINTS. Per-arm medians "
            "over interleaved pairs; a single pair proves nothing.",
            "io PSI is a COVARIATE here. If the two arms' PSI windows differ "
            "materially, the comparison is confounded and must be reported as "
            "such rather than concluded from.",
            "The healthy-case baseline is NOT measured by this tool. It needs "
            "an unloaded host, and on this box that may never arrive (T-1081).",
            "queue_wait_s is the PRICE of the gate and is deliberately kept "
            "out of the ratio, so neither number hides inside the other.",
        ],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "runs"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
