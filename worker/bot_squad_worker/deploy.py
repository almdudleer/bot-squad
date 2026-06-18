"""Deploy queue + per-project recipe runner.

Public API
----------
enqueue(cfg, slug, target, reason, requested_by) -> queue_id: str
    Write a JSON request file to data/<slug>/_jobs/deploy/queue/.

list_queued(cfg, slug) -> list[Path]
    Return queue files sorted oldest-first (by filename, which is ts-prefixed).

run_next(cfg, slug) -> DeployResult | None
    Pop the oldest queued request, run the commit-guard + clone prep, run recipe.
    - Returns None if queue is empty.
    - Returns None if the editing clone has unpushed local-only commits (queue
      file stays — push first), or deploy-clone provisioning failed.
    - Returns None if the legacy in-place tree is dirty (queue file stays).
      When a deploy clone is configured (T-0143) the recipe runs in a disposable
      clone force-synced to origin, so dev-tree dirtiness no longer gates.
    - Returns DeployResult(ok, returncode, queue_id, log_path) on success or
      failure (rc != 0 or recipe file missing → rc=99).

Quiescence-ignore patterns (same as cctv):
    Any line from ``git status --porcelain`` whose filename matches
    logs/, cache/, __pycache__/, .pyc, *.tmp, .claude/ is ignored when
    deciding whether the tree is "dirty".

Queue file lifecycle:
    queue/<id>.json
        → processing/<id>.json   (while recipe runs)
        → processed/<id>.ok      (on success)
        → processed/<id>.fail.<rc>  (on failure)

Recipe convention: data/<slug>/deploy/<target>.sh
    Executed via ``bash <recipe_path>`` with cwd = the exec clone for the
    target (``project.repo_for_target``): the deploy clone when configured,
    else the dev clone (staging) / master clone (prod).
    stdout+stderr captured to data/<slug>/_jobs/deploy/runs/<id>.log.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# Sentinel deploy exit codes (T-0212). Distinct values so the run-log marker,
# the TG/operator alert text, and the tests can all tell *why* a deploy failed
# apart from a plain non-zero recipe rc.
RC_TIMEOUT = 124       # hard wall-clock backstop tripped (build ran too long)
RC_NO_PROGRESS = 125   # no-progress watchdog: zero run-log output for N minutes
RC_ORPHAN = 126        # stale-orphan reaper: file stranded in processing/ swept

# Patterns in git --porcelain output that are ignored for cleanliness checks.
# Each is tested against the full line (e.g. " M logs/something.log").
_QUIESCENCE_IGNORE = (
    "logs/",
    "cache/",
    "__pycache__/",
    ".pyc",
    ".tmp",
    # T-0204: per-session Claude scratch (e.g. watchrobot git-TRACKS
    # .claude/scheduled_tasks.lock, rewritten every session). It is never part
    # of the deployed artifact, so it must never block a deploy on the legacy
    # in-place path.
    ".claude/",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DeployResult:
    ok: bool
    returncode: int
    queue_id: str
    log_path: Path | None
    # Same-(slug,target) queue entries collapsed into this single run, including
    # the leading one. Always >= 1 on success/failure. Lets the caller report
    # "5 queued requests collapsed into one deploy" instead of pretending the
    # other 4 happened separately.
    collapsed_count: int = 1
    collapsed_reasons: tuple[str, ...] = ()
    # T-0212: set when the watchdog (not the recipe) ended the run —
    # "no_progress" (no run-log output for N min) or "timeout" (hard backstop).
    # None on a normal exit (success OR an honest non-zero recipe rc). Lets the
    # caller fire the loud, TARGETED operator alert only for a wedged build.
    killed_reason: str | None = None


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def _queue_dir(cfg: "Config", slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "queue"


def _processing_dir(cfg: "Config", slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "processing"


def _processed_dir(cfg: "Config", slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "processed"


def _runs_dir(cfg: "Config", slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "runs"


def _pause_marker(cfg: "Config", slug: str) -> Path:
    """Presence-of-file gate: when this exists, deploy_monitor skips the queue."""
    return cfg.data_dir / slug / "_jobs" / "deploy" / "PAUSED.json"


def is_paused(cfg: "Config", slug: str) -> dict | None:
    """Return the pause metadata dict if the slug's deploys are paused, else None."""
    marker = _pause_marker(cfg, slug)
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except Exception:
        # Malformed marker file still counts as paused — fail-closed.
        return {"reason": "(unparseable PAUSED.json)", "paused_by": "?", "paused_at": 0.0}


