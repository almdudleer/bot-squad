"""T-0630 (T-0620 seam, D-0056): operator pause/resume + fleet-default model.

Thin RPC wrappers over EXISTING worker actions — the worker owns all state
(the operator_redrive pause flag, T-0474; the linux user's own
``~/.claude/settings.json``, T-0630's ``fleet_model`` module). This module
adds no new state of its own, mirroring ``routes_autopilot.py``.

  POST /api/projects/{slug}/operator/pause   {reason?}
  POST /api/projects/{slug}/operator/resume
  GET  /api/projects/{slug}/worker/model
  PUT  /api/projects/{slug}/worker/model     {model}

Pause/resume are project-scoped writes (require_project_member, T-0381
convention). The fleet model is a single worker-wide setting, not a
per-project one — GET is a plain authenticated read; PUT is deliberately
admin-only (D-0056) since it changes the driving engine for the whole fleet,
not just one project.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.project_authz import require_project_member
from app.routes_auth import require_admin, require_auth
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}",
    tags=["operator"],
    dependencies=[Depends(require_auth)],
)

# Mirrors worker.bot_squad_worker.fleet_model.ALLOWED_MODELS — the API cannot
# import the worker package (separate deployable, see routes_transparency's
# module docstring), so this is a literal mirror purely so a bad value gets a
# real 400 here instead of surfacing as an opaque 502 from the worker socket.
# The worker action is still the enforcing SSOT (re-validated worker-side).
_ALLOWED_MODELS = frozenset({
    "", "claude-sonnet-5", "claude-opus-4-8", "opus[1m]", "claude-fable-5",
})


def _worker(request: Request) -> WorkerClient:
    return request.app.state.worker_router.coordinator()


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/operator/pause
# POST /api/projects/{slug}/operator/resume
# ---------------------------------------------------------------------------

class PauseRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/operator/pause")
async def pause_operator(slug: str, body: PauseRequest, request: Request,
                          user: dict = Depends(require_project_member)) -> dict:
    """Pause the operator re-drive tick for a project (T-0474 flag, idempotent)."""
    _check_project(request, slug)
    params: dict = {"slug": slug, "requested_by": (user or {}).get("username") or "api"}
    if body.reason is not None:
        params["reason"] = body.reason
    try:
        return await _worker(request).call_action("operator_pause", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/operator/resume")
async def resume_operator(slug: str, request: Request,
                           user: dict = Depends(require_project_member)) -> dict:
    """Resume a paused operator re-drive. No-op if not paused."""
    _check_project(request, slug)
    params = {"slug": slug, "requested_by": (user or {}).get("username") or "api"}
    try:
        return await _worker(request).call_action("operator_resume", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# GET/PUT /api/projects/{slug}/worker/model
# ---------------------------------------------------------------------------

class ModelRequest(BaseModel):
    model: str


@router.get("/worker/model")
async def get_worker_model(slug: str, request: Request) -> dict:
    """Current fleet-default ``claude --model`` value ("" = built-in default)."""
    _check_project(request, slug)
    try:
        result = await _worker(request).call_action("fleet_model_get", {})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"model": result.get("model", "")}


@router.put("/worker/model")
async def set_worker_model(slug: str, body: ModelRequest, request: Request,
                            admin: dict = Depends(require_admin)) -> dict:
    """Change the fleet-default model. Admin-only — this is fleet-wide, not
    per-project (D-0056: "Per-role defaults ... are NOT surfaced this wave —
    fleet default + pause were the stakeholder's outage pain")."""
    _check_project(request, slug)
    if body.model not in _ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"model not allowed: {body.model!r}")
    try:
        result = await _worker(request).call_action(
            "fleet_model_set", {"model": body.model},
        )
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"model": result.get("model", body.model)}
