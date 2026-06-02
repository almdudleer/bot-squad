"""Listen for Telegram updates and route replies into sessions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import httpx


SID_RE = re.compile(r"\[(S-[A-Za-z0-9_-]+?-p\d+)")


def _last_update_id_path(cfg) -> Path:
    return cfg.data_dir / "_worker" / "tg_last_update_id"


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
    try:
        r = httpx.get(url, params=params, timeout=timeout + 5)
        r.raise_for_status()
        return r.json().get("result", [])
    except (httpx.HTTPError, ValueError):
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
    if cmd not in {"sessions", "say", "help"}:
        return None
    return (cmd, args)


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

    slash = extract_slash_command(msg)
    if slash:
        return _handle_slash(cfg, chat_id, *slash)

    reply = extract_reply_target(msg)
    if reply:
        return _handle_reply(cfg, chat_id, *reply)

    return {"ok": True, "action": "skip", "reason": "not a reply or command"}


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
    """Lightweight outbound message -- bypass debounce, no SID prefix."""
    if not cfg.tg_bot_token:
        return
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/sendMessage"
    try:
        httpx.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    except httpx.HTTPError:
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
