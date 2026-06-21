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
from app.project_authz import require_project_member
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


def _resolve_feedback(request: Request, slug: str, fid: str) -> tuple[str, Path]:
    """Map a feedback artifact id to ``(stem_id, path)``.

    T-0283: the nesting endpoints take the ARTIFACT ID — the filename stem, the
    same value ``list_feedback`` returns as ``id`` and that cross-store
    ``parent_doc_id`` refs use. A trailing ``.md`` is tolerated so the legacy
    ``name`` form works too. No canonical ``F-NNNN`` shortening — the stem IS
    the id."""
    stem = fid[:-3] if fid.endswith(".md") else fid
    if "/" in stem or "\\" in stem or not _FEEDBACK_NAME_RE.match(f"{stem}.md"):
        raise HTTPException(status_code=400, detail=f"invalid feedback id: {fid!r}")
    path = _fb_dir(request, slug) / f"{stem}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"feedback not found: {fid}")
    return stem, path


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
    # Audit item 8 (Fork-4): PURE read. `bsq feedback`/voice intake write F-*.md
    # directly now, so list_feedback no longer materializes inbox.log on read
    # (the cut side-effecting GET). A stray inbox.log is ignored (not an F-*.md).
    #
    # Audit item 12 (Fork-4 close): default-hide CLOSED feedback (promoted /
    # dismissed) so the intake stops leaking forever; ?include_closed=true shows
    # all. Each row surfaces `status` (absent frontmatter → "open"/visible).
    include_closed = (request.query_params.get("include_closed") or "").lower() in (
        "1", "true", "yes",
    )
    out = []
    for f in sorted(fb_dir.glob("*.md")):
        # T-0283: feedback is a nestable artifact. Surface its artifact `id`
        # (filename stem) + `parent_doc_id`; `content` is the BODY (frontmatter
        # stripped) so the editor never round-trips the nesting block. Legacy
        # files have no frontmatter, so body == the whole file (unchanged).
        meta, body = AN.split_frontmatter(f.read_text(encoding="utf-8"))
        status = str(meta.get("status") or "open")
        if not include_closed and status in ("promoted", "dismissed"):
            continue
        ref = AN.ref_for_path(AN.KIND_FEEDBACK, f)
        out.append({
            "name": f.name,
            "id": ref.id,
            "parent_doc_id": ref.parent_doc_id,
            "content": body,
            "status": status,
        })
    return out


@router.put("/{name}")
def put_feedback(
    slug: str,
    name: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
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
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
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

    # Item 12 (Fork-4 close): mark the feedback CLOSED (status: promoted) and
    # record the task link footer in ONE atomic write. Cross-container uid
    # (flaw-watch): `bsq` writes F-*.md at the host uid, this runs at the
    # container uid — an in-place append can EACCES, but a tmp-write + os.replace
    # only needs dir-write, so it works regardless of who owns the file.
    footer = (
        f"\n\n---\n"
        f"Promoted to backlog task [{task_id}](../backlog/{filename}) on {today}.\n"
    )
    _set_status_and_append(fb_path, "promoted", footer)

    return {"ok": True, "task_id": task_id}


def _set_status_and_append(fb_path: Path, status: str, footer: str) -> None:
    """Fold a ``status`` frontmatter set + a body footer into a single atomic
    tmp-write+os.replace (see the cross-container-uid note above). A legacy
    feedback file (no frontmatter) gains a frontmatter block, same as the
    put/parent paths (T-0283)."""
    meta, body = AN.split_frontmatter(fb_path.read_text(encoding="utf-8"))
    meta["status"] = status
    new_text = AN.with_frontmatter(meta, body + footer)
    tmp = fb_path.parent / (fb_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, fb_path)


@router.post("/{name}/dismiss")
def dismiss_feedback(
    slug: str,
    name: str,
    request: Request,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Item 12 (Fork-4 close): dismiss a feedback item without promoting it —
    the DOMINANT operator action per the product-iteration loop (most friction
    notes are cut, not built). Sets ``status: dismissed`` so ``list_feedback``
    default-hides it; the close is explicit and the intake stops leaking."""
    _validate_feedback_name(name)
    fb_path = _fb_dir(request, slug) / name
    if not fb_path.exists():
        raise HTTPException(status_code=404, detail=f"feedback file not found: {name}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _set_status_and_append(fb_path, "dismissed", f"\n\n---\nDismissed on {today}.\n")
    return {"ok": True}


@router.get("/{fid}/children")
def get_feedback_children(slug: str, fid: str, request: Request) -> list[dict]:
    """Cross-store children of a feedback theme (T-0283): every artifact whose
    ``parent_doc_id`` points at this feedback item. ``fid`` = the artifact id
    (filename stem) that ``list_feedback`` returns; a trailing ``.md`` is OK."""
    art_id, _ = _resolve_feedback(request, slug, fid)
    return AN.children_of(_project_root(request, slug), art_id)


class SetParent(BaseModel):
    parent_doc_id: str | None = None


@router.put("/{fid}/parent")
def set_feedback_parent(slug: str, fid: str, request: Request, body: SetParent,
                        user: dict = Depends(require_project_member)) -> dict:  # T-0381: project-write gate
    """Re-parent (adopt) or clear the parent (disown) of a feedback item across
    the artifact stores (T-0283). Cycle-safe; injects a frontmatter block into a
    legacy raw-markdown feedback file on first nesting, preserving the body."""
    root = _project_root(request, slug)
    art_id, _ = _resolve_feedback(request, slug, fid)
    ref = AN.find_artifact(root, art_id)
    if ref is None or ref.kind != AN.KIND_FEEDBACK:
        raise HTTPException(status_code=404, detail=f"feedback not found: {fid}")

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
