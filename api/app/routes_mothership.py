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

Contract is fixed in ``docs/architecture/D-0017-mothership-seam.md`` — read
that before touching the wire shapes.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
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
from app.mothership_store import AttachedServer, MothershipStore
from app.mothership_users_store import MothershipUsersStore
from app.payload_guard import str_field
from app.roles import GlobalRole
from app.routes_auth import require_auth


# ---- routers ----------------------------------------------------------------
# Cookie-auth surface for the logged-in mothership user.
# Fork-6 READY: the terminal checkpoint of a FRESH install (the last step of
# ``INSTALL_STEPS`` in scripts/install/install.sh). When the installer reports
# this checkpoint as ``done`` it has finished — the server moves to ``ready``.
# CONTRACT: this MUST equal install.sh's ``INSTALL_STEPS`` terminal step (and
# must NOT be the invite path's ``print_join_attach``). Pinned by
# test_mothership_install_state.py::test_terminal_constant_matches_install_sh_contract.
TERMINAL_INSTALL_CHECKPOINT = "print_attach"


def _seed_local_users_into(app) -> None:
    """Backfill the GlobalUser registry from this install's local ``auth.toml``,
    attaching each user to the mothership's own (``is_self``) server.

    T-0313/0317: on a self-dogfooded mothership the registry otherwise only
    populates via ``/attach`` or the one-shot migration, so ``GET /api/m/users``
    showed "No global users yet" to a logged-in operator while real local users
    existed; and each seeded user is attached to the self-server so the
    directory's ``attached_servers`` tally is a real number.

    item 21 (audit theme-2): this runs ONCE at mothership STARTUP (see
    ``_mothership_lifespan``) instead of on every ``GET /api/m/users`` — a read
    must not mutate the store. Idempotent: an already-established GlobalUser is
    returned unchanged, never clobbered; the self-attachment upsert is a no-op
    once present.
    """
    auth_cfg = getattr(app.state, "auth_config", None)
    api_cfg = getattr(app.state, "api_config", None)
    if auth_cfg is None or api_cfg is None:
        return
    root = api_cfg.data_dir / "_mothership"
    users_store = MothershipUsersStore(root)
    self_server = next(
        (s for s in MothershipStore(root).list_servers() if s.is_self), None
    )
    for username, password_hash in auth_cfg.users.items():
        meta = auth_cfg.meta_for(username)
        gu, _ = users_store.upsert_user_by_username(
            username=username,
            password_hash=password_hash,
            is_super_admin=meta.is_admin,
        )
        if self_server is not None:
            users_store.upsert_attachment(
                global_user_id=gu.id,
                server_id=self_server.id,
                server_username=username,
            )


@contextlib.asynccontextmanager
async def _mothership_lifespan(app):
    """Mothership-router startup: seed the GlobalUser registry from local
    auth.toml (item 21) so ``GET /api/m/users`` is a pure read. Best-effort — a
    backfill failure must never block the mothership from starting (mirrors
    main.py's ``register_self_if_missing`` OSError tolerance). Runs after
    main.py's synchronous self-registration, so the ``is_self`` row exists."""
    try:
        _seed_local_users_into(app)
    except OSError:
        pass
    yield


router = APIRouter(
    tags=["mothership"],
    dependencies=[Depends(require_auth)],
    lifespan=_mothership_lifespan,
)
# Bearer-auth surface for the installer script (no session cookie).
installer_router = APIRouter(tags=["mothership-installer"])
# No-auth bundle GETs — token in the URL path is the auth.
bundle_router = APIRouter(tags=["mothership-bundle"])
# T-0529: worker-token surface under a dedicated /worker prefix (-> /api/m/worker/*)
# so the public traefik router can exclude all worker-only routes (option-C
# least-exposure). Token-gated per-handler by _authenticate_worker (fails closed).
worker_router = APIRouter(prefix="/worker", tags=["mothership-worker"])


def _store(request: Request) -> MothershipStore:
    cfg = request.app.state.api_config
    return MothershipStore(cfg.data_dir / "_mothership")


def _users_store(request: Request) -> MothershipUsersStore:
    cfg = request.app.state.api_config
    return MothershipUsersStore(cfg.data_dir / "_mothership")


