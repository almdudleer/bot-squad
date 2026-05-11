# Two-way Telegram — design

**Spec date:** 2026-05-11
**Status:** awaiting user review
**Implements:** chunk #7 of the bot-squad decomposition

## 1. Goal

Today TG is outbound-only: the worker sends a `[SID] needs your input` ping when a session is idle. The stakeholder reads it on their phone but can't *reply* — they have to ssh into the tmux session, find the right pane, and type. Two-way TG lets the stakeholder **reply to the notification on Telegram** and have that reply land in the right session as if they typed it.

## 2. Architecture

```
TG (long-poll getUpdates 30s)
    │
    ▼
bot-squad-worker  ── TgListener job (APScheduler)
    │
    ├─ filter: message is a reply to a previous worker-bot message
    │
    ├─ extract SID from the replied-to message body (regex `^\[(S-[A-Za-z0-9_-]+(?:-p\d+)?)`)
    │
    ├─ resolve SID → tmux pane (via sessions.list_panes())
    │
    └─ tmux send-keys -t <pane_id> "<reply text>" Enter
```

Plus an optional **slash-command surface** for direct control without a reply:
- `/sessions` — bot replies with the list of active sessions (SID + window + last activity)
- `/say <SID> <text>` — send `<text>` to that SID's pane (escape hatch when reply-quoting is lost)

## 3. Components

### 3.1 New worker module `tg_listener.py`

- `poll_updates(cfg, last_update_id) -> list[dict]` — `httpx.get` to `getUpdates?offset=<last_update_id+1>&timeout=25` (long-poll). Returns the `result` array. Handles bot token absence (skip silently). Catches network errors (log + return []).
- `extract_reply_target(message) -> tuple[sid, text] | None` — pulls SID from `message.reply_to_message.text` via regex. Returns None if not a reply or no SID match.
- `extract_slash_command(message) -> tuple[cmd, args] | None` — `/sessions` and `/say <sid> <text>` parsing.
- `handle_update(cfg, update) -> dict` — dispatches one update to either `inject_input` or `slash_handler`. Returns `{ok, action, detail}`.

### 3.2 New worker action `inject_input`

```
params: {sid: str, text: str}
returns: {ok: true, pane_id, lines_sent: int}
```

Resolves SID via `sessions.list_panes()` filtering by SID. If pane not found, raises ActionError. Splits text on `\n`, sends each line with `tmux send-keys -t <pane_id> "<line>"` then `Enter`. The plain-text content goes as-is; tmux send-keys handles quoting via `--`.

### 3.3 New time-driven job `tg_listener`

Schedule: `interval, seconds=30`. Reads `data/_worker/tg_last_update_id` (single-line file holding the latest seen update_id), polls TG, processes each update via `handle_update`, writes new last_update_id back.

### 3.4 Cooldown / dedup

- Each update_id processed at most once (the file is the dedup state).
- If a reply targets a pane that's gone (paused/exited), reply with a TG message: `❌ session <sid> is not active — message dropped`.

### 3.5 No webhook needed

Long-poll is enough — single bot, low volume, no public IP juggling. Webhook deferred.

## 4. New TG bot command surface

Three commands, registered via BotFather (manual step) or just documented:

| Command | What | Who |
|---|---|---|
| (no command, just reply to a worker notification) | Inject the reply text into the session by SID | stakeholder |
| `/sessions` | List active SIDs + windows | stakeholder |
| `/say <SID> <text>` | Direct inject without reply-quoting | stakeholder |
| `/help` | One-line usage hint | stakeholder |

## 5. UI hint (no new pages)

Sessions page (spec #5) gets a small `TG → reply to ping` hint at the top mentioning the new flow. Not a new page.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Bot conflict if signal-tracker's polling loop is using the same token | We use the same `@watchbot` token but the worker's TgListener has a SEPARATE chat scope. Solution: long-poll with `allowed_updates=["message"]` and use `offset` strictly. Signal-tracker's polling loop won't see updates we ACK; we won't see ones it ACK'd. Race possible but rare. **Better**: use a different bot for bot-squad replies. Defer; for v1 reuse @watchbot and accept the race. |
| Update gets stuck (server restart loses offset) | `tg_last_update_id` persists across restarts. On startup, resume from there. |
| Reply quotes were truncated or SID stripped | If extract_reply_target returns None, bot replies `❌ couldn't find SID in your reply; use /say <sid> <text>` |
| Wrong-user reply (someone else in the chat) | TG bot is private; chat allowlist matches `tg_chat` in projects.toml. Defer multi-user. |
| Malicious reply tries shell injection via send-keys | `tmux send-keys -t <pane_id> -- "<text>"` with `--` end-of-options. tmux's send-keys does NOT eval; it sends literal keys to the pane. The receiving claude treats it as user input. Safe. |
| `/say` exposes ALL pane writing to anyone in the allowlisted chat | Acceptable; the stakeholder IS the trusted user. |

## 7. Out of scope

- TG webhooks (long-poll only)
- Voice / image / file replies
- Multi-line markdown replies (just plain text)
- Per-session ACLs (whole-chat allowlist only)
- Reply latency <30s (long-poll polls every 25s)

## 8. Files

### Worker
- `worker/bot_squad_worker/tg_listener.py` — NEW
- `worker/bot_squad_worker/actions.py` — `inject_input` action
- `worker/bot_squad_worker/jobs.py` — `tg_listener_tick` job
- `worker/bot_squad_worker/scheduler.py` — register the new job
- `worker/tests/test_tg_listener.py` — NEW (mock httpx + subprocess)
- `worker/tests/test_actions.py` — add `inject_input` tests

### API
- No new API endpoints (worker action surface is enough)

### Web
- `web/src/pages/Sessions.tsx` — add a small "TG reply" hint at top

## 9. Implementation order

1. `tg_listener.py` poll + extract functions + tests with mocked httpx
2. `inject_input` action + tests with mocked subprocess
3. Wire into `jobs.py` + scheduler registration
4. Frontend hint
5. Single docker build + restart worker + smoke (send a real reply from stakeholder's phone if they're awake; otherwise mock with curl-injected update)
