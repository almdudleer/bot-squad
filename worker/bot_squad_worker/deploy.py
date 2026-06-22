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
    - Returns None if deploy-clone provisioning failed.
    - (Legacy in-place path only) Returns None if the editing tree is dirty or
      has unpushed local-only commits (queue file stays — push first), because
      the recipe runs in place and its ff-only merge would wipe them.
    - When a deploy clone is configured (T-0143) the recipe runs in a disposable
      clone force-synced to origin, so the shared editing tree's dirtiness AND
      its unpushed commits no longer gate (T-0225) — unpushed commits there are
      merely OMITTED from the release, logged as a loud advisory, not blocked.
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

Recipe convention (T-0205): the recipe is version-controlled in the bot-squad
    repo at ``deploy-recipes/<slug>/<target>.sh`` and shipped into the install
    root by the install's git ff-merge. ``_recipe_path`` PREFERS that tracked
    copy and falls back to the legacy runtime copy at
    ``data/<slug>/deploy/<target>.sh`` for not-yet-migrated projects.
    Executed via ``bash <recipe_path>`` with cwd = the exec clone for the
    target (``project.repo_for_target``): the deploy clone when configured,
    else the dev clone (staging) / master clone (prod).
    stdout+stderr captured to data/<slug>/_jobs/deploy/runs/<id>.log.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
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

# next-wave #11 (T-0451): keep-last-N retention horizon for the deploy job
# archive (processed/ + runs/). Override via BOT_SQUAD_DEPLOY_RETENTION_N; a
# value <= 0 disables pruning. Keep-last-N (NOT age-prune) so recent forensics
# survive.
DEFAULT_RETENTION_N = 50


# ---------------------------------------------------------------------------
# git-sha helpers (T-0335 item-13, Fork-5: detect running != deployed worker)
# ---------------------------------------------------------------------------

def _git_head_sha(root: Path) -> str:
    """``git -C <root> rev-parse HEAD`` (40-hex), or "" if it can't be read."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — git absent / not a repo / timeout
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _install_root() -> Path:
    """The install tree the worker package is editable-installed from.

    ``.../worker/bot_squad_worker/deploy.py`` → ``parents[2]`` is the repo root
    (the git tree the deploy recipe ff-merges into).
    """
    return Path(__file__).resolve().parents[2]


_BOOT_GIT_SHA: str | None = None


def boot_git_sha() -> str:
    """The HEAD sha of the install tree captured at worker boot, then frozen.

    Computed once and cached: even after a deploy ff-merges the install dir the
    RUNNING worker keeps reporting this boot sha — the signal that the live
    process is on stale code until it is restarted. Surfaced at the worker's
    ``/health`` and compared against the live (post-merge) deployed sha to
    decide whether a ``worker/``-touching deploy needs an auto-restart (Fork-5).
    """
    global _BOOT_GIT_SHA
    if _BOOT_GIT_SHA is None:
        _BOOT_GIT_SHA = _git_head_sha(_install_root())
    return _BOOT_GIT_SHA


def _worker_subtree_changed(root: Path, a: str, b: str) -> bool:
    """True iff the ``worker/`` subtree differs between commits ``a`` and ``b``.

    The ``-- worker/`` path-spec is correct ONLY while the worker process imports
    nothing first-party from outside ``worker/`` — if the worker ever imports
    shared code living elsewhere in the repo, widen this path-spec or a relevant
    change won't trigger the restart. ``git diff --quiet`` exits 1 on differences,
    0 when identical; any other code (bad rev, git error) → treat as "unknown,
    don't auto-fire" so a flaky probe never bounces the worker.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", a, b, "--", "worker/"],
            capture_output=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return False
    return out.returncode == 1


def _worker_needs_restart(cfg: "Config") -> bool:
    """Fork-5 auto-restart predicate: the running worker is on stale code that a
    just-deployed ``worker/`` change would update.

    True iff the frozen boot sha differs from the live (post-ff-merge) deployed
    sha AND the ``worker/`` subtree changed between them. Kill-switch
    ``BOT_SQUAD_DEPLOY_AUTO_RESTART=0`` forces it OFF (the only mitigation for a
    cross-project SIGTERM on a busy multi-project host; the deploy build itself
    is already scope-isolated by T-0213).
    """
    if os.environ.get("BOT_SQUAD_DEPLOY_AUTO_RESTART", "1").strip() == "0":
        return False
    return _worker_subtree_changed_since_boot(cfg)


def _worker_subtree_changed_since_boot(cfg: "Config") -> bool:
    """The no-worker-change skip gate (kill-switch independent): True iff the
    running worker's frozen boot sha differs from the live (post-ff-merge)
    deployed sha AND the ``worker/`` subtree changed between them — i.e. there is
    genuinely new worker code for a restart to load."""
    root = _install_root()
    running = boot_git_sha()
    deployed = _git_head_sha(root)
    if not running or not deployed or running == deployed:
        return False
    return _worker_subtree_changed(root, running, deployed)


