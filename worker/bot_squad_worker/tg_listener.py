"""Listen for Telegram updates and route replies into sessions."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx


SID_RE = re.compile(r"\[(S-[A-Za-z0-9_-]+?-p\d+)")


def _proxy_kwargs(cfg) -> dict:
    """T-0194: ``{"proxy": url}`` when a per-installation TG proxy is set, else
    ``{}`` — so the no-proxy httpx call shape (and trust_env) is unchanged."""
    proxy = getattr(cfg, "tg_proxy_url", "") or ""
    return {"proxy": proxy} if proxy else {}


def _last_update_id_path(cfg) -> Path:
    return cfg.data_dir / "_worker" / "tg_last_update_id"


def _poll_health_path(cfg) -> Path:
    return cfg.data_dir / "_worker" / "tg_poll_health.json"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_poll_health(cfg, *, ok: bool, error: str = "") -> None:
    """Persist TG poll health so an operator can tell 'no mail' from an egress
    error (next-wave #13). On a successful poll stamp ``last_ok_poll_at``; on a
    network error stamp ``last_error`` + ``last_error_at`` WITHOUT clearing the
    last-good timestamp (so 'last ok 10m ago, erroring since' is visible).
    Best-effort + atomic (tmp+replace); never raises — a health-write failure
    must not break polling."""
    import json
    import os
    try:
        p = _poll_health_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(p.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        now = _now_iso()
        if ok:
            state["last_ok_poll_at"] = now
            state["last_error"] = ""
        else:
            state["last_error"] = error
            state["last_error_at"] = now
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, p)
    except OSError:
        pass


def _read_last_update_id(cfg) -> int:
    p = _last_update_id_path(cfg)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return 0


def _write_last_update_id(cfg, update_id: int) -> None:
    p = _last_update_id_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(update_id))


def poll_updates(cfg, last_update_id: int, timeout: int = 25) -> list[dict]:
    """Long-poll TG getUpdates. Returns empty on no-token / network errors."""
    if not cfg.tg_bot_token:
        return []
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/getUpdates"
    params = {
        "offset": last_update_id + 1,
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    # T-0194: route inbound long-poll through the per-installation TG proxy when
    # set (mirrors tg.py egress). Only passed when configured.
    extra = _proxy_kwargs(cfg)
    try:
        r = httpx.get(url, params=params, timeout=timeout + 5, **extra)
        r.raise_for_status()
        result = r.json().get("result", [])
        _record_poll_health(cfg, ok=True)
        return result
    except (httpx.HTTPError, ValueError) as e:
        # next-wave #13: don't swallow the error SILENTLY — record it so the
        # operator can distinguish a DPI/egress failure from a genuine no-mail
        # poll. Still returns [] so one bad poll never poisons the offset.
        _record_poll_health(cfg, ok=False, error=repr(e))
        return []


def extract_reply_target(message: dict) -> Optional[tuple[str, str]]:
    """Extract (sid, text) if this is a reply to a worker notification."""
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return None
    quoted = reply_to.get("text") or ""
    m = SID_RE.match(quoted)
    if not m:
        return None
    return (m.group(1), message.get("text", "").strip())


def extract_slash_command(message: dict) -> Optional[tuple[str, str]]:
    """Extract (cmd, args_str) if this is a /sessions, /say, or /help command."""
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    cmd = parts[0].lstrip("/").split("@")[0]   # strip @botname if present
    args = parts[1] if len(parts) > 1 else ""
    if cmd not in {"sessions", "say", "help", "project"}:
        return None
    return (cmd, args)


def _api_base_url() -> str:
    """Base URL of the mothership API the worker links TG senders against.

    T-0527: read a DEDICATED ``WORKER_API_BASE_URL`` first, falling back to
    ``MOTHERSHIP_BASE_URL``. T-0488 originally overloaded ``MOTHERSHIP_BASE_URL``
    for the worker's localhost API target, but that collides with the API
    container's OWN ``MOTHERSHIP_BASE_URL`` (its public mothership self-URL) in
    the shared ``.env`` — and on this host the worker's ``.env`` load wins over
    the systemd ``Environment=`` override regardless of textual order, so the
    override never took effect (worker kept calling the public URL). A dedicated
    var set ONLY in ``.env`` (``WORKER_API_BASE_URL=http://127.0.0.1:8099``) has
    no such conflict. Empty when neither is set → linkage is a no-op."""
    return (
        os.environ.get("WORKER_API_BASE_URL")
        or os.environ.get("MOTHERSHIP_BASE_URL")
        or ""
    ).rstrip("/")


def _worker_api_token() -> str:
    """T-0488: the shared-secret Bearer the worker presents to the token-gated
    linkage endpoint. From ``WORKER_API_TOKEN``; empty when unset → no-op."""
    return (os.environ.get("WORKER_API_TOKEN") or "").strip()


def _sender_display_name(frm: dict) -> str:
    """Human label for a TG sender: ``first last`` if present, else username."""
    name = " ".join(p for p in (frm.get("first_name"), frm.get("last_name")) if p).strip()
    return name or str(frm.get("username") or "")


def resolve_or_link_sender(cfg, msg: dict, slug: str = "") -> Optional[dict]:
    """T-0488: recognize the inbound TG sender as a cross-server mothership
    GlobalUser, linking it on first contact.

    The API owns the GlobalUser registry (single-writer-per-store); the worker
    only READS ``_mothership`` elsewhere and never writes it. So this calls the
    token-gated ``POST /api/m/tg/resolve-or-link`` endpoint (httpx, mirroring the
    autoupdate worker->API precedent) — NOT through the TG egress proxy, this is
    a local-API call, not Telegram traffic.

    Best-effort + env-gated: returns ``None`` (no-op, no HTTP) when there's no
    sender id, or when the API base URL / worker token aren't configured — so a
    misconfigured or pre-secret deploy never blocks inbound routing. On success
    returns the resolved identity ``{global_user_id, created, slug}`` for the
    downstream user-conversation seam (anchored on ``(slug, global_user_id)``)."""
    frm = msg.get("from") or {}
    tg_user_id = str(frm.get("id") or "").strip()
    if not tg_user_id:
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    url = f"{base}/api/m/worker/tg/resolve-or-link"  # T-0529: /worker prefix (localhost-only)
    try:
        r = httpx.post(
            url,
            json={
                "tg_user_id": tg_user_id,
                "display_name": _sender_display_name(frm),
                "slug": slug,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("global_user_id"):
        return None
    return {
        "global_user_id": data["global_user_id"],
        "created": bool(data.get("created", False)),
        "slug": slug,
    }


def _msg_attachments(msg: dict) -> list[dict]:
    """Light attachment descriptors for the conversation record. We keep just
    enough to know an attachment was present (type + file_id) — the binary lives
    in TG, not the thread. Voice is the live case (T-0386); photos/documents are
    recorded generically so the thread isn't silently lossy."""
    out: list[dict] = []
    voice = msg.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        out.append({"type": "voice", "file_id": voice["file_id"]})
    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        out.append({"type": "document", "file_id": doc["file_id"]})
    if msg.get("photo"):
        out.append({"type": "photo"})
    return out


def append_conversation(cfg, slug: str, global_user_id: str, msg: dict) -> Optional[bool]:
    """T-0489: record one inbound TG user message to the per-(project, user)
    conversation history store — the durable thread "we can always look up"
    (voice-04), the continuity substrate across session recycles.

    The API owns the store (single-writer); the worker POSTs to the token-gated
    append endpoint over the same worker->API path T-0488 established (httpx,
    base=MOTHERSHIP_BASE_URL, Bearer=WORKER_API_TOKEN) — NOT through the TG
    egress proxy (this is a local-API call, not Telegram traffic).

    Best-effort + env-gated: returns ``None`` (no-op, no HTTP) when there's no
    ``global_user_id``, or the API base / worker token aren't configured — so a
    record failure NEVER blocks inbound routing. Returns ``True`` on a recorded
    append."""
    gid = str(global_user_id or "").strip()
    if not gid or not str(slug or ""):
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    url = f"{base}/api/m/worker/conversations/{slug}/{gid}/messages"  # T-0529: /worker prefix
    try:
        r = httpx.post(
            url,
            json={
                "author": "user",
                "text": msg.get("text") or "",
                "attachments": _msg_attachments(msg),
                "timestamp": _msg_ts(msg),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return True


# ---- T-0492: hardwired project routing --------------------------------------
# A user pins a CURRENT project (a button / ``/project <slug>``); subsequent
# unquoted messages sticky-route to it; the bot ASKS which project when unset
# (voice-04: "the bot should be hardwired to ask the user which project he's
# talking to"). The pin is owned by the API (single-writer = pins_store); the
# worker reads/sets it through the token-gated routing endpoints, env-gated +
# best-effort so a routing-store outage never crashes inbound handling.


def _routing_url(base: str, global_user_id: str) -> str:
    return f"{base}/api/m/worker/routing/{global_user_id}/current-project"  # T-0529: /worker prefix


def get_current_project(cfg, global_user_id: str) -> Optional[str]:
    """The user's pinned current-project slug, or ``None`` when unset / the
    routing store is unreachable / linkage isn't configured (best-effort)."""
    gid = str(global_user_id or "").strip()
    if not gid:
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    try:
        r = httpx.get(
            _routing_url(base, gid),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict):
        slug = data.get("slug")
        return str(slug) if slug else None
    return None


def set_current_project(cfg, global_user_id: str, slug: str) -> Optional[bool]:
    """Pin (or switch) the user's current project via the API. Returns ``True``
    on success, ``None`` on a no-op / failure (best-effort + env-gated)."""
    gid = str(global_user_id or "").strip()
    if not gid or not str(slug or ""):
        return None
    base = _api_base_url()
    token = _worker_api_token()
    if not base or not token:
        return None
    try:
        r = httpx.post(
            _routing_url(base, gid),
            json={"slug": slug},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return True


def _ask_which_project(cfg, chat_id: str) -> None:
    """Hardwired 'which project?' prompt — a reply-keyboard listing every
    project. Each button sends ``/project <slug>`` as a NORMAL message, so the
    selection arrives under ``allowed_updates:["message"]`` (no poll-contract
    change; mirrors the voice-intake constraint). Best-effort."""
    if not cfg.tg_bot_token:
        return
    keyboard = [[{"text": f"/project {slug}"}] for slug in cfg.projects]
    reply_markup = {
        "keyboard": keyboard,
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    # T-0513: route through the channel abstraction (was a raw httpx sendMessage
    # that bypassed get_channel). Interactive group reply → urgent=True (don't
    # quiet-hours-drop the picker a user just triggered), sid="" (no prefix),
    # debounce=False (offer the picker every time it's needed). Best-effort: a
    # messenger outage must not break inbound routing (channel.send raises).
    _channel_notify(cfg, chat_id, "Which project are you talking to? Pick one:",
                    reply_markup=reply_markup)


def _handle_project(cfg, chat_id: str, gid: str, args: str) -> dict:
    """``/project [slug]`` — pin/switch the current project, or (no arg / unknown
    slug) re-offer the picker."""
    if not gid:
        _notify(cfg, chat_id, "Couldn't identify you yet — try again in a moment.")
        return {"ok": False, "action": "project_no_identity"}
    slug = args.strip()
    if not slug:
        _ask_which_project(cfg, chat_id)
        return {"ok": True, "action": "ask_project"}
    if slug not in cfg.projects:
        _notify(cfg, chat_id, f"Unknown project: {slug}")
        _ask_which_project(cfg, chat_id)
        return {"ok": False, "action": "project_unknown", "slug": slug}
    ok = set_current_project(cfg, gid, slug)
    if not ok:
        _notify(cfg, chat_id, f"Couldn't switch to {slug} right now — try again.")
        return {"ok": False, "action": "project_set_failed", "slug": slug}
    _notify(cfg, chat_id, f"You're on project {slug}. Messages now go there.")
    return {"ok": True, "action": "project_set", "slug": slug}


def _handle_unquoted(cfg, chat_id: str, gid: str, msg: dict) -> dict:
    """An unquoted (non-reply, non-command) message. Sticky-route it to the
    user's pinned project; ask which project when unset. Unrecognized senders
    keep the pre-T-0492 skip behavior (no identity to anchor routing on)."""
    if not gid:
        return {"ok": True, "action": "skip", "reason": "not a reply or command"}
    sticky = get_current_project(cfg, gid)
    if sticky:
        return {"ok": True, "action": "route", "slug": sticky}
    _ask_which_project(cfg, chat_id)
    return {"ok": True, "action": "ask_project"}


def handle_update(cfg, update: dict) -> dict:
    """Dispatch one update. Returns a small audit dict."""
    msg = update.get("message")
    if not msg:
        return {"ok": True, "action": "skip", "reason": "no message"}

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))

    # Allowlist: only accept from registered tg_chat values across projects.
    allowed_chats = {str(p.tg_chat) for p in cfg.projects.values()} - {"0"}
    if chat_id not in allowed_chats:
        return {"ok": True, "action": "skip", "reason": f"chat {chat_id} not allowlisted"}

    slug = _slug_for_chat(cfg, chat_id)

    # T-0488: recognize the TG sender as a cross-server GlobalUser (link on first
    # contact). Best-effort + env-gated, so inbound routing below is never
    # blocked by linkage. Surfaced on the audit dict as (slug, global_user_id)
    # for the downstream user-conversation seam.
    identity = resolve_or_link_sender(cfg, msg, slug)

    # T-0489: record EVERY inbound user message to the conversation history store
    # (the durable thread, voice-04) before routing — so the record is complete
    # regardless of how (or whether) the message routes below. Best-effort +
    # env-gated; only when the sender is a recognized GlobalUser (no gid => no
    # anchor to key the thread on).
    gid = identity.get("global_user_id") if identity else ""
    if gid:
        append_conversation(cfg, slug, gid, msg)

    slash = extract_slash_command(msg)
    if slash:
        cmd, args = slash
        # T-0492: /project pins/switches the user's current project (needs the
        # sender identity, which _handle_slash doesn't carry) — route it here.
        if cmd == "project":
            result = _handle_project(cfg, chat_id, gid, args)
        else:
            result = _handle_slash(cfg, chat_id, cmd, args)
    else:
        reply = extract_reply_target(msg)
        if reply:
            result = _handle_reply(cfg, chat_id, *reply)
        # T-0386 Phase 2: a voice message → transcribe + store as a feedback
        # artifact. Flag-off-safe: gated on [voice].enabled (default off) so
        # deploying the voice code is a no-op until the 1-time stakeholder TG
        # setup flips it on.
        elif msg.get("voice") and getattr(cfg, "voice_enabled", False):
            from bot_squad_worker import voice_intake as _vi
            r = _vi.process_voice(cfg, slug, msg, ts=_msg_ts(msg))
            result = {"ok": r.get("ok", True), "action": "voice", "slug": slug, "result": r}
        else:
            # T-0492: an unquoted message sticky-routes to the user's pinned
            # project (asks which when unset). Unrecognized senders keep the
            # pre-T-0492 skip.
            result = _handle_unquoted(cfg, chat_id, gid, msg)

    if identity and identity.get("global_user_id"):
        result.setdefault("global_user_id", identity["global_user_id"])
    return result


def _slug_for_chat(cfg, chat_id: str) -> str:
    """Resolve the project slug whose tg_chat == chat_id (allowlist already passed)."""
    for slug, p in cfg.projects.items():
        if str(getattr(p, "tg_chat", "")) == str(chat_id):
            return slug
    return ""


def _msg_ts(msg: dict) -> str:
    """ISO-8601 UTC timestamp for a message — from its TG `date`, else now."""
    from datetime import datetime, timezone
    d = msg.get("date")
    when = (
        datetime.fromtimestamp(d, tz=timezone.utc)
        if isinstance(d, (int, float))
        else datetime.now(timezone.utc)
    )
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _handle_reply(cfg, chat_id: str, sid: str, text: str) -> dict:
    """Find the pane by SID and inject the text."""
    from bot_squad_worker import actions as A
    try:
        result = A.dispatch("inject_input", {"sid": sid, "text": text})
        # T-0155: the stakeholder answered via TG — the agent is no longer
        # blocked on him; cancel any pending stall escalation.
        _clear_stall(cfg, chat_id, sid)
        return {"ok": True, "action": "inject", "sid": sid, "result": result}
    except A.ActionError as e:
        _notify(cfg, chat_id, f"❌ session {sid} not active — message dropped")
        return {"ok": False, "action": "inject_failed", "sid": sid, "error": str(e)}


def _clear_stall(cfg, chat_id: str, sid: str) -> None:
    """Clear ``sid``'s stall marker in whichever project owns ``chat_id``."""
    try:
        from bot_squad_worker import tg_stall as _tg_stall
        for slug, p in cfg.projects.items():
            if str(getattr(p, "tg_chat", "")) == str(chat_id):
                _tg_stall.clear_blocked(cfg, slug, sid)
    except Exception:  # noqa: BLE001
        pass


def _handle_slash(cfg, chat_id: str, cmd: str, args: str) -> dict:
    """Implement /sessions, /say, /help."""
    from bot_squad_worker import actions as A, sessions as S
    if cmd == "sessions":
        # List all sessions across all registered projects
        rows: list[str] = []
        for slug in cfg.projects:
            for row in S.list_sessions(cfg, slug):
                rows.append(f"  {row['sid']}  win={row['window']}  status={row['status']}")
        body = "Active sessions:\n" + ("\n".join(rows) if rows else "  (none)")
        _notify(cfg, chat_id, body)
        return {"ok": True, "action": "sessions", "count": len(rows)}

    if cmd == "say":
        # /say <sid> <text>
        parts = args.split(None, 1)
        if len(parts) < 2:
            _notify(cfg, chat_id, "Usage: /say <sid> <text>")
            return {"ok": False, "action": "say_usage"}
        sid, text = parts[0], parts[1]
        try:
            result = A.dispatch("inject_input", {"sid": sid, "text": text})
            return {"ok": True, "action": "say", "sid": sid, "result": result}
        except A.ActionError as e:
            _notify(cfg, chat_id, f"❌ /say failed: {e}")
            return {"ok": False, "action": "say_failed", "error": str(e)}

    if cmd == "help":
        _notify(cfg, chat_id,
                "Reply to a notification to inject text into the session.\n"
                "/sessions — list active sessions\n"
                "/say <sid> <text> — direct inject without reply-quoting")
        return {"ok": True, "action": "help"}

    return {"ok": False, "action": "unknown_cmd"}


def _notify(cfg, chat_id: str, message: str) -> None:
    """Lightweight outbound command reply -- no SID prefix, no debounce.

    T-0513: routes through the channel abstraction (``channels.get_channel``)
    instead of a raw httpx ``sendMessage`` that bypassed it. The egress proxy,
    token gate, and quiet-hours policy now live in one place (tg.py via the
    channel), not duplicated here.
    """
    _channel_notify(cfg, chat_id, message)


def _channel_notify(
    cfg, chat_id: str, message: str, *, reply_markup: dict | None = None
) -> None:
    """Send an interactive group reply via the channel abstraction (T-0513).

    These are replies to a user actively messaging the bot in its project
    group, so: ``urgent=True`` (never quiet-hours-drop a reply the user just
    asked for), ``sid=""`` (no ``[SID]`` prefix), ``debounce=False`` (echo every
    time, not once per 60s). Best-effort — ``channel.send`` raises on a
    transport/API error and an outage must not break inbound command handling.
    """
    if not cfg.tg_bot_token:
        return
    from bot_squad_worker import channels as _channels

    extra: dict[str, Any] = {"debounce": False}
    if reply_markup is not None:
        extra["reply_markup"] = reply_markup
    try:
        _channels.get_channel(cfg, project=_slug_for_chat(cfg, chat_id)).send(
            message, chat_id=chat_id, sid="", urgent=True, **extra
        )
    except Exception:  # noqa: BLE001 — best-effort; never break inbound routing
        pass


def tick(cfg) -> dict:
    """One poll-and-process cycle. Called by the APScheduler job."""
    last_id = _read_last_update_id(cfg)
    updates = poll_updates(cfg, last_id, timeout=25)
    handled = 0
    max_id = last_id
    for update in updates:
        uid = update.get("update_id", 0)
        max_id = max(max_id, uid)
        try:
            handle_update(cfg, update)
            handled += 1
        except Exception:
            # Log via logging would be nicer; for now just swallow so one bad
            # update doesn't poison the offset.
            continue
    if max_id > last_id:
        _write_last_update_id(cfg, max_id)
    return {"ok": True, "polled": len(updates), "handled": handled, "max_update_id": max_id}
