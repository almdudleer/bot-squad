"""Backlog read + write endpoints."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.canonical_status import derive_parent_status
from app.frontmatter import as_list, parse_or_none
from app.markdown_parser import ParseError, parse_task
from app.markdown_writer import (
    merge_task_update,
    slugify,
    write_task,
)
from app.project_authz import require_project_member
from app.routes_auth import require_auth
from app.task_body import compose_body, parse_body, regraft_progress, regraft_verbatim
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{slug}/backlog",
    tags=["backlog"],
    dependencies=[Depends(require_auth)],
)

# The six internal statuses are the SSOT for what a task may be. They are a
# refinement of the stakeholder's canonical 4-state model (backlog/in-progress/
# validating/done) — see app.canonical_status for the non-destructive mapping
# layer (T-0479) and docs/design/status-canonical-mapping.md.
_VALID_STATUSES = {"planned", "open", "in_progress", "totest", "reopened", "closed"}


def _invalid_status_detail(value: object) -> str:
    """T-0121: 400 message that cites the canonical schema, not just echoes input."""
    canon = ", ".join(sorted(_VALID_STATUSES))
    return f"invalid status: {value!r} — must be one of {{{canon}}}"


_TASK_ID_RE = re.compile(r"^T-\d{4,}$")  # T-0371: \d{4,} — ids cross the 9999 ceiling

# T-0038: optional linkage fields settable via PATCH alongside title/status.
# T-0172: `related_docs` (list of D-NNNN) — the ticket→doc half of the
# bidirectional mention. The doc→ticket half lives in routes_docs link/unlink,
# which keeps both sides in sync; this key lets the UI edit it directly too.
_LINKAGE_PATCH_KEYS = frozenset({"initiative", "parent_task", "blocked_by", "related_docs"})

_DOC_ID_RE = re.compile(r"^D-\d{4,}$")  # T-0371

# Permissive — basename of a vision/initiatives/<name> .md file. Empty string
# allowed (callers must pass null to clear, not empty).
_INITIATIVE_BASENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.md$")

# T-0480: `kind` marks an initiative-task. Absent == a normal task; we don't
# over-model — only these two values are accepted (the design reserves the
# enum but ships just task|initiative).
_VALID_KINDS = frozenset({"task", "initiative"})


def _backlog_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug) / "backlog"


def _find_task_file(backlog_dir: Path, task_id: str) -> Path:
    """Find T-NNNN-*.md for given id, or raise 404."""
    matches = list(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return matches[0]


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID_RE.match(task_id):
        raise HTTPException(status_code=400, detail=f"invalid task id format: {task_id!r}")


def _enrich_with_sections(task: dict) -> dict:
    """Attach parsed body sections to a task dict (in-place + return)."""
    body = task.get("body", "") or ""
    sections = parse_body(body)
    task["verbatim"] = sections["verbatim"]
    task["context"] = sections["context"]
    task["progress"] = sections["progress"]
    return task


def _session_map_by_task(sessions_dir: Path) -> dict[str, dict]:
    """Scan sessions/*.md → {task_id: {sid, status}}. Active beats paused.

    Phase 9: a dev session may also carry `extra_task_ids: [T-..., T-...]`
    (bracketed list). Every entry in {primary} ∪ extras gets the same
    session row. (Constraint: a task can only be in one session's
    primary-or-extras set; the worker enforces this at bind time.)
    """
    if not sessions_dir.exists():
        return {}
    out: dict[str, dict] = {}
    for f in sorted(sessions_dir.glob("*.md")):
        try:
            text = f.read_text()
        except OSError:
            continue
        parsed = parse_or_none(text)  # T-0075: shared parser
        if parsed is None:
            continue
        meta, _ = parsed
        sid = meta.get("sid") or f.stem
        status = meta.get("status") or "unknown"
        task_ids: list[str] = []
        primary = meta.get("task_id")
        if primary and primary != "~":
            task_ids.append(str(primary))
        task_ids.extend(as_list(meta.get("extra_task_ids")))
        for tid in task_ids:
            existing = out.get(tid)
            if existing and existing["status"] == "active" and status != "active":
                continue
            out[tid] = {"sid": sid, "status": status}
    return out


def _stamp_parent_derivation(tasks: list[dict]) -> None:
    """T-0512 (M9 / Part A): mark every task that has subtasks as an abstract
    parent and derive its canonical state from its children.

    A subtask is any task whose ``parent_task`` points at this task's id. For
    each parent we stamp ``child_count`` and ``derived_status`` (canonical 4-state
    rollup — see ``canonical_status.derive_parent_status``). Tasks without
    children are left untouched (not abstract). Mutates in place.
    """
    child_statuses: dict[str, list[str]] = {}
    for t in tasks:
        parent = str(t.get("parent_task") or "").strip()
        if parent:
            child_statuses.setdefault(parent, []).append(str(t.get("status") or ""))
    by_id = {t.get("id"): t for t in tasks}
    for parent_id, statuses in child_statuses.items():
        parent = by_id.get(parent_id)
        if parent is None:
            continue  # dangling parent_task ref — child renders standalone
        parent["child_count"] = len(statuses)
        parent["derived_status"] = derive_parent_status(statuses)


@router.get("")
def list_backlog(slug: str, request: Request) -> list[dict]:
    cfg = request.app.state.api_config
    proj = cfg.project(slug)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    project_data = cfg.project_data_dir(slug)
    backlog_dir = project_data / "backlog"
    if not backlog_dir.exists():
        return []
    session_map = _session_map_by_task(project_data / "sessions")
    tasks: list[dict] = []
    for f in sorted(backlog_dir.glob("*.md")):
        try:
            t = parse_task(f)
        except ParseError as e:
            log.warning("backlog parse error %s: %s", f, e)
            continue
        sess = session_map.get(t.get("id", ""))
        if sess:
            t["session"] = sess
        _enrich_with_sections(t)
        tasks.append(t)
    _stamp_parent_derivation(tasks)
    return tasks


@router.get("/{task_id}/children")
def list_children(slug: str, task_id: str, request: Request) -> list[dict]:
    """T-0512 (M9): list the subtasks of a task — every backlog task whose
    ``parent_task`` points at ``task_id``.

    404s if the parent task itself does not exist. Rows carry the same shape as
    ``list_backlog`` (enriched sections + bound-session info) so the FE can
    render them with the same card/affordances. Ordered by priority then id is
    left to the caller — here we return them in filename order (stable).
    """
    _validate_task_id(task_id)
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    project_data = cfg.project_data_dir(slug)
    backlog_dir = project_data / "backlog"
    # 404 cleanly if the parent task does not exist (so a stale link is loud).
    _find_task_file(backlog_dir, task_id)
    session_map = _session_map_by_task(project_data / "sessions")
    children: list[dict] = []
    for f in sorted(backlog_dir.glob("*.md")):
        try:
            t = parse_task(f)
        except ParseError as e:
            log.warning("backlog parse error %s: %s", f, e)
            continue
        if str(t.get("parent_task") or "").strip() != task_id:
            continue
        sess = session_map.get(t.get("id", ""))
        if sess:
            t["session"] = sess
        _enrich_with_sections(t)
        children.append(t)
    return children


def _validate_priority(value: object) -> int:
    """Coerce + validate priority for write paths. Raises 400 if not a non-negative int."""
    # Reject bools explicitly — Python treats `True` as 1 numerically.
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=400, detail="priority must be a non-negative integer")
    if value < 0:
        raise HTTPException(status_code=400, detail="priority must be a non-negative integer")
    return value


def _default_open_priority(backlog_dir: Path) -> int:
    """Default priority for a new task: max(open priorities) + 100, else 0."""
    max_open: int | None = None
    if not backlog_dir.exists():
        return 0
    for f in backlog_dir.glob("T-*.md"):
        try:
            t = parse_task(f)
        except ParseError:
            continue
        if t.get("status") != "open":
            continue
        prio = t.get("priority")
        if isinstance(prio, int) and not isinstance(prio, bool):
            if max_open is None or prio > max_open:
                max_open = prio
    return 0 if max_open is None else max_open + 100


@router.post("")
def create_task(
    slug: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")
    status = payload.get("status", "open")
    if status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=_invalid_status_detail(status))
    # T-0480: optional `kind` (task|initiative). Absent == normal task.
    kind = payload.get("kind")
    if kind is not None and kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind!r} — must be one of {sorted(_VALID_KINDS)}")
    # Phase 7: prefer `verbatim_request` (composed into canonical body).
    # Fall back to legacy `body` (stored as-is — caller knows the convention).
    verbatim_request = payload.get("verbatim_request")
    if verbatim_request is not None:
        body = compose_body(verbatim_request, "", "")
    else:
        body = payload.get("body") or ""

    backlog_dir = _backlog_dir(request, slug)
    backlog_dir.mkdir(parents=True, exist_ok=True)

    # Phase 8: priority. If the caller specified one, validate. Else compute
    # default from existing open tasks so new ones land at the bottom.
    if "priority" in payload and payload["priority"] is not None:
        priority = _validate_priority(payload["priority"])
    else:
        priority = _default_open_priority(backlog_dir)

    # T-0174: allocate via the shared idalloc counter — the SAME
    # data/<slug>/_counters/task.txt the worker's task_new uses, so a web
    # create and an agent `bsq task new` can no longer collide on a T-id
    # (they previously locked different files: .lock here vs .task-id.lock
    # in the worker).
    from app import idalloc
    cfg = request.app.state.api_config
    task_id = idalloc.allocate_id(cfg.data_dir, slug, "task")
    slug_part = slugify(title)
    filename = f"{task_id}-{slug_part}.md" if slug_part else f"{task_id}.md"
    path = backlog_dir / filename

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = {
        "id": task_id,
        "title": title,
        "status": status,
        "priority": priority,
        "created": now,
        "updated": now,
        # T-0080: stamp the creator's UI username so per-user task
        # scoping (deferred for v0.9) has the data it needs without a
        # backfill. Legacy tasks lack this field; they default to
        # admin-only when filtering eventually lands. None gets
        # dropped by write_task — only emit owner: if known.
        "owner": user.get("username") or None,
        # T-0480: None drops out via write_task — only stamp kind when set.
        "kind": kind,
    }
    write_task(path, fm, body)

    return _enrich_with_sections(parse_task(path))


@router.patch("/{task_id}")
def patch_task(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    _validate_task_id(task_id)

    if "status" in payload and payload["status"] not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=_invalid_status_detail(payload["status"]))
    if "title" in payload and not (payload.get("title") or "").strip():
        raise HTTPException(status_code=400, detail="title must not be empty")
    # T-0480: `kind` (task|initiative). null clears it (write_task drops None).
    if "kind" in payload and payload["kind"] is not None and payload["kind"] not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {payload['kind']!r} — must be one of {sorted(_VALID_KINDS)}")

    # T-0038: validate linkage fields if provided. `null` clears the field
    # (write_task drops None entries). String must look like a vision/
    # initiatives basename; parent_task must be T-NNNN; blocked_by must be a
    # list of T-NNNN.
    if "initiative" in payload and payload["initiative"] is not None:
        v = payload["initiative"]
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail=f"invalid initiative basename: {v!r}")
        # T-0424 fold: accept a bare stem too — canonicalize to the .md FILE form
        # via the normalize_id SSOT so a bare-stem input stores consistently with
        # task_new + the initiative file. Empty string stays empty (callers clear
        # via null, not ""); traversal/garbage still fails the basename regex below.
        from app.routes_feedback import normalize_id
        if v.strip():
            v = f"{normalize_id(v.strip())}.md"
        if not _INITIATIVE_BASENAME_RE.match(v):
            raise HTTPException(
                status_code=400,
                detail=f"invalid initiative basename: {payload['initiative']!r}",
            )
        payload["initiative"] = v
    if "parent_task" in payload and payload["parent_task"] is not None:
        v = payload["parent_task"]
        if not isinstance(v, str) or not _TASK_ID_RE.match(v):
            raise HTTPException(status_code=400, detail=f"invalid parent_task id: {v!r}")
    if "blocked_by" in payload and payload["blocked_by"] is not None:
        v = payload["blocked_by"]
        if not isinstance(v, list) or not all(
            isinstance(x, str) and _TASK_ID_RE.match(x) for x in v
        ):
            raise HTTPException(status_code=400, detail="blocked_by must be a list of T-NNNN ids")
    if "related_docs" in payload and payload["related_docs"] is not None:
        v = payload["related_docs"]
        if not isinstance(v, list) or not all(
            isinstance(x, str) and _DOC_ID_RE.match(x) for x in v
        ):
            raise HTTPException(status_code=400, detail="related_docs must be a list of D-NNNN ids")

    backlog_dir = _backlog_dir(request, slug)
    path = _find_task_file(backlog_dir, task_id)

    allowed = {"title", "status", "kind"} | _LINKAGE_PATCH_KEYS
    updates = {k: v for k, v in payload.items() if k in allowed}
    body = payload.get("body")
    if body is not None:
        # T-0289 + T-0335 item-14: `## Verbatim request` (human-only) and
        # `## Progress` (append-only audit feed, on-disk SSOT) must never be
        # clobbered by a body replace. Re-graft both on-disk sections over
        # whatever the caller sent; every other section in `body` is preserved.
        on_disk = parse_task(path)["body"]
        body = regraft_verbatim(on_disk, body)
        body = regraft_progress(on_disk, body)
    try:
        merge_task_update(path, updates, body=body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return _enrich_with_sections(parse_task(path))


@router.patch("/{task_id}/priority")
def patch_task_priority(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Set a task's priority (int sort key for Kanban ordering)."""
    _validate_task_id(task_id)
    if "priority" not in payload:
        raise HTTPException(status_code=400, detail="priority required")
    priority = _validate_priority(payload["priority"])

    backlog_dir = _backlog_dir(request, slug)
    path = _find_task_file(backlog_dir, task_id)

    try:
        merge_task_update(path, {"priority": priority})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return _enrich_with_sections(parse_task(path))


@router.delete("/{task_id}")
def delete_task(
    slug: str,
    task_id: str,
    request: Request,
    user: dict = Depends(require_project_member),  # T-0381: destructive — project-write gate
) -> dict:
    _validate_task_id(task_id)

    backlog_dir = _backlog_dir(request, slug)
    path = _find_task_file(backlog_dir, task_id)
    path.unlink()
    return {"ok": True, "deleted_id": task_id}


# T-0335 item 18: POST /{task_id}/comments removed — the ``## Comments`` channel
# was orphaned (zero FE callers after the board kebab repointed to a Progress
# note). The progress feed below is the single comment channel now.


@router.post("/{task_id}/progress")
async def add_progress(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Append a short progress note. Proxies to worker action `task_progress_add`."""
    _validate_task_id(task_id)
    sid = (payload.get("sid") or "").strip()
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    if not sid:
        raise HTTPException(status_code=400, detail="sid must not be empty")

    backlog_dir = _backlog_dir(request, slug)
    # Pre-flight: 404 cleanly if the task does not exist (worker would also 4xx
    # but the API contract maps it to 502 otherwise).
    _find_task_file(backlog_dir, task_id)

    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action("task_progress_add", {
            "slug": slug,
            "task_id": task_id,
            "sid": sid,
            "text": text,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result
