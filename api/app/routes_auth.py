"""Auth routes — username/password login, logout, me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import AuthError, issue_jwt, verify_jwt, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "session"


@router.post("/login")
def login(request: Request, response: Response, payload: dict) -> dict:
    cfg = request.app.state.auth_config
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    expected = cfg.users.get(username)
    if expected is None or not verify_password(password, expected):
        raise HTTPException(status_code=401, detail="bad credentials")

    token = issue_jwt(
        {"username": username},
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
    return {"ok": True, "username": username}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


def _enrich(claims: dict, request: Request) -> dict:
    """Attach linux_user + is_admin (+ tg_chat_id) from auth_config.user_meta to the JWT claims."""
    username = claims.get("username", "")
    cfg = request.app.state.auth_config
    meta = cfg.meta_for(username)
    return {
        "username": username,
        "linux_user": meta.linux_user,
        "is_admin": meta.is_admin,
        "tg_chat_id": meta.tg_chat_id,
    }


@router.get("/me")
def me(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        claims = verify_jwt(token, request.app.state.jwt_secret)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    return _enrich(claims, request)


def require_auth(request: Request) -> dict:
    """Dependency for routes that require an authenticated session.

    Returns enriched user dict: {username, linux_user, is_admin}.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        claims = verify_jwt(token, request.app.state.jwt_secret)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return _enrich(claims, request)


def require_admin(user: dict = Depends(require_auth)) -> dict:
    """Dependency layered on require_auth: 403 unless user is_admin."""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return user
