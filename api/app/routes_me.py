"""Per-user state — onboarding seen-steps tracker + tg-chat-id binding.

Framework for T-0014..T-0022 spotlight beats. The set of step ids is owned
by the frontend; the backend just remembers which ones the logged-in user
has dismissed. POST /skip writes a sentinel so future step ids are dark by
default without an API change.

T-0019 adds the per-user Telegram chat-id binding: GET /me returns the
profile (incl. tg_chat_id), PUT /me/tg-chat-id sets it, POST
/me/tg-chat-id/test pings the worker tg_notify action to confirm wiring.

T-0061 layers per-attachment endpoints on top: ``/me/attachment/{server_id}/
tg-chat-id`` GET/PUT/test reads/writes the Attachment store (T-0066). For
un-migrated users (no ``attached_to_global_user``) the new endpoints fall
back to the legacy UserMeta.tg_chat_id so they work transparently before
the migration script runs. ``server_id == "self"`` resolves to this
install's own ``is_self`` server-id from the mothership registry.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import ONBOARDING_SKIP_ALL, ApiConfig, AuthConfig, UserMeta
from app.mothership_store import MothershipStore
from app.mothership_users_store import Attachment, MothershipUsersStore
from app.routes_auth import require_auth
from app.worker_client import WorkerError

router = APIRouter(prefix="/me", tags=["me"], dependencies=[Depends(require_auth)])


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _write_user_meta(request: Request, username: str, new_meta: UserMeta) -> None:
    """Rewrite auth.toml with one user_meta row replaced.

    Mirrors routes_users._write_auth_toml — kept as its own helper here so the
    me-endpoints don't have to import admin-only code, and so the writer is
    cheap to audit when per-user fields land (seen_steps, tg_chat_id).
    """
    from app.routes_users import _serialize_auth_toml, _ttl_to_string

    cfg: AuthConfig = request.app.state.auth_config
    new_user_meta = dict(cfg.user_meta)
    new_user_meta[username] = new_meta
    ttl_str = _ttl_to_string(cfg.session_ttl_seconds)
    text = _serialize_auth_toml(dict(cfg.users), new_user_meta, ttl_str)
    config_dir: Path = request.app.state.api_config.config_dir
    path = config_dir / "auth.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.rename(tmp, path)
    request.app.state.auth_config = AuthConfig.load(config_dir)


def _state_payload(meta: UserMeta) -> dict:
    steps = list(meta.seen_steps)
    return {
        "steps_seen": steps,
        "skipped": ONBOARDING_SKIP_ALL in steps,
    }


@router.get("/onboarding")
def get_onboarding(request: Request, user: dict = Depends(require_auth)) -> dict:
    cfg: AuthConfig = request.app.state.auth_config
    meta = cfg.meta_for(user["username"])
    return _state_payload(meta)


@router.post("/onboarding/seen")
def mark_step_seen(request: Request, payload: dict, user: dict = Depends(require_auth)) -> dict:
    step = (payload.get("step") or "").strip()
    if not step:
        raise HTTPException(status_code=400, detail="step required")
    if step == ONBOARDING_SKIP_ALL:
        raise HTTPException(status_code=400, detail="reserved step id")

    cfg: AuthConfig = request.app.state.auth_config
    username = user["username"]
    meta = cfg.meta_for(username)
    if step in meta.seen_steps:
        return _state_payload(meta)

    new_meta = UserMeta(
        linux_user=meta.linux_user,
        server_role=meta.server_role,
        seen_steps=meta.seen_steps + (step,),
        tg_chat_id=meta.tg_chat_id,
        attached_to_global_user=meta.attached_to_global_user,
    )
    _write_user_meta(request, username, new_meta)
    return _state_payload(new_meta)


@router.post("/onboarding/skip")
def skip_onboarding(request: Request, user: dict = Depends(require_auth)) -> dict:
    cfg: AuthConfig = request.app.state.auth_config
    username = user["username"]
    meta = cfg.meta_for(username)
    if ONBOARDING_SKIP_ALL in meta.seen_steps:
        return _state_payload(meta)

    new_meta = UserMeta(
        linux_user=meta.linux_user,
        server_role=meta.server_role,
        seen_steps=meta.seen_steps + (ONBOARDING_SKIP_ALL,),
        tg_chat_id=meta.tg_chat_id,
        attached_to_global_user=meta.attached_to_global_user,
    )
    _write_user_meta(request, username, new_meta)
    return _state_payload(new_meta)


# --- T-0019: profile + tg-chat-id binding ---


def _profile_payload(username: str, meta: UserMeta) -> dict:
    return {
        "username": username,
        "linux_user": meta.linux_user,
        "is_admin": meta.is_admin,
        "tg_chat_id": meta.tg_chat_id or None,
    }


@router.get("")
def get_me(request: Request, user: dict = Depends(require_auth)) -> dict:
    """Return the logged-in user's profile, including tg_chat_id binding.

    Distinct from /api/auth/me (which is the session-validation surface);
    this is the read endpoint paired with PUT /me/tg-chat-id.
    """
    cfg: AuthConfig = request.app.state.auth_config
    meta = cfg.meta_for(user["username"])
    return _profile_payload(user["username"], meta)


@router.put("/tg-chat-id")
def put_tg_chat_id(request: Request, payload: dict, user: dict = Depends(require_auth)) -> dict:
    raw = payload.get("tg_chat_id", "")
    if raw is None:
        raw = ""
    new_id = str(raw).strip()
    # Loose validation: TG chat ids are integers (possibly negative for groups);
    # we accept the digit/sign shape here and let the worker surface real errors
    # from the test-ping. Empty string clears the binding.
    if new_id and not (new_id.lstrip("-").isdigit()):
        raise HTTPException(status_code=400, detail="tg_chat_id must be an integer or empty")

    cfg: AuthConfig = request.app.state.auth_config
    username = user["username"]
    meta = cfg.meta_for(username)
    new_meta = UserMeta(
        linux_user=meta.linux_user,
        server_role=meta.server_role,
        seen_steps=meta.seen_steps,
        tg_chat_id=new_id,
        attached_to_global_user=meta.attached_to_global_user,
    )
    _write_user_meta(request, username, new_meta)
    return _profile_payload(username, new_meta)


@router.post("/tg-chat-id/test")
async def test_tg_chat_id(request: Request, user: dict = Depends(require_auth)) -> dict:
    """Send a fixed test ping to the user's bound chat via worker tg_notify."""
    cfg: AuthConfig = request.app.state.auth_config
    username = user["username"]
    meta = cfg.meta_for(username)
    chat_id = (meta.tg_chat_id or "").strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="no tg_chat_id bound")

    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action(
            "tg_notify",
            {
                "chat_id": chat_id,
                "message": f"bot-squad test ping for {username} — binding works",
                "user": username,
            },
        )
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=str(result))
    return {"ok": True, "sent": bool(result.get("sent", True))}


