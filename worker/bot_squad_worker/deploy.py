"""Deploy queue + per-project recipe runner.

Public API
----------
enqueue(cfg, slug, target, reason, requested_by) -> queue_id: str
    Write a JSON request file to data/<slug>/_jobs/deploy/queue/.

list_queued(cfg, slug) -> list[Path]
    Return queue files sorted oldest-first (by filename, which is ts-prefixed).

run_next(cfg, slug) -> DeployResult | None
    Pop the oldest queued request, check git tree cleanliness, run the recipe.
    - Returns None if queue is empty.
    - Returns None if tree is dirty (queue file stays).
    - Returns DeployResult(ok, returncode, queue_id, log_path) on success or
      failure (rc != 0 or recipe file missing → rc=99).

Quiescence-ignore patterns (same as cctv):
    Any line from ``git status --porcelain`` whose filename matches
    logs/, cache/, __pycache__/, .pyc, *.tmp is ignored when deciding
    whether the tree is "dirty".

Queue file lifecycle:
    queue/<id>.json
        → processing/<id>.json   (while recipe runs)
        → processed/<id>.ok      (on success)
        → processed/<id>.fail.<rc>  (on failure)

Recipe convention: data/<slug>/deploy/<target>.sh
    Executed via ``bash <recipe_path>`` with cwd = proj.repo_path.
    stdout+stderr captured to data/<slug>/_jobs/deploy/runs/<id>.log.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# Patterns in git --porcelain output that are ignored for cleanliness checks.
# Each is tested against the full line (e.g. " M logs/something.log").
_QUIESCENCE_IGNORE = (
    "logs/",
    "cache/",
    "__pycache__/",
    ".pyc",
    ".tmp",
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
    """Public cleanliness check for the clone associated with this target.

    Lets callers (e.g. the deploy_monitor in jobs.py) gate user-facing
    notifications without re-implementing the check or popping a queue file.
    """
    project = cfg.projects[slug]
    return _is_clean(project.repo_for_target(target))


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

    # Pick the clone for this target: prod → master clone (separate dir so
    # dev work continues uninterrupted); everything else → dev clone.
    repo = project.repo_for_target(target)
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
    log.info("deploy.run_next: running %s (recipe: %s, cwd: %s)", queue_id, recipe, repo)
    with log_path.open("w") as lf:
        proc = subprocess.run(
            ["bash", str(recipe)],
            cwd=str(repo),
            stdout=lf,
            stderr=subprocess.STDOUT,
            timeout=600,
        )

    rc = proc.returncode
    _finish(cfg, slug, processing_file, queue_id, rc=rc)
    for pf in collapsed_processing:
        _finish(cfg, slug, pf, _queue_id_of(pf), rc=rc)
    ok = rc == 0
    log.info("deploy.run_next: %s/%s finished rc=%d (collapsed=%d)", slug, target, rc, 1 + len(collapsed_processing))
    return DeployResult(
        ok=ok, returncode=rc, queue_id=queue_id, log_path=log_path,
        collapsed_count=1 + len(collapsed_processing),
        collapsed_reasons=tuple(collapsed_reasons),
    )


def _queue_id_of(processing_file: Path) -> str:
    """Recover the queue_id from a processing file's name (<ts>-<uuid>.json)."""
    stem = processing_file.stem  # "<ts_ms>-<uuid>"
    # Everything after the first dash is the uuid
    _, _, qid = stem.partition("-")
    return qid


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
