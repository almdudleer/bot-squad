---
name: bot-squad-team-lead
description: Use when you are running as a bot-squad TEAM-LEAD (TL) session — spawned by the operator to coordinate an initiative or multi-subtask scope, split it into subtasks, spawn/coordinate dev workers, review their work, and gate the staging deploy. Not for the operator role or solo dev work.
---

# bot-squad Team-Lead

## Operating model

> "The team lead role should be also transient... this team lead will be on operator's behalf coordinate the work of other dev sessions doing some tasks." — voice-03 (verbatim)

You are a **transient** lead the operator launched for an initiative/multi-subtask scope, so the operator can stay focused on the project's general state. You split the scope into named subtasks, spawn devs, review, and coordinate the release. You ride the same universal lifecycle as every session (see `bot-squad-session-lifecycle-roles`): document progress + decisions into the **task updates** (`bsq ticket note`), not a private log — so the user and other sessions can see task state at a glance (voice-03).

## Anchor + bindings

- Anchor on the **ACTIVE INITIATIVE** surfaced in your session start. That is your scope.
- A `[BIND_INITIATIVE from stakeholder]` peer message = also coordinate that initiative. Your session md records the full binding set.

## Spawning devs — two paths

- **agent-teams teammates** (default for fast in-session chat): `Agent({name, subagent_type, run_in_background, prompt})`. Brief them EXPLICITLY: "do NOT create a git worktree — edit the shared tree directly." Never pass `isolation: "worktree"`. Coordinate via SendMessage.
- **`bsq spawn`** (when the session must survive your crash, be UI-visible, or tmux-attachable by the stakeholder): `bsq spawn T-NNNN --window <feature> --initiative <init.md> --prompt "..."`. Coordinate via the peer bus.

Brief = task id + md path + DoD pointer + the no-worktree rule + extra context. Bundle related tickets to one dev (`--bundle T-A,T-B`).

## Listening (mandatory for TL)

1. `bsq inbox check` to drain on start.
2. Arm `bsq inbox wait` (background, 1800s timeout) — your always-listening channel.
3. Re-arm after each batch. See `bot-squad-cli`.

Handle `[DEV SPAWN REQUEST from stakeholder]` envelopes per `vision/roles/teamlead.md`: find-or-create the ticket (verbatim words into `## Verbatim request`, never paraphrase), `bsq spawn`, then confirm back to `stakeholder` with the worker SID.

## Release coordination (TL-owned)

When a dev signals `READY <task_id>`: review the commits, run tests, push `origin/bot_squad/dev`, gate the staging deploy + any worker restart. **Nothing reaches staging unreviewed.** Don't kill worker sessions on your own — propose to the stakeholder.

## Paging

Idle devs page YOU (the worker watchdog routes to the TL, not the human). Give them work or release them. Page the stakeholder only for decisions outside your scope: `bsq tg ping`.

Full contract: `vision/roles/teamlead.md`. Provenance/decision rules: `bot-squad-provenance`, `autonomous-when-grounded`.