# --- T-0061: per-attachment endpoints (TG binding scoped per server) ---


def _users_store(request: Request) -> MothershipUsersStore:
    cfg: ApiConfig = request.app.state.api_config
    return MothershipUsersStore(cfg.data_dir / "_mothership")


def _self_server_id(request: Request) -> str | None:
    """Resolve this install's own server-id from ``_mothership/servers.json``.

    Returns ``None`` on a detach build (no mothership registry on disk) so
    the caller can fall through to the UserMeta-backed legacy path.
    """
    cfg: ApiConfig = request.app.state.api_config
    try:
        for s in MothershipStore(cfg.data_dir / "_mothership").list_servers():
            if s.is_self:
                return s.id
    except (FileNotFoundError, OSError):
        return None
    return None


def _resolve_server_id(request: Request, server_id: str) -> str:
    """``"self"`` → this install's id; everything else passes through.

    On a detach build with no registry the sentinel stays literal — the
    legacy UserMeta fallback doesn't actually need a real server-id, but
    the response payload reflects whatever the caller asked for so the
    FE round-trips the picker's value cleanly.
    """
    if server_id == "self":
        return _self_server_id(request) or "self"
    return server_id


def _attachment_payload(server_id: str, tg_chat_id: str) -> dict:
    return {
        "server_id": server_id,
        "tg_chat_id": tg_chat_id or None,
    }


