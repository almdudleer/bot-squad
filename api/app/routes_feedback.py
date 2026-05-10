"""Feedback read endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.routes_auth import require_auth

router = APIRouter(prefix="/projects/{slug}/feedback", tags=["feedback"], dependencies=[Depends(require_auth)])


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