def _should_restart_worker(cfg: "Config", *, ok: bool, forced: bool) -> bool:
    """T-0305 part-a: decide whether a finished deploy should bounce the worker.

    The restart fires ONLY on a clean deploy that genuinely changed the
    ``worker/`` subtree (the no-worker-change skip gate) — for BOTH an explicit
    ``restart_worker:true`` (``forced``) and the Fork-5 auto-detect. A
    no-worker-change deploy never bounces the worker, eliminating the ~6min
    SIGTERM cadence that was churning the inbox-wait long-polls.

    ``forced`` overrides the ``BOT_SQUAD_DEPLOY_AUTO_RESTART=0`` kill-switch (an
    explicit ask wins) but STILL requires a real worker/ change; the auto path
    honors the kill-switch."""
    if not ok:
        return False
    try:
        if not _worker_subtree_changed_since_boot(cfg):
            return False
    except Exception:  # noqa: BLE001 — a flaky git probe never bounces the worker
        log.exception("deploy._should_restart_worker: worker-change probe failed")
        return False
    if forced:
        return True
    return os.environ.get("BOT_SQUAD_DEPLOY_AUTO_RESTART", "1").strip() != "0"


# ---------------------------------------------------------------------------
# systemd-scope detach (T-0213)
# ---------------------------------------------------------------------------
#
# The deploy recipe runs as a child of the worker process. The worker is a
# user-systemd service (bot-squad-worker.service) whose default KillMode is
# control-group, so `systemctl --user restart bot-squad-worker` SIGTERMs the
# WHOLE cgroup — including an in-flight `docker build` started by the recipe
# (the live rc=-15 break, 2026-06-18). T-0212's `start_new_session=True` only
# gives the child its own PROCESS GROUP (so the watchdog's killpg is scoped);
# it does NOT move it out of the worker's CGROUP.
#
# Fix: launch the recipe inside a transient `systemd-run --user --scope` unit.
# A scope is its own cgroup under the user slice (app.slice), NOT under
# bot-squad-worker.service, so a worker restart can't reach it. The same
# primitive lets T-0181's post-deploy worker-restart step survive restarting
# the very worker it runs under.
#
# `--scope` runs SYNCHRONOUSLY and execs into the command, so:
#   - stdout/stderr still redirect to the run-log (Popen stdout=lf), and
#   - `proc.pid` IS the recipe bash (pgid == pid), so the watchdog's
#     `os.getpgid(proc.pid)` + `os.killpg` is unchanged — verified live.


def _use_systemd_scope() -> bool:
    """Whether to wrap deploy children in a transient systemd --user scope.

    Explicit override: BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE in {"1","0"}. Otherwise
    auto-detect: a reachable user systemd manager (XDG_RUNTIME_DIR) AND the
    systemd-run client on PATH. Tests and non-systemd hosts fall back to a
    plain `bash` invocation (the legacy behaviour) so nothing breaks where the
    primitive is unavailable.
    """
    knob = os.environ.get("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE")
    if knob in ("0", "1"):
        return knob == "1"
    return bool(os.environ.get("XDG_RUNTIME_DIR")) and shutil.which("systemd-run") is not None


def _scope_wrap(argv: list[str], unit: str) -> list[str]:
    """Prefix ``argv`` with ``systemd-run --user --scope`` when scopes are on.

    ``unit`` names the transient scope (must be unique while active — callers
    pass a uuid-derived name). When scopes are off, returns ``argv`` unchanged.
    """
    if not _use_systemd_scope():
        return argv
    return [
        "systemd-run", "--user", "--scope", "--quiet",
        f"--unit={unit}", "--collect", "--", *argv,
    ]

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
    # T-0446 (next-wave #2): the commit sha this run actually shipped (parsed
    # from the run log's sha-assert / release line; "" when the recipe emits
    # none) — so the terminal #deploy-logs ping echoes WHICH commit deployed.
    resolved_sha: str = ""
    # The post-deploy worker-restart decision, surfaced so the terminal status
    # explains the (async, detached) restart instead of a green "release
    # deployed" preceding the bounce → the T-0436 false stale-worker panic. One
    # of: "fired" | "skipped: no worker change" | "skipped: deploy failed".
    worker_restart_status: str = ""


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


def _tracked_recipe_path(cfg: "Config", slug: str, target: str) -> Path:
    """Version-controlled recipe location (T-0205).

    Tracked in the bot-squad repo at ``deploy-recipes/<slug>/<target>.sh`` and
    shipped into the install root (``config_dir.parent`` ==
    ``/home/www/bot-squad``) by the install's git ff-merge on every bot-squad
    deploy. This is the SSOT a recipe change goes through review + git history,
    instead of a live hand-edit of the runtime copy under ``data/``.
    """
    return cfg.config_dir.parent / "deploy-recipes" / slug / f"{target}.sh"


