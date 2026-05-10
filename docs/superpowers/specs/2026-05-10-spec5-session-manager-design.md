# Session manager UI — design

**Spec date:** 2026-05-10
**Status:** awaiting user review
**Implements:** chunk #5 of the bot-squad decomposition

## 1. Goals

Surface every Claude session attached to a project's repo so the stakeholder can see, pause, resume, and spawn them from the UI:

- **List**: every active tmux pane running `claude` whose CWD is inside a registered project's `repo_path`, plus every paused session previously recorded
- **Pause**: gracefully exit the claude process, kill the pane, record the session's UUID + cwd + window name in `data/<slug>/sessions/<sid>.md`
- **Resume**: spawn a new tmux pane and run `claude --resume <UUID>` in it
- **Spawn new**: open a fresh pane in the project's repo and start `claude` (no resume; new session)
- Show per-row: SID, window name, cwd, started, last user prompt time, linked task IDs (best-effort discovery)

Multi-user is deferred — v1 sees only `almdudleer`'s panes (worker runs as `almdudleer`; cross-user tmux requires sudo). Live message-history viewer is spec #6.

## 2. Architecture

Sessions are host-level (tmux + claude). The worker is the only thing that can interact with them. The API is a thin proxy.

```
Browser ──fetch──► bot-squad-api ──UDS──► bot-squad-worker
                                          │
                                          ├── tmux list-panes (host)
                                          ├── ~/.claude/projects/<cwd>/*.jsonl (host)
                                          ├── tmux send-keys / kill-pane
                                          └── tmux new-window claude [--resume UUID]
```

### 2.1 New worker actions (typed allowlist)

| Action | Params | Returns |
|---|---|---|
| `list_sessions` | `{slug}` | `[{sid, status, window, cwd, started_at, last_prompt_at, claude_uuid, linked_tasks}]` |
| `pause_session` | `{slug, sid}` | `{ok, paused: true}` |
| `resume_session` | `{slug, sid}` | `{ok, sid: <new SID>}` (resume creates a new tmux pane → new SID with same UUID) |
| `spawn_session` | `{slug, window?, initial_prompt?}` | `{ok, sid}` |

`tmux_verify_login` action stays as-is from spec #1; the new actions reuse the same dispatch infrastructure.

### 2.2 Discovery

For each registered project:

1. Run `tmux list-panes -a -F '#{pane_id}|#{window_name}|#{pane_pid}|#{pane_current_path}|#{pane_current_command}'`
2. Filter rows where:
   - `pane_current_command` is `claude` (or starts with `claude`)
   - `pane_current_path` equals or is descendant of the project's `repo_path`
3. For each match, derive the SID using the spec #3 format: `S-<user>-<window>-p<pane_id_no_pct>`
4. Resolve claude UUID: the latest `*.jsonl` file (by mtime) in `~/.claude/projects/<encoded_cwd>/` where `encoded_cwd = cwd.replace('/', '-').lstrip('-')`. Inspect the file's last line to extract the session UUID (it's in the JSON object as `session_id` or similar — check the actual format on this host before locking in the parser).
5. `last_prompt_at` from `<repo>/.claude/last_user_prompt_ts` mtime (Stop hook touches this).
6. `linked_tasks` from a backlog task scan: any task whose `session: <sid>` or `session: <claude_uuid>` frontmatter matches this session.

Paused sessions: read `data/<slug>/sessions/*.md` files where `status: paused`. Same shape as active rows but `last_prompt_at` is the `paused_at` timestamp.

### 2.3 Pause

1. Read SID metadata (window, cwd, claude UUID).
2. Write `data/<slug>/sessions/<sid>.md` with frontmatter:
   ```yaml
   ---
   sid: S-almdudleer-tg-deeplink-p5
   status: paused
   window: tg-deeplink
   cwd: /home/almdudleer/signal_tracker_mgmt
   claude_uuid: 9d7b3153-...
   started_at: 2026-05-10T12:00:00Z
   paused_at: 2026-05-10T19:00:00Z
   linked_tasks: [T-0042]
   ---
   ```
3. Send `Ctrl-c` followed by `/exit` to the pane (graceful exit; claude saves state via UUID).
4. Wait up to 10s for the pane to exit; then `tmux kill-pane -t <pane_id>`.

### 2.4 Resume