@router.get("/attachments")
def list_my_attachments(request: Request, user: dict = Depends(require_auth)) -> dict:
    """List attachments for the calling user.

    Migrated users: one row per (this GlobalUser × server) from the
    Attachment store. Un-migrated users: a single synthetic row for the
    local install carrying their UserMeta.tg_chat_id, so the FE can render
    the same shape on both branches without an in-component fork.
    """
    cfg: AuthConfig = request.app.state.auth_config
    meta = cfg.meta_for(user["username"])

    if not meta.attached_to_global_user:
        self_id = _self_server_id(request) or "self"
        return {
            "global_user_id": None,
            "attachments": [
                {
                    "server_id": self_id,
                    "server_username": user["username"],
                    "tg_chat_id": meta.tg_chat_id or "",
                    "attached_at": None,
                    "legacy_unmigrated": True,
                }
            ],
        }

    store = _users_store(request)
    rows = [
        {
            "server_id": a.server_id,
            "server_username": a.server_username,
            "tg_chat_id": a.tg_chat_id or "",
            "attached_at": a.attached_at,
            "legacy_unmigrated": False,
        }
        for a in store.list_attachments_for_user(meta.attached_to_global_user)
    ]
    return {
        "global_user_id": meta.attached_to_global_user,
        "attachments": rows,
    }


def _read_attachment_tg(
    request: Request, user: dict, server_id: str
) -> tuple[str, str, Attachment | None]:
    """Return ``(resolved_server_id, tg_chat_id, attachment)``.

    Falls back to UserMeta.tg_chat_id when the user isn't attached yet OR
    when an Attachment row doesn't exist for this server (e.g. before the
    migration script has run). The fallback is a read-only convenience —
    writes still go to the per-server store once the user is attached.
    """
    cfg: AuthConfig = request.app.state.auth_config
    meta = cfg.meta_for(user["username"])
    resolved = _resolve_server_id(request, server_id)

    if not meta.attached_to_global_user:
        return resolved, (meta.tg_chat_id or ""), None

    attachment = _users_store(request).get_attachment(
        meta.attached_to_global_user, resolved
    )
    if attachment is None:
        return resolved, (meta.tg_chat_id or ""), None
    return resolved, (attachment.tg_chat_id or ""), attachment


@router.get("/attachment/{server_id}/tg-chat-id")
def get_attachment_tg_chat_id(
    server_id: str, request: Request, user: dict = Depends(require_auth)
) -> dict:
    resolved, tg, _ = _read_attachment_tg(request, user, server_id)
    return _attachment_payload(resolved, tg)


@router.put("/attachment/{server_id}/tg-chat-id")
def put_attachment_tg_chat_id(
    server_id: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    raw = payload.get("tg_chat_id", "")
    if raw is None:
        raw = ""
    new_id = str(raw).strip()
    # Same loose validation as the legacy /me/tg-chat-id route — accept the
    # digit/sign shape, let tg_notify surface real errors on the test ping.
    if new_id and not (new_id.lstrip("-").isdigit()):
        raise HTTPException(
            status_code=400, detail="tg_chat_id must be an integer or empty"
        )

    cfg: AuthConfig = request.app.state.auth_config
    username = user["username"]
    meta = cfg.meta_for(username)
    resolved = _resolve_server_id(request, server_id)

    if not meta.attached_to_global_user:
        # Un-migrated: write to UserMeta (legacy storage). Once
        # ``scripts/migrate/users_split.py`` lifts the value into an
        # Attachment, subsequent PUTs will route to the store branch below.
        new_meta = UserMeta(
            linux_user=meta.linux_user,
            server_role=meta.server_role,
            seen_steps=meta.seen_steps,
            tg_chat_id=new_id,
            attached_to_global_user=meta.attached_to_global_user,
        )
        _write_user_meta(request, username, new_meta)
        return _attachment_payload(resolved, new_id)

    store = _users_store(request)
    existing = store.get_attachment(meta.attached_to_global_user, resolved)
    # Preserve seen_steps / server_username on update — the writer overwrites
    # the whole row, so an unintentional reset of those fields would be a
    # silent data-loss bug.
    server_username = existing.server_username if existing else username
    seen_steps = existing.seen_steps if existing else ()
    last_seen_at = existing.last_seen_at if existing else None
    attachment, _ = store.upsert_attachment(
        global_user_id=meta.attached_to_global_user,
        server_id=resolved,
        server_username=server_username,
        tg_chat_id=new_id,
        seen_steps=seen_steps,
        last_seen_at=last_seen_at,
    )
    return _attachment_payload(resolved, attachment.tg_chat_id or "")


@router.post("/attachment/{server_id}/tg-chat-id/test")
async def test_attachment_tg_chat_id(
    server_id: str, request: Request, user: dict = Depends(require_auth)
) -> dict:
    _, chat_id, _ = _read_attachment_tg(request, user, server_id)
    chat_id = chat_id.strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="no tg_chat_id bound")

    username = user["username"]
    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action(
            "tg_notify",
            {
                "chat_id": chat_id,
                "message": (
                    f"bot-squad test ping for {username} — binding works"
                ),
                "user": username,
            },
        )
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=str(result))
    return {"ok": True, "sent": bool(result.get("sent", True))}


