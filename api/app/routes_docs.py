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

from app import artifact_nesting as AN
from app.project_authz import require_project_member
from app.routes_auth import require_auth

router = APIRouter(
    prefix="/projects/{slug}/docs",
    tags=["docs"],
    dependencies=[Depends(require_auth)],
)

_MAX_CONTENT_BYTES = 200 * 1024
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TASK_ID_RE = re.compile(r"^T-\d{4,}$")  # T-0371
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


def _project_root(request: Request, slug: str) -> Path:
    """Project data dir — the root the cross-store artifact_nesting walks."""
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug)


def _backlog_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    return cfg.project_data_dir(slug) / "backlog"


def _validate_doc_id(doc_id: str) -> None:
    """Reject an id that could not name a doc file at all (T-0751).

    Was ``^D-\\d{4,}$``, which the LIST endpoint never applied: it advertised
    ``roadmap/03-docs-artifacts.md`` as ``03-docs`` and this then 400'd it —
    19 of 74 live docs (26%), across roadmap / design / qa / operator. Both
    ends now defer to ``artifact_nesting``: :func:`AN.id_from_stem` says what an
    id IS, :func:`AN.is_valid_artifact_id` says which strings can be one. The
    traversal guard the old regex doubled as moved into :func:`AN.find_doc`,
    which resolves by enumerating the tree instead of globbing the id.
    """
    if not AN.is_valid_artifact_id(doc_id):
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
    # T-0751: ONE derivation, shared with the detail endpoint and the
    # cross-store walk — an allocated `D-NNNN-<slug>` stem yields `D-NNNN`, and
    # any other stem (`03-docs-artifacts`, `verbatim-contract`) IS its own id.
    derived_id = AN.id_from_stem(path.stem)
    parent = meta.get("parent_doc_id")
    parent = str(parent).strip() if parent else None
    return {
        **meta,
        "id": meta.get("id") or derived_id,
        # category from the parent dir is authoritative (matches storage layout)
        "category": path.parent.name,
        "related_tickets": meta.get("related_tickets") or [],
        # T-0234 nested docs: a child carries `parent_doc_id`; None == root.
        "parent_doc_id": parent or None,
        "body": body,
        "raw": text,
        "path": str(path),
    }


