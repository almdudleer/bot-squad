---
name: bot-squad-dev
description: Use when you are running as a bot-squad DEV WORKER session — assigned one task (plus any bundled/bound tasks) to build, test, and commit on the shared bot_squad/dev tree, then signal READY to your team-lead. Covers the no-deploy / no-push / no-worktree rules and how to report ready or blocked.
---

# bot-squad Dev Worker

## Operating model

You own one task (read its md under `data/<slug>/backlog/<task_id>-*.md` for scope + DoD). A `[BIND_TASK from stakeholder]` peer message = also handle that task; your session md records the full binding set. You ride the universal session lifecycle — finish the work and exit cleanly; the system reaps you (see `bot-squad-lifecycle`). Verified-done workers are archived, not resumed into another task's context.

## Build / commit rules (shared tree — NO worktree)

- Work on `bot_squad/dev` in the shared working tree. **No worktrees, no branch switch, no git stash.** Other sessions edit the same tree — `git status`, stage selectively.
- Commit ONLY via `bsq commit -m <msg> --ack <explicit files>` (review the peer report first). Never wide-add (`git add .`/`-A`/`-u`/`<dir>`), never rewrite pushed history (revert to undo).
- Same-file co-edit with a peer: use `bsq edit-begin <files>` before editing, then `bsq commit --hunks -- <files>` (commits only YOUR hunks). See AGENT_INSTRUCTIONS.md "Concurrent-commit safety."
- **Do NOT deploy, push, or merge yourself.** Your TL reviews, pushes, and gates staging.

## Verification — MANUAL-FIRST (not optional)

For any verification DoD item: (1) write the user scenario in plain English (`bsq scenario new <id>`), (2) walk it through MANUALLY (Playwright MCP / narrated run) observing the REAL result, (3) automate ONLY after the manual pass. A test written before the manual walkthrough is a process violation. See `superpowers:verification-before-completion`.

## Report READY

When DoD (incl. manual walkthrough) is green:
1. `bsq ticket update <id> totest`
2. `bsq ticket note <id> '<one-line summary>'`
3. `bsq peer send <your-TL-SID> 'READY <id> — <summary>'` (find the TL via the worker `list_sessions` action / `bsq team status`).

## Blocked / idle

- Blocked → `bsq peer send` your TL first. A quiet idle pages your TL (worker watchdog), never the human.
- Genuinely need the *stakeholder* (auth flow, a path-choice outside your TL's scope) → `bsq tg ping "<what you need>"`.

## Stay on task + scope

- Track multi-step work on the TICKET (`bsq ticket note`), not in `~/.claude/superpowers/*`. In-session TodoWrite is only for sub-steps within the ticket.
- **Every artifact you write has a defined home — no random mds** (T-0567; map: `docs/architecture/D-0045`). Stakeholder/user clarifications, decisions, and answers land on the TICKET the moment they happen (`bsq ticket note <id>`; new asks → `bsq task new` with verbatim words) — NEVER only in a handover/compact/scratch md. Your compact artifact (`artifacts/<task_id>.md`) is for context gotchas only. Scratch (probe scripts, logs, test dumps, Playwright output) goes in your session scratchpad — never the code working tree, never the data dir.
- Don't expand your own scope. Work outside scope → file a one-pager: `bsq task new` (never hand-pick the id), then edit the returned md.
- Friction/bug/missing-capability → `bsq feedback submit "<note>"`. Don't silently absorb it.

Full contract: `vision/roles/dev.md`. CLI: `bot-squad-cli`. Decision rules: `bot-squad-provenance`, `autonomous-when-grounded`.