def _recipe_path(cfg: "Config", slug: str, target: str) -> Path:
    """Resolve the deploy recipe for ``slug``/``target``.

    Prefers the version-controlled copy (``_tracked_recipe_path``) so recipe
    changes are reviewable + versioned (T-0205). Falls back to the legacy
    runtime copy at ``data/<slug>/deploy/<target>.sh`` for projects whose
    recipe has not been brought under version control yet — so migration is
    incremental and un-tracked projects keep working unchanged.
    """
    tracked = _tracked_recipe_path(cfg, slug, target)
    if tracked.exists():
        return tracked
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
    restart_worker: bool = False,
) -> str:
    """Write a deploy request file and return its queue_id.

    ``restart_worker`` (T-0181, default OFF) opts the deploy into a post-sync
    worker restart: after a successful recipe, ``run_next`` restarts
    bot-squad-worker (in a detached scope so it survives) and re-smokes the
    worker. The agent opts in only when the diff touches worker-loaded code
    (e.g. ``worker/.../actions.py``); most web/api deploys leave it off.

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
        "restart_worker": bool(restart_worker),
    }
    (queue_dir / filename).write_text(json.dumps(payload, indent=2))
    log.info("deploy.enqueue: %s/%s queued as %s", slug, target, queue_id)
    return queue_id


def resolve_target_sha(cfg: "Config", slug: str, target: str) -> str:
    """Best-effort: the commit a deploy of ``slug``/``target`` WOULD ship now.

    T-0458 (T-0446 C6.4 residual): the deploy clone is force-synced to
    ``origin/<deploy_branch>``, so the to-be-built commit is that ref as
    currently known to the editing clone. This is an ENQUEUE-time echo — no
    network fetch (the recipe re-fetches origin at build time, and the
    authoritative resolved sha is re-parsed from the build log for the terminal
    #deploy-logs ping, T-0446). Returns a 40-hex sha, or "" if it can't be read
    (unknown slug/target, no origin ref, git error) — never raises.
    """
    project = cfg.projects.get(slug)
    if project is None or target not in project.deploy_targets:
        return ""
    edit_repo = project.editing_repo_for_target(target)
    ref = f"origin/{project.deploy_branch}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(edit_repo), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


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

    Deploy-clone path (T-0143/T-0225): when a deploy clone is configured the
    recipe runs from a disposable clone ``run_next`` force-syncs to
    origin/<branch>; the shared editing clone is never touched. So NEITHER its
    dirty working tree NOR its unpushed commits can affect or be destroyed by
    the deploy — they'd merely be OMITTED (origin is the SSOT). Gating on the
    shared clone's state HOL-blocks EVERY team's deploy whenever any one team
    has WIP there (the multi-team stall T-0225 fixes), so this returns True and
    the deploy is always clear to start. ``run_next`` still logs a loud advisory
    for any unpushed commits so the deployer knows to push them.

    Legacy in-place path (no deploy clone) returns False when:
    - the exec/editing clone has a dirty working tree — the recipe runs in
      place, so uncommitted edits would leak in; OR
    - the editing clone has local-only commits not yet on origin (T-0110/T-0116)
      — the recipe's ``git merge --ff-only origin/<branch>`` would WIPE them,
      so the agent must push first.
    """
    project = cfg.projects[slug]
    edit_repo = project.editing_repo_for_target(target)
    if project.uses_deploy_clone(target):
        return True
    # Legacy in-place deploy: the exec clone IS the editing clone — uncommitted
    # edits would leak into the recipe and unpushed commits would be destroyed
    # by its ff-only merge, so both gate.
    if not _is_clean(edit_repo):
        return False
    return not _local_only_commits(edit_repo)


_RESOLVED_SHA_RE = re.compile(r"deployed sha \(([0-9a-fA-F]{7,40})\)")
_RELEASE_SHA_RE = re.compile(r"release deployed:\s*([0-9a-fA-F]{7,40})")


def _parse_resolved_sha(log_path: "Path | None") -> str:
    """T-0446: the commit sha a finished run shipped, recovered from its run log.

    Prefers the sha-assert line (``deployed sha (<40hex>)`` — the full sha the
    recipe verified the running artifact against), falling back to the
    ``release deployed: <sha>`` line. "" when the recipe emits neither or the log
    is unreadable (best-effort; the terminal status just omits the sha then)."""
    if log_path is None:
        return ""
    try:
        text = log_path.read_text()
    except OSError:
        return ""
    matches = _RESOLVED_SHA_RE.findall(text)
    if matches:
        return matches[-1]
    rel = _RELEASE_SHA_RE.findall(text)
    return rel[-1] if rel else ""


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

    # Commit-guard (T-0110/T-0116/T-0143/T-0225): the EDITING clone may carry
    # commits not yet on origin. What that means for the deploy depends on the
    # path, so the guard is BLOCK-on-destroy / WARN-on-omit:
    #   - Legacy in-place path (no deploy clone): the recipe runs `git merge
    #     --ff-only origin/<branch>` IN the editing clone, so unpushed local
    #     commits would be DESTROYED. Hard refuse — push first. List them loudly.
    #     Runs regardless of recipe presence so a fresh-host install inherits it.
    #     Bypass for tests/emergency via BOT_SQUAD_DEPLOY_ALLOW_LOCAL_COMMITS=1.
    #   - Deploy-clone path (T-0225): the recipe runs in a disposable clone
    #     force-synced to origin/<branch>; the shared editing clone is never
    #     touched, so unpushed commits there can't be destroyed — only OMITTED
    #     from this release. Hard-blocking here HOL-defers EVERY team's deploy
    #     whenever any one team has committed-but-unpushed WIP on the shared
    #     clone (the multi-team stall). So log a loud advisory naming the omitted
    #     commits (so the deployer can push) and PROCEED from origin.
    local_only = _local_only_commits(edit_repo)
    if local_only:
        if uses_deploy:
            log.warning(
                "deploy.run_next: %s/%s — %d commit(s) on the editing clone %s are NOT on "
                "origin and will be OMITTED from this release (the deploy ships origin/%s from "
                "the deploy clone, which never touches the shared editing tree):\n    %s\n"
                "Push them if they belong in this deploy; proceeding from origin/%s.",
                slug, target, len(local_only), edit_repo, project.deploy_branch,
                "\n    ".join(local_only), project.deploy_branch,
            )
        elif os.environ.get("BOT_SQUAD_DEPLOY_ALLOW_LOCAL_COMMITS") != "1":
            log.error(
                "deploy.run_next: %s/%s REFUSED — %d local-only commit(s) on %s not pushed to origin "
                "(a legacy in-place deploy ff-only-merges origin/<branch> and would WIPE them):\n    %s\n"
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
        _record_run_rc(cfg, slug, queue_id, 99)
        _finish(cfg, slug, processing_file, queue_id, rc=99)
        for pf in collapsed_processing:
            qid_c = _queue_id_of(pf)
            _record_run_rc(cfg, slug, qid_c, 99)
            _finish(cfg, slug, pf, qid_c, rc=99)
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
    rc, killed_reason = _run_recipe_watchdog(
        recipe, repo, log_path, timeout_s, no_progress_s, queue_id
    )

    # T-0243: record the terminal rc BEFORE the _finish move, for the winner and
    # every collapsed qid, so a worker restart racing this finalization can't
    # strand a FINISHED job — the reconcile pass replays the move from the sentinel.
    _record_run_rc(cfg, slug, queue_id, rc)
    _finish(cfg, slug, processing_file, queue_id, rc=rc)
    for pf in collapsed_processing:
        qid_c = _queue_id_of(pf)
        _record_run_rc(cfg, slug, qid_c, rc)
        _finish(cfg, slug, pf, qid_c, rc=rc)
    ok = rc == 0
    log.info(
        "deploy.run_next: %s/%s finished rc=%d killed=%s (collapsed=%d)",
        slug, target, rc, killed_reason, 1 + len(collapsed_processing),
    )

    # T-0181: optional post-deploy worker restart. Only on a clean success, and
    # only when the request opted in (the diff touched worker-loaded code). The
    # restart is detached into its own scope so it survives restarting the very
    # worker this code runs under — see _restart_worker_detached. Best-effort:
    # a launch failure is logged but never flips the deploy's own outcome (the
    # build + install sync already succeeded; the operator can restart by hand).
    # T-0335 item-13 (Fork-5): besides the explicit FORCE flag (D5), auto-fire
    # the restart when a clean deploy actually changed the worker/ subtree out
    # from under the running (stale-code) worker — closing the "deploy succeeded
    # but the worker kept running old code" gap. Kill-switch:
    # BOT_SQUAD_DEPLOY_AUTO_RESTART=0.
    forced = bool(payload.get("restart_worker"))
    will_restart = _should_restart_worker(cfg, ok=ok, forced=forced)
    if will_restart:
        reason = payload.get("reason", "")
        tag = "restart_worker" if forced else "auto-restart: worker/ changed"
        reason = f"{tag} — {reason}".strip(" —")
        try:
            _restart_worker_detached(cfg, slug, queue_id, reason)
        except Exception:
            log.exception(
                "deploy.run_next: %s/%s post-deploy worker restart launch failed "
                "(deploy itself succeeded — restart the worker manually)", slug, target,
            )
    # T-0446: surface which commit shipped + the worker-restart decision so the
    # terminal #deploy-logs ping is self-explaining (no false stale-worker panic).
    worker_restart_status = (
        "fired" if will_restart
        else ("skipped: deploy failed" if not ok else "skipped: no worker change")
    )
    return DeployResult(
        ok=ok, returncode=rc, queue_id=queue_id, log_path=log_path,
        collapsed_count=1 + len(collapsed_processing),
        collapsed_reasons=tuple(collapsed_reasons),
        killed_reason=killed_reason,
        resolved_sha=_parse_resolved_sha(log_path),
        worker_restart_status=worker_restart_status,
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
    queue_id: str = "",
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

    T-0213: when a user systemd manager is available the recipe is wrapped in a
    transient ``systemd-run --user --scope`` unit so it lives in its OWN cgroup
    (under app.slice), not the worker service's — a concurrent
    ``systemctl --user restart bot-squad-worker`` then can't SIGTERM the build.
    With ``--scope`` systemd-run execs into the command, so ``proc.pid`` is the
    recipe bash and ``getpgid``/``killpg`` are unaffected.
    """
    poll_interval = float(os.environ.get("BOT_SQUAD_DEPLOY_POLL_SECONDS", "5"))
    argv = _scope_wrap(["bash", str(recipe)], f"bot-squad-deploy-{queue_id or 'run'}")
    with log_path.open("w") as lf:
        proc = subprocess.Popen(
            argv,
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


def _coalesce_write(marker_path: str, token: str) -> None:
    """T-0287: claim the shared restart-coalesce marker (last writer wins).

    Atomic replace so a concurrent reader never sees a partial token.
    """
    p = Path(marker_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.tmp.{os.getpid()}"
    tmp.write_text(token)
    os.replace(tmp, p)


def _coalesce_winner(marker_path: str, token: str) -> bool:
    """T-0287: True iff ``token`` is still the latest claim — i.e. no later
    restart superseded it during the debounce window, so this one should fire."""
    try:
        return Path(marker_path).read_text().strip() == token
    except OSError:
        return False


def _build_worker_restart_script(
    worker_dir: Path,
    pip_path: Path,
    sock_path: Path,
    log_path: Path,
    fail_marker: Path,
    service: str,
    delay_s: int,
    smoke_attempts: int,
    coalesce_marker: Path | None = None,
    token: str = "",
    expected_sha: str = "",
) -> str:
    """Render the bash script the detached restart scope runs (T-0181).

    Sequence: (1) brief delay so the caller returns + the worker idles;
    (2) PIP GUARD — ``pip install -e <worker>`` to pull any new deps the synced
    code needs (the worker is editable-installed, so code is already live; only
    deps are at risk). On guard failure the worker is left RUNNING on the old
    code and a FAIL marker is written — a half-applied venv that downs the
    worker would kill the peer bus, so failing loud-but-safe beats restarting;
    (3) restart the service; (4) smoke worker ``/health`` (ok + fresh uptime)
    then one action (``scheduler_state``). Any failure writes the FAIL marker.
    All paths are shlex-quoted by the caller.
    """
    import shlex
    q = shlex.quote
    wd, pip, sock = q(str(worker_dir)), q(str(pip_path)), q(str(sock_path))
    log, fail, svc = q(str(log_path)), q(str(fail_marker)), q(service)
    # T-0287: trailing-edge restart coalescing. Claim the shared marker, wait the
    # debounce window, then proceed ONLY if still the latest claim — a later
    # deploy's restart supersedes this one and will pick up all synced code, so a
    # burst of concurrent deploys collapses to a single worker restart. Uses the
    # worker venv python (bot_squad_worker is editable-installed → importable).
    py = q(str(worker_dir / ".venv" / "bin" / "python"))
    coalesce_block = ""
    if coalesce_marker is not None and token:
        # Pass the marker + token as argv (shell-quoted), NEVER interpolated into
        # the `python -c` SOURCE — a bare path/UUID there is invalid Python
        # (`_coalesce_write(/home/..., 1b6a39cf-...)` → SyntaxError) which silently
        # broke every deploy's worker restart. argv keeps them plain strings.
        mk, tok = q(str(coalesce_marker)), q(token)
        coalesce_block = f"""
{py} -c "import sys; from bot_squad_worker.deploy import _coalesce_write; _coalesce_write(sys.argv[1], sys.argv[2])" {mk} {tok} >> "$LOG" 2>&1 || true
sleep {int(delay_s)}
if ! {py} -c "import sys; from bot_squad_worker.deploy import _coalesce_winner; sys.exit(0 if _coalesce_winner(sys.argv[1], sys.argv[2]) else 1)" {mk} {tok}; then
  echo "[worker-restart] COALESCED — a newer restart superseded this claim; skipping (it restarts with all synced code)" >> "$LOG"
  exit 0
fi
"""
    else:
        coalesce_block = f"\nsleep {int(delay_s)}\n"
    # T-0335 item-13 (Fork-5): assert the restarted worker came back on the
    # DEPLOYED sha. $resp holds the last (healthy) /health body, which carries
    # "git_sha":"<sha>". The expected sha is shell-quoted into EXPECTED_SHA and
    # matched with a bash `case` glob — NEVER interpolated into python -c source
    # (the T-0287 lesson). Empty expected_sha (legacy/forced path) skips the
    # assertion so a non-sha-stamped install still restarts.
    sha_assert_block = ""
    if expected_sha:
        exp = q(expected_sha)
        sha_assert_block = f"""EXPECTED_SHA={exp}
case "$resp" in
  *'"git_sha":"'"$EXPECTED_SHA"'"'*) echo "[worker-restart] git_sha verified: $EXPECTED_SHA" >> "$LOG";;
  *) echo "[worker-restart] GIT_SHA_MISMATCH — worker did not come back on the deployed sha $EXPECTED_SHA" >> "$LOG"; : > {fail}; exit 1;;
esac
"""
    return f"""
set -u
LOG={log}
{coalesce_block}
echo "[worker-restart] pip guard: install -e {wd}" >> "$LOG"
if ! {pip} install -e {wd} >> "$LOG" 2>&1; then
  echo "[worker-restart] PIP_GUARD_FAILED — leaving worker on old code, NOT restarting" >> "$LOG"
  : > {fail}
  exit 1
fi
echo "[worker-restart] restarting {svc}" >> "$LOG"
if ! systemctl --user restart {svc} >> "$LOG" 2>&1; then
  echo "[worker-restart] RESTART_CMD_FAILED" >> "$LOG"
  : > {fail}
  exit 1
fi
ok=0
for i in $(seq 1 {int(smoke_attempts)}); do
  sleep 2
  resp=$(curl -sS --max-time 5 --unix-socket {sock} http://w/health 2>/dev/null || true)
  echo "[worker-restart] /health attempt $i: $resp" >> "$LOG"
  case "$resp" in *'\"ok\":true'*) ok=1; break;; esac
done
if [ "$ok" != 1 ]; then
  echo "[worker-restart] SMOKE_HEALTH_FAILED — worker did not answer /health after restart" >> "$LOG"
  : > {fail}
  exit 1
fi
{sha_assert_block}act=$(curl -sS --max-time 5 --unix-socket {sock} -X POST -H 'Content-Type: application/json' -d '{{}}' http://w/actions/scheduler_state 2>/dev/null || true)
echo "[worker-restart] scheduler_state: $act" >> "$LOG"
case "$act" in
  *worker_started_at*) echo "[worker-restart] WORKER_RESTART_OK" >> "$LOG"; exit 0;;
  *) echo "[worker-restart] SMOKE_ACTION_FAILED" >> "$LOG"; : > {fail}; exit 1;;
