"""Session management endpoints — proxy to worker actions."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.routes_auth import require_auth
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_auth)],
)


def _worker(request: Request) -> WorkerClient:
    return WorkerClient(request.app.state.sock_path)


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/sessions
# ---------------------------------------------------------------------------

@router.get("")
async def list_sessions(slug: str, request: Request) -> list[dict]:
    """List all Claude sessions (active + paused) for a project."""
    _check_project(request, slug)
    client = _worker(request)
    try:
        result = await client.call_action("list_sessions", {"slug": slug})
        return result.get("sessions", [])
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/pause
# ---------------------------------------------------------------------------

@router.post("/{sid}/pause")
async def pause_session(slug: str, sid: str, request: Request) -> dict:
    """Pause a running Claude session."""
    _check_project(request, slug)
    client = _worker(request)
    try:
        return await client.call_action("pause_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/resume
# ---------------------------------------------------------------------------

@router.post("/{sid}/resume")
async def resume_session(slug: str, sid: str, request: Request) -> dict:
    """Resume a paused Claude session."""
    _check_project(request, slug)
    client = _worker(request)
    try:
        return await client.call_action("resume_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions  (spawn new)
# ---------------------------------------------------------------------------

class SpawnRequest(BaseModel):
    window: str
    initial_prompt: Optional[str] = None


@router.post("")
async def spawn_session(slug: str, body: SpawnRequest, request: Request) -> dict:
    """Spawn a new Claude session in the project's repo."""
    _check_project(request, slug)
    if not body.window.strip():
        raise HTTPException(status_code=400, detail="window name must not be empty")

    client = _worker(request)
    params: dict = {"slug": slug, "window": body.window}
    if body.initial_prompt:
        params["initial_prompt"] = body.initial_prompt

    try:
        return await client.call_action("spawn_session", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