# --- T-0218: per-project personal override + resolved view (D-0022) ---
#
# Per-project personal notification override, now fully persisted (follow-up to
# the stub commit). Storage (Q2): a ``project_tg_chat_ids: dict[slug -> chat_id]``
# map on the ``Attachment`` (migrated users) with a parallel map on ``UserMeta``
# for un-migrated users — mirroring how ``tg_chat_id`` already lives in both
# stores. The map sits ABOVE the per-server ``tg_chat_id`` in the notify
# precedence (project -> server -> global), enforced here for the resolved view
# and in the worker's ``_resolve_user_tg_chat_id`` for actual sends. Q1
# (topic_id): personal targeting stays chat_id-only; personal ``topic_id`` is OUT
# of scope (topic_id remains project-wide-only). Q3: yes — the per-project path
# falls back to UserMeta exactly like the per-server route. Q4: paths confirmed
# (``/me/project/{slug}/tg-chat-id`` + ``/me/notifications/resolved``).


def _validate_chat_id(raw: object) -> str:
    """Loose int-or-empty validation (same convention as the other tg-chat-id
    routes): digits with an optional leading ``-``; empty == clear/inherit."""
    if raw is None:
        raw = ""
    new_id = str(raw).strip()
    if new_id and not (new_id.lstrip("-").isdigit()):
        raise HTTPException(
            status_code=400, detail="tg_chat_id must be an integer or empty"
        )
    return new_id


def _raw_server_tg(request: Request, user: dict, server_id: str) -> tuple[str, str, bool]:
    """``(resolved_server_id, raw server-level tg_chat_id, set?)`` — with NO
    global fallback. Unlike ``_read_attachment_tg`` this does not borrow
    ``UserMeta.tg_chat_id`` when the per-server override is absent: the resolved
    view must report each level's *own* value honestly so ``set`` and the
    precedence winner are correct."""
    cfg: AuthConfig = request.app.state.auth_config
    meta = cfg.meta_for(user["username"])
    resolved = _resolve_server_id(request, server_id)
    if not meta.attached_to_global_user:
        return resolved, "", False
    att = _users_store(request).get_attachment(meta.attached_to_global_user, resolved)
    if att is None:
        return resolved, "", False
    raw = att.tg_chat_id or ""
    return resolved, raw, bool(raw)


def _read_project_override(
    request: Request, user: dict, slug: str, server_id: str = "self"
) -> tuple[str, bool]:
    """``(raw per-project tg_chat_id, set?)`` for (user, resolved server, slug).

    Migrated users read the ``Attachment.project_tg_chat_ids`` map for the
    resolved server; un-migrated users read the parallel ``UserMeta`` map (Q3
    fallback). NO fallthrough to coarser levels — the resolved view reports
    each level's own value so ``set`` + the precedence winner stay honest."""
    if not slug:
        return "", False
    cfg: AuthConfig = request.app.state.auth_config
    meta = cfg.meta_for(user["username"])
    if not meta.attached_to_global_user:
        raw = (meta.project_tg_chat_ids or {}).get(slug, "")
        return raw, bool(raw)
    resolved = _resolve_server_id(request, server_id)
    att = _users_store(request).get_attachment(meta.attached_to_global_user, resolved)
    if att is None:
        return "", False
    raw = (att.project_tg_chat_ids or {}).get(slug, "")
    return raw, bool(raw)


