"""Per-user state — onboarding seen-steps tracker + tg-chat-id binding.

Framework for T-0014..T-0022 spotlight beats. The set of step ids is owned
by the frontend; the backend just remembers which ones the logged-in user
has dismissed. POST /skip writes a sentinel so future step ids are dark by
default without an API change.

T-0019 adds the per-user Telegram chat-id binding: GET /me returns the
profile (incl. tg_chat_id), PUT /me/tg-chat-id sets it, POST
/me/tg-chat-id/test pings the worker tg_notify action to confirm wiring.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import ONBOARDING_SKIP_ALL, AuthConfig, UserMeta
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
        is_admin=meta.is_admin,
        seen_steps=meta.seen_steps + (step,),
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
        is_admin=meta.is_admin,
        seen_steps=meta.seen_steps + (ONBOARDING_SKIP_ALL,),
        tg_chat_id=meta.tg_chat_id,
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
        is_admin=meta.is_admin,
        seen_steps=meta.seen_steps,
        tg_chat_id=new_id,
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
