"""Backlog read endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.markdown_parser import ParseError, parse_task
from app.routes_auth import require_auth

log = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{slug}/backlog", tags=["backlog"], dependencies=[Depends(require_auth)])


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