def _resolve_notification(
    request: Request, user: dict, server_id: str, slug: str
) -> dict:
    """Compute the 3-level resolved view for (user, server, project).

    Precedence (most specific wins, falling through on empty):
    ``project -> server -> global -> none``.
    """
    cfg: AuthConfig = request.app.state.auth_config
    meta = cfg.meta_for(user["username"])
    global_raw = meta.tg_chat_id or ""
    resolved_sid, server_raw, server_set = _raw_server_tg(request, user, server_id)
    project_raw, project_set = _read_project_override(request, user, slug, server_id)

    levels = {
        "global": {"tg_chat_id": global_raw or None, "set": bool(global_raw)},
        "server": {
            "server_id": resolved_sid,
            "tg_chat_id": server_raw or None,
            "set": server_set,
        },
        "project": {
            "slug": slug,
            "tg_chat_id": project_raw or None,
            "set": project_set,
        },
    }
    if project_set:
        effective, source = project_raw, "project"
    elif server_set:
        effective, source = server_raw, "server"
    elif global_raw:
        effective, source = global_raw, "global"
    else:
        effective, source = "", "none"
    return {
        "levels": levels,
        "effective": {"tg_chat_id": effective or None, "source": source},
    }


def _project_payload(slug: str, tg_chat_id: str | None) -> dict:
    return {"slug": slug, "tg_chat_id": tg_chat_id or None}


@router.get("/project/{slug}/tg-chat-id")
def get_project_tg_chat_id(
    slug: str, request: Request, user: dict = Depends(require_auth)
) -> dict:
    raw, _ = _read_project_override(request, user, slug)
    return _project_payload(slug, raw or None)


@router.put("/project/{slug}/tg-chat-id")
def put_project_tg_chat_id(
    slug: str,
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    """Set (or clear, on empty) the per-project personal override for the
    current server. Migrated users write the Attachment map; un-migrated users
    write the parallel UserMeta map (Q3 fallback)."""
    new_id = _validate_chat_id(payload.get("tg_chat_id", ""))
    cfg: AuthConfig = request.app.state.auth_config
    username = user["username"]
    meta = cfg.meta_for(username)

    if not meta.attached_to_global_user:
        new_map = dict(meta.project_tg_chat_ids or {})
        if new_id:
            new_map[slug] = new_id
        else:
            new_map.pop(slug, None)
        new_meta = UserMeta(
            linux_user=meta.linux_user,
            server_role=meta.server_role,
            seen_steps=meta.seen_steps,
            tg_chat_id=meta.tg_chat_id,
            attached_to_global_user=meta.attached_to_global_user,
            project_tg_chat_ids=new_map,
        )
        _write_user_meta(request, username, new_meta)
        return _project_payload(slug, new_id or None)

    store = _users_store(request)
    resolved = _resolve_server_id(request, "self")
    existing = store.get_attachment(meta.attached_to_global_user, resolved)
    new_map = dict(existing.project_tg_chat_ids) if existing else {}
    if new_id:
        new_map[slug] = new_id
    else:
        new_map.pop(slug, None)
    # Preserve the other per-server fields — upsert overwrites the whole row.
    server_username = existing.server_username if existing else username
    store.upsert_attachment(
        global_user_id=meta.attached_to_global_user,
        server_id=resolved,
        server_username=server_username,
        tg_chat_id=existing.tg_chat_id if existing else "",
        seen_steps=existing.seen_steps if existing else (),
        last_seen_at=existing.last_seen_at if existing else None,
        project_tg_chat_ids=new_map,
    )
    return _project_payload(slug, new_id or None)


@router.get("/notifications/resolved")
def get_notifications_resolved(
    request: Request,
    server_id: str = "self",
    slug: str = "",
    user: dict = Depends(require_auth),
) -> dict:
    """Single read powering the 3-row override panel: each level's raw value +
    ``set`` flag + the effective winner + its ``source``."""
    return _resolve_notification(request, user, server_id, slug)


@router.post("/project/{slug}/tg-chat-id/test")
async def test_project_tg_chat_id(
    slug: str, request: Request, user: dict = Depends(require_auth)
) -> dict:
    """Ping the RESOLVED chat for this (user, self-server, project): the
    effective chat under the project -> server -> global precedence. The
    per-project personal override is persisted (T-0218 follow-up) and wins
    when set, so the ping targets it."""
    resolved = _resolve_notification(request, user, "self", slug)
    chat_id = (resolved["effective"]["tg_chat_id"] or "").strip()
    if not chat_id:
        raise HTTPException(
            status_code=400, detail="no tg_chat_id resolves for this project"
        )

    username = user["username"]
    client = request.app.state.worker_router.coordinator()
    try:
        result = await client.call_action(
            "tg_notify",
            {
                "chat_id": chat_id,
                "message": (
                    f"bot-squad test ping for {username} — project '{slug}' binding works"
                ),
                "user": username,
            },
        )
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=str(result))
    return {"ok": True, "sent": bool(result.get("sent", True))}