1. Read metadata file.
2. `tmux new-window -d -n <window> -c <cwd> 'claude --resume <claude_uuid>'`
3. Compute new SID, update metadata: `status: active`, drop `paused_at`, set new SID.
4. Move metadata file from `<old-sid>.md` to `<new-sid>.md`.

### 2.5 Spawn new

1. `tmux new-window -d -n <window> -c <repo_path> claude`
2. Discover the resulting pane (latest pane in the new window), compute SID.
3. Return SID.

If `initial_prompt` is provided: after spawn, `tmux send-keys -t <pane_id> '<prompt>' Enter`. Treats the user input boundary the same as if the stakeholder typed it.

## 3. UI

New page `/p/:slug/sessions`:

- Header: project name, "+ New session" button
- Table:
  - SID (clickable → spec #6's message-history view, deferred; for v1 just text)
  - Window name
  - CWD (relative to repo for brevity)
  - Status (active|paused) badge
  - Started (relative)
  - Last activity (relative; blank for paused)
  - Linked tasks (comma-separated T-IDs, each linked to its task detail page)
  - Actions: Pause (active), Resume (paused)

Auto-refresh every 10s while the page is open.

`+ New session` modal: window name (required), initial prompt (textarea, optional). Submit → spawns and refreshes.

## 4. Components & files

### Worker
- `worker/bot_squad_worker/sessions.py` — NEW. `list_sessions(cfg, slug)`, `pause(cfg, slug, sid)`, `resume(cfg, slug, sid)`, `spawn(cfg, slug, window, initial_prompt)`. Each is a pure function callable from actions and from tests.
- `worker/bot_squad_worker/actions.py` — add 4 new entries to the registry, each calling into `sessions.py`.
- `worker/tests/test_sessions.py` — NEW. Mock subprocess.run for tmux; use real filesystem for the metadata files.

### API
- `api/app/routes_sessions.py` — NEW. GET list, POST pause/resume, POST new.
- `api/app/main.py` — register router.
- `api/tests/test_routes_sessions.py` — NEW. Use the existing fake-worker fixture (extend with the new endpoints).

### Web
- `web/src/api.ts` — add `sessions(slug)`, `pauseSession(slug, sid)`, `resumeSession(slug, sid)`, `spawnSession(slug, window, prompt?)`
- `web/src/pages/Sessions.tsx` — NEW
- `web/src/App.tsx` — register `/p/:slug/sessions`
- `web/src/pages/Project.tsx` — add a Sessions nav link in the breadcrumb row

## 5. Risks

| Risk | Mitigation |
|---|---|
| `claude` UUID parsing — log file format may change | Check `~/.claude/projects/*/*.jsonl` format on this host before locking in regex; fallback: derive UUID from filename if it's `<uuid>.jsonl` |
| Pause sends Ctrl-c then `/exit` but pane doesn't die in 10s | After timeout, force-kill via `tmux kill-pane`; metadata is already written so the state is recoverable |
| Two stakeholders click pause concurrently | The worker holds a per-session lock (file in `data/<slug>/sessions/.<sid>.lock`); second pause exits 409 |
| Resume creates a SECOND active session with same UUID | The metadata's `claude_uuid` is unique; resume guards by checking no live tmux pane exists with that UUID before spawning |
| Cross-user discovery (timpo's tmux) | Out of scope for v1; pane filter requires the worker user (almdudleer) to be the pane owner |
| `tmux send-keys` injects characters that break the prompt | initial_prompt is sent as a single literal arg; tmux send-keys quotes it. Special characters will literally appear in the prompt — fine for plain text |

## 6. Out of scope (deferred)

- Cross-user session discovery (timpo, anton, ...) — needs sudo or a per-user worker
- Live message-history viewer (spec #6)
- Killing/cleaning up dead sessions automatically
- Per-task session claim mechanism (spec #X)
- Detached/attached state distinction
- Auto-pause on idle for >N hours

## 7. Implementation order

1. Worker `sessions.py` + tests — pure functions over mocked subprocess
2. Worker action registry additions + tests
3. Worker scheduler: nothing new (these are sync actions, no time-driven jobs)
4. API routes + tests (fake-worker fixture extension)
5. Frontend api.ts methods
6. Sessions page
7. Project breadcrumb update
8. Build + deploy + smoke