def _require_super_admin(user: dict = Depends(require_auth)) -> dict:
    """Super-admin gate for the MOTHERSHIP routes that aren't bearer-auth.

    T-0216 Phase A: reads the explicit ``global_role`` from the enriched
    session (derived via routes_auth._derive_global_role — the quarantined
    build-flag bridge, removal tracked by T-0228). Behavior-identical to the
    pre-T-0216 ``is_admin`` check on the MOTHERSHIP build where these routes
    mount. Non-global-admin users MUST get 403, not see anyone else's profile.
    """
    if user.get("global_role") != GlobalRole.GLOBAL_ADMIN.value:
        raise HTTPException(status_code=403, detail="super-admin only")
    return user


def _is_global_admin(user: dict) -> bool:
    """Same super-admin predicate as ``_require_super_admin`` (T-0216 global_role)."""
    return user.get("global_role") == GlobalRole.GLOBAL_ADMIN.value


def _can_manage(server, user: dict) -> bool:
    """Owner-only MANAGEMENT predicate — the single gate (audit Fork-3 SSOT)
    for server-management writes (invites, grants, delete). D3 (operator
    2026-06-21): management is owner-only.

    Stricter than ``_can_access`` on purpose: a grantee may ENTER + proxy into a
    server but must NEVER manage it (no privilege escalation), and a non-owning
    global-admin has no standing on a PEER server (god-mode removed, T-0221 D2).
    The ``is_self`` server is the one carve-out — the is_self ACCESS bypass
    (every user can enter their own install) must not confer WRITE standing, so
    managing the self-server is a global-admin action (T-0377).
    """
    if getattr(server, "is_self", False):
        return _is_global_admin(user)
    username = user.get("username")
    return bool(username) and username == server.owner_user


def _require_manage(server, user: dict) -> None:
    """Raise 403 unless ``user`` may MANAGE ``server`` (see ``_can_manage``)."""
    if not _can_manage(server, user):
        raise HTTPException(status_code=403, detail="server owner only")


def require_manage(
    request: Request,
    server_id: str,
    user: dict = Depends(require_auth),
) -> AttachedServer:
    """Server-resolving management gate (audit Fork-3). A management write route
    cannot obtain its ``server`` without passing through this dependency, so the
    owner check is structurally impossible to skip — the SSOT that replaced the
    scattered ``_require_owner`` / ``_require_admin_on_self`` placements.

    NOTE (flaw-watch): the ``server_id`` parameter name MUST match the route's
    ``{server_id}`` path param — FastAPI injects path params into dependencies
    BY NAME, so renaming it would silently feed ``server_id=None`` and turn the
    gate into a no-op. The enumeration + per-route 403 tests pin this.
    """
    server = _store(request).get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    _require_manage(server, user)
    return server


def _has_active_grant(server, username: str | None) -> bool:
    """True iff ``username`` holds a non-revoked grant on ``server``.

    A grant row is ``{username, granted_by, granted_at, revoked_at}``; it
    authorizes access only while ``revoked_at`` is falsy. Re-granting a
    previously-revoked user appends a fresh active row, so a single active
    row anywhere in the list is enough.
    """
    if not username:
        return False
    for g in getattr(server, "grants", None) or []:
        if g.get("username") == username and not g.get("revoked_at"):
            return True
    return False


def _can_access(server, user: dict) -> bool:
    """Single authorization predicate shared by the server-enter gate and the
    server-list scope (T-0221 D2 / T-0219 D3).

    A mothership user may see + enter a server iff:
      - it is the mothership's own ``is_self`` entry (fans into the LOCAL api
        whose own auth applies), OR
      - they OWN it (``owner_user``), OR
      - they hold an active per-server GRANT.

    ``is_admin`` is deliberately ABSENT: stakeholder decision D2 removes
    global-admin god-mode — every bot-squad install is an independent server,
    and cross-server access is an explicit, revocable grant, not a blanket
    admin power. This SUPERSEDES T-0169's conservative kept-god-mode stopgap.
    """
    if getattr(server, "is_self", False):
        return True
    username = user.get("username")
    if username and username == server.owner_user:
        return True
    return _has_active_grant(server, username)


