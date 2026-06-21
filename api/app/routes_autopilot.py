"""Autopilot endpoints (T-0153) — proxy to the worker autopilot actions.

GET  /api/projects/{slug}/autopilot          → all autopilot states
POST /api/projects/{slug}/autopilot/start     → start a team/session/project run
POST /api/projects/{slug}/autopilot/stop      → end a run early (operator / TL early-exit)

The UI surfaces *Autopilot* in the kebab popover on a team lane, a single
session row, and the project header. Each opens a dialog (duration hours +
prompt + early-exit condition + optional stall threshold) that POSTs ``start``.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.project_authz import require_project_member
from app.routes_auth import require_auth
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/autopilot",
    tags=["autopilot"],
    dependencies=[Depends(require_auth)],
)


def _worker(request: Request) -> WorkerClient:
    return request.app.state.worker_router.coordinator()


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/autopilot
# ---------------------------------------------------------------------------

@router.get("")
async def get_autopilot_status(slug: str, request: Request) -> dict:
    """Return every autopilot state (active + recently ended) for a project."""
    _check_project(request, slug)
    client = _worker(request)
    try:
        return await client.call_action("autopilot_status", {"slug": slug})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/autopilot/start
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    kind: str                              # "team" | "session" | "project"
    ref: Optional[str] = None              # team name / sid (defaults to slug for project)
    prompt: str
    early_exit: Optional[str] = None
    duration_hours: Optional[float] = None
    stall_minutes: Optional[int] = None
    watchdog_minutes: Optional[int] = None


@router.post("/start")
async def start_autopilot(slug: str, body: StartRequest, request: Request,
                          user: dict = Depends(require_project_member)) -> dict:  # T-0381
    """Start an autopilot run for a team / session / project target."""
    _check_project(request, slug)
    if body.kind not in ("team", "session", "project"):
        raise HTTPException(status_code=400, detail="kind must be team|session|project")
    if not (body.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    params: dict = {"slug": slug, "kind": body.kind, "prompt": body.prompt}
    if body.ref:
        params["ref"] = body.ref
    if body.early_exit is not None:
        params["early_exit"] = body.early_exit
    if body.duration_hours is not None:
        params["duration_hours"] = body.duration_hours
    if body.stall_minutes is not None:
        params["stall_minutes"] = body.stall_minutes
    if body.watchdog_minutes is not None:
        params["watchdog_minutes"] = body.watchdog_minutes
    username = (user or {}).get("username") or ""
    if username:
        params["created_by"] = username

    client = _worker(request)
    try:
        return await client.call_action("autopilot_start", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/autopilot/stop
# ---------------------------------------------------------------------------

class StopRequest(BaseModel):
    key: Optional[str] = None
    target_sid: Optional[str] = None
    reason: Optional[str] = None


@router.post("/stop")
async def stop_autopilot(slug: str, body: StopRequest, request: Request,
                         user: dict = Depends(require_project_member)) -> dict:  # T-0381
    """End an autopilot run early (operator cancel, or TL early-exit with reason)."""
    _check_project(request, slug)
    if not (body.key or body.target_sid):
        raise HTTPException(status_code=400, detail="one of key, target_sid required")

    params: dict = {"slug": slug}
    if body.key:
        params["key"] = body.key
    if body.target_sid:
        params["target_sid"] = body.target_sid
    if body.reason is not None:
        params["reason"] = body.reason
    username = (user or {}).get("username") or ""
    if username:
        params["stopped_by"] = username

    client = _worker(request)
    try:
        return await client.call_action("autopilot_stop", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
