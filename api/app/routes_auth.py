"""Auth routes — username/password login, logout, me + T-0066 /attach."""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import AuthError, issue_jwt, verify_jwt, verify_password
from app.roles import GlobalRole, ServerRole

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "session"


def _is_mothership() -> bool:
    """``MOTHERSHIP=1`` build flag, mirrors the seam check in main.py."""
    return os.environ.get("MOTHERSHIP", "0") == "1"


def _derive_global_role(meta) -> GlobalRole:
    """The legacy build-flag bridge — now the TRANSITIONAL FALLBACK (T-0228).

    Used only when the session user has no resolvable GlobalUser (un-migrated,
    or a dangling attachment): every server admin on the MOTHERSHIP build is
    treated as a global admin. This keeps the bootstrap sole-admin (who may not
    have a GlobalUser yet) from being locked out. For MIGRATED users the real
    stored role is read instead — see :func:`_resolve_global_role`. The fallback
    fully retires once every admin has a GlobalUser.
    """
    if meta.server_role is ServerRole.SERVER_ADMIN and _is_mothership():
        return GlobalRole.GLOBAL_ADMIN
    return GlobalRole.GLOBAL_MEMBER


def _resolve_global_role(meta, request: Request) -> GlobalRole:
    """T-0228: resolve the session user's global role from the STORED
    ``GlobalUser.global_role`` (the fix for "the gate ignored its own field").

    Fork-1 A (operator-confirmed): a migrated user (``attached_to_global_user``
    set) is gated on their stored ``global_role``; an un-migrated user, or one
    whose GlobalUser can't be found, falls back to the legacy bridge so the
    bootstrap sole-admin is never locked out. Off the mothership build the
    mothership routes aren't mounted, so the role is irrelevant → member; the
    store import stays lazy + guarded for detach-safety.
    """
    if not _is_mothership():
        return GlobalRole.GLOBAL_MEMBER
    if meta.attached_to_global_user:
        from app.mothership_users_store import MothershipUsersStore

        cfg = request.app.state.api_config
        store = MothershipUsersStore(cfg.data_dir / "_mothership")
        gu = store.get_user(meta.attached_to_global_user)
        if gu is not None:
            return gu.global_role
    return _derive_global_role(meta)  # transitional fallback (no GlobalUser)


def _is_super_admin(meta) -> bool:
    """Fallback-bridge super-admin bool (no GlobalUser resolution). The session
    value comes from :func:`_resolve_global_role`; this stays for the un-migrated
    determination + its tests."""
    return _derive_global_role(meta) is GlobalRole.GLOBAL_ADMIN


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
    """Attach per-user metadata from auth_config.user_meta to the JWT claims.

    T-0066 adds ``is_super_admin`` (derived) and ``attached_to_global_user``
    so Bundle A's Shell.tsx restructure can gate the MOTHERSHIP sidebar
    section without a second round-trip.
    """
    username = claims.get("username", "")
    cfg = request.app.state.auth_config
    meta = cfg.meta_for(username)
    # T-0228: resolve the global role ONCE from the stored GlobalUser (migrated)
    # or the transitional fallback (un-migrated). is_super_admin/global_role both
    # derive from it; is_admin/is_super_admin keys stay for FE compat (Team 2).
    global_role = _resolve_global_role(meta, request)
    return {
        "username": username,
        "linux_user": meta.linux_user,
        "is_admin": meta.is_admin,
        "is_super_admin": global_role is GlobalRole.GLOBAL_ADMIN,
        "server_role": meta.server_role.value,
        "global_role": global_role.value,
        "tg_chat_id": meta.tg_chat_id,
        "attached_to_global_user": meta.attached_to_global_user or None,
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


# ---- T-0066: /attach — claim a ServerUser as a GlobalUser -------------------
# The logged-in server user submits their GlobalUser credentials; the server
# validates them against the mothership using its server_bearer (the bearer
# minted by /api/m/installer/connect), then writes the resulting GlobalUser
# uuid into auth.toml's ``attached_to_global_user`` field for the session's
# server-side username. Idempotent: re-attaching with the same global creds
# returns the same uuid; switching attachments requires unattach-then-attach
# (out of scope for this ticket).


def _verify_global_user(
    *,
    global_username: str,
    global_password: str,
) -> dict | None:
    """Validate global creds against the mothership; return its GlobalUser dict.

    Routing splits on the build flag rather than the wire layout: on a
    MOTHERSHIP=1 install the mothership IS this process, so we skip the
    HTTP hop and read the registry directly (same in-process correctness,
    one fewer socket). On a single-install consumer we call
    ``POST {MOTHERSHIP_API_URL}/api/m/users/verify`` with the server_bearer
    stored on disk by install.sh.

    Returns the parsed GlobalUser projection on success, ``None`` on bad
    creds, raises ``HTTPException(502)`` on transport / config failure.
    """
    if _is_mothership():
        # Same-process short-circuit: import the store lazily so non-
        # mothership consumers never load it (detach-safe).
        from app.mothership_users_store import MothershipUsersStore

        # Resolve DATA_DIR the same way main.py does; the env is the
        # source of truth, and ApiConfig.data_dir derives from CONFIG_DIR
        # which is a sibling, not the canonical knob.
        data_dir = os.environ.get("DATA_DIR", "/data")
        store = MothershipUsersStore(__import__("pathlib").Path(data_dir) / "_mothership")
        user = store.user_by_username(global_username)
        if user is None:
            return None
        if not verify_password(global_password, user.password_hash):
            return None
        return user.to_public()

    mothership_url = (
        os.environ.get("MOTHERSHIP_API_URL")
        or os.environ.get("BOT_SQUAD_MOTHERSHIP_URL")
        or os.environ.get("BOTSQUAD_MOTHERSHIP_URL")
        or ""
    ).rstrip("/")
    if not mothership_url:
        raise HTTPException(
            status_code=502,
            detail="mothership URL not configured on this server",
        )
    bearer_file = os.environ.get(
        "MOTHERSHIP_SERVER_BEARER_FILE",
        # Default mirrors install.sh:1062 — ``${BOTSQUAD_STATE_DIR}/server.token``.
        # We re-derive the same path via env so tests can point at a tmp file.
        "",
    )
    if not bearer_file or not os.path.isfile(bearer_file):
        raise HTTPException(
            status_code=502,
            detail="server bearer not available (server not connected to mothership)",
        )
    bearer = open(bearer_file, encoding="utf-8").read().strip()
    if not bearer:
        raise HTTPException(
            status_code=502,
            detail="server bearer file is empty",
        )
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            resp = client.post(
                f"{mothership_url}/api/m/users/verify",
                json={"username": global_username, "password": global_password},
                headers={"Authorization": f"Bearer {bearer}"},
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"mothership unreachable: {e}")
    if resp.status_code == 401:
        return None
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"mothership /users/verify returned {resp.status_code}",
        )
    try:
        return resp.json()
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"mothership returned non-JSON: {e}")