def pause(cfg: "Config", slug: str, reason: str, requested_by: str) -> dict:
    """Create the pause marker. Returns the metadata that was written."""
    marker = _pause_marker(cfg, slug)
    marker.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "reason": reason or "(no reason given)",
        "paused_by": requested_by,
        "paused_at": time.time(),
    }
    marker.write_text(json.dumps(meta, indent=2))
    log.info("deploy.pause: %s paused by %s — %s", slug, requested_by, meta["reason"])
    return meta


def resume(cfg: "Config", slug: str) -> bool:
    """Remove the pause marker. Returns True if it was present (i.e. actually resumed)."""
    marker = _pause_marker(cfg, slug)
    if not marker.exists():
        return False
    marker.unlink()
    log.info("deploy.resume: %s resumed", slug)
    return True


def _recipe_path(cfg: "Config", slug: str, target: str) -> Path:
    return cfg.data_dir / slug / "deploy" / f"{target}.sh"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(
    cfg: "Config",
    slug: str,
    target: str,
    reason: str,
    requested_by: str,
) -> str:
    """Write a deploy request file and return its queue_id.

    Raises ValueError if ``target`` is not in the project's deploy_targets.
    Raises KeyError if ``slug`` is not registered.
    """
    project = cfg.projects[slug]
    if target not in project.deploy_targets:
        raise ValueError(
            f"unknown target {target!r} for {slug!r}; "
            f"allowed: {list(project.deploy_targets)}"
        )

    queue_dir = _queue_dir(cfg, slug)
    queue_dir.mkdir(parents=True, exist_ok=True)

    queue_id = str(uuid.uuid4())
    queued_at = time.time()
    # filename: <epoch_ms>-<queue_id>.json  — sorts oldest-first lexicographically
    ts_prefix = int(queued_at * 1000)
    filename = f"{ts_prefix}-{queue_id}.json"

    payload = {
        "queue_id": queue_id,
        "slug": slug,
        "target": target,
        "reason": reason,
        "requested_by": requested_by,
        "queued_at": queued_at,
    }
    (queue_dir / filename).write_text(json.dumps(payload, indent=2))
    log.info("deploy.enqueue: %s/%s queued as %s", slug, target, queue_id)
    return queue_id


def list_queued(cfg: "Config", slug: str) -> list[Path]:
    """Return queue files sorted oldest-first."""
    queue_dir = _queue_dir(cfg, slug)
    if not queue_dir.exists():
        return []
    return sorted(queue_dir.glob("*.json"))


def is_clean_for_target(cfg: "Config", slug: str, target: str) -> bool:
    """Public "is this deploy clear to start?" check for ``target``.

    Lets callers (e.g. the deploy_monitor in jobs.py) gate user-facing
    notifications without re-implementing the check or popping a queue file.

    Returns False when:
    - (legacy, no deploy clone) the exec/editing clone has a dirty working
      tree — the recipe runs in place, so uncommitted edits would leak in; OR
    - the EDITING clone (dev/master) has local-only commits not yet on origin
      (T-0110/T-0116) — a deploy ships ``origin/<branch>`` and would silently
      omit them, so the agent must push first.

    When a deploy clone is configured (T-0143), the dev tree's dirty/clean
    state is NO LONGER a gate: the deploy runs from a disposable clone that
    ``run_next`` force-syncs to origin. Only the unpushed-commit guard remains.
    """
    project = cfg.projects[slug]
    edit_repo = project.editing_repo_for_target(target)
    if not project.uses_deploy_clone(target):
        # Legacy in-place deploy: the exec clone IS the editing clone — its
        # uncommitted edits would leak into the recipe, so gate on cleanliness.
        if not _is_clean(edit_repo):
            return False
    return not _local_only_commits(edit_repo)