esac
"""


def _restart_worker_detached(
    cfg: "Config", slug: str, queue_id: str, reason: str
) -> None:
    """Launch a detached, self-surviving worker restart + smoke (T-0181).

    Runs only when systemd scopes are available (``_use_systemd_scope``) — the
    whole point is to survive restarting the worker we run under, which is
    impossible without the scope detach. Without a user systemd manager we log
    a clear note and SKIP (the operator restarts by hand, the legacy behaviour);
    a synchronous in-process restart would SIGTERM this very code mid-run.

    The restart script (``_build_worker_restart_script``) is launched in its own
    transient scope so it outlives the ``systemctl --user restart`` it issues,
    writing its progress + outcome to ``runs/<queue_id>.worker-restart.log`` and
    a ``.worker-restart.FAIL`` marker on any failure.
    """
    restart_log = _runs_dir(cfg, slug) / f"{queue_id}.worker-restart.log"
    fail_marker = _runs_dir(cfg, slug) / f"{queue_id}.worker-restart.FAIL"
    restart_log.parent.mkdir(parents=True, exist_ok=True)

    if not _use_systemd_scope():
        msg = (
            f"[worker-restart] systemd --user scope unavailable — SKIPPING "
            f"auto-restart for {slug} (queue {queue_id}). Restart the worker by "
            f"hand: systemctl --user restart bot-squad-worker.service\n"
        )
        log.warning("deploy._restart_worker_detached: %s", msg.strip())
        restart_log.write_text(msg)
        return

    install_root = cfg.config_dir.parent
    worker_dir = install_root / "worker"
    pip_path = worker_dir / ".venv" / "bin" / "pip"
    service = os.environ.get("BOT_SQUAD_WORKER_SERVICE", "bot-squad-worker.service")
    delay_s = int(os.environ.get("BOT_SQUAD_DEPLOY_WORKER_RESTART_DELAY", "5"))
    smoke_attempts = int(os.environ.get("BOT_SQUAD_DEPLOY_WORKER_SMOKE_ATTEMPTS", "10"))

    # T-0287: global (one-worker) coalesce marker + this deploy's queue_id token,
    # so concurrent deploys' detached restarts collapse to a single restart.
    coalesce_marker = cfg.data_dir / "_worker" / "restart_coalesce.token"
    # T-0335 item-13: the live (post-ff-merge) HEAD sha is the sha the restarted
    # worker must come back on; the smoke asserts it. "" when the tree has no git
    # (the assertion is then skipped — the restart still proceeds).
    expected_sha = _git_head_sha(install_root)
    script = _build_worker_restart_script(
        worker_dir, pip_path, cfg.sock_path, restart_log, fail_marker,
        service, delay_s, smoke_attempts,
        coalesce_marker=coalesce_marker, token=str(queue_id or "run"),
        expected_sha=expected_sha,
    )
    argv = _scope_wrap(["bash", "-c", script], f"bot-squad-worker-restart-{queue_id}")
    log.info(
        "deploy._restart_worker_detached: %s launching detached worker restart "
        "(queue %s, reason=%r) → %s", slug, queue_id, reason, restart_log,
    )
    # Fire-and-forget: detached session, no wait. The scope keeps it alive past
    # the worker restart; we must not block run_next / the monitor on it.
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _run_rc_path(cfg: "Config", slug: str, queue_id: str) -> Path:
    return _runs_dir(cfg, slug) / f"{queue_id}.rc"


def _record_run_rc(cfg: "Config", slug: str, queue_id: str, rc: int) -> None:
    """T-0243: durably record a run's TERMINAL rc next to its log, the instant the
    recipe returns — BEFORE run_next's ``_finish`` move. If a worker restart then
    SIGTERMs run_next before/ mid-``_finish`` (the orphan race), this sentinel
    survives so the reconcile pass can land the marker on its ACTUAL outcome
    instead of letting the age-fail reaper blindly mark a SUCCESS as .fail.ORPHAN.
    Best-effort + atomic (tmp+rename); a write failure never breaks the deploy."""
    try:
        p = _run_rc_path(cfg, slug, queue_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".rc.tmp")
        tmp.write_text(str(int(rc)))
        tmp.replace(p)
    except OSError:
        log.warning("deploy._record_run_rc: could not persist rc for %s/%s", slug, queue_id)


def _read_run_rc(cfg: "Config", slug: str, queue_id: str) -> int | None:
    """The recorded terminal rc for ``queue_id``, or None when absent/unreadable
    (no sentinel yet ⇒ the run hasn't terminated — leave it in-flight)."""
    try:
        return int(_run_rc_path(cfg, slug, queue_id).read_text().strip())
    except (OSError, ValueError):
        return None


def reconcile_finished_orphans(cfg: "Config", slug: str) -> list[dict]:
    """T-0243: reconcile FINISHED-but-orphaned ``processing/`` markers to their
    ACTUAL recorded rc.

    A run can complete (rc recorded via ``_record_run_rc``) yet have its
    ``_finish`` move interrupted by a worker restart racing run_next's
    finalization (aggravated by a same-target deploy collapsing onto it). The
    marker then lingers in ``processing/``. This pass — run on worker /
    deploy_monitor startup AND each monitor tick, BEFORE the age-fail
    ``reap_orphans`` — moves any such marker to ``processed/.ok`` (rc=0) or
    ``.fail.<rc>`` (rc!=0) per its sentinel, so a SUCCESS is never blindly
    age-failed and the queue reconciles to truth. A marker with no sentinel yet
    (genuinely in-flight) is left untouched — never reap a live build.

    Collapsed-pair: run_next records a sentinel per collapsed qid, so each
    orphaned marker reconciles independently to the surviving run's rc.

    Returns one info dict per reconciled marker (queue_id / rc / target)."""
    processing_dir = _processing_dir(cfg, slug)
    if not processing_dir.exists():
        return []
    reconciled: list[dict] = []
    for f in sorted(processing_dir.glob("*.json")):
        qid = _queue_id_of(f)
        rc = _read_run_rc(cfg, slug, qid)
        if rc is None:
            continue  # no terminal rc → still in-flight, leave for the deploy / age-reaper
        try:
            payload = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            payload = {}
        _finish(cfg, slug, f, qid, rc=rc)
        log.warning(
            "deploy.reconcile_finished_orphans: %s reconciled orphaned job %s "
            "(recorded rc=%d, target=%s) → processed/%s",
            slug, qid, rc, payload.get("target"), ".ok" if rc == 0 else f".fail.{rc}",
        )
        reconciled.append({"queue_id": qid, "rc": rc, "target": payload.get("target")})
    return reconciled


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


def prune_processed_runs(
    cfg: "Config", slug: str, keep_last_n: int | None = None
) -> dict:
    """Keep-last-N retention reaper for the deploy job archive (next-wave #11).

    ``processed/`` and ``runs/`` only ever GROW — ``_finish`` MOVES each terminal
    job's queue file into ``processed/<epoch_ms>-<uuid>.ok|.fail.<rc>`` and
    run_next drops per-job artifacts (``<uuid>.log`` / ``.rc`` /
    ``.worker-restart.log`` …) into ``runs/``, but nothing ever prunes them (live:
    processed=164, runs=185 since May). This keeps the N most-RECENT jobs and
    unlinks the rest, so recent deploy FORENSICS survive — keep-last-N,
    deliberately NOT an age-prune.

    Recency order comes from the ``processed/`` filenames' monotonic
    ``<epoch_ms>`` prefix (runs/ files are ``<uuid>.*`` and carry no timestamp, so
    runs retention is TIED to the processed keep-set by queue_id). A queue_id
    still present in ``processing/`` (an in-flight build) is NEVER pruned — its
    runs log is being written right now. Best-effort + never raises: a prune
    error must not wedge the monitor.

    ``keep_last_n`` defaults to ``BOT_SQUAD_DEPLOY_RETENTION_N`` (``DEFAULT_RETENTION_N``);
    a value <= 0 disables pruning. Returns
    ``{processed_pruned, runs_pruned, kept}``.
    """
    result = {"processed_pruned": 0, "runs_pruned": 0, "kept": 0}
    if keep_last_n is None:
        try:
            keep_last_n = int(
                os.environ.get("BOT_SQUAD_DEPLOY_RETENTION_N", str(DEFAULT_RETENTION_N))
            )
        except ValueError:
            keep_last_n = DEFAULT_RETENTION_N
    if keep_last_n <= 0:
        return result  # pruning disabled

    try:
        processed_dir = _processed_dir(cfg, slug)
        runs_dir = _runs_dir(cfg, slug)
        processing_dir = _processing_dir(cfg, slug)

        # Never prune an in-flight job (its marker is still in processing/).
        in_flight: set[str] = set()
        if processing_dir.exists():
            for f in processing_dir.glob("*.json"):
                in_flight.add(_queue_id_of(f))  # the bare uuid

        # processed/ names: "<epoch_ms>-<uuid>.ok" / "<epoch_ms>-<uuid>.fail.<rc>".
        # Sort by the epoch_ms prefix (chronological); the last N are the keepers.
        def _ts_of(f: Path) -> int:
            ts, _, _ = f.name.split(".", 1)[0].partition("-")
            try:
                return int(ts)
            except ValueError:
                return 0

        def _qid_of(f: Path) -> str:
            _, _, qid = f.name.split(".", 1)[0].partition("-")
            return qid

        processed_files = (
            sorted((f for f in processed_dir.iterdir() if f.is_file()), key=_ts_of)
            if processed_dir.exists()
            else []
        )
        keepers = processed_files[-keep_last_n:] if keep_last_n else []
        keep_qids = in_flight | {_qid_of(f) for f in keepers}
        result["kept"] = len(keep_qids)

        # Prune older processed/ markers (but never an in-flight one).
        for f in processed_files[: max(0, len(processed_files) - keep_last_n)]:
            if _qid_of(f) in in_flight:
                continue
            try:
                f.unlink()
                result["processed_pruned"] += 1
            except OSError:
                pass

        # Prune runs/ artifacts whose queue_id is not a keeper. runs names are
        # "<uuid>.<ext...>"; the uuid carries no dots so split on the first '.'.
        if runs_dir.exists():
            for f in runs_dir.iterdir():
                if not f.is_file():
                    continue
                qid = f.name.split(".", 1)[0]
                if qid in keep_qids:
                    continue
                try:
                    f.unlink()
                    result["runs_pruned"] += 1
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — retention must never wedge the monitor
        log.exception("deploy.prune_processed_runs: prune failed for %s", slug)
    return result


def surface_worker_restart_fails(cfg: "Config", slug: str) -> list[dict]:
    """Surface — once — any ``.worker-restart.FAIL`` marker the detached restart
    left behind (T-0335 item-13, Fork-5).

    ``_restart_worker_detached`` writes ``runs/<id>.worker-restart.FAIL`` on a
    pip-guard / restart / smoke failure, but nothing ever read it: a failed
    auto-restart left the worker silently on OLD code with no alert (the actual
    leak Fork-5 closes). This scans for those markers, returns one alert dict per
    marker (queue_id + the log tail for context), and TOMBSTONES each to
    ``.FAIL.alerted`` so the next tick doesn't re-alert. The caller raises the
    operator alert. Best-effort + never raises — a surface error must not wedge
    the monitor.
    """
    runs = _runs_dir(cfg, slug)
    if not runs.exists():
        return []
    out: list[dict] = []
    for marker in sorted(runs.glob("*.worker-restart.FAIL")):
        qid = marker.name[: -len(".worker-restart.FAIL")]
        tail = ""
        log_path = runs / f"{qid}.worker-restart.log"
        try:
            if log_path.exists():
                tail = "".join(log_path.read_text(errors="replace").splitlines(keepends=True)[-12:])
        except OSError:
            tail = ""
        try:
            marker.rename(marker.parent / (marker.name + ".alerted"))
        except OSError:
            # Couldn't tombstone — skip rather than risk re-alerting every tick.
            log.exception("deploy.surface_worker_restart_fails: tombstone failed for %s", marker)
            continue
        out.append({"queue_id": qid, "tail": tail})
    return out


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
