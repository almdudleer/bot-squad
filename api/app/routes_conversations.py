"""TG conversation history endpoints (T-0489).

Two auth surfaces over the per-(project, user) conversation thread
(``conversation_store``):

- ``worker_router`` — ``POST /conversations/{slug}/{gid}/messages``. The worker
  (tg_listener) records each inbound TG user message here. Token-gated by the
  shared-secret ``WORKER_API_TOKEN`` (reusing T-0488's ``_authenticate_worker``,
  the established worker->API trust path); fails closed when unset.
- ``router`` — ``GET /conversations/{slug}/{gid}/messages``. Session-auth
  list/search (paginated) — the durable lookup surface an attending session uses
  to review the thread.

Mounted only on the MOTHERSHIP build (see ``main.py``): the bot's
user-communication module is centralized on the mothership (voice-04), and the
``global_user_id`` key is a mothership identity (T-0488).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import conversation_store as CS
from app import pins_store
from app.routes_auth import require_auth
from app.routes_mothership import _authenticate_worker


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Session-auth read surface.
router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(require_auth)],
)
# Worker-token write surface (no session cookie); token checked per-handler.
worker_router = APIRouter(prefix="/conversations", tags=["conversations-worker"])


def _data_dir(request: Request):
    return request.app.state.api_config.data_dir


@worker_router.post("/{slug}/{global_user_id}/messages")
def append_message(slug: str, global_user_id: str, request: Request, payload: dict) -> dict:
    """Append one message to the (slug, global_user_id) thread. Worker-only.

    Body: ``{author, text, attachments?, timestamp?}`` (``text`` required —
    empty string is allowed, but the key must be present). Returns the stored
    record."""
    _authenticate_worker(request)
    if "text" not in payload:
        raise HTTPException(status_code=400, detail="text required")
    try:
        record = CS.append(
            _data_dir(request),
            slug,
            global_user_id,
            author=str(payload.get("author") or "user"),
            text=payload.get("text"),
            attachments=payload.get("attachments"),
            timestamp=payload.get("timestamp"),
        )
    except ValueError as e:
        # An unsafe slug / global_user_id segment.
        raise HTTPException(status_code=400, detail=str(e))
    return record


# ---- T-0492: per-(user, server) current-project routing (worker-token) -------
# The same user-communication module owns conversation history AND the hardwired
# routing of unquoted messages to a user's pinned project (voice-04). The worker
# reads/sets the pin through these endpoints; single-writer = API (pins_store).


@worker_router.post("/routing/{global_user_id}/current-project")
def set_current_project(global_user_id: str, request: Request, payload: dict) -> dict:
    """Pin (or switch) the user's current project. Worker-only. ``slug`` must be
    a known project (validated against the registry, so a typo can't strand the
    user on a non-existent project). Returns the stored ``{slug, at}`` record."""
    _authenticate_worker(request)
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=400, detail=f"unknown project: {slug}")
    return pins_store.set_current_project(cfg.data_dir, global_user_id, slug, at=_now_iso())


@worker_router.get("/routing/{global_user_id}/current-project")
def get_current_project(global_user_id: str, request: Request) -> dict:
    """The user's current pinned project slug (``null`` when unset). Worker-only."""
    _authenticate_worker(request)
    cfg = request.app.state.api_config
    return {"slug": pins_store.get_current_project(cfg.data_dir, global_user_id)}


@router.get("/{slug}/{global_user_id}/messages")
def list_conversation(
    slug: str,
    global_user_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
) -> dict:
    """Paginated thread lookup. With ``q`` set, returns only records whose text
    contains it (case-insensitive); otherwise the full chronological thread."""
    try:
        if q:
            return CS.search(_data_dir(request), slug, global_user_id, q, limit=limit, offset=offset)
        return CS.list_messages(_data_dir(request), slug, global_user_id, limit=limit, offset=offset)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
