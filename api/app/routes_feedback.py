"""Feedback read + write endpoints."""
from __future__ import annotations

import fcntl
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.markdown_writer import allocate_next_id, slugify, write_task
from app.routes_auth import require_auth

router = APIRouter(
    prefix="/projects/{slug}/feedback",
    tags=["feedback"],
    dependencies=[Depends(require_auth)],
)

_MAX_CONTENT_BYTES = 200 * 1024  # 200 KB

# F-<alphanumeric and - and .>.md  e.g. F-2026-04-15-id520-user-1.md
_FEEDBACK_NAME_RE = re.compile(r"^F-[A-Za-z0-9_.-]+\.md$")

_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)


def _fb_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "feedback"


def _backlog_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    return cfg.project_data_dir(slug) / "backlog"


def _validate_feedback_name(name: str) -> None:
    if not _FEEDBACK_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid feedback file name: {name!r}")


def _validate_content(content: str) -> None:
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail="content must be valid UTF-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")


@router.get("")
def list_feedback(slug: str, request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    fb_dir = cfg.project_data_dir(slug) / "feedback"
    if not fb_dir.exists():
        return []
    return [
        {"name": f.name, "content": f.read_text()}
        for f in sorted(fb_dir.glob("*.md"))
    ]


@router.put("/{name}")
def put_feedback(
    slug: str,
    name: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_feedback_name(name)
    content = payload.get("content") or ""
    _validate_content(content)

    fb_dir = _fb_dir(request, slug)
    path = fb_dir / name

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"feedback file not found: {name}")

    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)

    return {"ok": True}


@router.post("/{name}/promote")
def promote_feedback(
    slug: str,
    name: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_feedback_name(name)

    fb_dir = _fb_dir(request, slug)
    fb_path = fb_dir / name

    if not fb_path.exists():
        raise HTTPException(status_code=404, detail=f"feedback file not found: {name}")

    fb_content = fb_path.read_text()

    # Derive default title from H1 or filename
    m = _H1_RE.search(fb_content)
    default_title = m.group(1).strip() if m else fb_path.stem

    title = (payload.get("title") or "").strip() or default_title
    custom_body = payload.get("body")

    # Build task body
    link = f"[{name}](../feedback/{name})"
    if custom_body is not None:
        task_body = f"**From feedback** {link}:\n\n{custom_body}"
    else:
        task_body = f"**From feedback** {link}:\n\n{fb_content}"

    backlog_dir = _backlog_dir(request, slug)
    backlog_dir.mkdir(parents=True, exist_ok=True)

    lock_path = backlog_dir / ".lock"
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        task_id = allocate_next_id(backlog_dir)
        slug_part = slugify(title)
        filename = f"{task_id}-{slug_part}.md" if slug_part else f"{task_id}.md"
        task_path = backlog_dir / filename

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fm = {
            "id": task_id,
            "title": title,
            "status": "open",
            "created": now,
            "updated": now,
            "from": name,
        }
        write_task(task_path, fm, task_body)

    # Append footer to feedback file
    footer = (
        f"\n\n---\n"
        f"Promoted to backlog task [{task_id}](../backlog/{filename}) on {today}.\n"
    )
    with open(fb_path, "a", encoding="utf-8") as f:
        f.write(footer)

    return {"ok": True, "task_id": task_id}