def _find_doc(project_root: Path, doc_id: str) -> Path | None:
    """Locate the doc file for ``doc_id`` under any category dir, or None.

    T-0751: delegates to ``artifact_nesting``, which enumerates the tree and
    compares DERIVED ids rather than globbing ``*/{doc_id}-*.md``. The id never
    reaches a path, so `..`, separators and glob metacharacters have nothing to
    act on — and the resolver matches whatever the list endpoint advertises,
    because both read the same derivation over the same walk.
    """
    ref = AN.find_doc(project_root, doc_id)
    return ref.path if ref is not None else None


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
                # T-0234: nesting key so the UI can build the doc tree.
                "parent_doc_id": d.get("parent_doc_id"),
                # T-0756: the FILENAME stem, alongside the id derived from it.
                # Doc bodies link to each other by relative filename
                # (`01-sessions-task-manager.md`) because that is what makes
                # them readable in an editor and on GitHub. The renderer has to
                # turn that filename into a doc id — and `id_from_stem` is a
                # PYTHON function it cannot call. Shipping the stem lets the web
                # side resolve by LOOKUP instead of re-deriving the rule in
                # TypeScript, which is how T-0751's bug (two copies of one
                # derivation, quietly disagreeing) got made in the first place.
                "stem": f.stem,
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
    path = _find_doc(_project_root(request, slug), doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")
    out = _parse(path)
    # T-0234 / T-0283 / T-0415: ONE cross-store children scan is the single
    # source. child_artifact_ids is the full set (doc + use-case + feedback
    # children); child_doc_ids (back-compat for the pre-cross-store FE) is its
    # kind==doc subset — the docs-only _children_of mirror this replaced ran a
    # second, redundant scan. `kind` lets the FE tree render without a relookup.
    children = AN.children_of(_project_root(request, slug), doc_id)
    out["kind"] = AN.KIND_DOC
    out["child_artifact_ids"] = [c["id"] for c in children]
    out["child_doc_ids"] = [c["id"] for c in children if c["kind"] == AN.KIND_DOC]
    return out


@router.get("/{doc_id}/children")
def get_doc_children(slug: str, doc_id: str, request: Request) -> list[dict]:
    """List the child artifacts attached to a mother doc.

    T-0283: cross-store — returns every artifact (doc / use-case / feedback)
    whose ``parent_doc_id`` points at this doc, each carrying its ``kind``. A
    doc with no children returns ``[]``.
    """
    _validate_doc_id(doc_id)
    return AN.children_of(_project_root(request, slug), doc_id)


class NewDoc(BaseModel):
    category: str
    title: str
    # T-0234: optionally attach the new doc under a mother doc at creation.
    parent_doc_id: str | None = None


@router.post("")
def create_doc(slug: str, request: Request, body: NewDoc,
               user: dict = Depends(require_project_member)) -> dict:  # T-0381: project-write gate
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

    # T-0234/T-0283: an optional mother artifact the new child attaches under.
    # The parent must already exist (explicit relationship) but may live in ANY
    # store (doc / use-case / feedback) — the unified nestable model. A
    # freshly-allocated child can't be its own parent, so no cycle check here.
    parent_doc_id = (body.parent_doc_id or "").strip() or None
    if parent_doc_id is not None and AN.find_artifact(
        _project_root(request, slug), parent_doc_id
    ) is None:
        raise HTTPException(
            status_code=404, detail=f"parent artifact not found: {parent_doc_id}"
        )

    cdir = _docs_dir(request, slug) / category
    cdir.mkdir(parents=True, exist_ok=True)
    doc_id = idalloc.allocate_id(cfg.data_dir, slug, "doc")
    path = cdir / f"{doc_id}-{_slugify(title)}.md"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm_lines = [
        f"id: {doc_id}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"category: {category}",
        "status: draft",
        f"created: {now}",
        "related_tickets: []",
    ]
    if parent_doc_id is not None:
        fm_lines.append(f"parent_doc_id: {parent_doc_id}")
    fm = "\n".join(fm_lines)
    content = f"---\n{fm}\n---\n\n# {title}\n\n(new {category} doc — T-0172 docs system)\n"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": doc_id, "category": category,
            "parent_doc_id": parent_doc_id}


@router.delete("/{doc_id}")
def delete_doc(slug: str, doc_id: str, request: Request,
               user: dict = Depends(require_project_member)) -> dict:  # T-0381: project-write gate
    """Delete a doc (T-0276): remove the file + scrub its ticket backlinks.

    Refuses (409) to delete a MOTHER doc that still has children, so a subtree
    is never silently orphaned — the caller must re-parent or delete the
    children first. The ``D-NNNN`` id is TOMBSTONED, not reclaimed: the
    per-type atomic allocator is monotonic, so a deleted id is never reissued
    (no counter rollback, no collision risk).
    """
    _validate_doc_id(doc_id)
    path = _find_doc(_project_root(request, slug), doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")

    # Item 5: the orphan guard spans STORES — a doc can mother use-cases /
    # feedback / other docs (cross-store nesting, T-0283). A docs-only scan
    # would miss those, silently orphaning them. AN.children_of walks every
    # store.
    children = AN.children_of(_project_root(request, slug), doc_id)
    if children:
        child_ids = ", ".join(c["id"] for c in children)
        raise HTTPException(
            status_code=409,
            detail=(
                f"doc {doc_id} has child artifacts ({child_ids}) — "
                "re-parent or delete them first"
            ),
        )

    # Scrub the bidirectional mentions: drop this doc from each linked ticket's
    # related_docs, mirroring unlink_doc's task-side cleanup so no ticket keeps
    # a dangling related_docs entry.
    meta = _parse(path)
    backlog_dir = _backlog_dir(request, slug)
    for ticket in meta.get("related_tickets") or []:
        task_path = _find_task_file(backlog_dir, str(ticket))
        if task_path is not None:
            _mutate_list_field(task_path, "related_docs", doc_id, add=False)

    path.unlink()
    return {"ok": True, "id": doc_id, "deleted": True}


class PutDoc(BaseModel):
    content: str


@router.put("/{doc_id}")
def put_doc(slug: str, doc_id: str, request: Request, body: PutDoc,
            user: dict = Depends(require_project_member)) -> dict:  # T-0381: project-write gate
    _validate_doc_id(doc_id)
    content = body.content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="content must not be empty")
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail="content exceeds 200 KB limit")
    path = _find_doc(_project_root(request, slug), doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "id": doc_id}


