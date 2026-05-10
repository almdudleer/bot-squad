# Session manager UI — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** List/pause/resume/spawn Claude tmux sessions per project from the bot-squad UI.

**Spec:** `docs/superpowers/specs/2026-05-10-spec5-session-manager-design.md`

**Architecture:** Worker has tmux + claude access; exposes 4 new sync actions. API proxies. Frontend lists in a new page.

---

## Phase 1 — worker sessions module + actions

### Task 1: `worker/bot_squad_worker/sessions.py` + tests

**Files:** Create `sessions.py`, `tests/test_sessions.py`.

Functions:
- `list_panes() -> list[PaneInfo]` — runs `tmux list-panes -a -F '...'`, parses lines into a dataclass `PaneInfo(pane_id, window, pid, cwd, command)`. Returns empty if tmux not running.
- `discover_claude_uuid(cwd: str, user_home: str) -> str | None` — encodes cwd via `cwd.replace('/', '-').lstrip('-')`, looks in `<home>/.claude/projects/<encoded>/`, returns the basename (without `.jsonl`) of the most recent `*.jsonl` by mtime. None if no project dir or no jsonl.
- `compute_sid(user: str, window: str, pane_id: str) -> str` — `S-<user>-<window>-p<pane_id_strip_pct>` per spec #3.
- `list_sessions(cfg, slug) -> list[dict]` — combines live tmux scan filtered by project's `repo_path` + paused metadata files. Each row has `{sid, status, window, cwd, started_at?, last_prompt_at?, claude_uuid?, linked_tasks: list[str]}`.
- `pause(cfg, slug, sid) -> dict` — find pane via SID; write metadata with `status: paused`; send Ctrl-C + "/exit" + Enter via `tmux send-keys`; sleep up to 10s polling for pane disappearance; if still alive, `kill-pane`.
- `resume(cfg, slug, sid) -> dict` — read metadata; check no live pane has the same `claude_uuid` already; `tmux new-window -d -n <window> -c <cwd> 'claude --resume <uuid>'`; compute new SID from the new pane; rename metadata file to new sid; set `status: active`.
- `spawn(cfg, slug, window, initial_prompt=None) -> dict` — `tmux new-window -d -n <window> -c <repo_path> claude`; locate the new pane (latest pane in the new window); if initial_prompt, `tmux send-keys -t <pane_id> '<prompt>' Enter`; return `{ok, sid}`.

Tests use `monkeypatch.setattr(subprocess, "run", fake_run)` to fake tmux calls. ≥10 tests covering happy path + each branch.

TDD. Commit: `worker: sessions module (list/pause/resume/spawn)`.

### Task 2: `actions.py` register the 4 new actions

**Files:** modify `actions.py`, `tests/test_actions.py`.

Each action validates exact param keys and dispatches to `sessions.py`. Same pattern as the existing `deploy` action.

Tests: each new action callable; param validation rejects extras; unknown slug rejected.

Commit: `worker: list/pause/resume/spawn session actions`.

Restart worker: `systemctl --user restart bot-squad-worker`.

---

## Phase 2 — API routes

### Task 3: `routes_sessions.py` + tests

**Files:** Create `api/app/routes_sessions.py`, `api/tests/test_routes_sessions.py`. Modify `main.py` to register the router.

Endpoints:
- `GET /api/projects/{slug}/sessions` → proxy to worker `list_sessions`
- `POST /api/projects/{slug}/sessions/{sid}/pause` → worker `pause_session`
- `POST /api/projects/{slug}/sessions/{sid}/resume` → worker `resume_session`
- `POST /api/projects/{slug}/sessions` body `{window, initial_prompt?}` → worker `spawn_session`

All behind `require_auth`. 502 if worker unreachable. 400 on bad params (bubbles up from worker `ActionError`).

Tests use the existing fake-worker fixture pattern from `test_worker_proxy.py`. Add fake handlers for the 4 new actions returning canned responses.

Commit: `api: session management endpoints`.

---

## Phase 3 — frontend

### Task 4: api.ts + Sessions.tsx + breadcrumb link

**Files:** Modify `web/src/api.ts`, `App.tsx`, `pages/Project.tsx`. Create `web/src/pages/Sessions.tsx`.

`api.ts` additions:

```ts
sessions: (slug: string) => call<SessionRow[]>(`/api/projects/${slug}/sessions`),
pauseSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/pause`, { method: "POST" }),
resumeSession: (slug: string, sid: string) =>
    call(`/api/projects/${slug}/sessions/${encodeURIComponent(sid)}/resume`, { method: "POST" }),
spawnSession: (slug: string, window: string, initial_prompt?: string) =>
    call(`/api/projects/${slug}/sessions`, {
        method: "POST",
        body: JSON.stringify({ window, initial_prompt }),
    }),
```

`SessionRow` type with the fields the worker returns.

`App.tsx`: register `/p/:slug/sessions` route → `<Sessions />`.

`Project.tsx`: add `Sessions` link in the breadcrumb nav (alongside Vision and Feedback).

`Sessions.tsx`:
- Loads via `api.sessions(slug)` on mount
- Auto-refreshes every 10s (`setInterval`, cleared on unmount)
- Renders Bootstrap table
- "+ New session" button opens Modal with window + initial_prompt fields
- Per-row Pause/Resume buttons fire the corresponding API call then refresh

Commit: `web: Sessions page (list/pause/resume/spawn)`.

---

## Phase 4 — build + deploy + smoke

### Task 5: rebuild + verify

```bash
cd /home/www/bot-squad/api && .venv/bin/pytest -v       # ~115 tests pass
cd /home/www/bot-squad/web && npm run build              # green
cd /home/www/bot-squad && docker compose up -d --build
sleep 5
curl -k -s -o /dev/null -w '%{http_code}\n' https://bot-squad.dev.uzinvestapi.com/
```

Browser smoke (optional):
- Login alexey / d27fkhhrrf26fzm8
- Visit Sessions page on signal-tracker
- Should list at least the current claude pane (the implementer's own session)
- Spawn a new session with window "spec5-smoke" → verify a new tmux pane appears (run `tmux list-panes -a -F '#W'` from a separate shell to see it)
- Pause that session → confirm metadata file in `data/signal-tracker/sessions/`
- Resume → verify new pane spawns

If browser unavailable: curl-based smoke against the API endpoints (after login).

Push to origin.

---

## Self-review

- API tests pass (was 99 → ~115)
- Worker tests pass (was 62 → ~75)
- `docker compose up -d --build` succeeds
- /api/health 200
- Sessions page accessible from Project breadcrumb
- The current claude pane shows up in the listing (proves discovery works against this host)