@router.post("/attach")
def attach(
    request: Request,
    payload: dict,
    user: dict = Depends(require_auth),
) -> dict:
    """Claim the logged-in ServerUser as a given GlobalUser (T-0066).

    Body: ``{"global_username": str, "global_password": str}``. The session
    cookie identifies which ServerUser row gets the binding written — we
    intentionally do NOT take a ``server_username`` parameter so an admin
    can't accidentally re-attach someone else's row by typing the wrong
    name. (Admin-driven cross-row attach can be a separate endpoint if a
    use case lands.)
    """
    global_username = (payload.get("global_username") or "").strip()
    global_password = payload.get("global_password") or ""
    if not global_username or not global_password:
        raise HTTPException(
            status_code=400,
            detail="global_username and global_password required",
        )

    verified = _verify_global_user(
        global_username=global_username,
        global_password=global_password,
    )
    if verified is None:
        raise HTTPException(status_code=401, detail="bad global credentials")
    global_user_id = verified.get("id")
    if not global_user_id:
        raise HTTPException(
            status_code=502, detail="mothership returned no global_user_id"
        )

    # Idempotent: a re-attach to the same global user is a 200 noop.
    server_username = user["username"]
    from app.config import AuthConfig, UserMeta

    cfg: AuthConfig = request.app.state.auth_config
    current = cfg.meta_for(server_username)
    if current.attached_to_global_user and current.attached_to_global_user != global_user_id:
        # Detaching is out of scope; refuse a silent overwrite so a misclick
        # doesn't transfer a ServerUser to the wrong GlobalUser without a
        # deliberate detach step.
        raise HTTPException(
            status_code=409,
            detail="server user already attached to a different global user",
        )

    new_meta = UserMeta(
        linux_user=current.linux_user,
        server_role=current.server_role,
        seen_steps=current.seen_steps,
        tg_chat_id=current.tg_chat_id,
        attached_to_global_user=global_user_id,
    )
    # Reuse the routes_users helpers to avoid re-implementing the atomic
    # auth.toml writer.
    from app.routes_users import _serialize_auth_toml, _ttl_to_string

    new_user_meta = dict(cfg.user_meta)
    new_user_meta[server_username] = new_meta
    ttl_str = _ttl_to_string(cfg.session_ttl_seconds)
    text = _serialize_auth_toml(dict(cfg.users), new_user_meta, ttl_str)
    config_dir = request.app.state.api_config.config_dir
    path = config_dir / "auth.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.rename(tmp, path)
    request.app.state.auth_config = AuthConfig.load(config_dir)

    # T-0067 (Bundle D) coordination seam: drop a per-(linux user) marker so
    # the UI can surface the "still need to run the per-user worker enable
    # script" banner. Best-effort — attach_hooks.on_attach swallows OSError
    # and returns ``{"ok": bool, ...}``; we deliberately do not branch on
    # the result, attach itself has already succeeded by this point.
    try:
        from app.attach_hooks import on_attach as _on_attach

        _on_attach(
            request.app.state.api_config.data_dir,
            current.linux_user,
        )
    except Exception:
        # attach_hooks is best-effort — never let a marker-write failure
        # 500 the attach itself. The next call to is_enable_pending will
        # report False, and Bundle D's surface degrades gracefully.
        pass

    return {
        "ok": True,
        "server_username": server_username,
        "global_user_id": global_user_id,
        "global_username": verified.get("username", global_username),
    }
