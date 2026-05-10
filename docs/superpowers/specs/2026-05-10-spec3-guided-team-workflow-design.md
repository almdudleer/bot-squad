# Guided team workflow — design

**Spec date:** 2026-05-10
**Status:** awaiting user review
**Implements:** chunk #3 from the bot-squad decomposition (the cctv-style guided workflow). Builds on the foundation shipped by spec #1.

## 1. Goals

Make the day-to-day flow snappy and agent-light:

- One shared dev branch (`agent_team/dev`) per project. All agents (every tmux pane, every sub-agent inside an agent_team) commit there. Agents never push, never merge, never amend; users own the staging→prod path.
- Auto-deploy to staging on a clean tree, queued via a one-line shim agents already know (`ops/bot-squad/deploy`). Worker handles queueing, gating, recipe execution, and TG-notification.
- Time-driven jobs (deploy monitor, daily kick-stuck summary, OAuth refresh) live in the worker, not in cron — your existing crontab stays untouched.
- TG notifications are outbound only in this spec: deploy events, idle-session pings, daily summaries. Two-way TG (replying back into a session) is spec #7.
- Tmux pane names carry feature context that flows into TG so you know *which* session is asking for attention.
- Hooks (Stop, plus a small refinement to SessionStart) tighten the loop.

What this spec does NOT do (still deferred):

