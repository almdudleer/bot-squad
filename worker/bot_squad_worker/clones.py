"""Per-project clone health read-model + pull-master (T-0296).

The mothership's ``/p/:slug/clones`` page needs to SEE a project's dev+master
clone topology — the "Installation != Project" legibility deliverable. The
WORKER owns this (not the API): only the worker runs on-host with git access to
the clones; the API container mounts just its own data dirs, so it proxies to
these via worker actions.

``clone_status`` is read-only. ``pull_master`` mutates the prod (master) clone
and is admin-gated at the API edge before the action is ever called.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from bot_squad_worker.deploy import _is_clean, _processed_dir


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run ``git -C <repo> <args>``; return ``(returncode, stdout.strip())``.

    Never raises — git/permission/dir failures collapse to a non-zero rc so the
    read-model degrades to nulls rather than 500ing the whole page."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return 1, ""
    return p.returncode, p.stdout.strip()


def _ahead_behind(repo: Path, upstream: str) -> tuple[int | None, int | None]:
    """``(ahead, behind)`` of HEAD vs ``upstream`` (e.g. ``origin/master``).

    ``git rev-list --left-right --count <upstream>...HEAD`` prints
    ``<behind>\\t<ahead>`` (left = upstream-only commits, right = HEAD-only).
    Returns ``(None, None)`` when the upstream ref is missing/unfetched."""
    rc, out = _git(repo, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    if rc != 0 or "\t" not in out:
        return None, None
    left, _, right = out.partition("\t")
    try:
        return int(right), int(left)
    except ValueError:
        return None, None


def _clone_view(repo: Path | None, master_branch: str) -> dict[str, Any]:
    """Health of a single clone: configured / present / branch / ahead-behind
    vs ``origin/<master_branch>`` / clean (quiescence-aware via deploy._is_clean)."""
    if repo is None:
        return {"configured": False}
    if not repo.exists() or not (repo / ".git").exists():
        return {"configured": True, "present": False, "path": str(repo)}
    rc, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    # Best-effort refresh of the remote-tracking ref so ahead/behind reflects
    # TRUE divergence from origin, not a stale local ref. Offline/auth failures
    # are swallowed by _git → we fall back to whatever origin/<branch> the clone
    # last knew (still useful, just possibly stale).
    _git(repo, "fetch", "--quiet", "origin", master_branch)
    ahead, behind = _ahead_behind(repo, f"origin/{master_branch}")
    return {
        "configured": True,
        "present": True,
        "path": str(repo),
        "branch": branch if rc == 0 else None,
        "clean": _is_clean(repo),
        "ahead": ahead,
        "behind": behind,
    }


def _last_deploy(cfg: Any, slug: str) -> dict[str, Any] | None:
    """Best-effort last-deploy summary from the PROCESSED deploy records (newest).

    T-0355: deploy records land in ``processed/`` as ``<epoch_ms>-<queue_id>.ok``
    (success) or ``<epoch_ms>-<queue_id>.fail.<rc>`` (failure) — the JSON deploy
    job, renamed with an outcome suffix. (``runs/`` holds ``<id>.log`` build logs,
    NOT records — globbing ``runs/*.json`` there always found nothing, so the UI
    permanently showed "No deploys recorded yet".) The ``<epoch_ms>-`` prefix
    makes lexicographic max == newest. Returns ``None`` when nothing's deployed."""
    processed = _processed_dir(cfg, slug)
    if not processed.is_dir():
        return None
    records = [
        p for p in processed.iterdir()
        if p.is_file() and (p.name.endswith(".ok") or ".fail" in p.name)
    ]
    if not records:
        return None
    newest = max(records, key=lambda p: p.name)
    try:
        data = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    ok = newest.name.endswith(".ok")
    returncode: int | None = None
    if not ok and ".fail." in newest.name:
        try:
            returncode = int(newest.name.rsplit(".fail.", 1)[1])
        except (ValueError, IndexError):
            returncode = None
    # `at`: prefer the epoch-ms filename prefix, else the file mtime.
    at = newest.stat().st_mtime
    prefix = newest.name.split("-", 1)[0]
    if prefix.isdigit():
        at = int(prefix) / 1000.0
    return {
        "run_id": data.get("queue_id") or newest.name.split("-", 1)[-1].split(".")[0],
        "at": at,
        "target": data.get("target"),
        "reason": data.get("reason"),
        "requested_by": data.get("requested_by"),
        "ok": ok,
        "returncode": returncode,
    }


def clone_status(cfg: Any, slug: str) -> dict[str, Any]:
    """Read-model: dev + prod clone health + workspace + last-deploy for ``slug``."""
    project = cfg.projects.get(slug)
    if project is None:
        raise KeyError(slug)
    ws = getattr(project, "repo_workspace", None)
    return {
        "slug": slug,
        "master_branch": project.master_branch,
        "deploy_branch": project.deploy_branch,
        "repo_workspace": str(ws) if ws else None,
        "workspace_present": (ws.exists() if ws else None),
        "dev": _clone_view(project.repo_path, project.master_branch),
        "prod": _clone_view(getattr(project, "repo_master", None), project.master_branch),
        "last_deploy": _last_deploy(cfg, slug),
    }


def pull_master(cfg: Any, slug: str) -> dict[str, Any]:
    """Fast-forward the prod (master) clone to ``origin/<master_branch>``.

    Admin-gated at the API edge (no-god-mode) BEFORE this is called. Fetches
    then ``merge --ff-only`` so a diverged/dirty prod clone fails loudly rather
    than silently rewriting. Returns ``{ok, detail, from_sha?, to_sha?}``."""
    project = cfg.projects.get(slug)
    if project is None:
        raise KeyError(slug)
    repo = getattr(project, "repo_master", None)
    if repo is None:
        return {"ok": False, "detail": "no master clone configured (repo_master)"}
    if not repo.exists() or not (repo / ".git").exists():
        return {"ok": False, "detail": f"master clone not present at {repo}"}

    branch = project.master_branch
    rc_from, from_sha = _git(repo, "rev-parse", "--short", "HEAD")
    rc_f, _ = _git(repo, "fetch", "origin", branch)
    if rc_f != 0:
        return {"ok": False, "detail": f"git fetch origin {branch} failed"}
    rc_m, out = _git(repo, "merge", "--ff-only", f"origin/{branch}")
    if rc_m != 0:
        return {
            "ok": False,
            "detail": f"ff-only merge of origin/{branch} failed (diverged/dirty): {out}",
            "from_sha": from_sha if rc_from == 0 else None,
        }
    _, to_sha = _git(repo, "rev-parse", "--short", "HEAD")
    return {
        "ok": True,
        "detail": f"prod clone fast-forwarded to origin/{branch}",
        "from_sha": from_sha if rc_from == 0 else None,
        "to_sha": to_sha,
    }
