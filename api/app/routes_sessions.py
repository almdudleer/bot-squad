"""Session management endpoints — proxy to worker actions.

Phase 2 multi-user: session ops route through the WorkerRouter:
  - list_sessions fans out across all user workers (timeout-bounded).
  - spawn lands on the caller's own user worker.
  - pause/suspend/resume route by SID (S-<linux_user>-…) and enforce that
    non-admin callers can only act on sessions in their own linux_user.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.routes_auth import require_auth
from app.worker_client import WorkerClient, WorkerError, WorkerRouter

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_auth)],
)

# Separate router for dev-spawn-request: the prefix differs (no /sessions
# suffix), keeping the URL semantically distinct from direct spawns.
dev_spawn_router = APIRouter(
    prefix="/projects/{slug}",
    tags=["sessions"],
    dependencies=[Depends(require_auth)],
)


def _router(request: Request) -> WorkerRouter:
    return request.app.state.worker_router


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


def _check_sid_ownership(sid: str, user: dict, router: WorkerRouter) -> None:
    """Non-admins can only act on SIDs owned by their own linux_user."""
    if user.get("is_admin"):
        return
    sid_user = router.user_for_sid(sid)
    if sid_user is None:
        return  # legacy SID format — let the worker decide
    if sid_user != user.get("linux_user"):
        raise HTTPException(
            status_code=403,
            detail=f"session {sid!r} belongs to linux_user {sid_user!r}; "
                   f"you are {user.get('linux_user')!r}",
        )


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/sessions  — fan-out across all user workers
# ---------------------------------------------------------------------------

@router.get("")
async def list_sessions(slug: str, request: Request) -> list[dict]:
    """List all Claude sessions (active + paused) for a project.

    Fans out across every configured user worker. Each call is bounded
    by a 5 s timeout so a dead/slow user worker can't block the whole list.
    Results are deduped by sid.
    """
    _check_project(request, slug)
    wrouter = _router(request)

    async def _one(client: WorkerClient, who: str) -> list[dict]:
        try:
            result = await client.call_action(
                "list_sessions", {"slug": slug}, timeout=5.0,
            )
            return result.get("sessions", [])
        except WorkerError as e:
            log.warning("list_sessions fan-out for %s failed: %s", who, e)
            return []
        except Exception as e:
            log.warning("list_sessions fan-out for %s crashed: %s", who, e)
            return []

    pairs = wrouter.all_user_workers()
    results = await asyncio.gather(*[_one(c, u) for (u, c) in pairs])
    merged: dict[str, dict] = {}
    for batch in results:
        for row in batch:
            sid = row.get("sid")
            if sid and sid not in merged:
                merged[sid] = row
    return list(merged.values())


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/pause
# ---------------------------------------------------------------------------

@router.post("/{sid}/pause")
async def pause_session(
    slug: str, sid: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Pause a running Claude session — interrupt only (Ctrl-C)."""
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.for_sid(sid)
    try:
        return await client.call_action("pause_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/suspend
# ---------------------------------------------------------------------------

@router.post("/{sid}/suspend")
async def suspend_session(
    slug: str, sid: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Suspend a Claude session — close the pane to free resources.

    Registry md preserved so resume can resurrect via ``claude --resume``.
    """
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.for_sid(sid)
    try:
        return await client.call_action("suspend_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/resume
# ---------------------------------------------------------------------------

@router.post("/{sid}/resume")
async def resume_session(
    slug: str, sid: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Resume a paused or suspended Claude session.

    For paused with live pane: clears paused flag.
    For suspended or zombie: spawns new tmux window with ``claude --resume``.
    """
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.for_sid(sid)
    try:
        return await client.call_action("resume_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions  (spawn new)
# ---------------------------------------------------------------------------

class SpawnRequest(BaseModel):
    window: str
    initial_prompt: Optional[str] = None
    task_id: Optional[str] = None
    initiative: Optional[str] = None


@router.post("")
async def spawn_session(
    slug: str, body: SpawnRequest, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Spawn a new Claude session in the project's repo.

    Spawned in the logged-in user's own tmux server (linux_user from auth).
    Optional `task_id` writes ``.claude/task_id`` so the SessionStart hook
    links the new session to that backlog task.
    """
    _check_project(request, slug)
    if not body.window.strip():
        raise HTTPException(status_code=400, detail="window name must not be empty")

    wrouter = _router(request)
    client = wrouter.for_user(user["linux_user"])
    params: dict = {"slug": slug, "window": body.window}
    if body.initial_prompt:
        params["initial_prompt"] = body.initial_prompt
    if body.task_id:
        params["task_id"] = body.task_id
    if body.initiative:
        params["initiative"] = body.initiative

    try:
        return await client.call_action("spawn_session", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Phase 9: POST /api/projects/{slug}/sessions/{sid}/bind/task
# Phase 9: POST /api/projects/{slug}/sessions/{sid}/bind/initiative
# ---------------------------------------------------------------------------

class BindTaskRequest(BaseModel):
    task_id: str


class BindInitiativeRequest(BaseModel):
    initiative: str


@router.post("/{sid}/bind/task")
async def bind_task(
    slug: str, sid: str, body: BindTaskRequest, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Bind another task to an existing dev session (multi-binding).

    The session must already be a dev (have a primary task_id). Task must
    exist and not be bound to any other dev.
    """
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    # Binding state lives on the coordinator (the session md is canonical there).
    client = wrouter.coordinator()
    try:
        return await client.call_action("bind_task", {
            "slug": slug, "sid": sid, "task_id": body.task_id,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{sid}/bind/initiative")
async def bind_initiative(
    slug: str, sid: str, body: BindInitiativeRequest, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Bind another initiative to an existing TL session (multi-binding).

    The session must be a TL (no primary task_id). Initiative file must
    exist and not be bound to any other TL.
    """
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.coordinator()
    try:
        return await client.call_action("bind_initiative", {
            "slug": slug, "sid": sid, "initiative": body.initiative,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{sid}/unbind/task")
async def unbind_task(
    slug: str, sid: str, body: BindTaskRequest, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Remove a task from a dev session's extras (cannot remove the primary)."""
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.coordinator()
    try:
        return await client.call_action("unbind_task", {
            "slug": slug, "sid": sid, "task_id": body.task_id,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{sid}/unbind/initiative")
async def unbind_initiative(
    slug: str, sid: str, body: BindInitiativeRequest, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Remove an initiative from a TL session's bindings.

    Clears primary `initiative` if it matches, otherwise filters from
    `extra_initiatives`. Idempotent.
    """
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.coordinator()
    try:
        return await client.call_action("unbind_initiative", {
            "slug": slug, "sid": sid, "initiative": body.initiative,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Archive / unarchive (small features batch)
# ---------------------------------------------------------------------------

@router.post("/{sid}/archive")
async def archive_session(
    slug: str, sid: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Archive a suspended session (hidden from main list by default).

    Worker validates that the session has no live pane. Archive flag
    lives in the session md frontmatter.
    """
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.coordinator()
    try:
        return await client.call_action("archive_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{sid}/unarchive")
async def unarchive_session(
    slug: str, sid: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Clear the archived flag on a session md."""
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter)
    client = wrouter.coordinator()
    try:
        return await client.call_action("unarchive_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/dev-spawn-request
# ---------------------------------------------------------------------------

class DevSpawnRequest(BaseModel):
    tl_sid: str
    task_id: Optional[str] = None
    instructions: str


def _read_session_meta(sessions_dir: Path, sid: str) -> dict | None:
    """Lightweight YAML-frontmatter parser for session md files."""
    p = sessions_dir / f"{sid}.md"
    if not p.exists():
        return None
    text = p.read_text()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta


def _compose_tl_message(slug: str, task_id: Optional[str], instructions: str) -> str:
    """Build the structured DEV SPAWN REQUEST message body for the TL."""
    header = "[DEV SPAWN REQUEST from stakeholder]"
    body = instructions.strip()
    if task_id:
        return (
            f"{header}\n\n"
            f"Bound task: {task_id}\n\n"
            f"Stakeholder instructions:\n{body}\n\n"
            f"Action:\n"
            f"1. Read data/{slug}/backlog/{task_id}-*.md for full task context.\n"
            f"2. Spawn a dev teammate via Claude Code's native agent-teams feature\n"
            f"   (\"form a team with one teammate for <feature name>\"). Name the\n"
            f"   window after the feature.\n"
            f"3. Brief the teammate on the task scope + DoD + the stakeholder's\n"
            f"   additional instructions above."
        )
    return (
        f"{header}\n\n"
        f"No task bound.\n\n"
        f"Stakeholder instructions:\n{body}\n\n"
        f"Action:\n"
        f"1. Search data/{slug}/backlog/ for an existing task that matches\n"
        f"   the stakeholder's instructions. If found, optionally expand its body\n"
        f"   with the new context (preserve existing content) and use it.\n"
        f"2. If no matching task: create a new T-NNNN-<slug>.md backlog file with\n"
        f"   the stakeholder's instructions verbatim as the body. Set status: open.\n"
        f"3. Spawn a dev teammate via Claude Code's native agent-teams feature.\n"
        f"   Name the window after the feature.\n"
        f"4. Brief the teammate on the chosen task."
    )


@dev_spawn_router.post("/dev-spawn-request")
async def dev_spawn_request(
    slug: str, body: DevSpawnRequest, request: Request,
) -> dict:
    """Delegate a dev-worker spawn to a teamlead via the peer message bus.

    The UI never spawns dev sessions directly — it asks a TL to spawn one
    using Claude Code's native agent-teams feature. This endpoint validates
    the target TL and forwards a structured instruction message.
    """
    _check_project(request, slug)
    cfg = request.app.state.api_config
    if not body.tl_sid.strip():
        raise HTTPException(status_code=400, detail="tl_sid is required")
    if not body.instructions.strip():
        raise HTTPException(status_code=400, detail="instructions are required")

    # Validate the target session: must be an active teamlead in this project.
    sessions_dir = Path(cfg.data_dir) / slug / "sessions"
    meta = _read_session_meta(sessions_dir, body.tl_sid)
    if meta is None:
        raise HTTPException(status_code=400, detail=f"unknown session: {body.tl_sid}")
    tid = meta.get("task_id", "") or ""
    if tid and tid != "~":
        raise HTTPException(status_code=400, detail="target session is a dev worker, not a teamlead")
    if meta.get("status", "") != "active":
        raise HTTPException(status_code=400, detail="target teamlead is not active")

    text = _compose_tl_message(slug, body.task_id, body.instructions)
    # peer_send is coordinator-tagged (the inbox lives on the coordinator).
    client = _router(request).coordinator()
    try:
        result = await client.call_action("peer_send", {
            "slug": slug,
            "from_sid": "stakeholder",
            "to": body.tl_sid,
            "text": text,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "delivered_to": result.get("delivered_to", [body.tl_sid])}
