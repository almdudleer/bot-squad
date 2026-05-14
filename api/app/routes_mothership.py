"""Mothership centralization-layer routes.

Mounted only when the ``MOTHERSHIP`` env var is ``"1"`` (see ``main.py``).
Detach build: delete this file + ``mothership_store.py`` + ``install_tokens.py``
+ ``web/src/mothership/``, rebuild — no other code references the
centralization layer.

This module ships THREE routers because the install flow has three
distinct auth surfaces:

- ``router`` — cookie auth (session). Mounted at ``/api/m``. The
  logged-in bot-squad.org user creates servers, lists them, watches a
  server's install progress over SSE.
- ``installer_router`` — bearer auth via the install_token (pre-/connect)
  or the server_bearer (post-/connect). Mounted at ``/api/m``. Called by
  the installer script on a fresh box, which has no session cookie.
- ``bundle_router`` — no auth (token is in the URL path). Mounted at
  ``/`` (root). Serves the substituted ``install.sh`` + bootstrap-claude
  ``instructions.md`` to the very-first ``curl`` on a fresh box, which
  has no credentials at all yet.

Contract is fixed in ``vision/architecture/mothership-seam.md`` — read
that before touching the wire shapes.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.install_tokens import (
    INSTALL_PREFIX,
    SERVER_PREFIX,
    hash_token,
    is_install_token,
    is_server_bearer,
)
from app.mothership_store import MothershipStore
from app.routes_auth import require_auth


# ---- routers ----------------------------------------------------------------
# Cookie-auth surface for the logged-in mothership user.
router = APIRouter(tags=["mothership"], dependencies=[Depends(require_auth)])
# Bearer-auth surface for the installer script (no session cookie).
installer_router = APIRouter(tags=["mothership-installer"])
# No-auth bundle GETs — token in the URL path is the auth.
bundle_router = APIRouter(tags=["mothership-bundle"])


def _store(request: Request) -> MothershipStore:
    cfg = request.app.state.api_config
    return MothershipStore(cfg.data_dir / "_mothership")


def _mothership_base_url(request: Request) -> str:
    """Public URL the install bundle should point installers back at.

    Set via the ``MOTHERSHIP_BASE_URL`` env var at deploy time. We never
    derive this from the incoming request's ``Host`` header — that would
    embed whatever the upstream proxy sent (incl. private hostnames) into
    install URLs that have to work from a fresh box on the public
    internet.
    """
    base = os.environ.get("MOTHERSHIP_BASE_URL", "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=500,
            detail="MOTHERSHIP_BASE_URL not configured on this mothership",
        )
    return base


def _bundle_dir() -> Path:
    return Path(
        os.environ.get(
            "INSTALL_BUNDLE_DIR",
            "/app/scripts/install",
        )
    )


# ---- live broadcast ---------------------------------------------------------
# In-process fan-out keyed by server_id. SSE handlers register a queue on
# subscribe and drain it as events arrive; POST /installer/checkpoint
# pushes to every queue for that server_id after persisting to the JSONL
# log on disk. Disk is the source of truth (used for the replay-on-
# reconnect path); the broadcast is just the cheap live path.
_subscribers: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)


@contextlib.asynccontextmanager
async def _subscribe(server_id: str) -> AsyncIterator[asyncio.Queue[dict]]:
    q: asyncio.Queue[dict] = asyncio.Queue()
    _subscribers[server_id].add(q)
    try:
        yield q
    finally:
        _subscribers[server_id].discard(q)


def _broadcast(server_id: str, event: dict) -> None:
    for q in list(_subscribers.get(server_id, ())):
        # asyncio.Queue is unbounded by default — put_nowait can only fail
        # if a maxsize was set, which we don't do.
        q.put_nowait(event)


# ---- cookie-auth: list, create, watch ---------------------------------------


@router.get("/servers")
def list_servers(request: Request) -> list[dict]:
    return [s.to_public() for s in _store(request).list_servers()]


@router.post("/servers")
def create_server(
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    display_name = (payload.get("display_name") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    if not display_name or not base_url:
        raise HTTPException(
            status_code=400, detail="display_name and base_url are required"
        )
    entry, token = _store(request).register_server(
        display_name=display_name,
        base_url=base_url,
        owner_user=user["username"],
    )
    base = _mothership_base_url(request)
    install_url = f"{base}/i/{token}/install.sh"
    return {
        "id": entry.id,
        "install_token": token,
        "install_url": install_url,
        "instructions_url": f"{base}/i/{token}/instructions.md",
        "expires_at": entry.install_token_expires_at,
    }


@router.get("/servers/{server_id}/checkpoints")
async def checkpoints_stream(server_id: str, request: Request) -> StreamingResponse:
    """SSE: replay the persisted log, then forward live events.

    The wizard UI calls this immediately after the mothership returns the
    install_token from POST /api/m/servers, and again on reconnect when
    the user closes + re-opens the tab. The replay-then-live ordering is
    what makes reconnect trivial — the client doesn't track its position.
    """
    store = _store(request)
    server = store.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")

    async def _gen() -> AsyncIterator[bytes]:
        # Subscribe BEFORE replay. A POST /installer/checkpoint that
        # lands while we're walking the disk log appends to disk AND
        # broadcasts. If we subscribed only after the replay, the
        # broadcast would find no subscribers and the event would be
        # dropped for this session. Subscribing first means the queue
        # captures any in-flight events; we then walk disk + drain the
        # queue afterwards. Cost: one possible duplicate (event read
        # from disk AND read from queue) — fine for installer progress
        # events; the client just renders the same checkpoint state
        # twice.
        async with _subscribe(server_id) as q:
            # 1) Replay everything currently on disk, in append order.
            for event in store.read_checkpoints(server_id):
                yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
            # End-of-replay marker. EventSource ignores ": ..." comment
            # lines but a test client can use it as a deterministic stop
            # point without depending on a wall-clock timeout.
            yield b": replay-complete\n\n"
            # 2) Drain the queue forever.
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    # SSE keepalive: comment lines (": ...") are ignored
                    # by EventSource but keep the TCP path warm against
                    # idle-timeouts in any intermediate proxy. Polling
                    # at 0.5s also lets the disconnect check above pick
                    # up a closed connection within ~0.5s instead of 15s.
                    yield b": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
        },
    )


# ---- installer surface: bearer auth -----------------------------------------


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return auth.split(" ", 1)[1].strip()


def _authenticate_installer(request: Request) -> tuple[str, str]:
    """Resolve the installer's bearer to a server_id.

    Accepts BOTH the install_token (pre-/connect) and the server_bearer
    (post-/connect). Returns ``(server_id, bearer_kind)`` where
    ``bearer_kind`` is one of ``"install_token"`` / ``"server_bearer"`` so
    the caller can enforce the rotation rule.

    The rotation rule (see ``mothership-seam.md``): an install_token is
    rejected by /checkpoint after /connect has burned it — even though
    the plaintext still exists on the installer host, the registry no
    longer has a hash to match it against, so this fall-through to 401
    is automatic, no explicit check needed.
    """
    bearer = _extract_bearer(request)
    store = _store(request)
    if is_install_token(bearer):
        server = store.server_by_install_token(bearer)
        if server is None:
            raise HTTPException(status_code=401, detail="invalid bearer")
        return server.id, "install_token"
    if is_server_bearer(bearer):
        server = store.server_by_bearer(bearer)
        if server is None:
            raise HTTPException(status_code=401, detail="invalid bearer")
        return server.id, "server_bearer"
    raise HTTPException(status_code=401, detail="invalid bearer")


@installer_router.post("/installer/connect")
def installer_connect(request: Request, payload: dict) -> dict:
    """Burn the install_token, mint + return the server_bearer.

    The single state transition for a pending server: ``pending →
    connected``. 410 covers all the unhappy paths (unknown / expired /
    already burned) so the installer's structured "what + how" message
    can key off it verbatim.
    """
    token = (payload.get("token") or "").strip()
    if not token or not is_install_token(token):
        raise HTTPException(status_code=400, detail="install token required")
    result = _store(request).consume_install_token(
        token, payload.get("server_meta") or {}
    )
    if result is None:
        raise HTTPException(status_code=410, detail="install token gone")
    server, bearer = result
    return {
        "server_id": server.id,
        "server_bearer": bearer,
    }


@installer_router.post("/installer/checkpoint")
def installer_checkpoint(
    request: Request,
    payload: dict,
) -> Response:
    server_id, _kind = _authenticate_installer(request)
    checkpoint = (payload.get("checkpoint") or "").strip()
    status = (payload.get("status") or "").strip()
    if not checkpoint or not status:
        raise HTTPException(status_code=400, detail="checkpoint and status required")
    event = {
        "checkpoint": checkpoint,
        "status": status,
        "hostname": payload.get("hostname"),
        "ts": payload.get("ts"),
    }
    store = _store(request)
    stamped = store.append_checkpoint(server_id, event)
    store.touch_last_seen(server_id)
    _broadcast(server_id, stamped)
    return Response(status_code=204)


# ---- install bundle: no auth, no burn ---------------------------------------


def _validate_bundle_token(request: Request, token: str) -> None:
    if not is_install_token(token):
        raise HTTPException(status_code=410, detail="install token gone")
    if _store(request).server_by_install_token(token) is None:
        raise HTTPException(status_code=410, detail="install token gone")


def _substitute_bundle(text: str, *, token: str, mothership_base: str) -> str:
    clone_url = os.environ.get("BOTSQUAD_CLONE_URL", "")
    repo_ref = os.environ.get("BOTSQUAD_REPO_REF", "master")
    return (
        text.replace("__INSTALL_TOKEN__", token)
        .replace("__MOTHERSHIP_URL__", mothership_base)
        .replace("__CLONE_URL__", clone_url)
        .replace("__REPO_REF__", repo_ref)
    )


@bundle_router.get("/i/{token}/install.sh")
def install_sh(request: Request, token: str) -> PlainTextResponse:
    _validate_bundle_token(request, token)
    path = _bundle_dir() / "install.sh"
    if not path.is_file():
        raise HTTPException(status_code=500, detail="install bundle missing")
    body = _substitute_bundle(
        path.read_text(encoding="utf-8"),
        token=token,
        mothership_base=_mothership_base_url(request),
    )
    return PlainTextResponse(body, media_type="text/x-shellscript")


@bundle_router.get("/i/{token}/instructions.md")
def install_instructions(request: Request, token: str) -> PlainTextResponse:
    _validate_bundle_token(request, token)
    path = _bundle_dir() / "bootstrap-claude-instructions.md"
    if not path.is_file():
        raise HTTPException(status_code=500, detail="install bundle missing")
    body = _substitute_bundle(
        path.read_text(encoding="utf-8"),
        token=token,
        mothership_base=_mothership_base_url(request),
    )
    return PlainTextResponse(body, media_type="text/markdown")


# Touch unused imports to silence linters — these are part of the
# install-token contract surface and stay imported even if a future
# refactor of this file stops using them directly.
_ = (INSTALL_PREFIX, SERVER_PREFIX, hash_token)


# ---- per-server proxy (T-0023) ---------------------------------------------
# The mothership FE renders per-project pages by talking to the *target
# server's* API. ``/api/m/servers/{id}/api/{rest:path}`` forwards each call
# to ``<server.base_url>/api/<rest>`` using the bearer-sidecar plaintext
# (the ``server_bearer`` plaintext that ``/connect`` minted and stored at
# ``DATA_DIR/_mothership/bearers/<id>``). See "Per-server backend client
# (T-0023)" in ``mothership-seam.md`` — proxy variant.


def _proxy_client(base_url: str) -> httpx.AsyncClient:
    """httpx.AsyncClient factory the proxy route uses. Lives at module scope
    so tests can monkeypatch it to point at an ``httpx.MockTransport``
    instead of opening a real socket to ``base_url``."""
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
    )


@router.get("/servers/{server_id}/projects")
def list_server_projects(server_id: str, request: Request) -> list[dict]:
    """Return the registry's cached project list for ``server_id``.

    Cheap, no upstream call. T-0025's all-projects page polls this for
    every attached server in parallel — partial-failure isolation is
    handled FE-side via the fanOut envelope contract.
    """
    server = _store(request).get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return server.projects_cache


@router.post("/servers/{server_id}/projects/refresh")
async def refresh_server_projects(server_id: str, request: Request) -> list[dict]:
    """Force-refresh the projects cache by hitting upstream ``/api/projects``.

    On upstream failure (502), the existing cache is preserved — a
    transient outage shouldn't blank the last-known-good state. The
    eventual ``last_seen`` ping channel will refresh asynchronously; this
    endpoint is the manual handle the UI's "refresh" button hits.
    """
    store = _store(request)
    server = store.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    bearer = store.read_server_bearer(server_id)
    if not bearer:
        raise HTTPException(
            status_code=503,
            detail="server bearer not available (server not connected)",
        )
    async with _proxy_client(server.base_url) as client:
        try:
            upstream = await client.get(
                "/api/projects",
                headers={"Authorization": f"Bearer {bearer}"},
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"upstream unreachable: {e}")
    if upstream.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"upstream returned {upstream.status_code}",
        )
    try:
        listing = upstream.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"upstream returned non-JSON: {e}")
    if not isinstance(listing, list):
        raise HTTPException(status_code=502, detail="upstream /api/projects must return a list")
    persisted = store.update_projects_cache(server_id, listing)
    if persisted is None:
        # Race: server was removed between the get_server check and the
        # cache update. Treat as 404 — the FE will re-fetch the server list.
        raise HTTPException(status_code=404, detail="server not found")
    return persisted


_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


@router.api_route("/servers/{server_id}/api/{rest:path}", methods=_PROXY_METHODS)
async def server_api_proxy(server_id: str, rest: str, request: Request) -> Response:
    store = _store(request)
    server = store.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    bearer = store.read_server_bearer(server_id)
    if not bearer:
        raise HTTPException(
            status_code=503,
            detail="server bearer not available (server not connected)",
        )
    upstream_path = f"/api/{rest}"
    # Forward the request body verbatim. The FE / single-install API only
    # ever uses JSON bodies, but we don't decode here — pass-through keeps
    # the proxy method-agnostic and avoids round-trip encoding bugs.
    body = await request.body()
    headers: dict[str, str] = {"Authorization": f"Bearer {bearer}"}
    ctype = request.headers.get("content-type")
    if ctype:
        headers["Content-Type"] = ctype
    async with _proxy_client(server.base_url) as client:
        try:
            upstream = await client.request(
                request.method,
                upstream_path,
                content=body or None,
                headers=headers,
                params=request.query_params,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"upstream unreachable: {e}")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


