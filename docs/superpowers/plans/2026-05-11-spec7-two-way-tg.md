# Two-way Telegram — implementation plan

> NO Agent tool inside subagent. NO parallel docker builds. Mock all network + subprocess in tests.

**Goal:** Stakeholder replies to a TG notification on their phone → reply lands in the right Claude session.

**Spec:** `docs/superpowers/specs/2026-05-11-spec7-two-way-tg-design.md`

## Phase 1 — worker tg_listener module

### Task 1: `worker: tg_listener module + tests`

`worker/bot_squad_worker/tg_listener.py`:

```python
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
    allowed_chats = {p.tg_chat for p in cfg.projects.values()} - {"0"}
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
        return {"ok": True, "action": "inject", "sid": sid, "result": result}
    except A.ActionError as e:
        _notify(cfg, chat_id, f"❌ session {sid} not active — message dropped")
        return {"ok": False, "action": "inject_failed", "sid": sid, "error": str(e)}


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
    """Lightweight outbound message — bypass debounce, no SID prefix."""
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
```

Tests in `worker/tests/test_tg_listener.py` — mock `httpx.get`/`httpx.post`, mock `subprocess.run` for the action dispatch path. Tests:
1. `extract_reply_target` — well-formed reply with SID returns tuple; missing reply returns None; reply without SID returns None
2. `extract_slash_command` — `/sessions`, `/say sid hello`, `/say@botname sid hello`, `/help` all parse; `/garbage` returns None
3. `handle_update` skips non-message, skips non-allowlisted chat, dispatches reply to inject_input, dispatches /sessions to list, dispatches /say to inject_input
4. `tick` reads last_update_id, polls (mocked), processes each, writes new max
5. `tick` survives empty result, network error (mocked), missing token

Commit: `worker: tg_listener module for reply-routing + /sessions /say /help`

## Phase 2 — inject_input action + job

### Task 2: `worker: inject_input action`

In `actions.py`:

```python
_INJECT_INPUT_REQUIRED = {"sid", "text"}
_INJECT_INPUT_ALLOWED = _INJECT_INPUT_REQUIRED


def _action_inject_input(params: dict[str, Any]) -> dict[str, Any]:
    """Send text to the tmux pane for a SID (one Enter per line)."""
    extra = set(params) - _INJECT_INPUT_ALLOWED
    if extra:
        raise ActionError(f"inject_input got unexpected params: {sorted(extra)}")
    missing = _INJECT_INPUT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"inject_input missing required params: {sorted(missing)}")

    sid = params["sid"]
    text = params["text"]
    if not text.strip():
        raise ActionError("inject_input: empty text")

    from bot_squad_worker import sessions as S
    panes = S.list_panes()
    pane = next((p for p in panes if S.compute_sid(S._get_current_user(), p.window, p.pane_id) == sid), None)
    if pane is None:
        raise ActionError(f"inject_input: no live pane for sid {sid!r}")

    import subprocess
    lines_sent = 0
    for line in text.split("\n"):
        subprocess.run(
            ["tmux", "send-keys", "-t", pane.pane_id, "--", line, "Enter"],
            check=False,
        )
        lines_sent += 1
    return {"ok": True, "pane_id": pane.pane_id, "lines_sent": lines_sent}


ACTION_REGISTRY["inject_input"] = _action_inject_input
```

Tests in `test_actions.py`:
- Happy path: mocks list_panes returns a pane matching the SID, mocks subprocess.run, verifies send-keys called once per line
- Unknown SID: raises ActionError
- Empty text: raises ActionError
- Extra params: raises ActionError

Test allowlist asserter (`test_registry_lists_only_allowed_actions`) — update to include `inject_input`.

Commit: `worker: inject_input action`

## Phase 3 — Wire job + frontend hint

### Task 3: register tg_listener job + hint in Sessions.tsx

`worker/bot_squad_worker/jobs.py` — add:

```python
def tg_listener_tick(cfg) -> None:
    from bot_squad_worker import tg_listener
    try:
        tg_listener.tick(cfg)
    except Exception:
        log.exception("tg_listener_tick error")
```

`worker/bot_squad_worker/scheduler.py` — register:

```python
sched.add_job(tg_listener_tick, "interval", seconds=30, args=[cfg],
              id="tg_listener", replace_existing=True)
```

Update `scheduler_state` if there's any hardcoded job list (it shouldn't be — should iterate `sched.get_jobs()`).

`worker/tests/test_jobs.py` — add a minimal test that `tg_listener_tick(cfg)` doesn't raise (mocks tg_listener.tick).

Frontend: in `web/src/pages/Sessions.tsx` add at top:
```tsx
<div className="alert alert-info py-2 small mb-3">
  💬 Tip: reply to a Telegram <code>[SID] needs your input</code> notification —
  your reply lands in that session. Or <code>/sessions</code>, <code>/say &lt;sid&gt; &lt;text&gt;</code> via the bot.
</div>
```

Commit: `worker: register tg_listener job; web: TG reply hint on Sessions page`

## Phase 4 — Build + smoke

1. `cd /home/www/bot-squad/web && npm run build`
2. `free -m` → must show >5 GB available
3. `docker compose build bot-squad-api`
4. Sleep 5s
5. `docker compose up -d bot-squad-api`
6. `systemctl --user restart bot-squad-worker`
7. Sleep 2; verify worker up
8. `curl -s --unix-socket /home/www/bot-squad/data/_sock/worker.sock http://w/health` → 200
9. `curl -s --unix-socket .../actions/inject_input -X POST -H 'Content-Type: application/json' -d '{}'` → 400 (validates rejection)
10. Push to origin

If a TG bot already polls @watchbot for signal-tracker, the listener may not see updates. Document this in the report — it's a known limitation per spec §6.

## Self-review

- 3 commits
- Worker tests +5 (~100 total)
- API tests unchanged (no new API routes)
- Memory <8 GB throughout
- Public URL OK
- /actions/inject_input rejects bad params
