"""Project list/detail routes."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request

from app.routes_auth import require_auth

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_auth)])

_MAX_CONTENT_BYTES = 200 * 1024  # 200 KB


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


@router.get("/{slug}/repo-agents-md")
def get_repo_agents_md(slug: str, request: Request) -> dict:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    path = proj.repo_path / "AGENTS.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="AGENTS.md not found in repo")
    return {"content": path.read_text(encoding="utf-8")}


@router.put("/{slug}/repo-agents-md")
def put_repo_agents_md(slug: str, request: Request, payload: dict) -> dict:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    content = payload.get("content") or ""
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail="content must be valid UTF-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")
    path = proj.repo_path / "AGENTS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)
    return {"ok": True}
