"""Auth routes — TG Login (proxied to worker) + logout."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.auth import AuthError, issue_jwt, verify_jwt
from app.worker_client import WorkerClient, WorkerError

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "session"


@router.post("/tg")
async def tg_login(request: Request, response: Response, payload: dict) -> dict:
    """TG Login Widget endpoint.

    The HMAC verification is delegated to the worker (which holds the bot token).
    The API only checks the allowed_ids list and issues the session JWT.
    """
    cfg = request.app.state.auth_config
    client = WorkerClient(request.app.state.sock_path)
    try:
        result = await client.call_action("tg_verify_login", {"payload": payload})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=f"worker unavailable: {e}")
    if not result.get("ok"):
        raise HTTPException(
            status_code=401, detail=result.get("error", "tg login rejected")
        )
    user = result["user"]
    if int(user["id"]) not in cfg.allowed_ids:
        raise HTTPException(status_code=403, detail="not allowed")
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