def run_next(cfg: "Config", slug: str) -> DeployResult | None:
    """Run the next queued deploy for ``slug``.

    Returns None if:
    - the queue is empty, or
    - the git working tree is dirty (after ignoring quiescence patterns).

    On success/failure, returns a DeployResult. Same-(slug,target) queue
    entries trailing the oldest are collapsed into this single run: there's
    no reason to redeploy identical code five times because five sessions
    each queued the same target. All collapsed queue files land in
    `processed/` with the same outcome suffix (.ok or .fail.<rc>), and
    `DeployResult.collapsed_count` / `.collapsed_reasons` carry the count
    + reason list for the caller's user-facing message.
    """
    queued = list_queued(cfg, slug)
    if not queued:
        return None

    project = cfg.projects[slug]

    # Pop oldest
    queue_file = queued[0]
    payload = json.loads(queue_file.read_text())
    queue_id = payload["queue_id"]
    target = payload["target"]

    # Pick the clone the recipe EXECUTES in: prod → master clone; staging/etc
    # → deploy clone if configured (T-0143), else dev clone. The EDITING clone
    # (dev/master) is where the commit-guard looks for unpushed work.
    repo = project.repo_for_target(target)
    edit_repo = project.editing_repo_for_target(target)
    uses_deploy = project.uses_deploy_clone(target)

    # Commit-guard (T-0110/T-0116/T-0143): refuse when the EDITING clone has
    # local-only commits not yet on origin. With a deploy clone the deploy ships
    # origin/<branch>, so unpushed commits would be silently OMITTED from the
    # release; without one the recipe's `git merge --ff-only origin/<branch>`
    # would wipe them. Either way the fix is the same — push first. List them
    # loudly so the agent can recover. Runs regardless of recipe presence so a
    # fresh-host install inherits the guard.
    # Bypass for tests / emergency via BOT_SQUAD_DEPLOY_ALLOW_LOCAL_COMMITS=1.
    if os.environ.get("BOT_SQUAD_DEPLOY_ALLOW_LOCAL_COMMITS") != "1":
        local_only = _local_only_commits(edit_repo)
        if local_only:
            log.error(
                "deploy.run_next: %s/%s REFUSED — %d local-only commit(s) on %s not pushed to origin "
                "(a deploy ships origin/<branch> and would silently omit them):\n    %s\n"
                "Recover by pushing them to origin (or cherry-picking into the canonical dev clone), then re-deploy. "
                "Set BOT_SQUAD_DEPLOY_ALLOW_LOCAL_COMMITS=1 to bypass.",
                slug, target, len(local_only), edit_repo, "\n    ".join(local_only),
            )
            # Leave the queue file in place so the next tick picks it up once
            # the agent has pushed.
            return None

    if uses_deploy:
        # Provision/refresh the disposable deploy clone to origin/<branch>. The
        # force-sync guarantees a clean tree at the latest pushed commit, so the
        # shared dev tree's dirty/clean state no longer gates the deploy (T-0143).
        if not _ensure_deploy_clone(edit_repo, repo, project.deploy_branch):
            log.error(
                "deploy.run_next: %s/%s deploy-clone provisioning failed (%s) — "
                "leaving queue file for retry next tick.",
                slug, target, repo,
            )
            return None
    else:
        # Legacy in-place deploy: the recipe runs in the editing clone, so its
        # uncommitted edits would leak in — gate on a clean tree.
        if not _is_clean(repo):
            log.info("deploy.run_next: %s %s tree is dirty — skipping (%s)", slug, target, repo)
            # Put the queue file back so we retry next tick
            return None

    # Collapse same-target trailing entries — they would deploy the same
    # code anyway (each recipe does `git checkout <branch> && git merge`
    # against current HEAD). Keep the oldest as the "primary" we report.
    collapsed_files: list[Path] = []
    collapsed_reasons: list[str] = [payload.get("reason", "") or ""]
    for f in queued[1:]:
        try:
            p = json.loads(f.read_text())
        except Exception:
            continue
        if p.get("target") != target:
            continue
        collapsed_files.append(f)
        collapsed_reasons.append(p.get("reason", "") or "")
    if collapsed_files:
        log.info(
            "deploy.run_next: %s/%s collapsing %d trailing queue entries into %s",
            slug, target, len(collapsed_files), queue_id,
        )

    # Move all collapsed queue files to processing/ atomically.
    processing_dir = _processing_dir(cfg, slug)
    processing_dir.mkdir(parents=True, exist_ok=True)
    processing_file = processing_dir / queue_file.name
    queue_file.rename(processing_file)
    collapsed_processing: list[Path] = []
    for f in collapsed_files:
        dest = processing_dir / f.name
        f.rename(dest)
        collapsed_processing.append(dest)

    # Prepare log file
    runs_dir = _runs_dir(cfg, slug)
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{queue_id}.log"

    # Locate recipe
    recipe = _recipe_path(cfg, slug, target)
    if not recipe.exists():
        log.warning("deploy.run_next: recipe missing: %s", recipe)
        _finish(cfg, slug, processing_file, queue_id, rc=99)
        for pf in collapsed_processing:
            _finish(cfg, slug, pf, _queue_id_of(pf), rc=99)
        log_path.write_text(f"recipe not found: {recipe}\n")
        return DeployResult(
            ok=False, returncode=99, queue_id=queue_id, log_path=log_path,
            collapsed_count=1 + len(collapsed_processing),
            collapsed_reasons=tuple(collapsed_reasons),
        )

    # Run recipe with cwd matching the target clone (dev clone for staging,
    # master clone for prod). Recipes assume their cwd is the right tree.
    #
    # Two watchdogs guard the run (T-0212), because the old single
    # `subprocess.run(timeout=)` both (a) let a hung build hold the whole
    # serial monitor for up to 30 min, and (b) RAISED TimeoutExpired on a
    # hang — uncaught, that stranded the file in processing/ forever (the
    # 2-week-old watchrobot orphans):
    #   - no-progress watchdog: kill the build when its run-log has produced
    #     no new bytes for BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS (default 600s =
    #     10 min). A healthy build emits layer/step progress continuously even
    #     when slow; 10 min of total silence means it's wedged (buildx frozen
    #     at `COPY web/ .` at 0% CPU, the live watchrobot symptom).
    #   - hard timeout: wall-clock backstop. signal-tracker's fresh-cache image
    #     build (vite + npm install + COPY backend) runs ~17 min, so the 1800s
    #     (30 min) default leaves headroom. Override $BOT_SQUAD_DEPLOY_TIMEOUT.
    # Either watchdog kills the whole process group (the recipe's children —
    # docker build etc. — survive a kill of just the bash parent), appends a
    # loud marker to the run-log, and returns a sentinel rc instead of raising,
    # so the queue file always lands in processed/ and the queue is unblocked.
    timeout_s = int(os.environ.get("BOT_SQUAD_DEPLOY_TIMEOUT", "1800"))
    no_progress_s = int(os.environ.get("BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS", "600"))
    log.info(
        "deploy.run_next: running %s (recipe: %s, cwd: %s, timeout=%ds, no_progress=%ds)",
        queue_id, recipe, repo, timeout_s, no_progress_s,
    )
    rc, killed_reason = _run_recipe_watchdog(recipe, repo, log_path, timeout_s, no_progress_s)

    _finish(cfg, slug, processing_file, queue_id, rc=rc)
    for pf in collapsed_processing:
        _finish(cfg, slug, pf, _queue_id_of(pf), rc=rc)
    ok = rc == 0
    log.info(
        "deploy.run_next: %s/%s finished rc=%d killed=%s (collapsed=%d)",
        slug, target, rc, killed_reason, 1 + len(collapsed_processing),
    )
    return DeployResult(
        ok=ok, returncode=rc, queue_id=queue_id, log_path=log_path,
        collapsed_count=1 + len(collapsed_processing),
        collapsed_reasons=tuple(collapsed_reasons),
        killed_reason=killed_reason,
    )


