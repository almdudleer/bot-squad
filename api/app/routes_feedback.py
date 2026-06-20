"""Feedback read + write endpoints."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import artifact_nesting as AN
from app.markdown_writer import slugify, write_task
from app.routes_auth import require_auth

router = APIRouter(
    prefix="/projects/{slug}/feedback",
    tags=["feedback"],
    dependencies=[Depends(require_auth)],
)

_MAX_CONTENT_BYTES = 200 * 1024  # 200 KB

# F-<alphanumeric and - and .>.md  e.g. F-2026-04-15-id520-user-1.md
_FEEDBACK_NAME_RE = re.compile(r"^F-[A-Za-z0-9_.-]+\.md$")

_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)


def _fb_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "feedback"


def _backlog_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    return cfg.project_data_dir(slug) / "backlog"


def _project_root(request: Request, slug: str) -> Path:
    """Project data dir — the root the cross-store artifact_nesting walks."""
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug)


def _validate_feedback_name(name: str) -> None:
    if not _FEEDBACK_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid feedback file name: {name!r}")


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
def list_feedback(slug: str, request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    fb_dir = cfg.project_data_dir(slug) / "feedback"
    if not fb_dir.exists():
        return []
    out = []
    for f in sorted(fb_dir.glob("*.md")):
        # T-0283: feedback is a nestable artifact. Surface its artifact `id`
        # (filename stem) + `parent_doc_id`; `content` is the BODY (frontmatter
        # stripped) so the editor never round-trips the nesting block. Legacy
        # files have no frontmatter, so body == the whole file (unchanged).
        meta, body = AN.split_frontmatter(f.read_text(encoding="utf-8"))
        ref = AN.ref_for_path(AN.KIND_FEEDBACK, f)
        out.append({
            "name": f.name,
            "id": ref.id,
            "parent_doc_id": ref.parent_doc_id,
            "content": body,
        })
    return out


@router.put("/{name}")
def put_feedback(
    slug: str,
    name: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_feedback_name(name)
    content = payload.get("content") or ""
    _validate_content(content)

    fb_dir = _fb_dir(request, slug)
    path = fb_dir / name

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"feedback file not found: {name}")

    # T-0283: preserve the nesting frontmatter (parent_doc_id) across a body
    # edit — a content replace must not silently drop it (same re-graft rule as
    # the task verbatim guard). Legacy files (no frontmatter) write verbatim.
    meta, _ = AN.split_frontmatter(path.read_text(encoding="utf-8"))
    new_text = AN.with_frontmatter(meta, content)

    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.rename(tmp, path)

    return {"ok": True}


@router.post("/{name}/promote")
def promote_feedback(
    slug: str,
    name: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    _validate_feedback_name(name)

    fb_dir = _fb_dir(request, slug)
    fb_path = fb_dir / name

    if not fb_path.exists():
        raise HTTPException(status_code=404, detail=f"feedback file not found: {name}")

    fb_content = fb_path.read_text()

    # Derive default title from H1 or filename
    m = _H1_RE.search(fb_content)
    default_title = m.group(1).strip() if m else fb_path.stem

    title = (payload.get("title") or "").strip() or default_title
    custom_body = payload.get("body")

    # Build task body
    link = f"[{name}](../feedback/{name})"
    if custom_body is not None:
        task_body = f"**From feedback** {link}:\n\n{custom_body}"
    else:
        task_body = f"**From feedback** {link}:\n\n{fb_content}"

    backlog_dir = _backlog_dir(request, slug)
    backlog_dir.mkdir(parents=True, exist_ok=True)

    # T-0174: allocate via the shared idalloc counter (same as routes_backlog
    # and the worker's task_new) so promotes can't collide with concurrent
    # creates on the T-id.
    from app import idalloc
    cfg = request.app.state.api_config
    task_id = idalloc.allocate_id(cfg.data_dir, slug, "task")
    slug_part = slugify(title)
    filename = f"{task_id}-{slug_part}.md" if slug_part else f"{task_id}.md"
    task_path = backlog_dir / filename

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fm = {
        "id": task_id,
        "title": title,
        "status": "open",
        "created": now,
        "updated": now,
        "from": name,
    }
    write_task(task_path, fm, task_body)

    # Append footer to feedback file
    footer = (
        f"\n\n---\n"
        f"Promoted to backlog task [{task_id}](../backlog/{filename}) on {today}.\n"
    )
    with open(fb_path, "a", encoding="utf-8") as f:
        f.write(footer)

    return {"ok": True, "task_id": task_id}


@router.get("/{name}/children")
def get_feedback_children(slug: str, name: str, request: Request) -> list[dict]:
    """Cross-store children of a feedback theme (T-0283): every artifact whose
    ``parent_doc_id`` points at this feedback item (id = filename stem)."""
    _validate_feedback_name(name)
    fb_path = _fb_dir(request, slug) / name
    if not fb_path.exists():
        raise HTTPException(status_code=404, detail=f"feedback file not found: {name}")
    art_id = name[:-3] if name.endswith(".md") else name
    return AN.children_of(_project_root(request, slug), art_id)


class SetParent(BaseModel):
    parent_doc_id: str | None = None


@router.put("/{name}/parent")
def set_feedback_parent(slug: str, name: str, request: Request, body: SetParent,
                        user: dict = Depends(require_auth)) -> dict:
    """Re-parent (adopt) or clear the parent (disown) of a feedback item across
    the artifact stores (T-0283). Cycle-safe; injects a frontmatter block into a
    legacy raw-markdown feedback file on first nesting, preserving the body."""
    _validate_feedback_name(name)
    root = _project_root(request, slug)
    art_id = name[:-3] if name.endswith(".md") else name
    ref = AN.find_artifact(root, art_id)
    if ref is None or ref.kind != AN.KIND_FEEDBACK:
        raise HTTPException(status_code=404, detail=f"feedback file not found: {name}")

    new_parent = (body.parent_doc_id or "").strip() or None
    if new_parent is not None:
        if new_parent == art_id:
            raise HTTPException(status_code=400, detail="feedback cannot be its own parent")
        if AN.find_artifact(root, new_parent) is None:
            raise HTTPException(status_code=404, detail=f"parent artifact not found: {new_parent}")
        if AN.would_cycle(root, art_id, new_parent):
            raise HTTPException(
                status_code=400,
                detail=f"refusing to set parent {new_parent}: would create a cycle",
            )

    AN.set_parent(ref, new_parent)
    return {"ok": True, "id": art_id, "parent_doc_id": new_parent}
