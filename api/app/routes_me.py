"""Per-user state — onboarding seen-steps tracker.

Framework for T-0014..T-0022 spotlight beats. The set of step ids is owned
by the frontend; the backend just remembers which ones the logged-in user
has dismissed. POST /skip writes a sentinel so future step ids are dark by
default without an API change.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import ONBOARDING_SKIP_ALL, AuthConfig, UserMeta
from app.routes_auth import require_auth

router = APIRouter(prefix="/me", tags=["me"], dependencies=[Depends(require_auth)])


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _write_user_meta(request: Request, username: str, new_meta: UserMeta) -> None:
    """Rewrite auth.toml with one user_meta row replaced.

    Mirrors routes_users._write_auth_toml — kept as its own helper here so the
    me-endpoints don't have to import admin-only code, and so the writer is
    cheap to audit when more per-user fields land (T-0019 tg_chat_id).
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
    )
    _write_user_meta(request, username, new_meta)
    return _state_payload(new_meta)
