"""Admin-only system settings — TG bot token + non-secret settings."""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.routes_auth import require_admin

router = APIRouter(
    prefix="/system-settings",
    tags=["system-settings"],
    dependencies=[Depends(require_admin)],
)

_TTL_RE = re.compile(r"^\d+[smhd]$")

_DEFAULTS = {
    "tg": {
        "quiet_hours_start_utc": 17,
        "quiet_hours_end_utc": 5,
    },
    "session": {"ttl": "7d"},
    "admin": {"coordinator_user": "almdudleer"},
}


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _read_system_settings(config_dir: Path) -> dict:
    path = config_dir / "system_settings.toml"
    if not path.exists():
        return {
            "tg": dict(_DEFAULTS["tg"]),
            "session": dict(_DEFAULTS["session"]),
            "admin": dict(_DEFAULTS["admin"]),
        }
    raw = tomllib.loads(path.read_text())
    tg = raw.get("tg", {}) or {}
    sess = raw.get("session", {}) or {}
    admin = raw.get("admin", {}) or {}
    return {
        "tg": {
            "quiet_hours_start_utc": int(
                tg.get("quiet_hours_start_utc", _DEFAULTS["tg"]["quiet_hours_start_utc"])
            ),
            "quiet_hours_end_utc": int(
                tg.get("quiet_hours_end_utc", _DEFAULTS["tg"]["quiet_hours_end_utc"])
            ),
        },
        "session": {"ttl": str(sess.get("ttl", _DEFAULTS["session"]["ttl"]))},
        "admin": {
            "coordinator_user": str(
                admin.get("coordinator_user", _DEFAULTS["admin"]["coordinator_user"])
            )
        },
    }


def _write_system_settings(config_dir: Path, settings: dict) -> None:
    out: list[str] = []
    out.append("# bot-squad system settings. Managed by /api/system-settings.")
    out.append("")
    out.append("[tg]")
    out.append(f"quiet_hours_start_utc = {int(settings['tg']['quiet_hours_start_utc'])}")
    out.append(f"quiet_hours_end_utc = {int(settings['tg']['quiet_hours_end_utc'])}")
    out.append("")
    out.append("[session]")
    out.append(f'ttl = "{_toml_escape(settings["session"]["ttl"])}"')
    out.append("")
    out.append("[admin]")
    out.append(f'coordinator_user = "{_toml_escape(settings["admin"]["coordinator_user"])}"')
    out.append("")
    path = config_dir / "system_settings.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(out))
    os.rename(tmp, path)


def _read_secrets(config_dir: Path) -> dict:
    path = config_dir / "secrets.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def _write_secrets(config_dir: Path, bot_token: str, auth_age_max: int) -> None:
    out = [
        "# bot-squad secrets — gitignored.",
        "# Mounted RW into the API container so admin Settings can write here;",
        "# also mounted RO into the worker.",
        "",
        "[telegram]",
        f'bot_token = "{_toml_escape(bot_token)}"',
        f"auth_age_max = {int(auth_age_max)}",
        "",
    ]
    path = config_dir / "secrets.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(out))
    os.rename(tmp, path)


def _bot_token_set(config_dir: Path) -> bool:
    sec = _read_secrets(config_dir)
    tok = (sec.get("telegram", {}) or {}).get("bot_token", "")
    return bool(tok)


def _shape(config_dir: Path) -> dict:
    s = _read_system_settings(config_dir)
    return {
        "tg": {
            "bot_token_set": _bot_token_set(config_dir),
            "quiet_hours_start_utc": s["tg"]["quiet_hours_start_utc"],
            "quiet_hours_end_utc": s["tg"]["quiet_hours_end_utc"],
        },
        "session": {"ttl": s["session"]["ttl"]},
        "admin": {"coordinator_user": s["admin"]["coordinator_user"]},
    }


@router.get("")
def get_settings(request: Request) -> dict:
    config_dir: Path = request.app.state.api_config.config_dir
    return _shape(config_dir)


@router.put("")
def put_settings(request: Request, payload: dict) -> dict:
    config_dir: Path = request.app.state.api_config.config_dir
    current = _read_system_settings(config_dir)

    tg_in = (payload.get("tg") or {}) if isinstance(payload.get("tg"), dict) else {}
    sess_in = (
        (payload.get("session") or {}) if isinstance(payload.get("session"), dict) else {}
    )
    admin_in = (
        (payload.get("admin") or {}) if isinstance(payload.get("admin"), dict) else {}
    )

    if "quiet_hours_start_utc" in tg_in:
        v = tg_in["quiet_hours_start_utc"]
        if not isinstance(v, int) or not (0 <= v <= 23):
            raise HTTPException(status_code=400, detail="quiet_hours_start_utc must be int 0–23")
        current["tg"]["quiet_hours_start_utc"] = v
    if "quiet_hours_end_utc" in tg_in:
        v = tg_in["quiet_hours_end_utc"]
        if not isinstance(v, int) or not (0 <= v <= 23):
            raise HTTPException(status_code=400, detail="quiet_hours_end_utc must be int 0–23")
        current["tg"]["quiet_hours_end_utc"] = v

    if "ttl" in sess_in:
        v = sess_in["ttl"]
        if not isinstance(v, str) or not _TTL_RE.match(v):
            raise HTTPException(
                status_code=400,
                detail="session.ttl must match ^\\d+[smhd]$",
            )
        current["session"]["ttl"] = v

    if "coordinator_user" in admin_in:
        v = admin_in["coordinator_user"]
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(status_code=400, detail="admin.coordinator_user must not be empty")
        current["admin"]["coordinator_user"] = v.strip()

    _write_system_settings(config_dir, current)

    if "bot_token" in tg_in:
        v = tg_in["bot_token"]
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="tg.bot_token must be a string")
        sec = _read_secrets(config_dir)
        age_max = int((sec.get("telegram", {}) or {}).get("auth_age_max", 86400))
        _write_secrets(config_dir, v, age_max)

    result = _shape(config_dir)
    result["ok"] = True
    result["restart_required"] = True
    return result
