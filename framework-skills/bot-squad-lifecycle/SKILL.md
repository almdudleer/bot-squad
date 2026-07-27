---
name: bot-squad-lifecycle
description: Use when you need to understand what the bot-squad system does FOR your session automatically — the ~1h cache-timeout recycle + compact-to-artifact, the 60s reconcile tick (session GC, dead-binding clearing, dev-zombie archival, team reconcile), single-source-of-truth renaming, and which sessions run non-stop vs. exit when done. So you don't hand-manage tmux, archival, or stale task bindings.
---

# bot-squad Session Lifecycle (the mechanics you rely on)

## The universal lifecycle

> "Whenever this session hits timeout, or when this session hits the token limit... it's always the same compact operation... this session is asked by the system to write down all the progress and terminate itself." — voice-03 (verbatim)

Every session — operator, TL, dev — shares ONE lifecycle. Sessions are transient work-execution processes, not persistent conversations (see `bot-squad-session-lifecycle-roles` for the paradigm). What that means operationally:

- **Finish → exit.** If the work the session was created to do is done, the session exits. Don't idle indefinitely.
- **Stale-wait → recycle on timeout.** A session blocked waiting past the **~1h cache-invalidation timeout** is asked to record its results and exit. You may **postpone** to the next timeout (repeatably) if you're actively waiting on a long, known-bounded process (e.g. a build).
- **Compact = write-to-artifact → terminate → relaunch.** "This compact just takes all the context, writes it, and starts a new session with this summary" (voice-03). On exit, state whether/when resuming this session beats starting fresh from the documented progress; full `--resume` reread is discouraged.
- **The artifact has a DEFINED home — never a random md** (T-0567). Task-bound session → `artifacts/<task_id>.md`; operator → `artifacts/operator-state.md`; other non-task roles → `artifacts/role-<role>-<sid>.md` (all system-resolved via `bsq compact-save`; overwrite in place, don't mint variants). **Content division:** the compact artifact carries context GOTCHAS only — ephemeral hints your successor needs (env quirks, mid-edit state, "X looks done but isn't"). TASK-RELEVANT detail — requirements, user clarifications/decisions, progress, follow-ups — goes on the TASK (`bsq ticket note` / `bsq task new`) BEFORE you compact; an unanswered stakeholder question parked in a handover md is a stranded request. Full kind→home map: project doc `docs/architecture/D-0045`.

**Which run non-stop:** the operator "is always on when the work is on until the user paused it or it stalled itself and ran out of time" and "always drives the backlog"; TLs "always drive the initiative" until stopped; devs exit when their task is done (voice-09).

## What the 60s reconcile tick guarantees (you don't babysit this)

The dispatch tick runs five ordered reconcilers (`binding_gc_tick`), so a TL/operator never hand-manages tmux/archival/stale ids (see the install's `vision/roles/session-lifecycle-contract.md`):

1. **`gc_sessions`** — an `active` SessionMd whose pane is gone → `suspended`.
2. **`gc_dead_bindings`** — a session's `task_id` cleared when that task is `closed`/missing; `initiative` cleared when its file is missing; closed/missing `extra_task_ids` pruned. (Why a suspended dev no longer shows a stale task_id.) Bindings for `open`/`in_progress`/`totest`/`reopened` are preserved.
3. **`gc_stale_bindings`** — two sessions claiming one `task_id` → live+latest wins; losers stripped (old kept as `last_task_id`).
4. **`archive_dead_teammates`** — **dev zombies are impossible**: a dev whose pane exited AND whose task is `totest`/`closed`/missing is auto-`suspended`+`archived`, binding cleared, lingering tmux window killed. A live `totest` dev is never force-killed; a crashed in-progress dev (pane gone, task open) is left *resumable*.
5. **`reconcile_teams`** — rebuilds Team rosters from the reconciled registry.

So: a dev can exit by simply finishing — no manual `suspend_session`. Verified-done workers are archived, not resumed into a new task's context.

## Names & teams (single source of truth)

- A **Team** is a persisted object keyed by tmux session name (`data/<slug>/teams/<name>.md`), a projection rebuilt every 60s — never hand-edit it; change the sessions and the tick reconciles.
- A session's name lives in three surfaces; the **tmux window name is the source of truth**. Rename only via `bsq team rename <sid> <new-name>` (→ `sync_session_name`) so surfaces can't drift.
- `bsq team archive <sid>` = suspend + archive (binding released); `bsq team resurrect <sid>` = unarchive + resume.

Full contract: the install's `vision/roles/session-lifecycle-contract.md`. The paradigm behind it: `bot-squad-session-lifecycle-roles`.
