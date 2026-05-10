"""Auth routes — TG Login + logout."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.auth import AuthError, issue_jwt, verify_jwt, verify_tg_login

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "session"


@router.post("/tg")
def tg_login(request: Request, response: Response, payload: dict) -> dict:
    cfg = request.app.state.auth_config
    try:
        user = verify_tg_login(payload, cfg)
    except AuthError as e:
        msg = str(e)
        # 401 for bad signature/stale, 403 for not-allowed-id.
        if "not allowed" in msg.lower():
            raise HTTPException(status_code=403, detail=msg)
        raise HTTPException(status_code=401, detail=msg)
    token = issue_jwt(
        {"tg_id": int(user["id"]), "name": user.get("first_name", "")},
        request.app.state.jwt_secret,
        ttl_seconds=cfg.session_ttl_seconds,
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=cfg.session_ttl_seconds,
        httponly=True,
        secure=request.app.state.cookie_secure,
        samesite="lax",
    )
    return {"ok": True, "tg_id": int(user["id"])}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


def require_auth(request: Request) -> dict:
    """Dependency for routes that require an authenticated session."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        return verify_jwt(token, request.app.state.jwt_secret)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
