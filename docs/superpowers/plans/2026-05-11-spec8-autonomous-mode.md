# Autonomous mode — implementation plan

> NO Agent tool. NO parallel docker builds. Mock subprocess + claude in tests. Orchestrator starts DISABLED for all projects.

**Goal:** Long-running orchestrator that picks one backlog task at a time, spawns a claude pane to work it, reviews via a separate claude subprocess, marks closed or reopens, repeats.

**Spec:** `docs/superpowers/specs/2026-05-11-spec8-autonomous-mode-design.md`

## Phase 1 — orchestrator module

### Task 1: `worker: autonomous module + state machine`

`worker/bot_squad_worker/autonomous.py`:

```python
"""Autonomous orchestrator — one task at a time, vision-anchored, DOD-reviewed.

The orchestrator runs on a 60-second tick driven by APScheduler. Each tick
runs a single state-machine step:

  idle    → pick a task, spawn a worker pane, transition to working
  working → poll pane liveness; if dead and task is now `totest`, → reviewing
                                if dead and task is still `wip`, → idle (reopen)
  reviewing → run DOD review; approve → closed/idle, reject → open + comment/idle
  sleeping  → return (sleep window — no new spawns)

State persists in data/_worker/autonomous/{slug}.json.

Orchestrator starts DISABLED by default per slug. Stakeholder enables via UI.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class AutonomousState:
    slug: str
    enabled: bool = False
    status: str = "idle"               # idle | working | reviewing | sleeping
    current_task_id: str | None = None
    current_pane_id: str | None = None
    current_started_at: str | None = None
    last_tick_at: str | None = None
    sleep_start_hour: int = 22         # UTC
    sleep_end_hour: int = 8            # UTC
    fail_counts: dict[str, int] = field(default_factory=dict)
    tick_log: list[dict] = field(default_factory=list)


# ... full state machine implementation per the spec ...
```

Full functions to implement:
- `state_path(cfg, slug) -> Path`
- `load_state(cfg, slug) -> AutonomousState`
- `save_state(cfg, state) -> None`
- `_now_iso() -> str`
- `_now_hour_utc() -> int`
- `in_sleep_window(state) -> bool`
- `_parse_task(path: Path) -> dict` (uses existing markdown_parser if available; else minimal yaml frontmatter parse)
- `pick_next_task(cfg, slug) -> dict | None` (rank by status=open/reopened, priority hint, vision-anchored)
- `spawn_worker(cfg, task) -> str` (tmux new-window with claude + initial prompt; returns pane_id; the prompt is the long block from spec §5)
- `pane_alive(pane_id) -> bool` (tmux list-panes with `-t` filter)
- `read_task_status(cfg, slug, task_id) -> str` (re-read frontmatter)
- `run_dod_review(cfg, slug, task) -> dict` (spawns claude -p with the review prompt; parses APPROVE/REJECT)
- `tick(cfg, slug)` (state machine step; appends to tick_log; updates state)

Tests in `worker/tests/test_autonomous.py` — mock subprocess + filesystem. ≥10 tests covering:
- state file round-trip
- in_sleep_window (mock now_hour)
- pick_next_task with empty backlog → None
- pick_next_task picks open before closed
- pick_next_task respects tactical priorities (mock vision file content)
- tick(idle) when disabled → no-op
- tick(idle) when enabled and in_sleep_window → status=sleeping
- tick(idle) when enabled with task available → spawns, status=working
- tick(working) when pane dead and task status=totest → reviewing
- tick(working) when pane dead and task status not totest → idle, reopens
- tick(reviewing) approve → closed
- tick(reviewing) reject → open with comment

Commit: `worker: autonomous orchestrator module + state machine`

### Task 2: `worker: autonomous_status/enable/disable actions`

In `actions.py`:

```python
_AUTO_STATUS_ALLOWED = {"slug"}
def _action_autonomous_status(params): ...

_AUTO_ENABLE_ALLOWED = {"slug", "sleep_start_hour", "sleep_end_hour"}
def _action_autonomous_enable(params): ...

_AUTO_DISABLE_ALLOWED = {"slug"}
def _action_autonomous_disable(params): ...
```

Each follows the strict-validation pattern. Update the registry test set.

Tests in `test_actions.py`.

Commit: `worker: autonomous_status/enable/disable actions`

### Task 3: `worker: autonomous_tick scheduler job`

`jobs.py` add `autonomous_tick(cfg)` that iterates registered projects and calls `autonomous.tick(cfg, slug)` for each one that has state.enabled=True.

`scheduler.py` register: `interval, seconds=60`.

Tests minimal (just verify it doesn't raise when called with mocked state).

Commit: `worker: autonomous_tick scheduler job`

## Phase 2 — API + frontend

### Task 4: `api: autonomous endpoints`

`api/app/routes_autonomous.py`:
- `GET /api/projects/{slug}/autonomous` → status (proxy autonomous_status action)
- `POST /api/projects/{slug}/autonomous/enable` body `{sleep_start_hour?, sleep_end_hour?}` → enable
- `POST /api/projects/{slug}/autonomous/disable` → disable
- `GET /api/projects/{slug}/autonomous/log` → last 50 tick decisions from state.tick_log

`main.py` register router. Tests via fake-worker fixture.

Commit: `api: autonomous endpoints`

### Task 5: `web: Autonomous page`

`web/src/pages/Autonomous.tsx`:
- Header: project name + Enable/Disable toggle
- Status card: state badge, current task (Link to TaskDetail), pane id, started_at relative
- Settings card: sleep start/end hours (editable inputs, save button)
- Tick log: timestamped list of decisions ("picked T-0042 / spawned %5 / pane dead / approved")
- Auto-refresh 15s

`api.ts` additions.

`App.tsx` route `/p/:slug/autonomous`. `Project.tsx` breadcrumb link.

Commit: `web: Autonomous orchestrator page`

## Phase 3 — Build + smoke

1. `npm run build`
2. `free -m` >5 GB available
3. `docker compose build bot-squad-api`
4. Sleep 5s
5. `docker compose up -d bot-squad-api`
6. `systemctl --user restart bot-squad-worker`
7. Smoke endpoints (verify enabled=false default, can't enable for unknown slug, etc.)
8. `git push origin master`

**Do not enable orchestrator for signal-tracker during smoke.** It would start spawning claude sessions on its own.

## Self-review

- 5 commits
- Worker tests ~140 (was 126 + ~14)
- API tests ~165 (was 150 + ~15)
- All actions in registry have tests
- Orchestrator state.enabled defaults to false everywhere
- Memory <8 GB throughout (`tail -30 /tmp/memwatch.log`)
- No real claude spawned during tests
