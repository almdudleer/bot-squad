"""Vision read + write endpoints."""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.markdown_writer import slugify
from app.routes_auth import require_auth

router = APIRouter(
    prefix="/projects/{slug}/vision",
    tags=["vision"],
    dependencies=[Depends(require_auth)],
)

_MAX_CONTENT_BYTES = 200 * 1024  # 200 KB

# Valid names: either top-level <name>.md or initiatives/<name>.md
_VISION_NAME_RE = re.compile(
    r"^(?:[A-Za-z0-9_.-]+\.md|initiatives/[A-Za-z0-9_.-]+\.md)$"
)


def _vision_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "vision"


def _validate_vision_name(name: str) -> None:
    if not _VISION_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid vision file name: {name!r}")


def _validate_content(content: str) -> None:
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail="content must be valid UTF-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")


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


@router.put("/{name:path}")
def put_vision(
    slug: str,
    name: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_vision_name(name)
    content = payload.get("content") or ""
    _validate_content(content)

    vision_dir = _vision_dir(request, slug)
    path = vision_dir / name

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"vision file not found: {name}")

    # Atomic write
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)

    return {"ok": True}


@router.post("")
def post_vision(
    slug: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    kind = payload.get("kind")
    if kind != "initiative":
        raise HTTPException(status_code=400, detail="only kind=initiative is supported")

    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be empty")

    content = payload.get("content")
    if content is None:
        content = f"# {name}\n"

    _validate_content(content)

    vision_dir = _vision_dir(request, slug)
    initiatives_dir = vision_dir / "initiatives"
    initiatives_dir.mkdir(parents=True, exist_ok=True)

    filename = slugify(name) + ".md"
    path = initiatives_dir / filename

    if path.exists():
        raise HTTPException(status_code=409, detail=f"initiative already exists: {filename}")

    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)

    return {"ok": True, "name": f"initiatives/{filename}"}
