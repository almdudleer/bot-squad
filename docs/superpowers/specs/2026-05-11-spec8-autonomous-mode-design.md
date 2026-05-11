# Autonomous mode — design

**Spec date:** 2026-05-11
**Status:** awaiting user review
**Implements:** chunk #8 of the bot-squad decomposition — the last in the foundation series

## 1. Goal

A separate operating mode where bot-squad **takes open backlog tasks and works them through to deploy without the stakeholder in the loop**, while staying anchored to the vision (north-star / strategy / tactical) and respecting Definition-of-Done before claiming a task complete.

Unlike the previous failed autonomous-Retro flow (archived under `signal-tracker-old/`), this design is **sequential, observable, and bounded**: one task at a time, every step logged, hard memory and time caps, optional TG pings only on blockers.

## 2. Design principles

1. **One task at a time.** No parallel claude sessions. Memory cost is bounded.
2. **Always vision-anchored.** Every spawned worker reads the layered vision before it claims a task; the orchestrator skips tasks that violate `## Hard rules` or fall outside `current strategy`.
3. **DOD review by a separate claude session.** After a worker claims `[totest]` on a task, a reviewer subprocess inspects the diff + test output and decides ✅ approve / ❌ reopen-with-feedback. Approval moves the task to `closed`; rejection moves it back to `open` and appends the feedback as a comment.
4. **Sleep windows.** Stakeholder-configurable hours (default 22:00–08:00 UTC) where the orchestrator stops spawning new work but lets in-flight tasks complete.
5. **TG-rare**: only ping for hard blockers (ambiguous spec, conflicting tasks, repeated failure). Daily summary at wakeup time.
6. **Resumable.** The orchestrator's state (current_task, started_at, status) is on disk; a worker restart picks up where it left off without re-running anything.

## 3. Architecture

```
                                      bot-squad-worker (systemd, almdudleer)
                                              │
                                              ├─ APScheduler: autonomous_tick (every 60s)
                                              │
                                              ▼
                                       autonomous.py orchestrator
                                              │
            ┌─────────────────────────────────┼─────────────────────────────────┐
            ▼                                 ▼                                 ▼
    state machine                    pick next task                    DOD-review claude
    (idle/working/                   (vision-aware                     (separate subprocess
     reviewing/sleeping)              prioritizer)                      with the diff)
                                              │
                                              ▼
                                    tmux new-window claude
                                       (worker session)
```

State machine, one tick:

```
state = read state_file
if state.in_sleep_window(): return        # don't pick new work; let in-flight finish
if state == "idle":
    task = pick_next_task(cfg)            # vision-anchored prioritizer
    if task is None: return                # nothing to do
    spawn worker pane for task; state→"working"; save state
elif state == "working":
    if not pane_alive(state.pane_id):     # claude finished or died
        if task_status == "totest":      state→"reviewing"
        elif task_status == "wip":       state→"idle"  (claude exited mid-work; reopen)
        else:                            state→"idle"
elif state == "reviewing":
    review_decision = run_dod_review(task)
    if approved: task→"closed"; state→"idle"
    else:        task→"open"; append comment with feedback; state→"idle"
```

## 4. Components

### 4.1 New worker module `autonomous.py`

Single module with:

- `class AutonomousState` — dataclass: status, current_task_id, current_pane_id, started_at, last_tick_at, sleep_hours
- `load_state(cfg) -> AutonomousState`, `save_state(cfg, state)` — file under `data/_worker/autonomous_state.json`
- `pick_next_task(cfg, slug) -> Task | None` — reads `data/<slug>/backlog/*.md`, ranks tasks by:
  1. Status open|reopened (skip totest/closed/wip)
  2. Priority hint in body (search for `**Priority: must**` or default to "should")
  3. Title length / spec-completeness heuristic (skip vague tasks; "TODO" or one-line titles flagged)
  4. Tasks that match `current tactical priorities` from `vision/tactical.md` get bumped
- `spawn_worker(cfg, task) -> pane_id` — creates a tmux window with claude, sends the initial prompt that includes the task body + vision context
- `is_pane_alive(pane_id) -> bool` — `tmux list-panes -t <pane_id>` exit code
- `read_task_status(cfg, slug, task_id) -> str` — re-parse the task file's frontmatter
- `run_dod_review(cfg, slug, task) -> dict` — spawns a separate claude with the task body + diff + test results, asks for "✅ approve / ❌ reject: <reason>"; parses the reply
- `tick(cfg)` — the state-machine step run by the scheduler

### 4.2 New worker actions

| Action | Params | Returns |
|---|---|---|
| `autonomous_status` | none | current state + last 5 ticks |
| `autonomous_enable` | `{slug, sleep_hours?}` | starts the orchestrator for the slug |
| `autonomous_disable` | `{slug}` | stops it (in-flight task completes) |

### 4.3 New time-driven job `autonomous_tick`

Schedule: `interval, seconds=60`. Reads state file; if disabled, returns immediately.

### 4.4 New API surface

- `GET /api/projects/{slug}/autonomous` → status JSON
- `POST /api/projects/{slug}/autonomous/enable` → enable orchestrator
- `POST /api/projects/{slug}/autonomous/disable` → disable
- `GET /api/projects/{slug}/autonomous/log` → last 50 tick decisions for transparency

### 4.5 Frontend page `/p/:slug/autonomous`

