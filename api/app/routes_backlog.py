"""Backlog read + write endpoints."""
from __future__ import annotations

import fcntl
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.markdown_parser import ParseError, parse_task
from app.markdown_writer import (
    allocate_next_id,
    append_comment,
    merge_task_update,
    slugify,
    write_task,
)
from app.routes_auth import require_auth

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{slug}/backlog",
    tags=["backlog"],
    dependencies=[Depends(require_auth)],
)

_VALID_STATUSES = {"open", "totest", "reopened", "closed"}
_TASK_ID_RE = re.compile(r"^T-\d{4}$")


def _backlog_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "backlog"


def _find_task_file(backlog_dir: Path, task_id: str) -> Path:
    """Find T-NNNN-*.md for given id, or raise 404."""
    matches = list(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return matches[0]


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID_RE.match(task_id):
        raise HTTPException(status_code=400, detail=f"invalid task id format: {task_id!r}")


@router.get("")
def list_backlog(slug: str, request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    backlog_dir = cfg.project_data_dir(slug) / "backlog"
    if not backlog_dir.exists():
        return []
    tasks = []
    for f in sorted(backlog_dir.glob("*.md")):
        try:
            tasks.append(parse_task(f))
        except ParseError as e:
            log.warning("backlog parse error %s: %s", f, e)
    return tasks


@router.post("")
def create_task(
    slug: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")
    status = payload.get("status", "open")
    if status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status: {status!r}")
    body = payload.get("body") or ""

    backlog_dir = _backlog_dir(request, slug)
    backlog_dir.mkdir(parents=True, exist_ok=True)

    lock_path = backlog_dir / ".lock"
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        task_id = allocate_next_id(backlog_dir)
        slug_part = slugify(title)
        filename = f"{task_id}-{slug_part}.md" if slug_part else f"{task_id}.md"
        path = backlog_dir / filename

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fm = {
            "id": task_id,
            "title": title,
            "status": status,
            "created": now,
            "updated": now,
        }
        write_task(path, fm, body)

    return parse_task(path)


@router.patch("/{task_id}")
def patch_task(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_task_id(task_id)

    if "status" in payload and payload["status"] not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status: {payload['status']!r}")
    if "title" in payload and not (payload.get("title") or "").strip():
        raise HTTPException(status_code=400, detail="title must not be empty")

    backlog_dir = _backlog_dir(request, slug)
    path = _find_task_file(backlog_dir, task_id)

    updates = {k: v for k, v in payload.items() if k in ("title", "status")}
    body = payload.get("body")
    try:
        merge_task_update(path, updates, body=body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return parse_task(path)


@router.delete("/{task_id}")
def delete_task(
    slug: str,
    task_id: str,
    request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_task_id(task_id)

    backlog_dir = _backlog_dir(request, slug)
    path = _find_task_file(backlog_dir, task_id)
    path.unlink()
    return {"ok": True, "deleted_id": task_id}


@router.post("/{task_id}/comments")
def add_comment(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_task_id(task_id)

    comment_body = (payload.get("body") or "").strip()
    if not comment_body:
        raise HTTPException(status_code=400, detail="comment body must not be empty")

    backlog_dir = _backlog_dir(request, slug)
    path = _find_task_file(backlog_dir, task_id)

    author = user.get("username", "unknown")
    try:
        append_comment(path, comment_body, author)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return parse_task(path)