def _queue_id_of(processing_file: Path) -> str:
    """Recover the queue_id from a processing file's name (<ts>-<uuid>.json)."""
    stem = processing_file.stem  # "<ts_ms>-<uuid>"
    # Everything after the first dash is the uuid
    _, _, qid = stem.partition("-")
    return qid


def _run_recipe_watchdog(
    recipe: Path,
    repo: Path,
    log_path: Path,
    timeout_s: int,
    no_progress_s: int,
) -> tuple[int, str | None]:
    """Run ``bash <recipe>`` (cwd=repo, output→log_path) under two watchdogs.

    Returns ``(returncode, killed_reason)``:
      - ``(rc, None)`` — the recipe exited on its own with code ``rc``.
      - ``(RC_NO_PROGRESS, "no_progress")`` — the run-log produced no new bytes
        for ``no_progress_s`` seconds; the build was killed.
      - ``(RC_TIMEOUT, "timeout")`` — the run exceeded ``timeout_s`` wall-clock
        seconds; the build was killed.

    The child is launched in its own process group (``start_new_session=True``)
    so a watchdog kill reaches the recipe's descendants (docker build, npm,
    …), not just the bash wrapper. A watchdog kill appends a loud marker to
    the run-log and NEVER raises — the caller relies on always getting an rc so
    the queue file is finished (moved out of processing/) and the queue
    unblocked. ``no_progress_s``/``timeout_s`` <= 0 disable that watchdog.
    """
    poll_interval = float(os.environ.get("BOT_SQUAD_DEPLOY_POLL_SECONDS", "5"))
    with log_path.open("w") as lf:
        proc = subprocess.Popen(
            ["bash", str(recipe)],
            cwd=str(repo),
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group → killpg reaches children
        )

    start = time.monotonic()
    last_progress = start
    last_size = -1
    killed_reason: str | None = None
    rc: int

    while True:
        try:
            rc = proc.wait(timeout=poll_interval)
            return rc, None  # recipe finished on its own
        except subprocess.TimeoutExpired:
            pass

        now = time.monotonic()
        try:
            size = log_path.stat().st_size
        except OSError:
            size = last_size
        if size != last_size:
            last_size = size
            last_progress = now

        if no_progress_s > 0 and (now - last_progress) >= no_progress_s:
            killed_reason = "no_progress"
            rc = RC_NO_PROGRESS
            break
        if timeout_s > 0 and (now - start) >= timeout_s:
            killed_reason = "timeout"
            rc = RC_TIMEOUT
            break

    _kill_process_group(proc)
    elapsed = time.monotonic() - start
    if killed_reason == "no_progress":
        marker = (
            f"\n\n❌ WATCHDOG: killed — no run-log output for "
            f"{no_progress_s}s (build wedged). Elapsed {elapsed:.0f}s. "
            f"rc={rc}.\n"
        )
    else:
        marker = (
            f"\n\n❌ WATCHDOG: killed — exceeded hard timeout of "
            f"{timeout_s}s. Elapsed {elapsed:.0f}s. rc={rc}.\n"
        )
    log.error(
        "deploy._run_recipe_watchdog: killed %s (reason=%s, elapsed=%.0fs)",
        recipe, killed_reason, elapsed,
    )
    try:
        with log_path.open("a") as lf:
            lf.write(marker)
    except OSError:
        log.exception("deploy._run_recipe_watchdog: could not append marker to %s", log_path)
    return rc, killed_reason


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """Terminate the recipe's whole process group: SIGTERM, then SIGKILL.

    The child was started with ``start_new_session=True`` so it leads its own
    process group; killing the group reaches grandchildren (docker, npm) that
    a kill of the bash wrapper alone would orphan.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return  # already gone
    for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 10.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def reap_orphans(cfg: "Config", slug: str, max_age_seconds: int | None = None) -> list[dict]:
    """Sweep stale files stranded in processing/ to processed/.fail.<RC_ORPHAN>.

    A file in ``processing/`` older than ``max_age_seconds`` is an orphan: a
    run whose worker died mid-deploy (or, pre-T-0212, a recipe that raised
    TimeoutExpired and left its file stranded). Left alone it lingers forever
    with no alert — the 2-week-old watchrobot orphans the ticket cites.

    ``max_age_seconds`` defaults to BOT_SQUAD_DEPLOY_ORPHAN_SECONDS (7200s =
    2h), comfortably above the 1800s hard deploy timeout so a legitimately
    in-flight deploy is never reaped. (Per-project monitor jobs also serialise
    reap-then-run, so the reaper never races a live deploy of the same slug.)

    Returns one info dict per reaped orphan (queue_id / target / reason /
    age_seconds) for the caller to raise a targeted alert on.
    """
    if max_age_seconds is None:
        max_age_seconds = int(os.environ.get("BOT_SQUAD_DEPLOY_ORPHAN_SECONDS", "7200"))
    processing_dir = _processing_dir(cfg, slug)
    if not processing_dir.exists():
        return []
    now = time.time()
    reaped: list[dict] = []
    for f in sorted(processing_dir.glob("*.json")):
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        qid = _queue_id_of(f)
        try:
            payload = json.loads(f.read_text())
        except Exception:
            payload = {}
        _finish(cfg, slug, f, qid, rc=RC_ORPHAN)
        log.error(
            "deploy.reap_orphans: %s swept stale processing job %s "
            "(age %.0fs >= %ds, target=%s) → processed/.fail.%d",
            slug, qid, age, max_age_seconds, payload.get("target"), RC_ORPHAN,
        )
        reaped.append({
            "queue_id": qid,
            "age_seconds": age,
            "target": payload.get("target"),
            "reason": payload.get("reason"),
        })
    return reaped


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_clean(repo_path: Path) -> bool:
    """Return True if the working tree has no relevant uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        log.exception("deploy._is_clean: git status failed for %s", repo_path)
        return False

    lines = result.stdout.splitlines()
    for line in lines:
        # Strip leading XY status chars (2 chars) + space, giving the path
        path_part = line[3:] if len(line) > 3 else line
        if not any(pat in path_part for pat in _QUIESCENCE_IGNORE):
            return False
    return True


