"""Mothership centralization-layer routes.

Mounted only when the ``MOTHERSHIP`` env var is ``"1"`` (see ``main.py``).
Detach build: delete this file + ``mothership_store.py`` + ``install_tokens.py``
+ ``web/src/mothership/``, rebuild — no other code references the
centralization layer.

This module ships THREE routers because the install flow has three
distinct auth surfaces:

- ``router`` — cookie auth (session). Mounted at ``/api/m``. The
  logged-in botsquad.dev user creates servers, lists them, watches a
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
    INVITE_PREFIX,
    SERVER_PREFIX,
    hash_token,
    is_install_token,
    is_invite_token,
    is_server_bearer,
)
from app.auth import verify_password
from app.mothership_store import MothershipStore
from app.mothership_users_store import MothershipUsersStore
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


def _users_store(request: Request) -> MothershipUsersStore:
    cfg = request.app.state.api_config
    return MothershipUsersStore(cfg.data_dir / "_mothership")


def _require_super_admin(user: dict = Depends(require_auth)) -> dict:
    """Super-admin gate for the MOTHERSHIP routes that aren't bearer-auth.

    Until GlobalUser-backed sessions land (follow-up), every server-local
    admin on the mothership build is the super-admin — same derivation as
    routes_auth._is_super_admin. Routes that touch the GlobalUser registry
    use this instead of the looser ``require_auth`` because non-admin
    users (no MOTHERSHIP scope) MUST get 403, not see anyone's else profile.
    """
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="super-admin only")
    return user


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


# ---- T-0066: GlobalUser registry (super-admin cookie auth) ------------------


@router.get("/users", dependencies=[Depends(_require_super_admin)])
def list_global_users(request: Request) -> list[dict]:
    """Return the GlobalUser registry, password hashes stripped.

    Consumed by Bundle A's MOTHERSHIP > all-users page. The list reflects
    only users that have been minted into the mothership registry — server-
    local ServerUsers that haven't gone through ``/api/auth/attach`` (or the
    one-shot migration) are NOT visible here. That's intentional: this
    surface is the cross-server identity directory, not a union of every
    local auth.toml.

    T-0129: each row carries an ``attached_servers`` count (number of
    Attachment rows for the user) so the FE can render the per-user
    attached-server tally without an N+1 follow-up GET. The count is
    computed by walking the per-user attachments dir — cheap at the
    registry sizes we expect (single-digit users × single-digit servers).
    """
    store = _users_store(request)
    out: list[dict] = []
    for u in store.list_users():
        d = u.to_public()
        d["attached_servers"] = len(store.list_attachments_for_user(u.id))
        out.append(d)
    return out


# ---- T-0066: /users/verify — server-bearer auth (consumed by /api/auth/attach)


@installer_router.post("/users/verify")
def verify_global_user(request: Request, payload: dict) -> dict:
    """Validate global creds on behalf of an attached server.

    Bearer-auth via the SERVER bearer (post-/connect) only. Pre-/connect
    install tokens are explicitly rejected — token-stage installers have
    no business validating user creds and rejecting here keeps the
    rotation invariant clean. Returns the GlobalUser public projection on
    success, 401 on bad creds, 404 on unknown username.
    """
    server_id, kind = _authenticate_installer(request)
    if kind != "server_bearer":
        raise HTTPException(
            status_code=403,
            detail="server_bearer required (install_token rejected for /users/verify)",
        )
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    store = _users_store(request)
    user = store.user_by_username(username)
    if user is None:
        # 404 distinguishes "no such global user" from "bad password" so
        # the calling server can surface a precise error to the attaching
        # user instead of a generic 401.
        raise HTTPException(status_code=404, detail="unknown global user")
    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="bad global credentials")
    # Touch last_seen on the (user × server) attachment if one already
    # exists; the attach side will upsert if this is the first claim.
    # Silent no-op when no attachment yet — verify is a precursor to the
    # writeback step on the server side, not the writeback itself.
    store.touch_last_seen(user.id, server_id)
    return user.to_public()


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


@router.post("/servers/{server_id}/invites")
def create_invite(
    request: Request,
    server_id: str,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    """Mint an invite token tied to an existing server + target Linux user.

    T-0026 Chapter I §3.0 invite-join flow. The plaintext is returned exactly
    once in this response body; only the SHA-256 lives in the registry.
    Target user runs the same ``curl … | bash`` surface but with an invite
    token in the URL — install.sh prefix-detects and runs the no-group,
    no-compose, user-scoped STEPS chain.

    Role is the install-time choice between admin (added to ``bot-squad``
    group on join) and non-admin (no group membership). Cannot be changed
    after the invite is minted; revocation is "let it expire" — single-use
    + 24h TTL bounds the blast radius if the link leaks.
    """
    store = _store(request)
    if store.get_server(server_id) is None:
        raise HTTPException(status_code=404, detail="server not found")
    target_username = (payload.get("target_username") or "").strip()
    role = (payload.get("role") or "").strip()
    if not target_username:
        raise HTTPException(status_code=400, detail="target_username is required")
    if role not in ("admin", "non-admin"):
        raise HTTPException(
            status_code=400, detail="role must be 'admin' or 'non-admin'"
        )
    result = store.register_invite(
        server_id=server_id,
        target_username=target_username,
        role=role,
        created_by=user["username"],
    )
    # ``register_invite`` returns ``None`` only on a missing server, which we
    # already filtered above — but keep the guard to surface a sane error if
    # a future caller races a server delete.
    if result is None:
        raise HTTPException(status_code=404, detail="server not found")
    server, token = result
    base = _mothership_base_url(request)
    # The invite URL reuses the same /i/<token>/install.sh path — the same
    # install.sh substitutes the token, prefix-detects at runtime, and runs
    # the invite-mode STEPS chain instead of the fresh-install one.
    install_url = f"{base}/i/{token}/install.sh"
    # Pull the just-minted invite back out so we can return its expires_at
    # (the only data the FE needs that isn't already in the request body).
    minted = next(
        (inv for inv in server.invites if inv["hash"] == hash_token(token)),
        None,
    )
    return {
        "server_id": server_id,
        "invite_token": token,
        "install_url": install_url,
        "instructions_url": f"{base}/i/{token}/instructions.md",
        "target_username": target_username,
        "role": role,
        "expires_at": minted["expires_at"] if minted else None,
    }


# ---- T-0129: install-token revoke + re-mint (super-admin) -------------------


@router.post(
    "/servers/{server_id}/install-tokens/revoke",
    dependencies=[Depends(_require_super_admin)],
)
def revoke_install_token(request: Request, server_id: str) -> dict:
    """Clear the install_token on a still-pending server.

    Self-service rotation for when the install URL leaks or is sent to the
    wrong host. Idempotent — re-revoking a server whose token is already
    absent (already revoked, or already burned by /connect) returns the
    same public projection. The FE doesn't track burn state, so a 200 in
    every safe case keeps the button "always clickable" without surfacing
    confusing 409s.

    Pair with ``/install-tokens/mint`` below to issue a fresh token; the
    two are split so the FE can offer "revoke then look later" without
    forcing a re-mint side-effect.
    """
    updated = _store(request).revoke_install_token(server_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="server not found")
    return updated.to_public()


@router.post(
    "/servers/{server_id}/install-tokens/mint",
    dependencies=[Depends(_require_super_admin)],
)
def remint_install_token(request: Request, server_id: str) -> dict:
    """Mint a fresh install_token for an existing pending server.

    Same one-shot envelope as ``POST /api/m/servers`` — the plaintext is in
    the response body exactly once, and only its SHA-256 lands on disk.
    Returns 409 if the server already burned its install_token via
    /connect (state ∈ {connected, ready, failed}); re-minting against a
    burned server is not the right surface for that, since the consumer
    has a server_bearer it would never know to drop.
    """
    store = _store(request)
    if store.get_server(server_id) is None:
        raise HTTPException(status_code=404, detail="server not found")
    try:
        result = store.remint_install_token(server_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if result is None:
        # Race: server was deleted between get_server and remint. Treat as 404.
        raise HTTPException(status_code=404, detail="server not found")
    entry, token = result
    base = _mothership_base_url(request)
    return {
        "id": entry.id,
        "install_token": token,
        "install_url": f"{base}/i/{token}/install.sh",
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


@installer_router.post("/installer/join")
def installer_join(request: Request, payload: dict) -> dict:
    """Burn an invite token; return server_id + target user + role.

    T-0026 invite-join handshake. Sibling of ``/installer/connect`` but the
    server already exists — there's no new registry row, no install_state
    transition, no server_bearer mint. The installer's invite-mode STEPS
    chain calls this once to learn the target_username + role it must
    enforce locally (admin → add to ``bot-squad`` group; non-admin → skip).

    The invite is burned on success. 410 covers all the unhappy paths
    (unknown / expired / already burned) so the installer's structured
    "what + how" message keys off it verbatim — same recipe as the
    install-token 410 from ``/connect``.
    """
    token = (payload.get("token") or "").strip()
    # Reject the wrong token KIND with a precise 400 — keeps the
    # install-token-vs-invite-token non-interchangeability invariant
    # auditable without leaking which token a stale plaintext was. Any
    # opaque non-invite string falls through to the same 400.
    if not token or not is_invite_token(token):
        raise HTTPException(status_code=400, detail="invite token required")
    result = _store(request).consume_invite_token(token)
    if result is None:
        raise HTTPException(status_code=410, detail="invite token gone")
    server, invite = result
    return {
        "server_id": server.id,
        "target_username": invite["target_username"],
        "role": invite["role"],
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
    """Accept either an install token (fresh install) OR an invite token
    (T-0026 join-existing-install) for the no-auth bundle GETs. install.sh
    detects the prefix at runtime and runs the appropriate STEPS chain.

    Bundle GETs never burn — re-fetch is idempotent so installer reruns
    after a failure can keep pulling the same script. The burn happens at
    ``/installer/connect`` (install) or ``/installer/join`` (invite).
    """
    store = _store(request)
    if is_install_token(token):
        if store.server_by_install_token(token) is None:
            raise HTTPException(status_code=410, detail="install token gone")
        return
    if is_invite_token(token):
        if store.server_by_invite_token(token) is None:
            raise HTTPException(status_code=410, detail="install token gone")
        return
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
_ = (INSTALL_PREFIX, SERVER_PREFIX, INVITE_PREFIX, hash_token)


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
async def list_server_projects(server_id: str, request: Request) -> list[dict]:
    """Return the project list for ``server_id``.

    For peer (attached) servers: returns the registry's cached list (cheap,
    no upstream call; refresh hits ``POST /projects/refresh``).
    For ``is_self`` servers: fans IN to the local ``/api/projects`` handler
    so the mothership shows its own projects without needing a bearer or
    proxy hop. T-0049's planned wiring; landed inline mid-T-0055 because
    the empty self list was confusing on the unified view.
    """
    server = _store(request).get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    if server.is_self:
        from .routes_projects import list_projects as _list_local_projects
        return await _list_local_projects(request)
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


