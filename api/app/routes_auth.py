"""Auth routes — username/password login, logout, me."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

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


@router.get("/me")
def me(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        claims = verify_jwt(token, request.app.state.jwt_secret)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"username": claims.get("username", "")}


def require_auth(request: Request) -> dict:
    """Dependency for routes that require an authenticated session."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        return verify_jwt(token, request.app.state.jwt_secret)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