- Backlog/Vision/Feedback **editing** UI (spec #4)
- Session manager UI — pause/resume buttons (spec #5)
- Headless run-history / message-history UI (spec #6)
- Two-way TG reply chain (spec #7)
- Autonomous mode (spec #8)
- The Vision Crystallizer (spec #X) and AGENTS.md auto-render (spec #Z)

## 2. Architecture refinement

Spec #1's two-process split holds: `bot-squad-api` (Docker, traefik) for FS reads and the auth boundary; `bot-squad-worker` (systemd, host access) for everything privileged. Spec #3 fills out the worker.

```
            user actions             time-driven jobs
            ─────────────            ─────────────────
   agent in tmux pane ─►          APScheduler:
     ops/bot-squad/deploy             • _heartbeat (60s)
       │                              • deploy_monitor (60s)
       │  (HTTP/JSON over UDS)        • kick_stuck (daily)
       ▼                              • oauth_refresh (every 6h)
   bot-squad-worker ───────────────►  per-project recipes
       │                              • data/<slug>/deploy/<target>.sh
       │ (typed action allowlist)     • per-project deploy windows
       ▼
   git, docker, tmux, claude, TG bot
```

Worker actions for spec #3 v1 (typed allowlist — kept minimal; agents continue to use bash/git directly for everything else):

| Action | Caller | Why it's an action and not bash |
|---|---|---|
| `noop` | smoke | proof-of-life (already in v1) |
| `deploy` | `ops/bot-squad-bin/deploy` shim | needs to write to bot-squad's per-project queue, which lives outside the project repo |
| `tg_notify` | hooks, scheduler | worker holds `bot_token` (split out of `auth.toml`); agents and the API container don't have it |
| `kick_stuck_now` | debug | mirrors the daily summary on demand for stakeholder use |

What's intentionally NOT an action (over-engineering avoided):

- `git_status` — agents can run `git status` themselves; `deploy_monitor` calls `subprocess.run(['git', 'status'])` directly, doesn't need to round-trip through the worker.
- `squash` — agents can run two git commands themselves; no wrapper helper needed.
- `tmux_rename_pane` — same; tmux is in agent's hands.

Time-driven jobs (in `worker/bot_squad_worker/jobs.py`):

- `_heartbeat` — already shipped
- `deploy_monitor` — every minute, drains queued deploy requests when tree is clean and not in a per-project danger window
- `kick_stuck` — once daily at 11:59 UTC (cctv timing), summarizes per-project state and TG-pings the stakeholder
- `oauth_refresh` — every 6h, runs `refresh_oauth.py` if Claude OAuth is near expiry (re-imports cctv's logic)

## 3. agent_team/dev branch strategy

### 3.1 Branch lifecycle

Per project (signal-tracker first):

- **Long-lived dev branch**: `agent_team/dev`. Branched from `master`. Lives indefinitely. All agent commits land here.
- **Staging branch**: `staging` (signal-tracker already has it). The deploy recipe merges `agent_team/dev → staging` to ship to the staging environment.
- **Master**: protected. The user manually merges `staging → master` (or `agent_team/dev → master`) when ready for prod.

Hard rules embedded in AGENTS.md:

- Agents commit on `agent_team/dev`. They check out the branch on session start if not already there.
- Agents NEVER:
  - `git push` anything
  - `git merge` anything (the worker does merges via deploy recipes, the user does master merges)
  - `git rebase` interactively or against master
  - `git commit --amend` (a recoverable but anti-pattern; squash via the helper instead)
- Agents MAY:
  - `git commit` normally on `agent_team/dev`
  - `git checkout` to inspect other branches read-only (then come back)
  - Run the `ops/bot-squad/squash` helper to consolidate their own commits before requesting a deploy

### 3.2 Squash policy

Stakeholder's directive: "agents should regularly squash commits that they made." Kept as **policy in AGENTS.md, no wrapper helper** — agents already speak git, no hoops to jump through.

AGENTS.md `## Branching & commits` includes the recipe inline:

```
Squash before requesting a deploy or handing off to the stakeholder:
  BASE=$(git merge-base HEAD master)
  git reset --soft "$BASE" && git commit -m "<single-line message>"
```

Plus the safety rules: don't squash on master/staging; don't squash with a dirty tree; don't squash if `merge-base == HEAD`.

Routine working commits stay un-squashed during a session. The policy isn't enforced in the worker — the deploy recipe doesn't care about commit shape.

### 3.3 Branch creation

Spec #3's migration sub-step (idempotent): `git checkout -b agent_team/dev master` if it doesn't already exist; otherwise just `git checkout agent_team/dev`. The `bot-squad project init` CLI gets a new flag `--create-deploy-branch` that does this.

## 4. Deploy flow

### 4.1 The shim

`/home/www/bot-squad/scripts/cli/deploy.sh` (called as `ops/bot-squad/deploy` via per-project symlink):

```bash
#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:?usage: deploy <target> \"<reason>\"}"
REASON="${2:-(no reason given)}"
SOCK="${WORKER_SOCK:-/home/www/bot-squad/data/_sock/worker.sock}"
SLUG=$(python3 - <<PY
import sys, tomllib, os
cfg = tomllib.loads(open("/home/www/bot-squad/config/projects.toml").read())
cwd = os.getcwd()
for slug, p in cfg.get("projects", {}).items():
    repo = p.get("repo_path", "")
    if cwd == repo or cwd.startswith(repo.rstrip("/") + "/"):
        print(slug); break
PY
)
[ -n "$SLUG" ] || { echo "deploy: not in a registered bot-squad project (cwd=$(pwd))"; exit 2; }
curl -sS --unix-socket "$SOCK" \
    -X POST -H "Content-Type: application/json" \
    -d "{\"slug\":\"$SLUG\",\"target\":\"$TARGET\",\"reason\":\"$REASON\"}" \
    http://w/actions/deploy
echo
```

Same shape as cctv's `ops/deploy_request.sh`. Agents type `ops/bot-squad/deploy staging "v0.6.0 fix"` and walk away.

### 4.2 The worker action

`actions.py::_action_deploy(params)`:

- Validates `params` keys: `slug`, `target`, `reason` (all required).
- Looks up project + target in `projects.toml`. Rejects unknown slug or target with `ActionError`.
- Generates a queue id `<UTC-ts>-<short-uuid>` and writes `data/<slug>/_jobs/deploy/queue/<id>.json`:
  ```json
  {"id": "...", "slug": "signal-tracker", "target": "staging",
   "reason": "v0.6.0 fix", "requested_by": "<sid>@<user>", "requested_at": "..."}
  ```
- Returns `{ok: true, queue_id, queued_at}`. Sync round-trip ~5ms — agent feels instant feedback.
- The actual deploy is handled by `deploy_monitor`. The worker action is fire-and-forget queueing.

### 4.3 The monitor

`jobs.py::deploy_monitor(cfg)` — fires every minute via APScheduler.

```
for each registered project:
    queue = data/<slug>/_jobs/deploy/queue/*.json
    if queue is empty: continue
    if in danger window (per-project, see §4.5): defer; log
    if `git status --porcelain` of the repo (excluding ops/state/-style noise) is non-empty:
        defer; log "tree dirty; N uncommitted files"
        continue
    pick oldest queue file, mv to data/<slug>/_jobs/deploy/processing/<id>.json
    run data/<slug>/deploy/<target>.sh   (per-project recipe; see §4.4)
    on rc=0:
        mv processing → processed/<id>.json (rename `.ok`)
        tg_notify: ✅ deploy <target> succeeded — <slug> — <reason>
    on rc!=0:
        mv processing → processed/<id>.json (rename `.fail.<rc>`)
        tg_notify: ❌ deploy <target> FAILED rc=<rc> — see <log>
    log to data/<slug>/_jobs/deploy/monitor.log
    re-check tree clean before processing the next queue item (in case the recipe dirtied something)
```

Subtleties:

- **Quiescence definition**: identical to cctv's. `git status --porcelain` filtered to drop `ops/state/` and any other paths matching a `quiescence_ignore` glob list in `projects.toml`. Default ignore: `ops/state/*`, `tests/playwright-report/*`, `**/__pycache__/*`, `*.pyc`.
- **Mutex**: `flock` on `data/<slug>/_jobs/deploy/monitor.lock` so concurrent firings don't double-execute.
- **Per-project recipes** are responsible for everything: branch checkout, merge, build, restart, post-checks. The monitor just runs them and captures rc + stdout/stderr to the log.
- **Run logs**: full stdout/stderr captured to `data/<slug>/_jobs/deploy/runs/<id>.log` for the spec #6 viewer.

### 4.4 Per-project deploy recipes

Recipe file: `data/<slug>/deploy/<target>.sh` (mode 755, lives outside repo via the bot-squad data dir, written by the user or scaffolded once per project).

For signal-tracker, `data/signal-tracker/deploy/staging.sh`:

```bash
#!/usr/bin/env bash
# signal-tracker → staging
set -euo pipefail
REPO=/home/almdudleer/signal_tracker_mgmt
cd "$REPO"

# Save current branch so we can restore.
ORIG=$(git rev-parse --abbrev-ref HEAD)
trap 'git checkout "$ORIG" >/dev/null 2>&1 || true' EXIT

git fetch origin
git checkout staging
if ! git merge agent_team/dev --no-edit --no-ff; then
    echo "merge conflict — aborting" >&2
    git merge --abort
    exit 2
fi
docker compose build signal-tracker-staging
docker compose up -d --force-recreate signal-tracker-staging
# Smoke test.
sleep 5
curl -fsS https://signal-staging.dev.uzinvestapi.com/api/version >/dev/null
```

For signal-tracker `dev.sh` and (future) other targets, similar patterns. Recipes are small + project-aware; the worker stays project-agnostic.

The `bot-squad project init` CLI grows a `--scaffold-recipes` flag that drops template recipes the user fills in.

### 4.5 Danger windows

cctv-backend has a 05:55–06:35 MSK window where touching `celery-cctv` would break the morning ops_tasks spawn. Signal-tracker has no equivalent today, but per-project danger windows belong in projects.toml:

```toml
[projects.signal-tracker]
# (existing fields)
deploy_danger_windows = []   # list of "HH:MM-HH:MM <tz>" strings; deploys defer if current time falls inside any
```

`deploy_monitor` checks against current time before processing the queue. If inside any window, defer with a log line.

## 5. APScheduler jobs

In `worker/bot_squad_worker/scheduler.py`:

```python
def build_scheduler(cfg: Config) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(heartbeat,        "interval", seconds=60,           args=[cfg], id="heartbeat")
    sched.add_job(deploy_monitor,   "interval", seconds=60,           args=[cfg], id="deploy_monitor")
    sched.add_job(kick_stuck,       "cron",     hour=11, minute=59,   args=[cfg], id="kick_stuck")
    sched.add_job(oauth_refresh,    "interval", hours=6,              args=[cfg], id="oauth_refresh")
    return sched
```

Every job:

- Logs to `data/_worker/<job>.log` (size-rotated by APScheduler-aware logger).
- TG-pings on hard errors only (debounced by the existing `tg_notify` debounce).
- Catches its own exceptions; APScheduler's default `coalesce=True` ensures missed runs don't pile up.

## 6. Worker action surface for spec #3

Adding to the existing `ACTION_REGISTRY` (`actions.py`):

```python
ACTION_REGISTRY = {
    "noop": _action_noop,
    "deploy": _action_deploy,
    "git_status": _action_git_status,
    "tg_notify": _action_tg_notify,
    "kick_stuck_now": _action_kick_stuck_now,
}
```

Each handler:

- Validates parameters with explicit allowed-key lists (rejects unexpected keys, like `noop` does today).
- Returns a `dict` with at least `ok: bool`.
- Raises `ActionError` for caller errors (400 from the API). Internal failures raise other exceptions and get 500.

`tg_notify` is exposed as an action so:

- Hooks call it via `curl --unix-socket .../actions/tg_notify`.
- The API can call it for UI-driven "ping me" buttons (later spec).
- The scheduler calls it via the same code path (no special internal channel).

## 7. Telegram — outbound

Reusing `@watchbot` (token `8036906248:...`, already in `auth.toml`).

`_action_tg_notify(params={slug?, message, chat_id?})`:

- Resolves chat: `params.chat_id` if given, else `projects.toml::tg_chat` for the slug, else default Alexey.
- Prefixes the message with `[<sid> @ <user>]` where SID is derived from the calling tmux window — same `S-<user>-<window>` as cctv, computed at call time (not from a stale `.current` file). For non-tmux callers (worker scheduler), prefix is `[<job> @ worker]`.
- Debounces: same 60s same-payload debounce as cctv's `tg_notify.sh`. State in `data/_worker/tg_debounce/`.
- Doesn't expose the bot token to the API container — the worker holds it (auth.toml is mounted into the worker only; in spec #1 it's mounted into both, but spec #3 tightens this — see §11).

What gets pinged in spec #3:

- `🚚 deploy <target> queued — <slug> — <reason>` — at queue time
- `✅ deploy <target> succeeded — <slug>` — on success
- `❌ deploy <target> FAILED rc=<rc> — <slug> — see <log>` — on failure
- `[<sid>] needs your input` — Stop hook ping (see §9)
- Daily kick-stuck summary — single consolidated message per project per day

What does NOT ping:

- Routine commits
- Test runs
- Backlog edits
- Anything more than one message in 60s with the same payload

## 8. tmux SID derivation (and why pane naming is optional)

You drive tmux interactively. In practice, lots of windows end up named just `claude` (default from auto-spawn) and never get renamed. So the SID can't rely on window name being meaningful — it has to disambiguate even when names collide.

**SID format**: `S-<user>-<window>-p<pane_id>`

- `<user>` — `id -un`, sanitized to `[A-Za-z0-9_]`
- `<window>` — `tmux display-message -p '#W'`, sanitized; defaults to `claude` or whatever
- `<pane_id>` — `tmux display-message -p '#{pane_id}'` minus the leading `%` (e.g., `%5` → `5`). Unique across the entire tmux server, so two windows both named `claude` produce different SIDs (`S-almdudleer-claude-p5` vs `S-almdudleer-claude-p6`).

When agents care: TG pings prefix with the SID. Stakeholder reads `[S-almdudleer-tg-deeplink-p5] needs your input` and either:

- Recognizes the window name (renamed it themselves) and goes there, OR
- Looks up `tmux list-panes -a -F '#{pane_id} #W #{pane_current_path}'` to find the pane.

**Renaming is purely cosmetic**: helps the stakeholder skim TG messages but isn't load-bearing. No CLI helper for it — `tmux rename-window <feature>` is two words.

`hook_my_sid.sh` (called by the Stop hook and the `deploy` shim) computes the SID FRESH each call using `tmux display-message -t $TMUX_PANE` — never reads a stale `.current` file, so it stays correct as the user renames panes.

## 9. Hooks

Foundation v1 ships `SessionStart`. Spec #3 adds:

### 9.1 `Stop` hook

Fires when a Claude session goes idle waiting for user input (the way cctv's does). Implementation: `/home/www/bot-squad/scripts/hooks/stop.sh`. Logic:

```bash
# Pseudocode
sid=$(hook_my_sid.sh)   # tmux-derived
last_user_message_age=$(stat -c %Y .claude/last_user_prompt_ts)
if [ "$now - $last_user_message_age" -gt 60 ] && session_is_idle; then
    /home/www/bot-squad/scripts/cli/tg_notify "$sid needs your input"
fi
```

Subtleties:

- Must not double-ping if the session re-stops within 60s.
- Must not fire if the stop is the session ending naturally (after the user typed a question and got an answer).
- We use the same heuristic cctv uses: stat of `.claude/last_user_prompt_ts` (touched by `UserPromptSubmit` hook — but in our case we don't need a separate UserPromptSubmit hook to drain inbox; we just use it for the timestamp).

### 9.2 Lightweight `UserPromptSubmit` hook

Sole purpose: touch `.claude/last_user_prompt_ts` so `Stop` knows when the user last spoke. NOT used for inbox drain (we don't have intersession.sh — agent_teams handles intra-session communication directly).

`scripts/hooks/user_prompt_submit.sh`:

```bash
#!/usr/bin/env bash
mkdir -p .claude
touch .claude/last_user_prompt_ts
```

### 9.3 Updated `.claude/settings.json` template

`bot-squad project init` now drops:

```json
{
  "hooks": {
    "SessionStart":      [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/session_start.sh"}]}],
    "UserPromptSubmit":  [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/user_prompt_submit.sh"}]}],
    "Stop":              [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/stop.sh"}]}]
  }
}
```

Existing project repos get the file rewritten by re-running `bot-squad project init <slug> --repo <path>` (idempotent).

## 10. AGENTS.md updates

The `## Branching & commits` and `## Deploy` sections in the slim AGENTS.md become real:

```markdown
## Branching & commits

- Working branch: `agent_team/dev`. Branch off master, never push, never merge,
  never amend. Squash before requesting a deploy: `ops/bot-squad/squash "<msg>"`.
- Commit prefix: `[backend]`, `[web]`, `[ops]`, `[docs]`. Imperative summary,
  ≤70 chars. Co-Authored-By auto-added.
- One commit per discrete change DURING work; collapse with the squash helper
  before deploy.

## Deploy

`ops/bot-squad/deploy <target> "<reason>"` — targets: `staging`, `dev`.
- Queues a deploy. The monitor processes it within ~60s when the tree is clean.
- Tree clean = `git status --porcelain` empty (logs/cache excluded).
- TG-pings on success/failure.
- You don't manage the loop. Commit, squash, request, walk away.

Manual prod release: stakeholder reviews staging → merges agent_team/dev (or
staging) into master → builds the prod container → deploys. Agents never
deploy prod.
```

The migration plan for spec #3 includes regenerating AGENTS.md (small `bot-squad render-agents-md <slug>` helper that re-renders from a template + vision layers).

## 11. Tightened security boundary for `auth.toml`

Foundation v1 mounts `config/` as `:ro` into the API container. Token is therefore visible to API. For spec #3 we tighten so only the worker reads the bot token:

- Split `auth.toml` into:
  - `config/auth.toml` — TG-Login allowlist + JWT secret. API needs both. Mode 0640 group `www`. Worker can read too.
  - `config/secrets.toml` — bot token, deploy SSH keys (future), other privileged secrets. Mode 0640 owner `almdudleer` only. Worker reads. API does NOT mount this.
- Worker exposes `tg_notify` as an action; the API calls it instead of doing TG itself. The API never sees the token after this change.

Migration: move `bot_token` line from `auth.toml` to `secrets.toml`, restart both services.

## 12. CLI additions

`scripts/cli/bot-squad` grows two subcommands:

- `bot-squad project init <slug> ... [--create-deploy-branch] [--scaffold-recipes]` — extends v1's project init with branch creation and recipe scaffolding flags.
- `bot-squad render-agents-md <slug>` — re-renders the project's AGENTS.md from template + vision layers. Spec #1's static AGENTS.md was a one-shot; this is the manual re-render pending spec #Z's auto-render.

Things explicitly NOT added (per the "no hoops for agents" principle):

- No `squash` wrapper — agents run `git reset --soft $(git merge-base HEAD master) && git commit -m "..."` themselves. Documented in AGENTS.md.
- No `pane` wrapper — `tmux rename-window <feature>` is two words and the SID still disambiguates duplicate names via pane-id.
- No `git_status` wrapper — `git status` is one word.

## 13. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Deploy recipe runs while agent has uncommitted work | `deploy_monitor` requires `git status --porcelain` clean (modulo log/cache ignores). Recipe never starts on a dirty tree. |
| Merge conflict during `agent_team/dev → staging` | Recipe `git merge --abort`s and exits non-zero. Worker reports failure via TG; user resolves manually. |
| Recipe leaves the repo on `staging` after a failed step | Recipe uses `trap 'git checkout "$ORIG"' EXIT` to restore. |
| Worker dies during a deploy mid-recipe | Queue file is in `processing/` — left there. Next worker start logs "stuck queue files present"; user moves them back to `queue/` or deletes them. Spec #6's run-history will surface this. |
| Two separate Claude sessions both call `deploy` for the same target | Both queue items get processed serially by the monitor. Second deploy is a no-op rebuild — annoying but safe. (Future: queue dedup by `(slug, target)` if it becomes a problem.) |
| TG bot token leaks via API CVE | Spec #3's §11 splits secrets so the API never holds the bot token. |
| `Stop` hook ping fires while user is actively typing | Hook checks `last_user_prompt_ts` age (>60s) before pinging. Active typing keeps it fresh. |
| `kick_stuck` fires while user is in middle of a deploy | Hook is read-only — no state mutation. Worst case: a single redundant TG message. |
| Per-project recipe `EXIT` trap doesn't fire on `kill -9` | Worker kills jobs gracefully (SIGTERM). `kill -9` would only happen on a panic — accept the edge case. |
| OAuth refresh fails silently | `oauth_refresh` job TG-pings on failure (loud). Same as cctv. |

## 14. Out of scope (still deferred)

- Backlog/Vision UI editing (spec #4)
- Session manager UI / play-pause/resume (spec #5)
- Headless run-history / message-history viewer (spec #6) — the data is captured to `data/<slug>/_jobs/deploy/runs/<id>.log`; a future spec just renders it
- Two-way TG: replying to a TG message that lands back in a session (spec #7)
- Autonomous mode (spec #8)
- Vision Crystallizer workflow (spec #X)
- AGENTS.md auto-render on vision change (spec #Z)
- `agent_team/dev → master` automation (always manual; spec #5 might add a UI button later)

## 15. Implementation order

1. Tighten `secrets.toml` split (move bot_token out of `auth.toml`); update API + worker config loaders + docker-compose mounts
2. Worker `tg.py` — outbound TG client with debounce + SID prefix
3. `actions.py` — add `tg_notify`, `kick_stuck_now` (no `git_status`, no `squash` wrapper)
4. Worker `deploy.py` — queue + per-project recipe runner (uses `subprocess` for `git status` directly; no action needed)
5. `actions.py` — add `deploy` action
6. `jobs.py` — add `deploy_monitor`, `kick_stuck`, `oauth_refresh` (with tests)
7. `scheduler.py` — register the new jobs
8. `scripts/cli/deploy.sh` shim
9. `scripts/cli/bot-squad project init` — drop the deploy shim symlink as `ops/bot-squad-bin/deploy`; drop new hook config
10. `scripts/hooks/{hook_my_sid,user_prompt_submit,stop}.sh`
11. `scripts/cli/render_agents_md.py` + `bot-squad render-agents-md` subcommand
12. `agent_team/dev` branch creation in signal-tracker; signal-tracker `staging.sh` and `dev.sh` recipes
13. AGENTS.md re-rendered with the real branching/deploy sections (and the inline squash recipe)
14. End-to-end smoke: agent commits → manual squash → `ops/bot-squad-bin/deploy staging "smoke"` → tree-clean → recipe runs → staging URL reflects new build → TG ping received

Each step gets tasks in the implementation plan with explicit acceptance criteria.

## 16. Forward-looking notes

These belong in later specs but are called out so the spec #3 architecture leaves room:

- `data/<slug>/_jobs/deploy/runs/<id>.log` is the substrate for spec #6's run-history viewer. Don't redesign storage there.
- The per-project recipe shape (`<target>.sh` returning rc) is reused by spec #5's UI "deploy now" button — same code path.
- `tg_notify` as a worker action is the foundation for spec #7's two-way TG: the inbound side adds a webhook that calls `claude --resume <UUID>` with the user's text as input.
- Squash policy is intentionally social, not enforced. Spec #X's Crystallizer might add a "review squashed history before merge" check, but that's product-led not workflow-led.
