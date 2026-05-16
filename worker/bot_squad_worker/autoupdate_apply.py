"""Consumer-side autoupdate apply pipeline + rollback (T-0084).

The poller (T-0083, ``bot_squad_worker.autoupdate``) writes manifest
entries into ``data/_worker/autoupdate_queue/<uuid>.json`` whenever the
mothership publishes a newer release. This module is the *drainer*: it
takes one queued entry at a time and runs the full apply pipeline on it,
with snapshot-and-restore semantics so a failed deploy never leaves the
install in a broken state.

Pipeline (per T-0084 DoD)::

    1. download tarball_url → data/_worker/autoupdate_tmp/<job>/release.tar.gz
    2. verify sha256 matches manifest_entry.sha256
    3. snapshot install tree → <install>.snapshot.<prev_version>
                               (excludes data/ — user state must persist)
    4. extract tarball → data/_worker/autoupdate_tmp/<job>/extracted/
       rsync extracted/ → install/ (--delete --exclude=data/)
    5. docker compose up -d --build  (with TMPDIR workaround per staging.sh)
    6. smoke GET <self_url>/api/health with retry/backoff
       (per T-0079 lesson — don't insta-curl)
    7. on success: bump installed_version, last_apply_outcome="success",
                   remove snapshot + tmp + queue file, clear alert.json
    8. on failure: restore snapshot, docker compose up -d,
                   write alert.json, _notify_failure (T-0085 stub),
                   move queue file to autoupdate_queue_failed/

Snapshot path
-------------
``<install_root>.snapshot.<prev_version>`` — sibling of the install root
so the swap is a directory-level rename. Excludes ``data/`` because the
data tree holds mutable user state (sessions, releases, oauth, etc.)
that must persist across releases. T-0084 spec is explicit about this.

Smoke retry/backoff
-------------------
``_smoke()`` retries with growing delays (5s, 10s, 20s, 30s — total ≤65s
wall time) before declaring failure. This is the T-0079 lesson made
permanent: docker compose up returns the moment containers are *created*,
not when they're *serving*. An insta-curl reliably races the api boot.

Mothership self-exclusion
-------------------------
Apply is a no-op on the mothership for the same reason the poller is
(T-0086). The drain entrypoint short-circuits before touching the queue.

Cross-refs
----------
* Manifest schema: T-0081.
* Queue producer: T-0083 (``bot_squad_worker.autoupdate``).
* Operator handoff on failure: T-0085 (``_notify_failure`` is a stub
  here; T-0085 fills in tg_notify + the retry/force actions).
* UI status surface: T-0089.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from bot_squad_worker import autoupdate as _poller
from bot_squad_worker.install_role import is_mothership

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (env-overridable so tests / ops can tweak without code change)
# ---------------------------------------------------------------------------

DOWNLOAD_TIMEOUT_SECONDS = 300.0  # large tarball over a slow link
DOCKER_BUILD_TIMEOUT_SECONDS = 1800  # 30min cap on docker compose up --build
SMOKE_PATH = "/api/health"
SMOKE_BACKOFF_SECONDS = (5, 10, 20, 30)  # ≤65s total wall time
SMOKE_REQUEST_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of a single ``apply()`` call.

    ``failed_step`` is None on success, otherwise one of the canonical
    step names stamped into ``last_apply_outcome`` (e.g. ``"sha_mismatch"``,
    ``"download"``, ``"snapshot"``, ``"extract"``, ``"build"``, ``"smoke"``,
    ``"restore"``). ``log_tail`` is best-effort context (last ~20 lines of
    whatever subprocess produced the failure, or the exception message).
    """

    ok: bool
    version: str
    failed_step: Optional[str] = None
    log_tail: str = ""
    skipped: bool = False  # True when apply was a no-op (e.g. mothership)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def install_root(cfg: Any) -> Path:
    """The install tree we're updating. ``data/`` lives under it; the snapshot
    excludes that subtree.

    Resolved as ``cfg.data_dir.parent``. The ``BOT_SQUAD_INSTALL_ROOT`` env
    var overrides for tests that want to point at a tmp dir.
    """
    override = os.environ.get("BOT_SQUAD_INSTALL_ROOT")
    if override:
        return Path(override)
    return cfg.data_dir.parent


