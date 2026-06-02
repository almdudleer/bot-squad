"""Project docs system (T-0172) — categorized, agent-readable project docs.

Storage: ``data/<slug>/docs/<category>/D-NNNN-<slug>.md`` with YAML frontmatter
(id, title, category, status, created, related_tickets) plus a markdown body.
The id (``D-NNNN``) is allocated atomically by the shared idalloc counter
(T-0174) — the SAME ``data/<slug>/_counters/doc.txt`` the worker's ``doc_new``
action uses, so a web create and an agent ``bsq doc new`` can't collide.

Categories are a directory convention, not an enum: the initial set is
product / architecture / design / support / runbook, but any safe lowercase
token (``[a-z0-9_-]``) is accepted so the set stays extensible. The known set
is surfaced via ``GET .../docs/categories`` (defaults ∪ whatever exists on disk)
so the UI can offer a dropdown without locking the taxonomy.

Bidirectional mentions (T-0172): a doc's ``related_tickets`` and a ticket's
``related_docs`` are kept in sync by the link/unlink endpoints — touching one
side updates the other. Both writes are plain filesystem edits in the API layer
(like routes_backlog / routes_usecases), not worker actions.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.routes_auth import require_auth

router = APIRouter(
    prefix="/projects/{slug}/docs",
    tags=["docs"],
    dependencies=[Depends(require_auth)],
)

_MAX_CONTENT_BYTES = 200 * 1024
_DOC_ID_RE = re.compile(r"^D-\d{4}$")
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TASK_ID_RE = re.compile(r"^T-\d{4}$")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)

# Initial category set (T-0172 DoD). Extensible: any on-disk category dir is
# also surfaced, and create accepts any safe token.
DEFAULT_CATEGORIES = ["product", "architecture", "design", "support", "runbook"]


def _slugify(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s).lower().strip()
    s = re.sub(r"[\s-]+", "-", s).strip("-")
    return s[:max_len]


def _docs_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "docs"


def _backlog_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    return cfg.project_data_dir(slug) / "backlog"


def _validate_doc_id(doc_id: str) -> None:
    if not _DOC_ID_RE.match(doc_id):
        raise HTTPException(status_code=400, detail=f"invalid doc id: {doc_id!r}")


def _parse(path: Path) -> dict:
    """Return ``{id, meta..., body, raw, category, path}`` for a doc md."""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    meta: dict = {}
    body = text
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        body = m.group(2).lstrip("\n")
    # The filename stem is `D-NNNN-<slug>`; the id is the `D-NNNN` prefix.
    stem = path.stem
    file_id = stem.split("-", 2)[:2]
    derived_id = "-".join(file_id) if len(file_id) == 2 else stem
    return {
        **meta,
        "id": meta.get("id") or derived_id,
        # category from the parent dir is authoritative (matches storage layout)
        "category": path.parent.name,
        "related_tickets": meta.get("related_tickets") or [],
        "body": body,
        "raw": text,
        "path": str(path),
    }


def _find_doc(docs_root: Path, doc_id: str) -> Path | None:
    """Locate ``D-NNNN-*.md`` under any category dir, or None."""
    if not docs_root.exists():
        return None
    for f in docs_root.glob(f"*/{doc_id}-*.md"):
        return f
    # tolerate a bare `D-NNNN.md` (no slug suffix)
    for f in docs_root.glob(f"*/{doc_id}.md"):
        return f
    return None


def _write_frontmatter(path: Path, meta: dict, body: str) -> None:
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    content = f"---\n{fm}---\n\n{body.lstrip(chr(10))}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


@router.get("")
def list_docs(slug: str, request: Request, category: str | None = None) -> list[dict]:
    root = _docs_dir(request, slug)
    if not root.exists():
        return []
    out = []
    cats = [category] if category else sorted(
        p.name for p in root.iterdir() if p.is_dir()
    )
    for cat in cats:
        cdir = root / cat
        if not cdir.is_dir():
            continue
        for f in sorted(cdir.glob("*.md")):
            d = _parse(f)
            out.append({
                "id": d["id"],
                "title": d.get("title", d["id"]),
                "category": cat,
                "status": d.get("status", ""),
                "related_tickets": d.get("related_tickets", []),
            })
    return out


@router.get("/categories")
def list_categories(slug: str, request: Request) -> list[str]:
    """Default category set ∪ whatever category dirs exist on disk."""
    root = _docs_dir(request, slug)
    found = set(DEFAULT_CATEGORIES)
    if root.exists():
        found |= {p.name for p in root.iterdir() if p.is_dir()}
    # defaults first (canonical order), then any extras alphabetically
    extras = sorted(found - set(DEFAULT_CATEGORIES))
    return DEFAULT_CATEGORIES + extras


@router.get("/{doc_id}")
def get_doc(slug: str, doc_id: str, request: Request) -> dict:
    _validate_doc_id(doc_id)
    path = _find_doc(_docs_dir(request, slug), doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")
    return _parse(path)


class NewDoc(BaseModel):
    category: str
    title: str


@router.post("")
def create_doc(slug: str, request: Request, body: NewDoc,
               user: dict = Depends(require_auth)) -> dict:
    """Allocate a D-NNNN id atomically and write a stub doc.

    Mirrors the worker ``doc_new`` action's storage + frontmatter so a web
    create and an agent ``bsq doc new`` are byte-compatible.
    """
    from app import idalloc

    category = (body.category or "").strip()
    title = (body.title or "").strip()
    if not _CATEGORY_RE.match(category):
        raise HTTPException(
            status_code=400,
            detail=f"invalid category {category!r} (expect lowercase [a-z0-9_-])",
        )
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")

    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")

    cdir = _docs_dir(request, slug) / category
    cdir.mkdir(parents=True, exist_ok=True)
    doc_id = idalloc.allocate_id(cfg.data_dir, slug, "doc")
    path = cdir / f"{doc_id}-{_slugify(title)}.md"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = "\n".join([
        f"id: {doc_id}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"category: {category}",
        "status: draft",
        f"created: {now}",
        "related_tickets: []",
    ])
    content = f"---\n{fm}\n---\n\n# {title}\n\n(new {category} doc — T-0172 docs system)\n"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": doc_id, "category": category}


class PutDoc(BaseModel):
    content: str


@router.put("/{doc_id}")
def put_doc(slug: str, doc_id: str, request: Request, body: PutDoc,
            user: dict = Depends(require_auth)) -> dict:
    _validate_doc_id(doc_id)
    content = body.content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="content must not be empty")
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")
    path = _find_doc(_docs_dir(request, slug), doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": doc_id}


# --------------------------------------------------------------------------
# Bidirectional mentions (T-0172): keep doc.related_tickets and
# ticket.related_docs in sync. Both sides are plain frontmatter edits.
# --------------------------------------------------------------------------
class LinkBody(BaseModel):
    ticket: str


def _find_task_file(backlog_dir: Path, task_id: str) -> Path | None:
    if not backlog_dir.exists():
        return None
    matches = list(backlog_dir.glob(f"{task_id}-*.md"))
    return matches[0] if matches else None


def _mutate_list_field(path: Path, field: str, value: str, *, add: bool) -> None:
    """Add/remove ``value`` from a frontmatter list field, idempotently."""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise HTTPException(status_code=400, detail=f"no frontmatter in {path.name}")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = m.group(2)
    cur = meta.get(field) or []
    if not isinstance(cur, list):
        cur = [cur]
    cur = [str(x) for x in cur]
    if add and value not in cur:
        cur.append(value)
    elif not add and value in cur:
        cur = [x for x in cur if x != value]
    meta[field] = cur
    _write_frontmatter(path, meta, body)


@router.post("/{doc_id}/link")
def link_doc(slug: str, doc_id: str, request: Request, body: LinkBody,
             user: dict = Depends(require_auth)) -> dict:
    """Link a doc ↔ ticket bidirectionally."""
    _validate_doc_id(doc_id)
    ticket = (body.ticket or "").strip()
    if not _TASK_ID_RE.match(ticket):
        raise HTTPException(status_code=400, detail=f"invalid ticket id: {ticket!r}")

    doc_path = _find_doc(_docs_dir(request, slug), doc_id)
    if doc_path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")
    task_path = _find_task_file(_backlog_dir(request, slug), ticket)
    if task_path is None:
        raise HTTPException(status_code=404, detail=f"ticket not found: {ticket}")

    _mutate_list_field(doc_path, "related_tickets", ticket, add=True)
    _mutate_list_field(task_path, "related_docs", doc_id, add=True)
    return {"ok": True, "id": doc_id, "ticket": ticket, "linked": True}


@router.delete("/{doc_id}/link/{ticket}")
def unlink_doc(slug: str, doc_id: str, ticket: str, request: Request,
               user: dict = Depends(require_auth)) -> dict:
    """Remove a doc ↔ ticket link from both sides."""
    _validate_doc_id(doc_id)
    if not _TASK_ID_RE.match(ticket):
        raise HTTPException(status_code=400, detail=f"invalid ticket id: {ticket!r}")

    doc_path = _find_doc(_docs_dir(request, slug), doc_id)
    if doc_path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")
    _mutate_list_field(doc_path, "related_tickets", ticket, add=False)
    task_path = _find_task_file(_backlog_dir(request, slug), ticket)
    if task_path is not None:
        _mutate_list_field(task_path, "related_docs", doc_id, add=False)
    return {"ok": True, "id": doc_id, "ticket": ticket, "linked": False}
