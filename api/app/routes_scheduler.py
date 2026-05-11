"""Scheduler dashboard endpoint — project-agnostic.

GET /api/scheduler
    Proxy to the worker's scheduler_state action and return the result.
    Returns {jobs, worker_started_at, last_heartbeat_age_seconds}.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.routes_auth import require_auth
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/scheduler",
    tags=["scheduler"],
    dependencies=[Depends(require_auth)],
)


@router.get("")
async def get_scheduler_state(request: Request) -> dict:
    """Return current APScheduler state from the worker."""
    client = WorkerClient(request.app.state.sock_path)
    try:
        return await client.call_action("scheduler_state", {})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
