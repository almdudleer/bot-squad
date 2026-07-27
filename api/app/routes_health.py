"""Health endpoint — surfaces worker liveness via heartbeat freshness."""
from __future__ import annotations

import json
import os
import time
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

from fastapi import APIRouter, Depends, Request

router = APIRouter()

# T-0739: the two markers the worker writes under `_worker/` to say a restart is
# owed (deferred by T-0305's rate limiter) or already launched and not yet up.
# Read here — not asked of the worker — deliberately: the whole point is to
# explain drift, and the worker that is mid-restart is exactly the one that
# can't answer. Same directory as the heartbeat, same mount, one stat each.
_RESTART_MARKERS = (
    ("restart_inflight.json", "in_flight"),
    ("restart_pending.json", "deferred"),
)
# Fallback deadline for a pre-T-0739 marker written without `expected_by`
# (5min rate-limit window + the worker's convergence grace). Bounded on purpose:
# an unbounded pending state would re-hide the T-0717 bug behind a nicer word.
_LEGACY_RESTART_DEADLINE_SECONDS = 480


def _restart_state(worker_dir: Path) -> dict | None:
    """The pending/in-flight restart a drift reading should be interpreted
    against, or None when nothing is coming.

    Mirrors ``bot_squad_worker.deploy.restart_pending_state``. Both markers can
    briefly coexist (the catch-up tick launches before clearing its deferral), so
    the later deadline wins — it's the one that still has time to make good.
    An unreadable/corrupt marker is skipped: degrading to a bare ``sha_drift``
    is a false alarm, and treating garbage as "a restart is coming" would be a
    false all-clear (T-0717's degradation rule).
    """
    best: dict | None = None
    for name, state in _RESTART_MARKERS:
        try:
            raw = json.loads((worker_dir / name).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            since = float(raw.get("at") or 0.0)
            expected_by = float(
                raw.get("expected_by") or (since + _LEGACY_RESTART_DEADLINE_SECONDS)
            )
        except (TypeError, ValueError):
            continue
        row = {
            "state": state,
            "since": since,
            "expected_by": expected_by,
            # Past its deadline the marker still explains WHY someone is staring
            # at drift, but it no longer excuses it — the flag goes back to
            # sha_drift and R-0005 is free to breach.
            "overdue": time.time() > expected_by,
            "reason": str(raw.get("reason") or ""),
        }
        if best is None or row["expected_by"] > best["expected_by"]:
            best = row
    return best


def _pkg_version() -> str:
    try:
        return version("bot-squad-api")
    except PackageNotFoundError:
        return "0.0.0"


_STARTED_AT = time.monotonic()


@router.get("/health")
def health(request: Request) -> dict:
    heartbeat: Path = request.app.state.heartbeat_path
    alive = False
    last_hb = None
    worker_sha = None
    if heartbeat.exists():
        last_hb = heartbeat.stat().st_mtime
        # Worker writes every 60s; >5min stale = dead.
        alive = (time.time() - last_hb) < 300
        # T-0456: the worker writes its boot_git_sha as the heartbeat body so we
        # can detect API/worker sha drift here (an empty body = a pre-T-0456 worker
        # or a sha-lookup fallback — treated as unknown, never a false drift).
        try:
            worker_sha = heartbeat.read_text().strip() or None
        except OSError:
            worker_sha = None

    api_sha = os.environ.get("BOT_SQUAD_GIT_SHA", "unknown")

    worker: dict = {"alive": alive, "last_heartbeat": last_hb, "git_sha": worker_sha}
    # T-0456: FAILURE-ONLY health signal — worker.health is present ONLY when there
    # is a real problem (no green noise). A dead heartbeat is the actionable signal
    # on its own; sha drift is only meaningful while the worker is alive (a dead
    # worker's last-written sha tells us nothing). Drift needs BOTH shas known.
    #
    # T-0739 splits the drift flag in two. After T-0717 a post-deploy drift is
    # USUALLY the expected, self-healing tail of a restart that fired or was
    # deferred — but it emitted the identical `sha_drift` as the failure T-0717
    # existed to fix, so an operator (and R-0005) could not tell "wait 20s" from
    # "the restart was dropped and nobody is coming". A restart the worker has
    # RECORDED as owed, and whose own deadline has not passed, reports
    # `restart_pending`; everything else still reports `sha_drift`. Note what
    # this is NOT: a blanket post-deploy grace period. Nothing is suppressed by
    # elapsed time since a deploy — only by a marker the worker wrote saying a
    # specific restart is coming, and only until that restart is overdue.
    problems = []
    restart = None
    if not alive:
        problems.append("dead_heartbeat")
    elif worker_sha and api_sha and api_sha != "unknown" and worker_sha != api_sha:
        restart = _restart_state(heartbeat.parent)
        problems.append(
            "restart_pending" if restart and not restart["overdue"] else "sha_drift"
        )
    if problems:
        worker["health"] = problems
    if restart:
        # Carried on the OVERDUE case too: "a restart has been pending since
        # 03:34 and never landed" is the single most useful line for whoever
        # R-0005 pages, and it's exactly what nobody had on the T-0717 night.
        worker["restart"] = restart

    return {
        "ok": True,
        "version": _pkg_version(),
        # T-0379: the git sha baked into this image at build time. Lets the
        # deploy recipe / monitor (and a human curl) assert that the RUNNING
        # container is the commit that was deployed — closing the stale-image
        # gap where a deploy 'succeeded' but shipped an older HEAD.
        "git_sha": api_sha,
        "uptime": time.monotonic() - _STARTED_AT,
        "worker": worker,
    }
