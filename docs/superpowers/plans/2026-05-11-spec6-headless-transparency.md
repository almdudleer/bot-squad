# Headless transparency views — implementation plan

> Implement inline or via single non-recursive subagent. NO nested subagent dispatching (causes Node heap exhaustion across processes). NO parallel docker builds.

**Goal:** Run history page + log viewer + session message transcript + scheduler dashboard. All read-only.

**Spec:** `docs/superpowers/specs/2026-05-11-spec6-headless-transparency-design.md`

## Phase 1 — worker scheduler_state action

### Task 1: `worker: scheduler_state action`

Add a function `state_for_api(sched: BackgroundScheduler) -> dict` in `worker/bot_squad_worker/scheduler.py`:

```python
def state_for_api(sched, cfg) -> dict:
    jobs = []
    for j in sched.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        })
    hb = cfg.heartbeat_path
    hb_age = None
    if hb.exists():
        hb_age = time.time() - hb.stat().st_mtime
    return {
        "jobs": jobs,
        "worker_started_at": _STARTED_AT.isoformat() if _STARTED_AT else None,
        "last_heartbeat_age_seconds": hb_age,
    }
```

Track `_STARTED_AT = datetime.now(timezone.utc)` at module top.

Add `_action_scheduler_state` action that takes no params and returns this dict.

Tricky bit: the action handler needs a handle to the scheduler. Use module-level `_SCHED: BackgroundScheduler | None = None`. The entrypoint (`__main__.py`) sets it via a setter after creating the scheduler. Action dispatches: `state_for_api(_SCHED, cfg)`. Tests can monkeypatch `_SCHED` with a stub.

Tests in `worker/tests/test_actions.py`: action returns the expected shape; rejects extra params.

Commit: `worker: scheduler_state action`

Restart worker.

## Phase 2 — API routes

### Task 2: `api: runs list + log endpoints`

`api/app/routes_runs.py` (under `/api/projects/{slug}`):

- `GET /runs` — iterate queue/, processing/, processed/ in `data/<slug>/_jobs/deploy/`. For each file, parse the JSON inside (queued/processing have the original request; processed have it too plus the suffix indicates ok/fail). The runs/ dir has the log files indexed by id. Combine into a list of `{id, target, status, reason, requested_by, queued_at, started_at?, ended_at?, rc?}`. Status derived: in queue → "queued"; in processing → "processing"; in processed with .ok → "ok"; in processed with .fail.N → "fail".
- `GET /runs/{id}/log` — read `data/<slug>/_jobs/deploy/runs/<id>.log`. Return as text/plain. 404 if missing. Cap at 2 MB by default; if `?full=1` query param, return whole file.

Tests: empty state, mixed states (some queued, some processed), log exists/missing, cap behavior.

Commit: `api: runs list + log endpoints`

### Task 3: `api: session messages parser + endpoint`

`api/app/messages_parser.py` — a generator that opens a `.jsonl` line-by-line and yields dicts:

```python
def parse_messages(path: Path, limit: int = 200, offset: int = 0) -> list[dict]:
    out = []
    with open(path) as f:
        for i, line in enumerate(f):
            if i < offset: continue
            if len(out) >= limit: break
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            mt = rec.get("type") or rec.get("role")
            # ... map to {role, ts, text, tool_use?, tool_result?}
            out.append(mapped)
    return out
```

`api/app/routes_messages.py`:

- `GET /api/projects/{slug}/sessions/{sid}/messages?limit=200&offset=0` — find the `.jsonl` via the same UUID-resolution as the worker's `discover_claude_uuid`. Return parsed records. 404 if not found.

Per record max 5 KB text; full-text fetch via `?full=1`.

Tests with fixture .jsonl files.

Commit: `api: session messages parser + endpoint`

### Task 4: `api: scheduler proxy endpoint`

`api/app/routes_scheduler.py` — `GET /api/scheduler` proxies to worker `scheduler_state`. Behind `require_auth`. Tests via fake worker fixture.

Commit: `api: scheduler proxy endpoint`

## Phase 3 — Frontend

### Task 5: `web: Runs, RunLog, Messages, Scheduler pages + routing`

- `api.ts` additions: `runs(slug)`, `runLog(slug, id, full?)`, `sessionMessages(slug, sid, limit, offset, full?)`, `scheduler()`
- `App.tsx` new routes: `/p/:slug/runs`, `/p/:slug/runs/:id`, `/p/:slug/sessions/:sid/messages`, `/scheduler`
- `pages/Runs.tsx` — table per spec §5.1, auto-refresh 15s
- `pages/RunLog.tsx` — header + `<pre>` log
- `pages/Messages.tsx` — conversation render per spec §5.3 (user-right, assistant-left, tool-use collapsed)
- `pages/Scheduler.tsx` — table with worker uptime + 4 jobs
- `pages/Project.tsx` — add "Runs" breadcrumb link
- `pages/Sessions.tsx` — SID becomes `<Link to={`/p/${slug}/sessions/${sid}/messages`}>`
- Top-level nav (in `App.tsx` or a layout component): add a small `Scheduler` link visible on every page

Clean styling per spec. Use existing `Modal` component if any modal is needed.

Commit: `web: Runs + RunLog + Messages + Scheduler pages`

## Phase 4 — Build + smoke

1. `cd /home/www/bot-squad/web && npm run build`
2. Verify `free -m` shows >5 GB available
3. `cd /home/www/bot-squad && docker compose build bot-squad-api`
4. Sleep 5s
5. `docker compose up -d bot-squad-api`
6. `sleep 5; curl -s https://bot-squad.dev.uzinvestapi.com/api/health`
7. Smoke each new endpoint with curl (after login)
8. `git push origin master`

No commit for Phase 4.

## Self-review

- 5 commits total (4 api + 1 web)
- API tests ~135 (was 114 + ~21 new)
- Worker tests ~95 (was 93 + 2)
- Public URL still 200
- Each new page renders (test by curling `/p/signal-tracker/runs` returns HTML 200)
- Memory stayed below 8 GB used throughout (`tail -30 /tmp/memwatch.log`)
- No regressions in existing routes
