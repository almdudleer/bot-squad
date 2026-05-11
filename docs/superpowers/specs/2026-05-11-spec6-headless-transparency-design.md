# Headless transparency views — design

**Spec date:** 2026-05-11
**Status:** awaiting user review
**Implements:** chunk #6 of the bot-squad decomposition

## 1. Goals

Make every background operation observable from the UI so the stakeholder can answer "what is the system doing right now? what did it do yesterday? why did that deploy fail?" without ssh-ing in:

- **Deploy run history** — every queued/processing/processed deploy with status, target, reason, requested-by, timestamps, exit code, full stdout/stderr log
- **Session message history** — given a Claude session UUID (from the session manager), render the conversation transcript from its `.jsonl` log
- **Scheduler state** — list APScheduler jobs the worker runs, last fire time, next fire time, status

Read-only views in v1. Editing logs, deleting runs, manual job-fire — deferred.

## 2. Architecture

All the data already exists on disk; we just need API routes and pages. Worker exposes scheduler state via a new `scheduler_state` action. Everything else reads files directly from the API.

```
Run history          : data/<slug>/_jobs/deploy/{queue,processing,processed,runs}/*
Session messages     : ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
Scheduler state      : worker action scheduler_state — APScheduler's get_jobs()
```

## 3. New API endpoints (all behind require_auth)

```
GET /api/projects/{slug}/runs                       → list of {id, target, status, reason, requested_by, queued_at, started_at, ended_at, rc}
GET /api/projects/{slug}/runs/{id}/log              → text/plain log content
GET /api/projects/{slug}/sessions/{sid}/messages    → parsed messages [{role, ts, text, tool_use?, tool_result?}]
GET /api/scheduler                                  → proxy to worker scheduler_state
```

## 4. Schemas

### 4.1 Run list item

```json
{
  "id": "1778435487-f64c241d",
  "target": "staging",
  "status": "ok",          // "queued" | "processing" | "ok" | "fail"
  "rc": 0,
  "reason": "spec #5 e2e smoke",
  "requested_by": "S-almdudleer-process-p2",
  "queued_at": "2026-05-10T17:51:27Z",
  "started_at": "2026-05-10T17:51:47Z",
  "ended_at":   "2026-05-10T17:52:12Z"
}
```

The API computes these by reading queue/, processing/, processed/ files and parsing the JSON each contains, plus the `.ok`/`.fail.<rc>` suffix in processed/.

### 4.2 Session messages

For each line of the `.jsonl`, parse the JSON object. v1 surfaces these record types:
- `user` message → `{role: "user", ts, text}`
- `assistant` message → `{role: "assistant", ts, text, tool_uses: [...]}`
- `tool_result` → `{role: "tool", ts, name, output}`

Truncate text >5 KB per message in the API response; UI shows a "Show full" toggle that re-fetches the full message. Defer raw record dump.

### 4.3 Scheduler state

```json
{
  "jobs": [
    {"id": "heartbeat",       "next_run": "...", "trigger": "interval[60s]"},
    {"id": "deploy_monitor",  "next_run": "...", "trigger": "interval[60s]"},
    {"id": "kick_stuck",      "next_run": "...", "trigger": "cron[11:59 UTC]"},
    {"id": "oauth_refresh",   "next_run": "...", "trigger": "interval[6h]"}
  ],
  "worker_uptime_seconds": 12345.0,
  "last_heartbeat_age_seconds": 23.4
}
```

Worker exposes via new action `scheduler_state` — pulls from APScheduler's `scheduler.get_jobs()` + reads `data/_worker/heartbeat` mtime.

## 5. UI

### 5.1 Run history (`/p/:slug/runs`)

Table:
- ID (short — first 8 chars of UUID, monospace)
- Target (badge — staging/dev colored)
- Status (badge — green ok, red fail, yellow processing, gray queued)
- Reason (truncated)
- Requested by (small)
- Queued / Started / Ended (relative)
- Duration (ended − started)
- "View log" link → opens log viewer

Auto-refresh every 15s. Newest at top. Default 50 rows; "Load more" loads next 50.

### 5.2 Log viewer (`/p/:slug/runs/:id`)

- Header: target + status + duration
- `<pre>` with the full stdout/stderr (scrollable). Don't render ANSI codes for v1.
- "Back to runs" link.

### 5.3 Session messages (`/p/:slug/sessions/:sid/messages`)

Conversation-style render:
- User messages: right-aligned bubble, blue tint
- Assistant messages: left-aligned, neutral
- Tool uses/results: collapsed by default, click to expand (compact JSON)
- Auto-scroll to bottom on load; "Jump to top" button

Reachable from the Sessions page (each row's SID becomes a link to messages).

### 5.4 Scheduler dashboard (`/scheduler` — project-agnostic)

Simple grid showing each job, its trigger, last run, next run, worker uptime. Auto-refresh every 30s. Linked from the top-level nav.

## 6. Files

### Worker
- `worker/bot_squad_worker/actions.py` — `scheduler_state` action
- `worker/bot_squad_worker/scheduler.py` — export a `state_for_api(sched)` function returning the dict
- `worker/tests/test_actions.py` — add tests

### API
- `api/app/routes_runs.py` — NEW: list + log + cleanup helpers
- `api/app/routes_messages.py` — NEW: session message parsing
- `api/app/routes_scheduler.py` — NEW: proxy
- `api/app/main.py` — register routers
- `api/app/messages_parser.py` — NEW: parse one .jsonl file into a list of message dicts (lazy, line-by-line, never reads full file into memory)
- `api/tests/test_routes_runs.py`, `test_messages_parser.py`, `test_routes_messages.py`, `test_routes_scheduler.py`

### Web
- `web/src/api.ts` — methods
- `web/src/pages/Runs.tsx` — NEW
- `web/src/pages/RunLog.tsx` — NEW
- `web/src/pages/Messages.tsx` — NEW
- `web/src/pages/Scheduler.tsx` — NEW
- `web/src/App.tsx` — 4 new routes
- `web/src/pages/Sessions.tsx` — SID column becomes a link to messages page
- `web/src/pages/Project.tsx` — add "Runs" breadcrumb link

## 7. Risks

| Risk | Mitigation |
|---|---|
| .jsonl files multi-MB → memory blow if loaded fully | Parser MUST iterate line-by-line. No `f.read()`. |
| Log file 10+ MB → slow page render | API returns first 2 MB by default; UI "Show full" param requests rest |
| Worker scheduler.get_jobs() expensive | APScheduler caches; not actually a problem, but cache it for 5s in the action handler anyway |
| Messages page exposes raw model output to browser | Plain markdown render; no `dangerouslySetInnerHTML`; tool_use args shown as monospace JSON. No XSS surface for v1. |
| Session not in current user's `~/.claude/projects/` | Return 404 with helpful message ("session log not found at expected path; this may be a session from another user") |

## 8. Out of scope

- Real-time tail of running deploys / live message streams (deferred to spec #7 territory)
- Searching across sessions
- Filtering runs by date range / status (basic table sort only)
- Re-running a failed deploy from the UI (button in #5 territory)
- Aggregated metrics / SLO dashboards

## 9. Implementation order

1. Worker `scheduler_state` action + tests (small)
2. API `routes_runs.py` (list + log) + tests
3. API `messages_parser.py` + `routes_messages.py` + tests
4. API `routes_scheduler.py` + tests
5. Frontend api.ts methods + 4 pages + routes + breadcrumbs
6. Single docker build of bot-squad-api + smoke