- Status card: current state, current task (link to task detail), pane id (link to session messages if any), started_at, time-in-state
- "Enable / Disable" toggle (with confirm modal that warns: "agents will work autonomously")
- Sleep hours: editable input (HH-HH UTC)
- Tick log: scrollable list of last 50 orchestrator decisions
- Banner if state == "working": shows the running session SID + a "View messages" link

## 5. Worker prompt for spawned task sessions

When the orchestrator spawns a claude pane to work a task, it sends as the initial prompt:

```
You are working on backlog task {task.id}: {task.title}.

## Task body

{task.body}

## Vision context (do not deviate)

[contents of ops/bot-squad/vision/tactical.md]
[contents of ops/bot-squad/vision/strategy.md, first 30 lines]

## How we decide what to build

[the 5-point decision framework from AGENTS.md]

## What to do

1. Read AGENTS.md if you haven't already (the SessionStart hook will inject it).
2. Implement the task within the constraints. NEVER push, NEVER merge, NEVER amend.
3. Commit on agent_team/dev.
4. Run tests; fix until green.
5. Squash your commits before deploy (see AGENTS.md "Branching & commits").
6. Request a deploy: ops/bot-squad-bin/deploy staging "<reason tied to task ID>"
7. Update the task frontmatter status to `totest` (write_task helper in API).
8. Add a comment to the task explaining what you shipped.
9. Stop. Do not start another task; the orchestrator handles that.

When done, the orchestrator's DOD reviewer will inspect your work. If it approves, the task moves to `closed`. If not, you'll get a comment with feedback and the task returns to `open`.
```

## 6. DOD review prompt

```
You are reviewing backlog task {task.id} for compliance with the project's
definition of done before it's marked `closed`.

## Task body
{task.body}

## Diff (worker's commits on agent_team/dev since starting)
{git diff <branch-start>..agent_team/dev --stat}
{git diff <branch-start>..agent_team/dev}     (truncated to 10 KB)

## Tests
{pytest output, truncated to 5 KB}

## Decision framework
1. Does the diff implement what the task body asked for?
2. Are tests green?
3. Does the work respect the project's vision/north-star and current tactical priorities?
4. Are there any obvious regressions, scope creep, or unnecessary refactoring?

Reply with EXACTLY ONE of:
✅ APPROVE — <one-sentence rationale>
❌ REJECT — <specific feedback for the worker>

Do not run any tool calls. This is a one-shot review.
```

The orchestrator parses the response: if it starts with `✅ APPROVE`, mark closed; otherwise reopen with the feedback as a comment.

## 7. Safety rails

- **Time cap per task**: 30 minutes wall-clock. If the worker pane is alive but the task status hasn't changed to totest after 30 min, kill the pane, append a comment "auto-killed after 30min — task likely too large or stuck", reopen.
- **Failure cap per task**: 3 rejections from the DOD reviewer → task marked `reopened` permanently and TG-pings stakeholder for human review.
- **Sleep window**: default 22:00–08:00 UTC. No new tasks spawned during this window. In-flight tasks complete or hit time-cap.
- **Disabled-by-default**: orchestrator starts in `disabled` state. Stakeholder must explicitly enable per project.
- **Per-project**: each project's autonomous state is independent; signal-tracker can be enabled while another project isn't.

## 8. Out of scope (deferred)

- Parallel multi-task execution (one at a time forever; if you want parallel, use multiple bot-squad hosts)
- Cross-project orchestration
- Auto-merge agent_team/dev → master (stays manual per current AGENTS.md hard rules)
- Vision Crystallizer integration (a future spec adds: when raw feedback comes in, run a crystallizer pass that updates vision and re-prioritizes the orchestrator)
- Web UI for editing the spawned-task prompt template (just edit the .py module)

## 9. Risks

| Risk | Mitigation |
|---|---|
| Spawned claude session ignores instructions and does the wrong thing | DOD reviewer catches; 3-strike cap escalates to TG |
| Spawned claude session OOMs the box (per past memory pattern) | One concurrent session only; time-capped at 30 min |
| Stakeholder asleep when something goes wrong | Sleep window blocks new starts; in-flight task time-caps; TG ping rare but explicit |
| Orchestrator dies between ticks | State file persists; next worker restart resumes |
| Wrong task picked (e.g., misclassified priority) | pick_next_task is heuristic; stakeholder can disable + manually re-prioritize via UI |
| Reviewer claude is also off | Same model, fresh context; if it returns garbage, the orchestrator treats as REJECT and adds a generic comment |
| Spawned worker pushes to master accidentally | AGENTS.md says NEVER push; the worker's job is bounded to commit-on-agent_team/dev + deploy_request. If it tries to push, deploy_request's `git status` check catches dirty state. |

## 10. v1 scope (this spec) vs deferred

v1 ships:
- `autonomous.py` module with state machine, prick-next, spawn, review
- 3 worker actions + 1 scheduler job
- 4 API endpoints
- Sessions/Autonomous page (single, simple)

Deferred to a follow-up spec:
- Vision Crystallizer ingestion (raw text → vision artifact updates)
- Real Interactive Retro flow
- Parallel multi-task scheduling
- Cross-project orchestration

## 11. Implementation order

1. `autonomous.py` state machine + pick_next + spawn (with mocked subprocess in tests)
2. DOD review subprocess invocation
3. 3 new worker actions + tests
4. `autonomous_tick` scheduler job + tests
5. 4 new API endpoints + tests
6. Frontend `/p/:slug/autonomous` page
7. Single docker build + smoke
8. **Do NOT enable for signal-tracker by default** — stakeholder enables manually via the UI when ready
