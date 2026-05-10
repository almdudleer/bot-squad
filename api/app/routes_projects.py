"""Project list/detail routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.routes_auth import require_auth

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_auth)])


@router.get("")
def list_projects(request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    return [
        {"slug": p.slug, "display_name": p.display_name}
        for p in cfg.projects.values()
    ]


@router.get("/{slug}")
def get_project(slug: str, request: Request) -> dict:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    data_dir = cfg.project_data_dir(slug)
    counts = {
        "backlog": _count(data_dir / "backlog"),
        "vision": _count(data_dir / "vision"),
        "feedback": _count(data_dir / "feedback"),
        "sessions": _count(data_dir / "sessions"),
    }
    return {
        "slug": proj.slug,
        "display_name": proj.display_name,
        "deploy_branch": proj.deploy_branch,
        "prod_url": proj.prod_url,
        "staging_url": proj.staging_url,
        "dev_url": proj.dev_url,
        "counts": counts,
    }


def _count(path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.glob("*.md"))
