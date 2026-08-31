"""Backlog read + write endpoints."""
from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app import artifact_nesting as AN, task_search
from app.canonical_status import derive_parent_status
from app.frontmatter import as_list, parse_or_none
from app.markdown_parser import ParseError, parse_task
from app.markdown_writer import (
    merge_task_update,
    slugify,
    write_task,
)
from app.payload_guard import opt_str_field, str_field
from app.project_authz import require_project_member
from app.routes_auth import require_auth
from app.task_body import (
    compose_body,
    is_legacy_body,
    parse_body,
    regraft_progress,
    regraft_verbatim,
)
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
_VALID_STATUSES = {"planned", "open", "in_progress", "paused", "blocked_on_user", "totest", "reopened", "closed"}


def _invalid_status_detail(value: object) -> str:
    """T-0121: 400 message that cites the canonical schema, not just echoes input."""
    canon = ", ".join(sorted(_VALID_STATUSES))
    return f"invalid status: {value!r} — must be one of {{{canon}}}"


_TASK_ID_RE = re.compile(r"^T-\d{4,}$")  # T-0371: \d{4,} — ids cross the 9999 ceiling

# T-0038: optional linkage fields settable via PATCH alongside title/status.
# T-0172: `related_docs` (list of doc ids; T-0751: not necessarily D-NNNN) —
# the ticket→doc half of the
# bidirectional mention. The doc→ticket half lives in routes_docs link/unlink,
# which keeps both sides in sync; this key lets the UI edit it directly too.
_LINKAGE_PATCH_KEYS = frozenset({"initiative", "parent_task", "blocked_by", "related_docs"})

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
    """Attach parsed body sections to a task dict (in-place + return).

    `verbatim_is_legacy` (T-0733) says WHERE `verbatim` came from: True means
    the ticket has no `## Verbatim request` heading, so its text is the whole
    body via the legacy fallback — a planning document, not the stakeholder's
    recorded words. Consumers must label those two cases differently.
    """
    body = task.get("body", "") or ""
    sections = parse_body(body)
    task["verbatim"] = sections["verbatim"]
    task["summary"] = sections["summary"]  # T-0863: one-paragraph status
    task["context"] = sections["context"]
    task["progress"] = sections["progress"]
    task["verbatim_is_legacy"] = is_legacy_body(body)
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


# T-0600 (N2): web parity for the worker's T-0577 dedupe-vs-create gate. The
# constants mirror worker/bot_squad_worker/actions.py (_TASK_DEDUPE_*); the
# threshold knob is the SAME [tasks].dedupe_threshold in system_settings.toml
# the worker config reads, so the two ingest surfaces can't drift on the knob.
_TASK_DEDUPE_MIN_TOKENS = 2
_TASK_DEDUPE_CANDIDATE_LIMIT = 5
_TASKS_DEDUPE_THRESHOLD_DEFAULT = 0.9


def _dedupe_threshold(request: Request) -> float:
    path = request.app.state.api_config.config_dir / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return float(
            (raw.get("tasks") or {}).get(
                "dedupe_threshold", _TASKS_DEDUPE_THRESHOLD_DEFAULT
            )
        )
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
        return _TASKS_DEDUPE_THRESHOLD_DEFAULT