def _require_server_access(server, user: dict) -> None:
    """Raise 403 unless ``user`` may act on / enter ``server`` (see
    ``_can_access``). Applied to every gated server handler."""
    if not _can_access(server, user):
        raise HTTPException(status_code=403, detail="not authorized for this server")


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
def list_servers(request: Request, user: dict = Depends(require_auth)) -> list[dict]:
    """List servers the caller may see — owner/grant-scoped (T-0219 D3).

    Uses the SAME ``_can_access`` predicate as the server-enter gate: a user
    sees only servers they own, hold an active grant for, or the mothership's
    own ``is_self`` entry. There is no global all-servers view; a global admin
    does NOT see others' ungranted servers (god-mode removed, T-0221 D2).
    """
    # T-0370 / T-0422: invites are MANAGEMENT metadata, so the visibility axis
    # must track the management axis (owner), not is_admin. Compute per-server
    # via the require_manage SSOT: an owner sees their server's invites; a
    # global admin sees the is_self server's invites (the _can_manage carve-out);
    # a non-owner admin GRANTEE — who require_manage 403s from minting — no
    # longer sees a peer's invite metadata.
    return [
        s.to_public(include_invites=_can_manage(s, user))
        for s in _store(request).list_servers()
        if _can_access(s, user)
    ]


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

    item 21 (audit theme-2): this is a PURE read — the local-auth.toml backfill
    that used to run here on every GET now runs once at mothership STARTUP
    (``_mothership_lifespan`` → ``_seed_local_users_into``).
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
    username = str_field(payload, "username")
    password = str_field(payload, "password", strip=False)
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


# ---- T-0488: TG sender -> GlobalUser linkage (single bot user recognition) ----


@worker_router.post("/tg/resolve-or-link")
def tg_resolve_or_link(request: Request, payload: dict) -> dict:
    """Resolve an inbound Telegram sender to its cross-server GlobalUser, minting
    one on first contact. Called by the worker (``tg_listener``) on the inbound
    TG path; token-gated by ``WORKER_API_TOKEN`` (see ``_authenticate_worker``).

    Single-writer-per-store: the API owns this write under the store's mutation
    lock — the worker only READS the ``_mothership`` registry, never writes it,
    so two installs (or two messages) can't race a double-mint.

    Body: ``{tg_user_id, display_name?, slug?}`` (``tg_user_id`` required).
    Returns the resolved identity ``(slug, global_user_id)`` + the ``created``
    flag + the GlobalUser public projection, so the caller can anchor the
    downstream user-conversation on (slug, global_user_id)."""
    _authenticate_worker(request)
    tg_user_id = str(payload.get("tg_user_id") or "").strip()
    if not tg_user_id:
        raise HTTPException(status_code=400, detail="tg_user_id required")
    slug = str(payload.get("slug") or "")
    display_name = str(payload.get("display_name") or "")
    store = _users_store(request)
    user, created = store.resolve_or_link_tg_user(
        tg_user_id=tg_user_id, display_name=display_name,
    )
    return {
        "global_user_id": user.id,
        "created": created,
        "slug": slug,
        "user": user.to_public(),
    }