# --------------------------------------------------------------------------
# Nested docs (T-0234): adopt / disown — set or clear a doc's parent so an
# existing flat doc (user-feedback, use-cases) can be re-parented under a
# mother page. parent_doc_id=null disowns (back to root).
# --------------------------------------------------------------------------
class SetParent(BaseModel):
    parent_doc_id: str | None = None


def _set_scalar_field(path: Path, field: str, value: str | None) -> None:
    """Set (or, if value is None, delete) a scalar frontmatter field."""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise HTTPException(status_code=400, detail=f"no frontmatter in {path.name}")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = m.group(2)
    if value is None:
        meta.pop(field, None)
    else:
        meta[field] = value
    _write_frontmatter(path, meta, body)


@router.put("/{doc_id}/parent")
def set_doc_parent(slug: str, doc_id: str, request: Request, body: SetParent,
                   user: dict = Depends(require_project_member)) -> dict:  # T-0381: project-write gate
    """Re-parent (adopt) or clear the parent (disown) of an existing doc.

    Validations: the doc must exist; a non-null parent must exist, must not be
    the doc itself, and must not close a cycle (any depth).
    """
    _validate_doc_id(doc_id)
    doc_path = _find_doc(_project_root(request, slug), doc_id)
    if doc_path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")

    # T-0283: a doc may nest under ANY artifact (doc / use-case / feedback), so
    # parent existence + the cycle check resolve cross-store via artifact_nesting.
    proj_root = _project_root(request, slug)
    new_parent = (body.parent_doc_id or "").strip() or None
    if new_parent is not None:
        if new_parent == doc_id:
            raise HTTPException(
                status_code=400, detail="a doc cannot be its own parent (self-parent)"
            )
        if AN.find_artifact(proj_root, new_parent) is None:
            raise HTTPException(
                status_code=404, detail=f"parent artifact not found: {new_parent}"
            )
        if AN.would_cycle(proj_root, doc_id, new_parent):
            raise HTTPException(
                status_code=400,
                detail=f"refusing to set parent {new_parent}: would create a cycle",
            )

    _set_scalar_field(doc_path, "parent_doc_id", new_parent)
    return {"ok": True, "id": doc_id, "parent_doc_id": new_parent}


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
             user: dict = Depends(require_project_member)) -> dict:  # T-0381: project-write gate
    """Link a doc ↔ ticket bidirectionally."""
    _validate_doc_id(doc_id)
    ticket = (body.ticket or "").strip()
    if not _TASK_ID_RE.match(ticket):
        raise HTTPException(status_code=400, detail=f"invalid ticket id: {ticket!r}")

    doc_path = _find_doc(_project_root(request, slug), doc_id)
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
               user: dict = Depends(require_project_member)) -> dict:  # T-0381: project-write gate
    """Remove a doc ↔ ticket link from both sides."""
    _validate_doc_id(doc_id)
    if not _TASK_ID_RE.match(ticket):
        raise HTTPException(status_code=400, detail=f"invalid ticket id: {ticket!r}")

    doc_path = _find_doc(_project_root(request, slug), doc_id)
    if doc_path is None:
        raise HTTPException(status_code=404, detail=f"doc not found: {doc_id}")
    _mutate_list_field(doc_path, "related_tickets", ticket, add=False)
    task_path = _find_task_file(_backlog_dir(request, slug), ticket)
    if task_path is not None:
        _mutate_list_field(task_path, "related_docs", doc_id, add=False)
    return {"ok": True, "id": doc_id, "ticket": ticket, "linked": False}
