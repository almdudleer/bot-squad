"""Session management endpoints — proxy to worker actions.

Phase 2 multi-user: session ops route through the WorkerRouter:
  - list_sessions fans out across all user workers (timeout-bounded).
  - spawn lands on the caller's own user worker.
  - pause/suspend/resume route by SID (S-<linux_user>-…) and enforce that
    non-admin callers can only act on sessions in their own linux_user.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import pins_store
from app.frontmatter import parse_or_none
from app.project_authz import require_project_member
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


def _data_dir(request: Request) -> Path:
    return request.app.state.api_config.data_dir


def _read_session_owner(data_dir: Path, slug: str, sid: str) -> str | None:
    """Read the SessionMd `owner:` field. Returns None if no md or no field.

    T-0080: owner is the UI username (JWT username claim) stamped at spawn
    time. Legacy sessions written before T-0080 have no owner field; we
    treat them as admin-only (callers handle the None case).
    """
    md = data_dir / slug / "sessions" / f"{sid}.md"
    if not md.exists():
        return None
    try:
        text = md.read_text()
    except OSError:
        return None
    parsed = parse_or_none(text)  # T-0075: shared parser
    if parsed is None:
        return None
    owner = parsed[0].get("owner")
    if owner is None or owner == "~" or owner == "":
        return None
    return str(owner)


def _read_session_owner_user(data_dir: Path, slug: str, sid: str) -> str | None:
    """Read the SessionMd `owner_user:` field — the human UI username for
    per-user scoping (T-0321), distinct from `owner` (the constant-team/TL-SID
    binding sentinel). None if no md or no field (legacy → fall back to owner)."""
    md = data_dir / slug / "sessions" / f"{sid}.md"
    if not md.exists():
        return None
    try:
        text = md.read_text()
    except OSError:
        return None
    parsed = parse_or_none(text)
    if parsed is None:
        return None
    ou = parsed[0].get("owner_user")
    if ou is None or ou == "~" or ou == "":
        return None
    return str(ou)


def _scope_match(owner_user: str | None, owner: str | None, me: str) -> bool:
    """T-0321: is this session the caller's? ``owner_user`` (the username) is
    authoritative when set; legacy sessions without it fall back to ``owner``."""
    if owner_user:
        return owner_user == me
    return (owner or "") == me


def _check_sid_ownership(sid: str, user: dict, router: WorkerRouter,
                         data_dir: Path | None = None, slug: str | None = None) -> None:
    """Non-admins can only act on SIDs they own.

    T-0080: ownership precedence is (1) SessionMd `owner` field equals the
    caller's UI username, otherwise (2) SID's linux_user prefix equals the
    caller's linux_user (legacy fallback for sessions spawned before owner
    stamping landed). Admins bypass both checks.
    """
    if user.get("is_admin"):
        return
    # (0) T-0321: owner_user (the dedicated per-user-scoping username) is the
    # authoritative match when present. Sessions stamped before T-0321 have no
    # owner_user → fall through to the legacy `owner`-based checks below so
    # nothing locks out.
    if data_dir is not None and slug:
        owner_user = _read_session_owner_user(data_dir, slug, sid)
        if owner_user is not None:
            if owner_user == user.get("username"):
                return
            raise HTTPException(
                status_code=403,
                detail=f"session {sid!r} is owned by {owner_user!r}; "
                       f"you are {user.get('username')!r}",
            )
    # (1) SessionMd owner field — authoritative when set.
    if data_dir is not None and slug:
        md_owner = _read_session_owner(data_dir, slug, sid)
        if md_owner is not None:
            if md_owner == user.get("username"):
                return
            # T-0135: TLs spawn devs with owner=<TL-SID>; allow the TL's own
            # human owner to act on those devs by resolving the TL-SID's md
            # `owner` field one hop deeper.
            if md_owner.startswith("S-"):
                tl_owner = _read_session_owner(data_dir, slug, md_owner)
                if tl_owner == user.get("username"):
                    return
            raise HTTPException(
                status_code=403,
                detail=f"session {sid!r} is owned by {md_owner!r}; "
                       f"you are {user.get('username')!r}",
            )
    # (2) Legacy SID-prefix check.
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

# T-0232: live = the session is alive in tmux (running or idle). paused +
# suspended (and any archived, which surfaces as suspended) are NOT live and
# drop out of the FE live-only sessions view (Pillar A of the reframe).
_LIVE_ACTIVITY = {"running", "idle"}


def _is_live(activity: object) -> bool:
    return activity in _LIVE_ACTIVITY


@router.get("")
async def list_sessions(
    slug: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """List all Claude sessions (active + paused) for a project.

    Fans out across every configured user worker. Each call is bounded
    by a 5 s timeout so a dead/slow user worker can't block the whole list.
    Results are deduped by sid.

    T-0601 (F5): per-socket fan-out failures are no longer silently
    swallowed — the response is ``{"sessions": [...], "errors":
    [{"user", "detail"}, ...]}`` so the UI can render the partial list
    plus a warning naming the unreachable user workers instead of a
    misleading "No sessions". Row shape is unchanged; only the top-level
    envelope is new (the web client accepts both shapes for old servers).
    Non-admin callers only see errors for their own linux_user (matching
    the row ownership scoping below).

    T-0080 owner gate: non-admin callers see only sessions whose SessionMd
    `owner` field equals their UI username. Sessions written before owner
    stamping landed have no owner — they are treated as admin-only so they
    don't leak to a second user. Admins still see every row.

    T-0104 activity status: each row also carries a worker-derived
    ``activity`` enum (``running|idle|paused|suspended``) and an
    ``activity_at`` epoch float. Frontends display labels off ``activity``,
    not the raw md ``status``, so a zombie session with ``status: active``
    surfaces as ``activity: suspended`` and a live-but-quiet pane surfaces
    as ``idle`` rather than ``running``. The derivation lives in the
    worker (``_pane_activity_at`` / ``_derive_activity`` in
    ``worker/bot_squad_worker/sessions.py``); this endpoint is a
    pass-through.
    """
    _check_project(request, slug)
    wrouter = _router(request)

    # T-0601 (F5): collect per-socket failures instead of dropping them.
    errors: list[dict] = []

    async def _one(client: WorkerClient, who: str) -> list[dict]:
        try:
            result = await client.call_action(
                "list_sessions", {"slug": slug}, timeout=5.0,
            )
            return result.get("sessions", [])
        except WorkerError as e:
            log.warning("list_sessions fan-out for %s failed: %s", who, e)
            errors.append({"user": who, "detail": str(e)})
            return []
        except Exception as e:
            log.warning("list_sessions fan-out for %s crashed: %s", who, e)
            errors.append({"user": who, "detail": str(e)})
            return []

    pairs = wrouter.all_user_workers()
    results = await asyncio.gather(*[_one(c, u) for (u, c) in pairs])
    merged: dict[str, dict] = {}
    for batch in results:
        for row in batch:
            sid = row.get("sid")
            if sid and sid not in merged:
                merged[sid] = row

    rows = list(merged.values())
    # T-0437: stamp the per-project pin signal onto each row so the Processes
    # view can surface pinned sessions distinctly and the Board card can show a
    # 📌 marker — both read off this ONE list (no extra fetch). Pins are an
    # API-side store; the worker doesn't know about them.
    pins = pins_store.load(_data_dir(request), slug)
    for r in rows:
        r["live"] = _is_live(r.get("activity"))  # T-0232
        meta = pins.get(r.get("sid"))
        r["pinned"] = meta is not None
        if meta is not None:
            r["pinned_by"] = meta.get("by")
            r["pinned_at"] = meta.get("at")
    if user.get("is_admin"):
        return {"sessions": rows, "errors": errors}
    # Non-admin: drop rows whose owner doesn't match. Missing owner
    # (legacy session) = admin-only. The worker emits "" for missing.
    # Errors follow the same scoping: a non-admin sees only their OWN
    # worker's failure (that IS their blank-list explanation), not the
    # health of other users' sockets.
    me = user.get("username") or ""
    my_linux = request.app.state.auth_config.meta_for(me).linux_user
    return {
        "sessions": [r for r in rows if _scope_match(r.get("owner_user"), r.get("owner"), me)],
        "errors": [e for e in errors if e.get("user") == my_linux],
    }


@dev_spawn_router.get("/telemetry")
async def get_telemetry(
    slug: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """T-0210: resource telemetry for a project (context/memory/quota).

    Reads the records the worker's ``telemetry_tick`` sampler persists. The
    records live in the SHARED install data dir (every user worker writes
    there), so this is a single coordinator read — not a per-user fan-out.

    Non-admin callers see only the sessions they own (same owner gate as the
    sessions list); the project-level ``quota`` rollup is shown to everyone
    (it carries no per-user secrets — just burn rate, projection, 429 flag).
    """
    _check_project(request, slug)
    client = _router(request).coordinator()
    try:
        result = await client.call_action("telemetry_get", {"slug": slug}, timeout=5.0)
    except WorkerError as e:
        log.warning("telemetry_get for %s failed: %s", slug, e)
        return {"sessions": [], "quota": {}, "caps": {}}
    except Exception as e:
        log.warning("telemetry_get for %s crashed: %s", slug, e)
        return {"sessions": [], "quota": {}, "caps": {}}

    sessions = result.get("sessions", [])
    quota = result.get("quota", {})
    # T-0335 items 7 + 22: enforced caps utilization (system-wide cap config +
    # live counts + token totals — no per-user secret, same as quota), shown to
    # everyone so the UI meter measures the number spawn actually enforces.
    caps = result.get("caps", {})
    if user.get("is_admin"):
        return {"sessions": sessions, "quota": quota, "caps": caps}
    # Non-admin owner gate: telemetry records don't carry the UI `owner`, so
    # join back to the SessionMd owner by sid (same rule as list_sessions —
    # missing owner = admin-only).
    data_dir = _data_dir(request)
    me = user.get("username") or ""
    visible = [
        s for s in sessions
        if _scope_match(
            _read_session_owner_user(data_dir, slug, s.get("sid", "")),
            _read_session_owner(data_dir, slug, s.get("sid", "")),
            me,
        )
    ]
    return {"sessions": visible, "quota": quota, "caps": caps}


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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
    client = wrouter.for_sid(sid)
    try:
        return await client.call_action("pause_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST / DELETE /api/projects/{slug}/sessions/{sid}/pin  (T-0437)
# ---------------------------------------------------------------------------
#
# Pin = a user signal ("I'm working closely with this session"). It is a hint
# surfaced in the UI; it does NOT change orchestration. Pins are per-project
# (the brain is per-project) and stored API-side (pins_store) — no worker hop.
#
# Authz: a pin mutates per-project state, so it goes through the project-WRITE
# SSOT (require_project_member, T-0381) — admin-only today, same as every other
# project write. Reads (the pinned flag on the sessions list) stay broad.

@router.post("/{sid}/pin")
async def pin_session(
    slug: str, sid: str, request: Request,
    user: dict = Depends(require_project_member),
) -> dict:
    """Pin a session within the project. Idempotent."""
    _check_project(request, slug)
    at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = pins_store.pin(
        _data_dir(request), slug, sid, by=user.get("username") or "", at=at,
    )
    return {"ok": True, "sid": sid, "pinned": True,
            "pinned_by": meta["by"], "pinned_at": meta["at"]}


@router.delete("/{sid}/pin")
async def unpin_session(
    slug: str, sid: str, request: Request,
    user: dict = Depends(require_project_member),
) -> dict:
    """Unpin a session within the project. Idempotent."""
    _check_project(request, slug)
    was_pinned = pins_store.unpin(_data_dir(request), slug, sid)
    return {"ok": True, "sid": sid, "pinned": False, "was_pinned": was_pinned}


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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
    client = wrouter.for_sid(sid)
    try:
        return await client.call_action("suspend_session", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{slug}/sessions/{sid}/resume
# ---------------------------------------------------------------------------

class ResumeRequest(BaseModel):
    # T-0407: thread the reused task straight through resume so a session
    # adopts it as PRIMARY in ONE round-trip. The worker resume action adopts an
    # EMPTY primary as task_id (T-0166) and delivers initial_prompt as the brief;
    # both optional, so a bare resume (no body) behaves exactly as before.
    task_id: Optional[str] = None
    initial_prompt: Optional[str] = None


@router.post("/{sid}/resume")
async def resume_session(
    slug: str, sid: str, request: Request,
    body: ResumeRequest = ResumeRequest(),
    user: dict = Depends(require_auth),
) -> dict:
    """Resume a paused or suspended Claude session.

    For paused with live pane: clears paused flag.
    For suspended or zombie: spawns new tmux window with ``claude --resume``.

    T-0407: an optional ``task_id`` (+ ``initial_prompt`` brief) is forwarded to
    the worker, which adopts an empty primary as that task (T-0166) — so reusing
    a session onto a new task is one round-trip with the task as PRIMARY, instead
    of a resume + a separate bind_task that only appended to extra_task_ids.
    """
    _check_project(request, slug)
    wrouter = _router(request)
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
    params: dict = {"slug": slug, "sid": sid}
    if body.task_id:
        params["task_id"] = body.task_id
    if body.initial_prompt:
        params["initial_prompt"] = body.initial_prompt
    client = wrouter.for_sid(sid)
    try:
        return await client.call_action("resume_session", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/projects/{slug}/sessions/reuse-candidates?task=T-NNNN
# ---------------------------------------------------------------------------

@router.get("/reuse-candidates")
async def reuse_candidates(
    slug: str, task: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """T-0280: surface the worker's reuse-vs-spawn recommendation for a task.

    Thin proxy of the worker ``dispatch_decision`` action (T-0237 Layer-2).
    Given a backlog ``task`` it returns the recommendation record
    ``{ok, task_id, task_initiative, decision: 'reuse'|'spawn', target_sid,
    reason, candidates: [{sid, role, live, idle, initiative_match,
    context_pct, eligible, reject}]}`` so the New-session modal can offer
    "resume before spawn". The worker derives the task's initiative itself,
    so there is no separate initiative param. Advisory only — this never
    spawns or resumes; the operator acts via the existing resume / spawn
    endpoints.

    Reads across all of the project's session mds (the shared data dir), so
    this is a single coordinator read — not a per-user fan-out (same shape as
    /telemetry).
    """
    _check_project(request, slug)
    if not task.strip():
        raise HTTPException(status_code=400, detail="task query param must not be empty")
    client = _router(request).coordinator()
    try:
        return await client.call_action(
            "dispatch_decision", {"slug": slug, "task_id": task}, timeout=5.0,
        )
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
    user: dict = Depends(require_project_member),  # T-0381: spawns an agent (cost) — project-write gate
) -> dict:
    """Spawn a new Claude session in the project's repo.

    Spawned in the logged-in user's own tmux server (linux_user from auth).
    Optional `task_id` rides the per-process ``BOT_SQUAD_TASK_ID`` env to the
    new claude (T-0525; the shared ``.claude/task_id`` marker is retired and
    T-0324 removed its last reader) so the SessionStart hook links the new
    session to that backlog task.
    """
    _check_project(request, slug)
    if not body.window.strip():
        raise HTTPException(status_code=400, detail="window name must not be empty")

    wrouter = _router(request)
    client = wrouter.for_user(user["linux_user"])
    # T-0080: always stamp the caller's UI username onto the spawned
    # session md so per-user listing filters scope correctly even when
    # multiple UI users share a linux_user (e.g. when WorkerRouter falls
    # back to the coordinator socket for a user without their own worker).
    params: dict = {
        "slug": slug,
        "window": body.window,
        "owner": user["username"],
        # T-0321: dedicated per-user-scoping username (owner stays for the
        # constant-team/binding sentinel; for a direct UI spawn both are the
        # caller's username).
        "owner_user": user["username"],
    }
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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
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
    _check_sid_ownership(sid, user, wrouter, _data_dir(request), slug)
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
    """Read session md frontmatter via the shared parser (T-0075)."""
    p = sessions_dir / f"{sid}.md"
    if not p.exists():
        return None
    parsed = parse_or_none(p.read_text())
    return parsed[0] if parsed is not None else None


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
    _perm: dict = Depends(require_project_member),  # T-0381: triggers agent spawn — project-write gate
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
