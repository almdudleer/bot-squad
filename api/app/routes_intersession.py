"""Cross-session message bus endpoints — proxy to worker peer_* actions.

These are HTTP long-polls: ``/wait`` will hold the connection open for up
to ``timeout`` seconds (capped at 1800 in the worker). The httpx client
in :class:`WorkerClient` overrides its 5s default for this route so the
upstream call doesn't time out before the worker returns.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.routes_auth import require_auth
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/peer",
    tags=["peer"],
    dependencies=[Depends(require_auth)],
)


def _worker(request: Request) -> WorkerClient:
    return request.app.state.worker_router.coordinator()


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


class SendBody(BaseModel):
    from_sid: str
    to: str
    text: str


class WaitBody(BaseModel):
    timeout: float = 1800


@router.post("/send")
async def peer_send(slug: str, body: SendBody, request: Request, user: dict = Depends(require_auth)) -> dict:
    """Forward a message to ``to`` (SID or role keyword).

    Lightly validates that ``from_sid`` belongs to the logged-in user
    (parsed out of the ``S-<user>-...`` prefix). This is informational —
    the from-sid is metadata, not an auth boundary — but a quick check
    keeps casual cross-user impersonation out of the inbox log.
    """
    _check_project(request, slug)
    if not body.from_sid.startswith("S-"):
        raise HTTPException(status_code=400, detail="from_sid must look like S-<user>-...")
    parts = body.from_sid.split("-")
    if len(parts) >= 2:
        claimed_user = parts[1]
        actual_user = (user or {}).get("username", "")
        if claimed_user and actual_user and claimed_user != actual_user:
            # Allow `stakeholder` as a recognised broadcast sender from the UI.
            if claimed_user != "stakeholder":
                raise HTTPException(
                    status_code=403,
                    detail=f"from_sid user {claimed_user!r} does not match logged-in user",
                )
    client = _worker(request)
    try:
        return await client.call_action("peer_send", {
            "slug": slug,
            "from_sid": body.from_sid,
            "to": body.to,
            "text": body.text,
        })
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{sid}/read")
async def peer_inbox_read(slug: str, sid: str, request: Request) -> dict:
    """Drain new inbox messages since the last read."""
    _check_project(request, slug)
    client = _worker(request)
    try:
        return await client.call_action("peer_inbox_read", {"slug": slug, "sid": sid})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{sid}/wait")
async def peer_inbox_wait(slug: str, sid: str, body: WaitBody, request: Request) -> dict:
    """Long-poll until inbox grows past the seen offset, or timeout (<=1800s).

    Uses a dedicated long-timeout httpx client so the proxy hop doesn't drop
    the connection before the worker returns.
    """
    _check_project(request, slug)
    timeout = max(0.0, min(float(body.timeout), 1800.0))
    # peer_inbox_wait is coordinator-only — inbox lives there.
    sock_path = request.app.state.worker_router.coordinator().sock_path
    transport = httpx.AsyncHTTPTransport(uds=str(sock_path))
    # Worker may block for `timeout` seconds; allow a generous tail.
    httpx_timeout = httpx.Timeout(timeout + 30, connect=5)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://w", timeout=httpx_timeout) as c:
            r = await c.post(
                "/actions/peer_inbox_wait",
                json={"slug": slug, "sid": sid, "timeout": timeout},
            )
            if r.status_code != 200:
                detail = r.json().get("detail", r.text) if r.content else r.text
                raise HTTPException(status_code=502, detail=f"worker rejected peer_inbox_wait: {detail}")
            return r.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"worker request error for peer_inbox_wait: {e}")
