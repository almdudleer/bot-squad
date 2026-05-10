"""Vision read endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.routes_auth import require_auth

router = APIRouter(prefix="/projects/{slug}/vision", tags=["vision"], dependencies=[Depends(require_auth)])


@router.get("")
def list_vision(slug: str, request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    vision_dir = cfg.project_data_dir(slug) / "vision"
    if not vision_dir.exists():
        return []
    out: list[dict] = []
    for f in sorted(vision_dir.glob("*.md")):
        out.append({"name": f.name, "content": f.read_text()})
    initiatives_dir = vision_dir / "initiatives"
    if initiatives_dir.exists():
        for f in sorted(initiatives_dir.glob("*.md")):
            out.append({"name": f"initiatives/{f.name}", "content": f.read_text()})
    return out
