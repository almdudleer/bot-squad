"""Admin-only system settings — TG bot token + non-secret settings."""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app import secret_crypto
from app.routes_auth import require_admin

router = APIRouter(
    prefix="/system-settings",
    tags=["system-settings"],
    dependencies=[Depends(require_admin)],
)

_TTL_RE = re.compile(r"^\d+[smhd]$")
# T-0194: accepted per-installation TG egress proxy schemes. socks5h:// is the
# DNS-through-proxy variant httpx also supports; http(s):// + socks5:// are the
# DoD set. Empty string = direct egress (no proxy).
_PROXY_RE = re.compile(r"^(socks5h?|https?)://.+", re.IGNORECASE)

_DEFAULTS = {
    "tg": {
        "quiet_hours_start_utc": 17,
        "quiet_hours_end_utc": 5,
        # T-0171: per-server default Telegram chat id, used by the local
        # (detached / standalone) bot when a notify has no explicit chat/slug.
        "default_chat_id": "",
        # T-0194: per-installation TG egress proxy (socks5/http/https). Empty →
        # direct. Consumed by the worker's tg.py + tg_listener.
        "proxy_url": "",
    },
    "session": {"ttl": "7d"},
    "admin": {"coordinator_user": "almdudleer"},
}


def _mothership_state() -> dict:
    """Describe this install's relation to a mothership (T-0171).

    - ``is_self``: this install IS the mothership (``MOTHERSHIP=1``). It owns
      @bot_squad_bot, so its TG token stays editable.
    - ``url``: the upstream mothership this server consumes from, if any
      (mirrors the worker / autoupdate env lookup order).
    - ``attached``: this is an attached *consumer* — not the mothership itself,
      but pointed at one. In this state notifications flow through the
      mothership's TG bot, so the per-server token + default chat are LOCKED.

    A *detached / standalone* server is ``is_self=False`` + no ``url`` → not
    attached → token editable (it runs its own bot).
    """
    is_self = os.environ.get("MOTHERSHIP", "0") == "1"
    url = (
        os.environ.get("BOT_SQUAD_MOTHERSHIP_URL")
        or os.environ.get("BOTSQUAD_MOTHERSHIP_URL")
        or ""
    ).rstrip("/")
    attached = (not is_self) and bool(url)
    return {"is_self": is_self, "url": url or None, "attached": attached}


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
            "default_chat_id": str(
                tg.get("default_chat_id", _DEFAULTS["tg"]["default_chat_id"])
            ),
            "proxy_url": str(tg.get("proxy_url", _DEFAULTS["tg"]["proxy_url"])),
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
    out.append(f'default_chat_id = "{_toml_escape(str(settings["tg"]["default_chat_id"]))}"')
    out.append(f'proxy_url = "{_toml_escape(str(settings["tg"]["proxy_url"]))}"')
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
        # T-0179: encrypt the token at rest when a BOT_SQUAD_SECRETS_KEY is
        # configured. No key (dev / fresh install) → encrypt() is a passthrough
        # and the value stays plaintext. A cleared ("") token stays "".
        f'bot_token = "{_toml_escape(secret_crypto.encrypt(bot_token))}"',
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
    ms = _mothership_state()
    return {
        "tg": {
            "bot_token_set": _bot_token_set(config_dir),
            "default_chat_id": s["tg"]["default_chat_id"],
            # T-0194: per-installation TG egress proxy. NOT mothership-locked —
            # it's a host-network concern independent of whose bot token routes.
            "proxy_url": s["tg"]["proxy_url"],
            "quiet_hours_start_utc": s["tg"]["quiet_hours_start_utc"],
            "quiet_hours_end_utc": s["tg"]["quiet_hours_end_utc"],
            # T-0171: when this server is an attached mothership consumer, the
            # per-server bot token + default chat are LOCKED — notifications
            # flow through the mothership's @bot_squad_bot. The UI greys the
            # fields + shows a banner; the PUT handler also refuses writes.
            "managed_by_mothership": ms["attached"],
            "mothership_url": ms["url"],
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

    # T-0171: the per-server TG bot token + default chat are locked while this
    # server is an attached mothership consumer (notifications flow through the
    # mothership's bot). The UI disables the inputs; enforce server-side too so
    # a stale client or direct API call can't write them.
    locked = _mothership_state()["attached"]
    if locked and ("bot_token" in tg_in or "default_chat_id" in tg_in):
        raise HTTPException(
            status_code=409,
            detail="TG bot token + default chat are locked while attached to the mothership",
        )

    if "default_chat_id" in tg_in:
        v = tg_in["default_chat_id"]
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="tg.default_chat_id must be a string")
        current["tg"]["default_chat_id"] = v.strip()

    # T-0194: per-installation TG egress proxy. Not mothership-locked (host
    # network concern). Empty clears it; otherwise must be socks5/http(s)://.
    if "proxy_url" in tg_in:
        v = tg_in["proxy_url"]
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="tg.proxy_url must be a string")
        v = v.strip()
        if v and not _PROXY_RE.match(v):
            raise HTTPException(
                status_code=400,
                detail="tg.proxy_url must be socks5://, http://, or https:// (or empty)",
            )
        current["tg"]["proxy_url"] = v

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