def tmp_dir(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "autoupdate_tmp"


def queue_dir(cfg: Any) -> Path:
    # SSOT: same dir the poller writes to.
    return _poller.queue_dir(cfg)


def failed_queue_dir(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "autoupdate_queue_failed"


def alert_path(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "autoupdate_alert.json"


def snapshot_path(cfg: Any, prev_version: str) -> Path:
    root = install_root(cfg)
    # Sibling dir: /home/www/bot-squad → /home/www/bot-squad.snapshot.<v>
    return root.parent / f"{root.name}.snapshot.{prev_version}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Self-URL resolution
# ---------------------------------------------------------------------------


def _self_url(cfg: Any) -> Optional[str]:
    """URL this server uses to talk to its own api. Used for the smoke test.

    Lookup order:
      1. ``BOT_SQUAD_SELF_URL`` env (operator override).
      2. ``cfg.projects["bot-squad"].prod_url`` (the canonical self-URL on
         each install: each consumer's own bot-squad project entry points
         at its own host).
    """
    override = os.environ.get("BOT_SQUAD_SELF_URL")
    if override:
        return override.rstrip("/")
    projects = getattr(cfg, "projects", None) or {}
    proj = projects.get("bot-squad") if hasattr(projects, "get") else None
    if proj is not None:
        url = getattr(proj, "prod_url", None)
        if url:
            return str(url).rstrip("/")
    return None


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _tarball_url(entry: dict) -> Optional[str]:
    """Resolve the tarball URL from a manifest entry.

    Prefers ``tarball_url`` (added by T-0082's API at response time). Falls
    back to constructing ``<mothership_url>/<tarball_path>`` if only the
    path is present — defensive for direct-from-file consumers.
    """
    url = entry.get("tarball_url")
    if isinstance(url, str) and url:
        return url
    path = entry.get("tarball_path")
    if isinstance(path, str) and path:
        base = _poller.mothership_url()
        if not base:
            return None
        return f"{base.rstrip('/')}/{path.lstrip('/')}"
    return None


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def _download(url: str, dest: Path, *, timeout: float = DOWNLOAD_TIMEOUT_SECONDS) -> None:
    """Stream the tarball to ``dest``. Raises on transport / HTTP error."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _rsync(src: Path, dest: Path, *, delete: bool, exclude: tuple[str, ...] = ()) -> None:
    """Mirror ``src`` → ``dest``. Trailing slash on src is enforced to copy
    contents (not the dir itself)."""
    src_arg = f"{str(src).rstrip('/')}/"
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a"]
    if delete:
        cmd.append("--delete")
    for pat in exclude:
        cmd.extend(["--exclude", pat])
    cmd.extend([src_arg, str(dest)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"rsync failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )


def _snapshot(cfg: Any, prev_version: str) -> Path:
    """Snapshot the install tree (sans data/) to a sibling dir. Returns the
    snapshot path. Idempotent: clears a prior snapshot at the same path."""
    snap = snapshot_path(cfg, prev_version)
    if snap.exists():
        shutil.rmtree(snap)
    _rsync(install_root(cfg), snap, delete=True, exclude=("data/",))
    return snap


def _restore(cfg: Any, snap: Path) -> None:
    """Restore the install tree from a snapshot. data/ in the live install
    is preserved (rsync ignores it via --exclude=data/)."""
    if not snap.exists():
        raise RuntimeError(f"snapshot missing during restore: {snap}")
    _rsync(snap, install_root(cfg), delete=True, exclude=("data/",))


def _extract(tarball: Path, dest: Path) -> None:
    """Extract a .tar.gz into ``dest``. Wipes ``dest`` first to avoid mixing
    leftovers from a prior aborted apply."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    proc = subprocess.run(
        ["tar", "-xzf", str(tarball), "-C", str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"tar -xzf failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )


def _docker_compose_up_build(cfg: Any) -> str:
    """Run ``docker compose up -d --build`` in the install root. Returns the
    tail of combined stdout/stderr (best-effort context for alerts)."""
    root = install_root(cfg)
    env = {**os.environ, "TMPDIR": str(root / "_tmp")}
    (root / "_tmp").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["docker", "compose", "up", "-d", "--build"],
        cwd=str(root),
        env=env,
        capture_output=True, text=True,
        timeout=DOCKER_BUILD_TIMEOUT_SECONDS,
    )
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker compose up --build failed (rc={proc.returncode})\n{_tail(out)}"
        )
    return _tail(out)


def _docker_compose_up_no_build(cfg: Any) -> str:
    """``docker compose up -d`` (no rebuild) — used during rollback to bring
    the snapshot-restored containers back."""
    root = install_root(cfg)
    env = {**os.environ, "TMPDIR": str(root / "_tmp")}
    (root / "_tmp").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=str(root),
        env=env,
        capture_output=True, text=True,
        timeout=DOCKER_BUILD_TIMEOUT_SECONDS,
    )
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        # Don't raise — we're mid-rollback; just surface the tail.
        log.warning("docker compose up (no-build) during rollback rc=%d:\n%s",
                    proc.returncode, _tail(out))
    return _tail(out)


def _smoke(url: str) -> None:
    """GET ``<url>/api/health`` with retry/backoff. Raises on permanent failure.

    Per T-0079 lesson: docker compose returns when containers are created,
    not when they're serving. We retry with 5s, 10s, 20s, 30s gaps (≤65s
    wall time) before giving up.
    """
    target = f"{url.rstrip('/')}{SMOKE_PATH}"
    last_err: Optional[Exception] = None
    for i, delay in enumerate(SMOKE_BACKOFF_SECONDS, start=1):
        # Wait before the attempt (api is almost never ready immediately
        # after `up -d`; first-curl misses are the norm not the exception).
        time.sleep(delay)
        try:
            resp = httpx.get(target, timeout=SMOKE_REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            log.info("autoupdate_apply: smoke OK on attempt %d (%s)", i, target)
            return
        except (httpx.HTTPError, httpx.HTTPStatusError) as e:
            last_err = e
            log.info("autoupdate_apply: smoke attempt %d failed (%s): %s", i, target, e)
            continue
    raise RuntimeError(
        f"smoke failed for {target} after {len(SMOKE_BACKOFF_SECONDS)} attempts: {last_err}"
    )


def _tail(s: str, n: int = 20) -> str:
    lines = s.splitlines()
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# Alert / handoff (the T-0085 seam)
# ---------------------------------------------------------------------------


def _write_alert(cfg: Any, *, version: str, step: str, log_tail: str) -> None:
    """Write a structured failure banner for T-0089's UI banner + T-0085's
    operator handoff. Schema is intentionally minimal — T-0085 may extend it."""
    payload = {
        "version": version,
        "step": step,
        "log_tail": log_tail,
        "occurred_at": _now_iso(),
        # T-0085 will fill these in with real operator commands.
        "retry_command": "bot-squad-cli autoupdate retry",
        "force_command": f"bot-squad-cli autoupdate force {version}",
    }
    p = alert_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _clear_alert(cfg: Any) -> None:
    p = alert_path(cfg)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            log.warning("autoupdate_apply: could not clear alert %s", p)


def _notify_failure(cfg: Any, *, version: str, step: str, log_tail: str) -> None:
    """Operator handoff seam.

    T-0085 owns the body (tg_notify to the consumer operator chat with the
    structured failure message). This stub exists so T-0084's failure paths
    have somewhere to call right now, and T-0085's wire-up is a single-edit
    diff. Do NOT inline tg_notify here — keep the seam clean.
    """
    # TODO(T-0085): tg_notify(cfg, ...) with install-id, version, step, log_tail,
    # and the autoupdate_retry / autoupdate_force command strings.
    log.warning(
        "autoupdate_apply: failure pending operator handoff "
        "(version=%s step=%s); see %s",
        version, step, alert_path(cfg),
    )


# ---------------------------------------------------------------------------
# Apply — the orchestrator
# ---------------------------------------------------------------------------


def apply(cfg: Any, entry: dict) -> ApplyResult:
    """Run the full apply pipeline for one manifest entry.

    Always returns an ApplyResult; never raises. On failure the install is
    restored from snapshot and the alert file + handoff hook are populated.
    The poller's ``autoupdate.json`` is updated either way (last_apply_at,
    last_apply_outcome, installed_version on success).
    """
    if is_mothership():
        log.debug("autoupdate_apply: mothership self-exclusion — apply is a no-op")
        return ApplyResult(ok=True, version=entry.get("version", "?"), skipped=True)

    version = entry.get("version")
    if not isinstance(version, str) or not version:
        log.warning("autoupdate_apply: manifest missing 'version': %r", entry)
        return ApplyResult(ok=False, version="?", failed_step="bad_entry",
                           log_tail="manifest entry missing 'version' field")

    expected_sha = entry.get("sha256")
    if not isinstance(expected_sha, str) or not expected_sha:
        return _finish_failure(cfg, version, "bad_entry",
                               "manifest entry missing 'sha256' field")

    url = _tarball_url(entry)
    if not url:
        return _finish_failure(cfg, version, "bad_entry",
                               "manifest entry has no tarball_url and no resolvable tarball_path")

    self_url = _self_url(cfg)
    if not self_url:
        return _finish_failure(cfg, version, "bad_config",
                               "cannot resolve self URL for smoke "
                               "(set BOT_SQUAD_SELF_URL or projects.bot-squad.prod_url)")

    state = _poller.load_state(cfg)
    prev_version = state.get("installed_version") or "unknown"

    job_id = uuid.uuid4().hex
    job_tmp = tmp_dir(cfg) / job_id
    tarball = job_tmp / "release.tar.gz"
    extracted = job_tmp / "extracted"
    snap: Optional[Path] = None

    try:
        # ---- 1. download
        try:
            _download(url, tarball)
        except Exception as e:
            return _finish_failure(cfg, version, "download", f"{type(e).__name__}: {e}")

        # ---- 2. verify sha
        actual_sha = _sha256(tarball)
        if actual_sha != expected_sha:
            # Per spec: sha_mismatch must NOT touch the install tree.
            return _finish_failure(
                cfg, version, "sha_mismatch",
                f"expected={expected_sha} actual={actual_sha}",
            )

        # ---- 3. snapshot install tree
        try:
            snap = _snapshot(cfg, prev_version)
        except Exception as e:
            return _finish_failure(cfg, version, "snapshot", f"{type(e).__name__}: {e}")

        # ---- 4. extract + sync into install
        try:
            _extract(tarball, extracted)
            _rsync(extracted, install_root(cfg), delete=True, exclude=("data/",))
        except Exception as e:
            _safe_restore(cfg, snap, version)
            return _finish_failure(cfg, version, "extract", f"{type(e).__name__}: {e}")

        # ---- 5. docker compose up -d --build
        try:
            _docker_compose_up_build(cfg)
        except Exception as e:
            _safe_restore(cfg, snap, version)
            _docker_compose_up_no_build(cfg)  # bring prior containers back
            return _finish_failure(cfg, version, "build", f"{type(e).__name__}: {e}")

        # ---- 6. smoke
        try:
            _smoke(self_url)
        except Exception as e:
            _safe_restore(cfg, snap, version)
            _docker_compose_up_no_build(cfg)  # bring prior containers back
            return _finish_failure(cfg, version, "smoke", f"{type(e).__name__}: {e}")

        # ---- 7. success bookkeeping
        new_state = _poller.load_state(cfg)
        new_state["installed_version"] = version
        new_state["current_git_sha"] = entry.get("git_sha") or new_state.get("current_git_sha")
        new_state["last_apply_at"] = _now_iso()
        new_state["last_apply_outcome"] = "success"
        _poller.save_state(cfg, new_state)

        # Remove snapshot + alert + tmp on success.
        if snap is not None and snap.exists():
            shutil.rmtree(snap, ignore_errors=True)
        _clear_alert(cfg)

        log.info("autoupdate_apply: success — installed_version=%s (prev=%s)",
                 version, prev_version)
        return ApplyResult(ok=True, version=version)

    finally:
        # Always clean the per-job tmp dir; never leaks across runs.
        shutil.rmtree(job_tmp, ignore_errors=True)


def _safe_restore(cfg: Any, snap: Optional[Path], version: str) -> None:
    """Restore the install tree from snapshot; swallow errors but log loudly
    (we still want the outer pipeline to record the original failure step
    rather than masking it with a restore error)."""
    if snap is None:
        return
    try:
        _restore(cfg, snap)
    except Exception:
        log.exception(
            "autoupdate_apply: RESTORE FAILED after pipeline failure on %s — "
            "install tree may be inconsistent; snapshot retained at %s",
            version, snap,
        )


def _finish_failure(cfg: Any, version: str, step: str, log_tail: str) -> ApplyResult:
    """Common failure tail: stamp state, write alert, fire handoff hook."""
    state = _poller.load_state(cfg)
    state["last_apply_at"] = _now_iso()
    state["last_apply_outcome"] = f"failed:{step}"
    _poller.save_state(cfg, state)
    _write_alert(cfg, version=version, step=step, log_tail=log_tail)
    _notify_failure(cfg, version=version, step=step, log_tail=log_tail)
    log.warning("autoupdate_apply: FAILED version=%s step=%s", version, step)
    return ApplyResult(ok=False, version=version, failed_step=step, log_tail=log_tail)


# ---------------------------------------------------------------------------
# Queue drain — APScheduler entrypoint
# ---------------------------------------------------------------------------


def _next_queued(cfg: Any) -> Optional[Path]:
    qdir = queue_dir(cfg)
    if not qdir.exists():
        return None
    files = sorted(qdir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return files[0] if files else None


def drain_one(cfg: Any) -> Optional[ApplyResult]:
    """Process at most ONE queue file. Returns None if the queue is empty
    (or this is the mothership)."""
    if is_mothership():
        return None

    qfile = _next_queued(cfg)
    if qfile is None:
        return None

    try:
        entry = json.loads(qfile.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("autoupdate_apply: bad queue file %s: %s — moving to failed", qfile, e)
        _move_to_failed(cfg, qfile)
        return None

    result = apply(cfg, entry)

    if result.ok:
        # Job done — remove the queue file.
        try:
            qfile.unlink()
        except OSError:
            log.warning("autoupdate_apply: could not remove queue file %s", qfile)
    else:
        # Park the failed job so the drain loop doesn't infinite-retry it.
        # T-0085's autoupdate_retry action moves it back into the live queue.
        _move_to_failed(cfg, qfile)

    return result


def _move_to_failed(cfg: Any, qfile: Path) -> None:
    dest_dir = failed_queue_dir(cfg)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / qfile.name
    try:
        qfile.replace(dest)
    except OSError:
        log.warning("autoupdate_apply: could not park %s under %s", qfile, dest_dir)


def tick(cfg: Any) -> None:
    """Scheduler entrypoint. Drains at most one job per tick.

    Cadence is short (see scheduler wiring) so a freshly-enqueued release
    starts applying within a minute, but the apply itself can take many
    minutes — APScheduler ``max_instances=1`` keeps overlapping ticks from
    starting a second apply while one is in flight.
    """
    try:
        drain_one(cfg)
    except Exception:
        log.exception("autoupdate_apply.tick: unhandled error")