@router.post("/servers", dependencies=[Depends(_require_super_admin)])
def create_server(
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    # T-0376: super-admin only. Registering a server mints a LIVE install_token
    # (a fleet-attach credential); leaving this at require_auth let a non-admin
    # mint one = privilege escalation. Mirrors install-tokens/mint, which mints
    # the same credential for an existing server and was already super-admin.
    display_name = str_field(payload, "display_name")
    base_url = str_field(payload, "base_url")
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


@router.delete("/servers/{server_id}")
def delete_server(
    request: Request,
    server_id: str,
    server: AttachedServer = Depends(require_manage),
) -> dict:
    """Deregister a server — the close for ``register_server`` (audit Fork-6).

    register opened the loop; nothing closed it, so orphan / wrong-host /
    abandoned rows were immortal. Owner-gated via the ``require_manage`` SSOT
    (T-0390) — routing this destructive op through anything else would be the
    next ``is_self`` privesc. REFUSES the ``is_self`` row: it self-resurrects via
    ``register_self_if_missing`` on next boot, so deleting it is pointless +
    confusing. ``remove_server`` sweeps the row + bearer sidecar + checkpoint log;
    then ``remove_attachments_for_server`` drops every GlobalUser's attachment
    sidecar (T-0412) so attached_servers doesn't stay inflated post-deregister.
    """
    if server.is_self:
        raise HTTPException(
            status_code=409,
            detail="cannot delete the mothership's own (is_self) server",
        )
    if not _store(request).remove_server(server_id):
        # require_manage already resolved the server, so a False is a race
        # (a concurrent delete burned it first). Treat as already-gone.
        raise HTTPException(status_code=404, detail="server not found")
    # Close the per-user side of the loop: the registry row is gone, but every
    # attached GlobalUser still holds a dangling attachments/<gid>/<id>.json
    # (inflating attached_servers). remove_server has no reach there (T-0412).
    _users_store(request).remove_attachments_for_server(server_id)
    return {"removed": server_id}


@router.post("/servers/{server_id}/invites")
def create_invite(
    request: Request,
    server_id: str,
    payload: dict,
    server: AttachedServer = Depends(require_manage),
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
    # ``server`` is resolved + owner-gated by ``require_manage`` (audit Fork-3,
    # D3 owner-only): minting an invite is a management action, so a grantee who
    # can ENTER the server can no longer mint one.
    target_username = str_field(payload, "target_username")
    role = str_field(payload, "role")
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


# ---- T-0221: per-server access grants (owner-only) --------------------------
# The owner of a server may grant other mothership users explicit, revocable
# access to see + enter it. Grant management is strictly the owner's — gated by
# ``require_manage`` (NOT ``_require_server_access``), so a grantee cannot
# re-grant and a non-owning admin has no standing.


def _active_grants(server) -> list[dict]:
    """Public projection of a server's ACTIVE grants (revoked rows dropped)."""
    return [
        {
            "username": g.get("username"),
            "granted_by": g.get("granted_by"),
            "granted_at": g.get("granted_at"),
        }
        for g in (getattr(server, "grants", None) or [])
        if not g.get("revoked_at")
    ]


@router.get("/servers/{server_id}/grants")
def list_grants(
    server_id: str,
    server: AttachedServer = Depends(require_manage),
) -> dict:
    return {"server_id": server_id, "grants": _active_grants(server)}


@router.post("/servers/{server_id}/grants")
def create_grant(
    request: Request,
    server_id: str,
    payload: dict,
    server: AttachedServer = Depends(require_manage),
    user: dict = Depends(require_auth),
) -> dict:
    store = _store(request)
    username = str_field(payload, "username")
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    updated = store.add_grant(server_id, username, granted_by=user["username"])
    if updated is None:
        # Race: server removed between get_server and add_grant. Treat as 404.
        raise HTTPException(status_code=404, detail="server not found")
    return {"server_id": server_id, "grants": _active_grants(updated)}


@router.delete("/servers/{server_id}/grants/{username}")
def delete_grant(
    request: Request,
    server_id: str,
    username: str,
    server: AttachedServer = Depends(require_manage),
) -> dict:
    store = _store(request)
    updated = store.revoke_grant(server_id, username)
    if updated is None:
        raise HTTPException(status_code=404, detail="server not found")
    return {"server_id": server_id, "grants": _active_grants(updated)}


# ---- T-0653: deliberate hold (owner-only) ------------------------------------
# Distinguishes "install deliberately held pending explicit stakeholder/owner
# action" from a genuinely stalled/dead install. The FE stands its 30-min
# elapsed-time stale heuristic down once ``hold_reason`` is set (see
# ``web/src/mothership/serverState.ts``), and both `/m` and `/m/users` read
# the SAME state so they stop disagreeing on vocabulary for the same row.


@router.post("/servers/{server_id}/hold")
def hold_server(
    request: Request,
    server_id: str,
    payload: dict,
    server: AttachedServer = Depends(require_manage),
    user: dict = Depends(require_auth),
) -> dict:
    reason = str_field(payload, "reason")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    store = _store(request)
    updated = store.set_hold(server_id, reason, held_by=user["username"])
    if updated is None:
        # Race: server removed between require_manage's lookup and set_hold.
        raise HTTPException(status_code=404, detail="server not found")
    return updated.to_public()


@router.post("/servers/{server_id}/unhold")
def unhold_server(
    request: Request,
    server_id: str,
    server: AttachedServer = Depends(require_manage),
) -> dict:
    store = _store(request)
    updated = store.clear_hold(server_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="server not found")
    return updated.to_public()


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
async def checkpoints_stream(
    server_id: str,
    request: Request,
    user: dict = Depends(require_auth),
) -> StreamingResponse:
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
    _require_server_access(server, user)

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


def _authenticate_worker(request: Request) -> None:
    """T-0488: gate the worker->API TG-linkage call with the shared-secret
    ``WORKER_API_TOKEN`` (Bearer). FAILS CLOSED: an unset/empty token rejects
    every request (so shipping the endpoint before the secret is provisioned can
    never leave it open — T-0179-style secrets ordering), as does a missing or
    mismatched Bearer. Constant-time compare; 401 on any failure.

    This is a SEPARATE trust path from ``_authenticate_installer`` (install_token
    / server_bearer): the local worker has no server bearer on the is_self row,
    so it presents this shared secret instead. The base URL + exposure
    (public-traefik vs localhost-only) are deploy-time config; the gate is
    identical either way (defense-in-depth even behind a localhost-only port)."""
    expected = (os.environ.get("WORKER_API_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=401, detail="worker token not configured")
    bearer = _extract_bearer(request)  # 401 when the header is absent/malformed
    if not secrets.compare_digest(bearer, expected):
        raise HTTPException(status_code=401, detail="invalid worker token")


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
    token = str_field(payload, "token")
    if not token or not is_install_token(token):
        raise HTTPException(status_code=400, detail="install token required")
    server_meta = payload.get("server_meta") or {}
    if not isinstance(server_meta, dict):
        raise HTTPException(status_code=400, detail="server_meta must be an object")
    result = _store(request).consume_install_token(token, server_meta)
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
    token = str_field(payload, "token")
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
    checkpoint = str_field(payload, "checkpoint")
    status = str_field(payload, "status")
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
    # Fork-6 READY: the installer's TERMINAL checkpoint closes the install
    # lifecycle — move the registry row connected|failed → ready (the state the
    # FE gates cross-server fan-out on). Gated on the exact (checkpoint, status)
    # pair; mark_install_state no-ops on any other current state so a replayed
    # terminal report or a stray report can't regress/over-promote. Only the
    # fresh-install terminal (TERMINAL_INSTALL_CHECKPOINT) fires — the invite
    # path's print_join_attach is deliberately excluded (it joins an existing,
    # already-ready server).
    if checkpoint == TERMINAL_INSTALL_CHECKPOINT and status == "done":
        store.mark_install_state(server_id, "ready", allowed_from={"connected", "failed"})
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


async def _fan_in_local(request: Request, path: str, body: bytes) -> Response:
    """Dispatch ``path`` to THIS app in-process (T-0312, self-server fan-in).

    Used by the proxy when the target is the mothership's own ``is_self``
    server, which has no server_bearer to proxy with. ``httpx.ASGITransport``
    re-enters ``request.app`` without a socket; the caller's session cookie is
    forwarded so the local routes authenticate as the same user. The upstream
    response is relayed verbatim — including any error status — so the FE sees
    exactly what the single-install API would return.
    """
    headers: dict[str, str] = {}
    cookie = request.headers.get("cookie")
    if cookie:
        headers["Cookie"] = cookie
    ctype = request.headers.get("content-type")
    if ctype:
        headers["Content-Type"] = ctype
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://self.local"
    ) as client:
        try:
            upstream = await client.request(
                request.method,
                path,
                content=body or None,
                headers=headers,
                params=request.query_params,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"local dispatch failed: {e}")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@router.get("/servers/{server_id}/projects")
async def list_server_projects(
    server_id: str,
    request: Request,
    user: dict = Depends(require_auth),
) -> list[dict]:
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
    _require_server_access(server, user)
    if server.is_self:
        from .routes_projects import list_projects as _list_local_projects
        return await _list_local_projects(request)
    return server.projects_cache


@router.post("/servers/{server_id}/projects/refresh")
async def refresh_server_projects(
    server_id: str,
    request: Request,
    user: dict = Depends(require_auth),
) -> list[dict]:
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
    _require_server_access(server, user)
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
async def server_api_proxy(
    server_id: str,
    rest: str,
    request: Request,
    user: dict = Depends(require_auth),
) -> Response:
    store = _store(request)
    server = store.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    _require_server_access(server, user)
    upstream_path = f"/api/{rest}"
    # Forward the request body verbatim. The FE / single-install API only
    # ever uses JSON bodies, but we don't decode here — pass-through keeps
    # the proxy method-agnostic and avoids round-trip encoding bugs.
    body = await request.body()

    # T-0312: the mothership's OWN ``is_self`` server never minted a
    # server_bearer, so there is nothing to proxy with — reading the (absent)
    # bearer made the cross-server project route 503 for the self server even
    # though its projects render fine on the home view (which fans in via
    # ``list_server_projects``). Mirror that fan-in for ALL proxied paths:
    # dispatch in-process to the LOCAL app, forwarding the caller's session
    # cookie so the local routes authenticate as the same user. ASGITransport
    # keeps it in-process (no socket / TLS hop, no bearer).
    if server.is_self:
        return await _fan_in_local(request, upstream_path, body)

    bearer = store.read_server_bearer(server_id)
    if not bearer:
        raise HTTPException(
            status_code=503,
            detail="server bearer not available (server not connected)",
        )
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