def _similar_backlog(backlog_dir: Path, query: str, threshold: float) -> list:
    """Rank the existing backlog against ``query`` and return the candidates
    whose COVERAGE (fraction of the query's distinct meaningful tokens found
    in the candidate) is at/above ``threshold``. Mirrors the worker's
    ``_task_new_similar_backlog`` (read-only, best-effort: unreadable or
    unparsable tickets drop out rather than blocking the mint)."""
    tokens = task_search.tokenize(query)
    if len(tokens) < _TASK_DEDUPE_MIN_TOKENS:
        return []
    if not backlog_dir.exists():
        return []
    tickets: list[task_search.Ticket] = []
    for md in sorted(backlog_dir.glob("T-*.md")):
        try:
            raw = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_or_none(raw)
        if not parsed:
            continue
        meta, body = parsed
        meta = meta or {}
        stem_parts = md.stem.split("-", 2)
        fallback_id = "-".join(stem_parts[:2]) if len(stem_parts) >= 2 else md.stem
        tickets.append(task_search.Ticket(
            id=str(meta.get("id") or fallback_id).strip(),
            title=str(meta.get("title") or md.stem),
            body=body,
            status=str(meta.get("status") or ""),
        ))
    if not tickets:
        return []
    out = []
    for c in task_search.rank(query, tickets, limit=_TASK_DEDUPE_CANDIDATE_LIMIT):
        if len(c.matched) / len(tokens) >= threshold:
            out.append(c)
    return out


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
    title = str_field(payload, "title")
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
    verbatim_request = opt_str_field(payload, "verbatim_request")
    if verbatim_request is not None:
        body = compose_body(verbatim_request, "", "")
    else:
        body = opt_str_field(payload, "body") or ""

    backlog_dir = _backlog_dir(request, slug)
    backlog_dir.mkdir(parents=True, exist_ok=True)

    # T-0600 (N2): the T-0577 dedupe-vs-create gate, web-parity edition. The
    # worker's task_new rejects a near-duplicate mint; this route previously
    # minted unconditionally, so the board bypassed the anti-proliferation
    # guarantee the voice/CLI lane enforces. Same recipe: rank title
    # (+ verbatim) against the existing backlog, 409 with the candidates and
    # the force escape hatch instead of silently creating a twin.
    force = payload.get("force", False)
    if not isinstance(force, bool):
        raise HTTPException(status_code=400, detail="force must be a boolean")
    if not force:
        dedupe_query = title
        if verbatim_request and verbatim_request.strip():
            dedupe_query = f"{title}\n{verbatim_request}"
        similar = _similar_backlog(backlog_dir, dedupe_query, _dedupe_threshold(request))
        if similar:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "near_duplicate",
                    "message": (
                        "looks like a near-duplicate of existing backlog "
                        "task(s) — retry with force:true to create anyway"
                    ),
                    "candidates": [{"id": c.id, "title": c.title} for c in similar],
                },
            )

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
    if "title" in payload and not str_field(payload, "title"):
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
        # T-0751: a doc id is whatever `artifact_nesting` derives from a doc
        # FILENAME, not `D-NNNN`. `POST /docs/{id}/link` writes this same field
        # for any doc the tree lists (roadmap chapters, gap matrices), so a
        # D-NNNN-only gate here would 400 a value the link endpoint just wrote.
        if not isinstance(v, list) or not all(
            isinstance(x, str) and AN.is_valid_artifact_id(x) for x in v
        ):
            raise HTTPException(status_code=400, detail="related_docs must be a list of doc ids")

    backlog_dir = _backlog_dir(request, slug)
    path = _find_task_file(backlog_dir, task_id)

    # T-0931: the state machine is enforced at the write boundary, not prose —
    # an invalid transition (not just an invalid status VALUE) is refused
    # loudly. A no-op (status echoed back unchanged) is always allowed.
    if "status" in payload:
        from app.task_states import is_valid_transition, invalid_transition_detail
        current_status = str(parse_task(path).get("status") or "")
        new_status = payload["status"]
        if current_status and not is_valid_transition(current_status, new_status):
            raise HTTPException(
                status_code=400,
                detail=invalid_transition_detail(current_status, new_status),
            )

    allowed = {"title", "status", "kind"} | _LINKAGE_PATCH_KEYS
    updates = {k: v for k, v in payload.items() if k in allowed}
    body = opt_str_field(payload, "body")
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
    sid = str_field(payload, "sid")
    text = str_field(payload, "text")
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


# T-0767: write paths for the two AUTHORED artifacts. Until these existed the
# only ergonomic writer on a ticket was the progress feed, which is why it grew
# to 57.4% of the backlog — sessions used the verb that existed, not the one
# that fit.


@router.put("/{task_id}/context")
async def set_task_context(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """REPLACE `## Context` — the working area. Proxies to `task_context_set`.

    PUT, not POST, and that is the contract rather than a preference: Context
    holds what is TRUE NOW, so the write is idempotent and replacing. An empty
    `text` clears the section — allowed, since a session that has finished
    should be able to leave a clean final state, but it must be sent
    explicitly (a missing field is a 400, not a silent wipe).
    """
    _validate_task_id(task_id)
    if "text" not in payload:
        raise HTTPException(status_code=400, detail="text is required (send \"\" to clear)")
    text = payload["text"]
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string")

    backlog_dir = _backlog_dir(request, slug)
    _find_task_file(backlog_dir, task_id)

    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action("task_context_set", {
            "slug": slug,
            "task_id": task_id,
            "text": text,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result


@router.put("/{task_id}/summary")
async def set_task_summary(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """REPLACE `## Executive summary` — the one-paragraph status (T-0863).

    Same PUT-is-the-contract reasoning as `set_task_context`: a status is what
    is true now, so the write replaces and is idempotent, and an empty `text`
    clears the section but must be sent explicitly.

    A body that is not one paragraph comes back as a **502 naming the rule**,
    not a 400, because the refusal is the worker's `set_summary` speaking
    through `WorkerError` — the same message a CLI caller gets. Re-deriving the
    paragraph rule here to answer 400 would be a second copy of it, free to
    drift from the one that actually gates the write.
    """
    _validate_task_id(task_id)
    if "text" not in payload:
        raise HTTPException(status_code=400, detail="text is required (send \"\" to clear)")
    text = payload["text"]
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string")

    backlog_dir = _backlog_dir(request, slug)
    _find_task_file(backlog_dir, task_id)

    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action("task_summary_set", {
            "slug": slug,
            "task_id": task_id,
            "text": text,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result


@router.post("/{task_id}/stakeholder-note")
async def add_stakeholder_note(
    slug: str,
    task_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_project_member),  # T-0381: project-write gate
) -> dict:
    """Append a stakeholder QUOTE. Proxies to `task_stakeholder_note_add`.

    The board's comment kebab has posted the stakeholder's own words as a
    Progress note since T-0238 (`S-stakeholder` sentinel). That is the overlap
    T-0767 removes: his words and the sessions' narration shared one feed, so
    his guidance aged and got trimmed along with it. This route is where they
    go now.
    """
    _validate_task_id(task_id)
    text = str_field(payload, "text")
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    source = payload.get("source") or "stakeholder"
    if not isinstance(source, str):
        raise HTTPException(status_code=400, detail="source must be a string")

    backlog_dir = _backlog_dir(request, slug)
    _find_task_file(backlog_dir, task_id)

    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action("task_stakeholder_note_add", {
            "slug": slug,
            "task_id": task_id,
            "text": text,
            "source": source,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result
