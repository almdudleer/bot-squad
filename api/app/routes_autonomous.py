"""Autonomous orchestrator endpoints — proxy to worker autonomous actions.

GET  /api/projects/{slug}/autonomous         → current state
POST /api/projects/{slug}/autonomous/enable  → enable orchestrator
POST /api/projects/{slug}/autonomous/disable → disable orchestrator
GET  /api/projects/{slug}/autonomous/log     → last 50 tick decisions
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.routes_auth import require_auth
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/autonomous",
    tags=["autonomous"],
    dependencies=[Depends(require_auth)],
)


def _worker(request: Request) -> WorkerClient:
    return WorkerClient(request.app.state.sock_path)


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/autonomous
# ---------------------------------------------------------------------------

@router.get("")
async def get_autonomous_status(slug: str, request: Request) -> dict:
    """Return the current autonomous orchestrator state for a project."""
    _check_project(request, slug)
    client = _worker(request)
    try:
        return await client.call_action("autonomous_status", {"slug": slug})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/autonomous/enable
# ---------------------------------------------------------------------------

class EnableRequest(BaseModel):
    sleep_start_hour: Optional[int] = None
    sleep_end_hour: Optional[int] = None


@router.post("/enable")
async def enable_autonomous(slug: str, body: EnableRequest, request: Request) -> dict:
    """Enable the autonomous orchestrator for a project.

    Optionally accepts sleep_start_hour and sleep_end_hour (UTC integers).
    """
    _check_project(request, slug)
    params: dict = {"slug": slug}
    if body.sleep_start_hour is not None:
        params["sleep_start_hour"] = body.sleep_start_hour
    if body.sleep_end_hour is not None:
        params["sleep_end_hour"] = body.sleep_end_hour

    client = _worker(request)
    try:
        return await client.call_action("autonomous_enable", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/autonomous/disable
# ---------------------------------------------------------------------------

@router.post("/disable")
async def disable_autonomous(slug: str, request: Request) -> dict:
    """Disable the autonomous orchestrator for a project.

    In-flight tasks complete normally; no new tasks will be started.
    """
    _check_project(request, slug)
    client = _worker(request)
    try:
        return await client.call_action("autonomous_disable", {"slug": slug})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/autonomous/log
# ---------------------------------------------------------------------------

@router.get("/log")
async def get_autonomous_log(slug: str, request: Request) -> list:
    """Return the last 50 tick log entries for the autonomous orchestrator."""
    _check_project(request, slug)
    client = _worker(request)
    try:
        result = await client.call_action("autonomous_status", {"slug": slug})
        # Return the full tick_log (status returns last 5; we need all 50)
        # Worker returns last 5 in status; log endpoint returns all available
        return result.get("tick_log", [])
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
