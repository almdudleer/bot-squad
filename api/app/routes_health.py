"""Health endpoint — surfaces worker liveness via heartbeat freshness."""
from __future__ import annotations

import json
import os
import re
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


# ---------------------------------------------------------------------------
# T-0754: drift explained by a deploy that is landing RIGHT NOW
# ---------------------------------------------------------------------------
# The markers above cover a drift a RESTART will close. They cannot cover the
# case measured on the 75dc01a deploy (web-only, worker/ byte-identical): T-0717
# leg 1 correctly skipped the restart, so there was correctly no marker to write
# — and health reported a bare `sha_drift` for a continuous 62.3s while nothing
# whatsoever was wrong.
#
# What that measurement CORRECTED, and why it is what makes this fix possible:
# the lagging side was the API, not the worker. worker/ being byte-identical
# means the worker's effective sha follows the new checkout immediately, while
# the OLD API container keeps answering /api/health with its own baked-in sha
# until it is recreated. So the process REPORTING the drift is the one that is
# behind, and it is behind because A DEPLOY IS IN FLIGHT — a fact already on
# disk, in the same job files routes_runs.py reads.
#
# NOT the blanket post-deploy grace period (refused five times). Nothing is
# excused by elapsed time since a deploy. The excuse requires a job file the
# system itself wrote AND that job's recorded `target_sha` to equal the sha of
# the side that has ALREADY converged — i.e. the deploy has to explain THIS
# drift, not merely coincide with it. When the job leaves queue/processing the
# excuse ends on its own; there is no timer to tune.
#
# The coverage is STRUCTURAL, not lucky: the staging recipe asserts the running
# container's BOT_SQUAD_GIT_SHA equals the deployed sha (T-0379) and then smokes
# /api/health, so the API cannot still be reporting the old sha by the time the
# recipe exits and `_finish` moves the job out of processing/. Measured on
# 75dc01a: job in processing 07:43:55.5 → 07:46:02.9, drift 07:44:51.7 →
# ≤07:46:02.2 — strictly contained, with ~56s of lead-in.
_DEPLOY_JOB_STATES = (("processing", "in_flight"), ("queue", "queued"))
# A full 40-hex sha, matched EXACTLY. A prefix match would weaken the one thing
# this excuse rests on — that the deploy's target and the converged side are the
# same commit — so a short or malformed sha buys no excuse at all.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The bound, and it is chosen against a number rather than by feel: R-0005 needs
# the breach condition to HOLD FOR 600s before it fires. Lapsing the excuse at
# 300s from the deploy's last sign of life guarantees a wedged deploy is back to
# a bare `sha_drift` well before the monitor could ever have breached on it —
# so this cannot hide the failure it is meant to explain away. (A deploy has its
# own supervision: two watchdogs and the 2h orphan reaper. A marker stranded in
# processing/ by a killed worker must not silence health for those two hours.)
_DEPLOY_PROGRESS_DEADLINE_SECONDS = 300


# ---------------------------------------------------------------------------
# T-0824: the INSTALL TREE's sha — the term that was missing entirely
# ---------------------------------------------------------------------------
# Two byte-identical QUIET payloads meant opposite things, measured on this
# install twelve hours apart:
#
#   12:56Z  api 3749ee8 · worker 3749ee8 · health ABSENT — worker 22 modules STALE
#   18:53Z  api 5ae2811 · worker 5ae2811 · health ABSENT — genuinely converged
#
# Same keys, same absence, opposite meanings. No smarter comparison of the two
# existing terms can separate them, because in BOTH of them those two terms are
# equal — the state they differ on (what the install tree is at) was not on the
# surface at all. It is a missing-DATA defect, not a comparison defect.
#
# The hole has a precise shape: `sha_drift` compares api-vs-worker, so it fires
# truthfully whenever the api HAS moved, and fails exactly when NEITHER container
# moved. That is precisely what a worker-only or roles-only deploy produces — the
# CHEAP deploy path, taken twice in one day on purpose to skip a 35-minute docker
# build. The blind spot is not exotic; it is the common case.
#
# ⚠ The sha is NOT already in hand, and assuming it was is how this stayed
# invisible: `worker.git_sha` is `deploy.effective_worker_git_sha()` — the frozen
# BOOT sha, advanced to the deployed sha only when `worker/` is byte-identical.
# It reads as the tree's sha on a converged install because the two numbers
# coincide there, and that coincidence is exactly the state where it tells you
# nothing. Measured 2026-07-30: `_worker/heartbeat` is 41 bytes, one sha, and it
# is the worker's, not the tree's.
#
# So the worker publishes the tree HEAD beside the heartbeat
# (`deploy.publish_install_tree_sha`) and this reads it. The API cannot compute
# it itself — the container mounts `./config` and `./data` and nothing else;
# there is no git tree inside it.
_INSTALL_MARKER = "install_tree.json"


