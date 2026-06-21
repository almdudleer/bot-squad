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
# T-0107 — delay before the detached worker-restart fires, in seconds. Gives
# the apply caller time to write final state + return; also lets the calling
# tick complete cleanly before systemd SIGTERMs the worker.
WORKER_RESTART_DELAY_SECONDS = 5


def _extra_rsync_excludes() -> tuple[str, ...]:
    """Per-install rsync exclude patterns for the apply pipeline.

    Read once per call from ``BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES`` (comma-
    separated). Lets a consumer install preserve site-local customizations
    that aren't in the upstream tarball — typically ``docker-compose.yml``
    overrides, ``.env`` files, or sidecar service definitions. The base
    ``data/`` exclusion is always applied separately and isn't configurable.

    Default: empty (canonical consumer behavior — full sync from upstream).
    """
    raw = os.environ.get("BOT_SQUAD_AUTOUPDATE_EXTRA_EXCLUDES", "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(parts)


def _sync_excludes() -> tuple[str, ...]:
    """Full exclude tuple for the install-tree sync step.

    Always includes ``data/`` (mutable user state — explicit in T-0084 spec).
    Appends ``_extra_rsync_excludes()`` for per-install customizations.
    """
    return ("data/", *_extra_rsync_excludes())


def _schedule_worker_restart(delay_sec: int = WORKER_RESTART_DELAY_SECONDS) -> None:
    """Schedule a detached worker restart after a successful apply (T-0107).

    The apply rewrites the worker's own code on disk; the running process is
    still serving the old in-memory bytecode. We can't synchronously
    ``systemctl restart`` from inside the worker — that SIGTERMs the very
    process running this call. So fork a detached child that sleeps a few
    seconds (long enough for the caller to record success state and return)
    and then invokes the restart command. systemd respawns the worker,
    which then runs the new code.

    Override the command via ``BOT_SQUAD_AUTOUPDATE_RESTART_CMD``. Set it to
    ``off`` / ``none`` / ``skip`` / empty string to disable (useful for
    tests, dogfood runs, or environments where the worker isn't under
    systemd-user).
    """
    cmd = os.environ.get(
        "BOT_SQUAD_AUTOUPDATE_RESTART_CMD",
        "systemctl --user restart bot-squad-worker",
    )
    if not cmd or cmd.strip().lower() in {"off", "none", "skip"}:
        log.info(
            "autoupdate_apply: worker restart disabled (BOT_SQUAD_AUTOUPDATE_RESTART_CMD=%r)",
            cmd,
        )
        return
    try:
        # start_new_session detaches from the worker's process group so
        # systemd's eventual SIGTERM on the worker doesn't take the
        # restart child down with it. nohup belt-and-suspenders.
        full = f"sleep {int(delay_sec)} && {cmd}"
        subprocess.Popen(
            ["nohup", "sh", "-c", full],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info(
            "autoupdate_apply: scheduled detached worker restart in %ds (%s)",
            delay_sec, cmd,
        )
    except Exception:
        log.exception(
            "autoupdate_apply: failed to schedule worker restart (cmd=%r)", cmd,
        )


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
    _rsync(install_root(cfg), snap, delete=True, exclude=_sync_excludes())
    return snap


def _restore(cfg: Any, snap: Path) -> None:
    """Restore the install tree from a snapshot. data/ in the live install
    is preserved (rsync ignores it via --exclude=data/)."""
    if not snap.exists():
        raise RuntimeError(f"snapshot missing during restore: {snap}")
    _rsync(snap, install_root(cfg), delete=True, exclude=_sync_excludes())


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


def _docker_compose_up_build(cfg: Any, git_sha: Optional[str] = None) -> str:
    """Run ``docker compose up -d --build`` in the install root. Returns the
    tail of combined stdout/stderr (best-effort context for alerts).

    T-0379: when ``git_sha`` is given (the released commit from the manifest
    entry), export it as the ``GIT_SHA`` build-arg so the image bakes
    ``BOT_SQUAD_GIT_SHA`` (surfaced at /api/health). The caller then asserts the
    running container == the released sha — the consumer-path mirror of
    ``deploy-recipes/bot-squad/staging.sh``'s running==deployed gate, so a build
    that didn't pick up the released source can't silently report success."""
    root = install_root(cfg)
    env = {**os.environ, "TMPDIR": str(root / "_tmp")}
    if git_sha:
        env["GIT_SHA"] = git_sha
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


def _running_git_sha(cfg: Any) -> str:
    """The ``BOT_SQUAD_GIT_SHA`` baked into the RUNNING api container, or ``""``
    if unreadable. T-0379: used to assert the just-built container is the
    released commit. Best-effort — a read failure yields "" (→ mismatch → the
    apply rolls back rather than trusting an unverifiable build)."""
    try:
        proc = subprocess.run(
            ["docker", "exec", "bot-squad-api", "printenv", "BOT_SQUAD_GIT_SHA"],
            cwd=str(install_root(cfg)),
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — never let the check itself wedge apply
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


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


def _install_identifier(cfg: Any) -> str:
    """Best-effort label for this install in operator handoff messages.

    Preference order:
      1. ``cfg.projects["bot-squad"].prod_url`` (the canonical self-URL — same
         lookup the smoke-test uses).
      2. ``BOT_SQUAD_SELF_URL`` env override.
      3. literal "unknown-install" — never raise; the alert must always go out.
    """
    projects = getattr(cfg, "projects", None) or {}
    proj = projects.get("bot-squad") if hasattr(projects, "get") else None
    if proj is not None:
        url = getattr(proj, "prod_url", None)
        if url:
            return str(url).rstrip("/")
    env = os.environ.get("BOT_SQUAD_SELF_URL")
    if env:
        return env.rstrip("/")
    return "unknown-install"


def _retry_command() -> str:
    """Operator-runnable command string for the retry path. Surfaced in the
    alert payload + the tg message body."""
    return "bot-squad-cli autoupdate retry"


def _force_command(version: str) -> str:
    return f"bot-squad-cli autoupdate force {version}"


def _write_alert(cfg: Any, *, version: str, step: str, log_tail: str) -> None:
    """Write a structured failure banner for T-0089's UI banner + T-0085's
    operator handoff."""
    payload = {
        "version": version,
        "step": step,
        "log_tail": log_tail,
        "occurred_at": _now_iso(),
        "retry_command": _retry_command(),
        "force_command": _force_command(version),
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


def _format_failure_message(
    *,
    install_id: str,
    version: str,
    step: str,
    log_tail: str,
) -> str:
    """Build the tg body for a failed apply.

    Format is intentionally plain-text (TG client doesn't pass a parse_mode)
    and bounded — log tail is already capped to ~20 lines upstream, but we
    trim the final body to ~3000 chars to stay well below TG's 4096 limit.
    """
    body = (
        "🚨 bot-squad autoupdate FAILED\n"
        f"install:  {install_id}\n"
        f"version:  {version}\n"
        f"step:     {step}\n"
        "\n"
        "log tail:\n"
        f"{log_tail}\n"
        "\n"
        "recovery — run on the consumer host:\n"
        f"  {_retry_command()}    # re-attempt the same failed version\n"
        f"  {_force_command(version)}  # force-apply a specific version"
    )
    if len(body) > 3000:
        body = body[:2997] + "..."
    return body


def _notify_failure(cfg: Any, *, version: str, step: str, log_tail: str) -> None:
    """Operator handoff: send a structured TG message to the consumer's
    operator chat on every apply failure.

    Errors here are swallowed (logged at WARNING) — the alert.json banner
    is already on disk by the time we get called, so the operator can still
    recover even if TG is unreachable / misconfigured.
    """
    install_id = _install_identifier(cfg)
    text = _format_failure_message(
        install_id=install_id,
        version=version,
        step=step,
        log_tail=log_tail,
    )

    chat_id = _operator_chat_id(cfg)
    if not chat_id:
        log.warning(
            "autoupdate_apply._notify_failure: no operator chat configured "
            "(missing projects['bot-squad'].tg_chat); banner-only handoff"
        )
        return

    try:
        from bot_squad_worker.tg import TgClient
        TgClient(cfg).send(
            chat_id=chat_id,
            text=text,
            sid="",
            user="",
            urgent=True,  # apply failure bypasses quiet hours per spec
        )
    except Exception:
        log.exception(
            "autoupdate_apply._notify_failure: tg send failed for "
            "version=%s step=%s — banner at %s remains the recovery surface",
            version, step, alert_path(cfg),
        )


def _operator_chat_id(cfg: Any) -> Optional[str]:
    """Resolve the operator chat for autoupdate alerts.

    Uses the ``bot-squad`` project's ``tg_chat`` (each install has its own
    bot-squad entry in projects.toml pointing at its operator). Returns
    None when projects config is unavailable — caller treats that as
    banner-only handoff.
    """
    projects = getattr(cfg, "projects", None) or {}
    proj = projects.get("bot-squad") if hasattr(projects, "get") else None
    if proj is None:
        return None
    chat = getattr(proj, "tg_chat", None)
    return str(chat) if chat else None


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
    if is_mothership(config_dir=getattr(cfg, "config_dir", None)):
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
            _rsync(extracted, install_root(cfg), delete=True, exclude=_sync_excludes())
        except Exception as e:
            _safe_restore(cfg, snap, version)
            return _finish_failure(cfg, version, "extract", f"{type(e).__name__}: {e}")

        # ---- 5. docker compose up -d --build (stamp the released sha — T-0379)
        try:
            _docker_compose_up_build(cfg, git_sha=entry.get("git_sha"))
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

        # ---- 6b. T-0379: assert the RUNNING container is the released commit.
        # The consumer-path mirror of staging.sh's running==deployed gate: a
        # build that didn't pick up the released source (cache/context drift)
        # must NOT report success — roll back instead of shipping stale.
        expected_git_sha = entry.get("git_sha")
        if expected_git_sha:
            running_sha = _running_git_sha(cfg)
            if running_sha != expected_git_sha:
                _safe_restore(cfg, snap, version)
                _docker_compose_up_no_build(cfg)  # bring prior containers back
                return _finish_failure(
                    cfg, version, "git_sha_verify",
                    f"running={running_sha!r} != released={expected_git_sha!r}",
                )

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

        # T-0107 — schedule the worker self-restart so the running process
        # picks up the new code. Fires AFTER state is persisted and we
        # return, so the caller (drain_one / the tick) finishes cleanly.
        _schedule_worker_restart()

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
    """Process at most ONE queue file. Returns None if the queue is empty,
    if the operator has paused autoupdate (T-0089), or if this is the
    mothership."""
    if is_mothership(config_dir=getattr(cfg, "config_dir", None)):
        return None

    # T-0089: honor the operator pause flag. Queued jobs sit untouched
    # until the flag is cleared; unpause + drain on the next tick.
    if _poller.is_paused(cfg):
        log.debug("autoupdate_apply: PAUSED (T-0089) — drain is a no-op")
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


# ---------------------------------------------------------------------------
# Operator handoff levers (T-0085): retry + force
# ---------------------------------------------------------------------------


def _newest_failed(cfg: Any) -> Optional[Path]:
    """Most-recently-parked file in the failed-queue (highest mtime)."""
    fdir = failed_queue_dir(cfg)
    if not fdir.exists():
        return None
    files = [p for p in fdir.iterdir() if p.is_file() and p.suffix == ".json"]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def retry_last_failed(cfg: Any) -> dict:
    """Move the most-recent failed manifest entry back into the live queue.

    Idempotent: no-op (``{"ok": True, "requeued": False}``) when the
    failed-queue is empty. On success returns the version it requeued so
    the operator gets confirmation in the action response.
    """
    src = _newest_failed(cfg)
    if src is None:
        return {"ok": True, "requeued": False, "reason": "failed-queue empty"}

    # Best-effort: peek at the file to surface the version in the response.
    version: str = "?"
    try:
        version = json.loads(src.read_text()).get("version", "?")
    except (OSError, json.JSONDecodeError):
        # Bad file — let the drain loop park it again rather than blocking
        # the retry; the operator at least gets the move acknowledged.
        log.warning("autoupdate_apply.retry: unreadable failed entry %s", src)

    qdir = queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / src.name
    try:
        src.replace(dest)
    except OSError as e:
        raise RuntimeError(f"could not requeue {src.name}: {e}") from e

    log.info("autoupdate_apply.retry: requeued %s (version=%s)", src.name, version)
    return {"ok": True, "requeued": True, "version": version, "queue_file": dest.name}


def _fetch_release_entry(version: str) -> Optional[dict]:
    """GET ``<mothership>/api/releases/<version>`` and return the manifest entry.

    Returns ``None`` when:
      * ``BOT_SQUAD_MOTHERSHIP_URL`` is unset (no mothership configured), or
      * the upstream returns a non-2xx / non-dict body, or
      * a transport/timeout error occurs.

    No retry — operator-driven action; let the operator re-run if the
    upstream is briefly unreachable.
    """
    base = _poller.mothership_url()
    if not base:
        return None
    url = f"{base.rstrip('/')}/api/releases/{version}"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        entry = resp.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as e:
        log.warning("autoupdate_apply.force: fetch failed for %s: %s", url, e)
        return None
    if not isinstance(entry, dict):
        log.warning("autoupdate_apply.force: bad body from %s: %r", url, type(entry))
        return None
    return entry


def force_apply(cfg: Any, version: str) -> dict:
    """Enqueue an apply job for ``version``, bypassing the poller's newer-than
    gate. Used to roll forward past a known-bad release once a fix is shipped.

    The manifest entry is fetched from the mothership's
    ``/api/releases/<version>`` endpoint (T-0082). On fetch failure we
    surface an error so the operator sees the problem immediately rather
    than the apply pipeline later choking on a bad entry.
    """
    if not isinstance(version, str) or not version:
        raise RuntimeError("force_apply: empty version")

    entry = _fetch_release_entry(version)
    if entry is None:
        raise RuntimeError(
            f"force_apply: could not fetch manifest for {version} "
            "(check BOT_SQUAD_MOTHERSHIP_URL and that the version exists)"
        )

    # Sanity: mothership returned a different version than asked for.
    got = entry.get("version")
    if got and got != version:
        raise RuntimeError(
            f"force_apply: mothership returned version={got!r} for request {version!r}"
        )

    path = _poller._enqueue_apply(cfg, entry)
    log.info("autoupdate_apply.force: enqueued %s as %s", version, path.name)
    return {"ok": True, "version": version, "queue_file": path.name}
