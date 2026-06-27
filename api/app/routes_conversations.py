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

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import conversation_store as CS
from app.routes_auth import require_auth
from app.routes_mothership import _authenticate_worker

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