def _install_state(worker_dir: Path) -> dict:
    """What the install tree is at, or an EXPLICIT unknown.

    Never returns a bare ``None`` for "we could not look". An absent field reads
    identically to a real negative, and the identity of a real negative and an
    unknown IS this ticket — so the unknown carries a ``reason`` and the caller
    raises a flag for it rather than falling quiet. A payload that cannot answer
    "is the worker running the deployed code?" must not look like one that
    answered "yes".

    Read from the heartbeat's own directory, so it describes the same install the
    drift was measured on by construction (the same reason ``_deploy_state`` is
    derived from the heartbeat path rather than from ``api_config.data_dir``).
    """
    path = worker_dir / _INSTALL_MARKER
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        # The overwhelmingly likely cause, and worth naming separately: a worker
        # running code older than T-0824. It clears on the next worker restart.
        return {"git_sha": None, "reason": "no_marker"}
    except (OSError, ValueError):
        return {"git_sha": None, "reason": "unreadable_marker"}
    if not isinstance(raw, dict):
        return {"git_sha": None, "reason": "unreadable_marker"}
    sha = raw.get("git_sha")
    if not isinstance(sha, str) or not _SHA_RE.match(sha.strip().lower()):
        return {"git_sha": None, "reason": "malformed_marker"}
    try:
        at = float(raw.get("at") or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    return {"git_sha": sha.strip().lower(), "at": at}


def _deploy_row(
    path: Path, state: str, slug: str, api_sha: str, worker_sha: str,
    install_sha: str | None = None,
) -> dict | None:
    """One in-flight deploy job, IF it explains the drift we are looking at.

    Returns None — i.e. no excuse, fall back to the alarm — for every degraded
    input: unreadable/corrupt payload, a pre-T-0754 worker that persisted no
    `target_sha`, a malformed one, or a perfectly good deploy of some OTHER
    commit. That last case is the point of form (b): a deploy merely being in
    progress is not evidence about this drift.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    target_sha = raw.get("target_sha")
    if not isinstance(target_sha, str) or not _SHA_RE.match(target_sha.strip().lower()):
        return None
    target_sha = target_sha.strip().lower()
    # Which side has already reached the deploy's target? That is the side the
    # deploy has landed on, and its counterpart is the one still catching up.
    # On the measured shape it is the WORKER (byte-identical subtree, no
    # restart); on a restart-bearing deploy it is the API. Both are real, and
    # naming which keeps the record diagnosable instead of just "a deploy".
    if target_sha == worker_sha.strip().lower():
        converged = "worker"
    elif target_sha == api_sha.strip().lower():
        converged = "api"
    elif install_sha and target_sha == install_sha.strip().lower():
        # T-0824: the third side, and the one a worker-only deploy lands on
        # first. Between the recipe's ff-merge and the restart it launches, the
        # TREE is on the target and neither process is — a real, self-healing
        # window that would otherwise read as `worker_stale` with no explanation.
        # Same evidential bar as the other two: this deploy's own recorded target
        # has to equal the sha of a side that has actually reached it.
        converged = "install"
    else:
        return None
    try:
        queued_at = float(raw.get("queued_at") or 0.0)
    except (TypeError, ValueError):
        return None
    # Anchor the deadline on the deploy's last SIGN OF LIFE, not on enqueue: the
    # run log is appended to continuously by the recipe (it is what the worker's
    # own no-progress watchdog measures), so a long-but-healthy build keeps its
    # excuse while a wedged one loses it. A queued job has no log yet and falls
    # back to queued_at — which also expires a job parked behind a paused queue.
    last_progress = None
    queue_id = raw.get("queue_id")
    if isinstance(queue_id, str) and queue_id:
        try:
            last_progress = (
                path.parent.parent / "runs" / f"{queue_id}.log"
            ).stat().st_mtime
        except OSError:
            last_progress = None
    expected_by = max(queued_at, last_progress or 0.0) + _DEPLOY_PROGRESS_DEADLINE_SECONDS
    return {
        "state": state,
        "slug": slug,
        "queue_id": queue_id if isinstance(queue_id, str) else "",
        "target_sha": target_sha,
        "converged": converged,
        "since": queued_at,
        "last_progress": last_progress,
        "expected_by": expected_by,
        "overdue": time.time() > expected_by,
        "reason": str(raw.get("reason") or ""),
    }


def _deploy_state(
    data_dir: Path, api_sha: str, worker_sha: str, install_sha: str | None = None
) -> dict | None:
    """The in-flight deploy a drift reading should be interpreted against.

    Scans every project's job dirs rather than guessing which slug owns this
    install: the sha match does the selecting on evidence, and another project's
    deploy can never carry a `target_sha` equal to this install's api/worker sha.
    Only reached when a drift has already been detected, so the cost is paid on
    the rare path — never on a healthy poll.
    """
    best: dict | None = None
    try:
        slug_dirs = sorted(p for p in data_dir.iterdir() if p.is_dir())
    except OSError:
        return None
    for slug_dir in slug_dirs:
        base = slug_dir / "_jobs" / "deploy"
        for dirname, state in _DEPLOY_JOB_STATES:
            try:
                jobs = sorted((base / dirname).glob("*.json"))
            except OSError:
                continue
            for job in jobs:
                row = _deploy_row(
                    job, state, slug_dir.name, api_sha, worker_sha, install_sha
                )
                if row is None:
                    continue
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
    #
    # T-0754 adds the third reading, for the drift NO restart will ever close
    # because none is owed. See `_deploy_state` — an in-flight deploy job whose
    # recorded target_sha equals the side that has already converged reports
    # `deploy_pending`. Same principle as the restart marker, same bound: an
    # explanation the system wrote down, and only while it is still current.
    #
    # T-0824 adds the FOURTH reading, and the one the endpoint is actually asked:
    # `worker_stale` — the running worker is not executing the DEPLOYED code.
    # That question has three terms and only two were on this surface, so it was
    # unanswerable rather than answered wrongly; see `_install_state`. It is kept
    # SEPARATE from `sha_drift` rather than folded into it because they are
    # different questions with different consequences: `worker_stale` means live
    # behaviour differs from the code that was shipped, `sha_drift` means the api
    # CONTAINER is behind. Both can hold at once, and either can hold alone.
    install = _install_state(heartbeat.parent)
    install_sha = install.get("git_sha")

    problems: list[str] = []
    restart = None
    deploy = None
    # Evaluate the two comparisons FIRST, so the shared explanations (a restart
    # the worker recorded, a deploy in flight) are looked up once and applied to
    # whichever of them fired.
    worker_stale = bool(alive and worker_sha and install_sha and worker_sha != install_sha)
    sha_drift = bool(
        alive and worker_sha and api_sha and api_sha != "unknown" and worker_sha != api_sha
    )
    if not alive:
        problems.append("dead_heartbeat")
    else:
        if worker_sha and install_sha is None:
            # ⚠ The unknown must NOT be silent, and it is raised INDEPENDENTLY of
            # any drift. A missing install term makes the payload unable to answer
            # the question, and rendering that as the same quiet payload a
            # converged install produces would recreate this ticket's exact defect
            # one level up — two identical readings, one meaning "converged", one
            # meaning "we cannot see". The reason is carried in `install.reason`.
            problems.append("install_sha_unknown")
        if worker_stale or sha_drift:
            restart = _restart_state(heartbeat.parent)
            pending = bool(restart and not restart["overdue"])
            if not pending:
                # Deliberately derived from the HEARTBEAT path, not from
                # `api_config.data_dir` (which is `CONFIG_DIR.parent / "data"`, a
                # different derivation that agrees only by convention). This reader
                # must answer about the same install the drift was measured on;
                # pointed elsewhere it would return a well-formed EMPTY result and
                # silently report `sha_drift` forever — most convincingly during a
                # real deploy.
                deploy = _deploy_state(
                    heartbeat.parent.parent, api_sha, worker_sha, install_sha
                )
            landing = bool(deploy and not deploy["overdue"])
            # A restart the worker recorded is the more specific statement — it
            # names the action that will close this drift. The deploy is not
            # consulted at all when one is pending, so an absent `deploy` key on
            # that path means "not looked at", never "no deploy is running".
            explained = "restart_pending" if pending else "deploy_pending" if landing else ""
            flags = []
            if worker_stale:
                flags.append(explained or "worker_stale")
            if sha_drift:
                flags.append(explained or "sha_drift")
            for flag in flags:
                # Both comparisons can resolve to the SAME explanation (one
                # restart closes both); report it once.
                if flag not in problems:
                    problems.append(flag)
    if problems:
        worker["health"] = problems
    if deploy:
        # Carried on the OVERDUE case too, for the same reason the restart block
        # is: "a deploy of <sha> has been in flight since 07:43 and still has
        # not converged" is the headline for whoever R-0005 pages.
        worker["deploy"] = deploy
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
        # T-0824: the DEPLOYED sha — what the install tree's HEAD is right now.
        # Top-level, beside the api's own sha, because it is a property of the
        # INSTALL and not of the worker: `git_sha` (api container), `worker.git_sha`
        # (running worker) and this are the three sides, and every question this
        # endpoint is asked is a comparison between two of them. Always present,
        # carrying `{"git_sha": null, "reason": ...}` when it could not be read —
        # an absent key would put a consumer back to guessing.
        "install": install,
        "uptime": time.monotonic() - _STARTED_AT,
        "worker": worker,
    }
