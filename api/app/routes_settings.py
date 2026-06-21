"""Admin-only system settings — TG bot token + non-secret settings."""
from __future__ import annotations

import os
import re
import tomllib
from datetime import datetime, timezone
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
    # T-0239: user-settable resource caps the system enforces at spawn-time.
    # 0 (or absent) = unlimited, so a fresh/legacy install is uncapped (back-
    # compat). max_parallel_sessions caps simultaneously-live sessions; the
    # token cap bounds aggregate usage (enforced against the telemetry quota).
    "caps": {"max_parallel_sessions": 0, "max_total_tokens": 0},
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


# Sections this endpoint hand-emits; everything else in the file is preserved
# verbatim on write (T-0368).
_MANAGED_SECTIONS = ("tg", "session", "admin", "caps")


def _now_iso() -> str:
    """UTC timestamp for the [quota] anchor (T-0418). String-compared by the
    worker's _anchor_key, so a stable second-resolution Z form is enough."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_quota(config_dir: Path) -> dict:
    """The current [quota] anchor dict ({set_at, budget_tokens, ...}) from disk,
    or {} if unset/unreadable. Read fresh so put_settings sees an operator's
    deliberate anchor and never clobbers it (T-0418)."""
    path = config_dir / "system_settings.toml"
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    q = raw.get("quota")
    return dict(q) if isinstance(q, dict) else {}


def _toml_value(v: object) -> str:
    """Render a scalar TOML value for a preserved (unmanaged) section."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return f'"{_toml_escape(str(v))}"'


def _read_system_settings(config_dir: Path) -> dict:
    path = config_dir / "system_settings.toml"
    if not path.exists():
        return {
            "tg": dict(_DEFAULTS["tg"]),
            "session": dict(_DEFAULTS["session"]),
            "admin": dict(_DEFAULTS["admin"]),
            "caps": dict(_DEFAULTS["caps"]),
        }
    raw = tomllib.loads(path.read_text())
    tg = raw.get("tg", {}) or {}
    sess = raw.get("session", {}) or {}
    admin = raw.get("admin", {}) or {}
    caps = raw.get("caps", {}) or {}

    def _cap(key: str) -> int:
        try:
            v = int(caps.get(key, _DEFAULTS["caps"][key]))
        except (TypeError, ValueError):
            v = _DEFAULTS["caps"][key]
        return v if v > 0 else 0

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
        "caps": {
            "max_parallel_sessions": _cap("max_parallel_sessions"),
            "max_total_tokens": _cap("max_total_tokens"),
        },
    }


def _write_system_settings(config_dir: Path, settings: dict, quota_override: dict | None = None) -> None:
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
    out.append("[caps]")
    out.append(f"max_parallel_sessions = {int(settings['caps']['max_parallel_sessions'])}")
    out.append(f"max_total_tokens = {int(settings['caps']['max_total_tokens'])}")
    out.append("")
    path = config_dir / "system_settings.toml"
    # T-0368: PRESERVE any top-level section this endpoint doesn't manage (e.g.
    # [max], the T-0247 MAX-DM recipient) — this writer hand-emits only the
    # managed sections, so without this a settings save silently DROPPED [max]
    # and reverted operator->stakeholder DMs to TG-only on the next worker reload.
    try:
        existing = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        existing = {}
    # T-0418: put_settings may stamp/override the [quota] anchor (token-cap
    # free) while every OTHER unmanaged section is still preserved verbatim.
    if quota_override is not None:
        existing = {**existing, "quota": quota_override}
    for name, body in existing.items():
        if name in _MANAGED_SECTIONS or not isinstance(body, dict):
            continue
        out.append(f"[{name}]")
        for key, value in body.items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
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
        # T-0239: resource caps — the contract Team 2's caps UI (T-0240) reads.
        "caps": {
            "max_parallel_sessions": s["caps"]["max_parallel_sessions"],
            "max_total_tokens": s["caps"]["max_total_tokens"],
        },
    }


@router.get("")
def get_settings(request: Request) -> dict:
    config_dir: Path = request.app.state.api_config.config_dir
    return _shape(config_dir)


@router.put("")
def put_settings(request: Request, payload: dict) -> dict:
    config_dir: Path = request.app.state.api_config.config_dir
    current = _read_system_settings(config_dir)
    # PASS-2 P2-01-BE: only a BOOT-CACHED field change needs a worker restart.
    # tg.proxy_url is read once at listener/worker boot; capture its old value
    # now (before validation overwrites it) to detect a real change. Caps + ttl +
    # quiet_hours + coordinator are fresh-read per spawn/tick → no restart.
    old_proxy_url = str((current.get("tg") or {}).get("proxy_url") or "")

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

    # T-0239: resource caps. Each is a non-negative int; 0 = unlimited. Partial
    # updates keep the unspecified cap. bool is rejected (isinstance(True, int)).
    caps_in = (payload.get("caps") or {}) if isinstance(payload.get("caps"), dict) else {}
    for key in ("max_parallel_sessions", "max_total_tokens"):
        if key in caps_in:
            v = caps_in[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise HTTPException(
                    status_code=400, detail=f"caps.{key} must be a non-negative int"
                )
            current["caps"][key] = v

    # T-0418 (PASS-2 P2-20): a token cap with no [quota] anchor is a permanent
    # ratchet — the worker's _output_since_anchor pins its baseline on the empty
    # anchor key and only grows, so once it hits max_total_tokens _enforce_token_cap
    # refuses EVERY spawn forever, with no in-UI free (the only release was a manual
    # [quota].set_at edit in a different section). So when a token cap is armed (>0)
    # and no anchor is set yet, auto-stamp [quota].set_at=now: the cap starts a real
    # budget period and the OPEN names its own free (re-arming re-anchors). A
    # deliberate operator anchor (set_at already present) is preserved untouched.
    quota_override: dict | None = None
    if current["caps"]["max_total_tokens"] > 0:
        quota = _read_quota(config_dir)
        if not str(quota.get("set_at", "") or ""):
            quota["set_at"] = _now_iso()
            quota_override = quota

    # T-0367: validate the bot_token BEFORE any write so the request is atomic —
    # validate-all-then-write. Previously _write_system_settings ran first, so a
    # request mixing a valid change (caps/ttl/...) with an invalid bot_token 400'd
    # yet silently persisted the other fields — and caps are spawn-time enforced,
    # so it could change the LIVE session cap while reporting failure. Nothing is
    # persisted until every field has validated.
    bot_token_to_write: str | None = None
    if "bot_token" in tg_in:
        v = tg_in["bot_token"]
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="tg.bot_token must be a string")
        bot_token_to_write = v

    # All inputs validated → persist (system settings, then secrets).
    _write_system_settings(config_dir, current, quota_override=quota_override)
    if bot_token_to_write is not None:
        sec = _read_secrets(config_dir)
        age_max = int((sec.get("telegram", {}) or {}).get("auth_age_max", 86400))
        _write_secrets(config_dir, bot_token_to_write, age_max)

    result = _shape(config_dir)
    result["ok"] = True
    # PASS-2 P2-01-BE: restart_required iff a boot-cached field actually changed.
    # bot_token is write-only (the FE never receives it, so a provided value is a
    # new token); proxy_url is shown + resent by the System-Settings form, so
    # compare to the pre-write value. Everything else is fresh-read → no restart,
    # so a caps-only save reports False and the two caps editors stop contradicting.
    boot_cached_changed = ("bot_token" in tg_in) or (
        "proxy_url" in tg_in and str(tg_in["proxy_url"]).strip() != old_proxy_url
    )
    result["restart_required"] = bool(boot_cached_changed)
    return result
