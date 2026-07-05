"""Admin-only user management — CRUD over auth.toml."""
from __future__ import annotations

import os
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import AuthConfig
from app.payload_guard import str_field
from app.roles import server_role_for
from app.routes_auth import require_admin

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_admin)])


def _coerce_bool(value: object, *, field: str = "is_admin") -> bool:
    """Strict bool for auth-toggle payloads (footgun fix).

    ``bool("false")`` is True (non-empty str), so a stringy ``is_admin="false"``
    used to silently GRANT admin = privilege escalation. Accept real JSON bools
    and the common string/int spellings; reject anything ambiguous with 400
    rather than coerce it true.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):  # bool subclass handled above
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off", ""):
            return False
    raise HTTPException(status_code=400, detail=f"{field} must be a boolean")


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _serialize_auth_toml(users: dict[str, str], user_meta: dict, session_ttl: str) -> str:
    """Hand-rolled TOML writer for auth.toml (schema is tightly controlled)."""
    out: list[str] = []
    out.append("# bot-squad authentication config. Managed by /api/users.")
    out.append("")
    out.append("[users]")
    for name in sorted(users):
        out.append(f'{name} = "{_toml_escape(users[name])}"')
    out.append("")
    for name in sorted(user_meta):
        meta = user_meta[name]
        out.append(f"[user_meta.{name}]")
        out.append(f'linux_user = "{_toml_escape(meta.linux_user)}"')
        # T-0216 Phase A: server_role is the canonical field. is_admin is kept
        # as a legacy mirror (same value, derived) so a rollback to pre-T-0216
        # code still reads admin status correctly. AuthConfig.load dual-reads:
        # explicit server_role wins, else falls back to is_admin.
        out.append(f'server_role = "{meta.server_role.value}"')
        out.append(f"is_admin = {'true' if meta.is_admin else 'false'}")
        # Only emit seen_steps when non-empty so existing auth.toml files
        # without onboarding state stay byte-identical on roundtrip.
        steps = getattr(meta, "seen_steps", ()) or ()
        if steps:
            items = ", ".join(f'"{_toml_escape(s)}"' for s in steps)
            out.append(f"seen_steps = [{items}]")
        # Only emit tg_chat_id when bound — keeps unbound users byte-identical
        # on roundtrip (same shape as seen_steps above).
        tg_chat_id = getattr(meta, "tg_chat_id", "") or ""
        if tg_chat_id:
            out.append(f'tg_chat_id = "{_toml_escape(tg_chat_id)}"')
        # T-0066: only emit attached_to_global_user when set. Unmigrated
        # rows omit the key entirely so the legacy login path (which
        # checks for a missing/empty value to fall back to local password)
        # stays a single read.
        attached = getattr(meta, "attached_to_global_user", "") or ""
        if attached:
            out.append(f'attached_to_global_user = "{_toml_escape(attached)}"')
        # T-0218: per-project personal notification overrides (un-migrated
        # users). Inline table keeps the one-key-per-line block intact and
        # avoids TOML sub-table ordering pitfalls. Omitted when empty so
        # override-less users stay byte-identical on roundtrip.
        proj_map = getattr(meta, "project_tg_chat_ids", {}) or {}
        if proj_map:
            items = ", ".join(
                f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
                for k, v in sorted(proj_map.items())
            )
            out.append(f"project_tg_chat_ids = {{{items}}}")
        out.append("")
    out.append("[session]")
    out.append(f'ttl = "{_toml_escape(session_ttl)}"')
    out.append("")
    return "\n".join(out)


def _ttl_to_string(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _write_auth_toml(request: Request, users: dict[str, str], user_meta: dict) -> None:
    cfg: AuthConfig = request.app.state.auth_config
    ttl_str = _ttl_to_string(cfg.session_ttl_seconds)
    text = _serialize_auth_toml(users, user_meta, ttl_str)
    config_dir: Path = request.app.state.api_config.config_dir
    path = config_dir / "auth.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.rename(tmp, path)
    request.app.state.auth_config = AuthConfig.load(config_dir)


def _user_to_dict(username: str, meta) -> dict:
    return {
        "username": username,
        "linux_user": meta.linux_user,
        "is_admin": meta.is_admin,
    }


def _admin_count(user_meta: dict, users: dict[str, str]) -> int:
    n = 0
    for name in users:
        meta = user_meta.get(name)
        if meta is not None and meta.is_admin:
            n += 1
    return n


def _bcrypt(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


@router.get("")
def list_users(request: Request) -> list[dict]:
    cfg: AuthConfig = request.app.state.auth_config
    out: list[dict] = []
    for name in sorted(cfg.users):
        meta = cfg.meta_for(name)
        out.append(_user_to_dict(name, meta))
    return out


@router.post("")
def create_user(request: Request, payload: dict) -> dict:
    cfg: AuthConfig = request.app.state.auth_config
    username = str_field(payload, "username")
    password = str_field(payload, "password", strip=False)
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if not password:
        raise HTTPException(status_code=400, detail="password required")
    if username in cfg.users:
        raise HTTPException(status_code=400, detail=f"user already exists: {username}")

    linux_user = str_field(payload, "linux_user") or username
    is_admin = _coerce_bool(payload.get("is_admin", False))

    new_users = dict(cfg.users)
    new_users[username] = _bcrypt(password)
    from app.config import UserMeta
    new_meta = dict(cfg.user_meta)
    new_meta[username] = UserMeta(
        linux_user=linux_user, server_role=server_role_for(is_admin)
    )

    _write_auth_toml(request, new_users, new_meta)
    fresh = request.app.state.auth_config
    return _user_to_dict(username, fresh.meta_for(username))


@router.patch("/{username}")
def patch_user(username: str, request: Request, payload: dict) -> dict:
    cfg: AuthConfig = request.app.state.auth_config
    if username not in cfg.users:
        raise HTTPException(status_code=404, detail=f"unknown user: {username}")

    from app.config import UserMeta
    current = cfg.meta_for(username)
    new_linux_user = current.linux_user
    new_is_admin = current.is_admin
    if "linux_user" in payload:
        lu = str_field(payload, "linux_user")
        if not lu:
            raise HTTPException(status_code=400, detail="linux_user must not be empty")
        new_linux_user = lu
    if "is_admin" in payload:
        new_is_admin = _coerce_bool(payload["is_admin"])

    new_meta = dict(cfg.user_meta)
    new_meta[username] = UserMeta(
        linux_user=new_linux_user,
        server_role=server_role_for(new_is_admin),
        seen_steps=current.seen_steps,
        tg_chat_id=current.tg_chat_id,
        attached_to_global_user=current.attached_to_global_user,
    )

    # Last-admin protection: refuse to demote the only remaining admin.
    if current.is_admin and not new_is_admin:
        if _admin_count(new_meta, cfg.users) < 1:
            raise HTTPException(
                status_code=400,
                detail="cannot remove admin from the last admin user",
            )

    _write_auth_toml(request, dict(cfg.users), new_meta)
    fresh = request.app.state.auth_config
    return _user_to_dict(username, fresh.meta_for(username))


@router.put("/{username}/password")
def reset_password(username: str, request: Request, payload: dict) -> dict:
    cfg: AuthConfig = request.app.state.auth_config
    if username not in cfg.users:
        raise HTTPException(status_code=404, detail=f"unknown user: {username}")
    password = str_field(payload, "password", strip=False)
    if not password:
        raise HTTPException(status_code=400, detail="password required")
    new_users = dict(cfg.users)
    new_users[username] = _bcrypt(password)
    _write_auth_toml(request, new_users, dict(cfg.user_meta))
    return {"ok": True}


@router.delete("/{username}")
def delete_user(username: str, request: Request) -> dict:
    cfg: AuthConfig = request.app.state.auth_config
    if username not in cfg.users:
        raise HTTPException(status_code=404, detail=f"unknown user: {username}")
    # Last-admin protection: never delete the only admin.
    current_meta = cfg.meta_for(username)
    new_users = dict(cfg.users)
    new_meta = dict(cfg.user_meta)
    del new_users[username]
    new_meta.pop(username, None)
    if current_meta.is_admin and _admin_count(new_meta, new_users) < 1:
        raise HTTPException(
            status_code=400,
            detail="cannot delete the last admin user",
        )
    _write_auth_toml(request, new_users, new_meta)
    return {"ok": True}