def _local_only_commits(repo_path: Path) -> list[str]:
    """Return short SHAs+subjects of commits on HEAD but not on origin/<branch>.

    T-0116: a non-empty list means the target clone has direct-install commits
    that would be wiped by the recipe's `git merge --ff-only origin/<branch>`.
    Returns [] when in sync, when no matching origin ref exists, or when git
    misbehaves (fail-open is safe — `_is_clean` already gates the bigger risk
    of uncommitted edits, and an unreachable origin shouldn't wedge deploys).
    """
    try:
        branch_proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if branch_proc.returncode != 0:
            return []
        branch = branch_proc.stdout.strip()
        if not branch or branch == "HEAD":
            return []
        upstream = f"origin/{branch}"
        # Verify upstream ref exists before asking for the symmetric diff —
        # `git log <missing-ref>..HEAD` otherwise errors out and we'd lose
        # the signal. A fresh-host install with no upstream yet is benign.
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", upstream],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if verify.returncode != 0:
            return []
        log_proc = subprocess.run(
            ["git", "log", "--oneline", f"{upstream}..HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if log_proc.returncode != 0:
            return []
        return [ln for ln in log_proc.stdout.splitlines() if ln.strip()]
    except Exception:
        log.exception("deploy._local_only_commits: git failed for %s", repo_path)
        return []


def _origin_url(repo_path: Path) -> str | None:
    """Return the ``origin`` remote URL of ``repo_path``, or None if unset."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            url = proc.stdout.strip()
            return url or None
    except Exception:
        log.exception("deploy._origin_url: git failed for %s", repo_path)
    return None


def _ensure_deploy_clone(edit_repo: Path, deploy_repo: Path, branch: str) -> bool:
    """Create-if-missing + fetch + force-checkout ``origin/<branch>`` in the
    disposable deploy clone (T-0143).

    The deploy clone is a local CI checkout, parallel to dev/master, that no
    agent ever edits. So unlike the shared dev clone — where history rewrites
    and forced checkouts are forbidden — force-syncing it to the latest pushed
    commit is the correct, expected behaviour. The clone tracks the SAME origin
    as the editing clone, so ``origin/<branch>`` is the latest *pushed* code.

    Returns True when the clone is ready at ``origin/<branch>``; False on any
    git failure (the caller leaves the queue file in place to retry next tick).
    """
    deploy_repo = Path(deploy_repo)
    try:
        if not (deploy_repo / ".git").exists():
            deploy_repo.parent.mkdir(parents=True, exist_ok=True)
            # Prefer the real remote URL so the clone tracks origin directly.
            # Fall back to the editing clone's path for local-only projects
            # (no remote) — "pushed" is moot there, but the deploy still works.
            url = _origin_url(edit_repo) or str(edit_repo)
            clone = subprocess.run(
                ["git", "clone", "--branch", branch, url, str(deploy_repo)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if clone.returncode != 0:
                log.error(
                    "deploy._ensure_deploy_clone: clone failed (url=%s, branch=%s): %s",
                    url, branch, clone.stderr.strip(),
                )
                return False
        # Refresh an existing (or freshly-cloned) tree to the latest pushed tip.
        fetch = subprocess.run(
            ["git", "fetch", "origin", "--prune"],
            cwd=str(deploy_repo),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if fetch.returncode != 0:
            log.error(
                "deploy._ensure_deploy_clone: fetch failed for %s: %s",
                deploy_repo, fetch.stderr.strip(),
            )
            return False
        checkout = subprocess.run(
            ["git", "checkout", "-f", "-B", branch, f"origin/{branch}"],
            cwd=str(deploy_repo),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if checkout.returncode != 0:
            log.error(
                "deploy._ensure_deploy_clone: checkout origin/%s failed for %s: %s",
                branch, deploy_repo, checkout.stderr.strip(),
            )
            return False
        return True
    except Exception:
        log.exception("deploy._ensure_deploy_clone: unexpected error for %s", deploy_repo)
        return False


def _finish(
    cfg: "Config", slug: str, processing_file: Path, queue_id: str, rc: int
) -> None:
    """Move processing file to processed/ with the correct suffix."""
    processed_dir = _processed_dir(cfg, slug)
    processed_dir.mkdir(parents=True, exist_ok=True)

    stem = processing_file.stem  # e.g. "1746000000000-<uuid>"
    if rc == 0:
        dest = processed_dir / f"{stem}.ok"
    else:
        dest = processed_dir / f"{stem}.fail.{rc}"

    processing_file.rename(dest)
