---
name: bot-squad-session-lifecycle-roles
description: Use when operating as ANY bot-squad session (operator, team-lead, dev, or user-conversation) and you need the core operating model — sessions are transient work-execution processes on one shared lifecycle, tasks are the glue, continuity lives in artifacts not in a kept-alive conversation — plus how to find your specific role's contract. Load this first when you realize you're inside a bot-squad project.
---

# bot-squad Operating Model — sessions, lifecycle & roles

This is the entry-point contract for every bot-squad session. It states the paradigm and routes you to your role + the mechanics. Ground your behavior in it — the principles below are enforced framework-wide via skills, not left to per-session memory.

## The paradigm (clarification-01, Part A)

> "The system is not focused on AI conversations, but treats them as just work-execution sessions, processes, which do not require to be interactive, synchronous, managed by a human on every turn." — Part A (verbatim)

> "All roles share ONE lifecycle (timeout / context-full → autocompact = write-everything-to-artifact + clear-context). Operator continuity = artifact + re-drive." — clarification-01

- **Sessions are transient processes, not persistent chats.** No role is a forever-session. The *system* is persistent; sessions are disposable.
- **One lifecycle for all roles.** Operator, TL, dev, user-conversation — same rules: finish→exit, or on the ~1h cache-timeout / token-limit / context-full, **compact** (write progress to an artifact) and terminate; a fresh session re-drives from the artifact. Mechanics: `bot-squad-lifecycle`.
- **Tasks are the glue.** A finite work item carries its own state, verbatim provenance, and binds to the session that ran it. Track work on the ticket (`bsq ticket note`), not a private log.
- **Continuity = artifact + re-drive**, NOT a kept-alive conversation or routine `--resume`.

## You don't hand-manage the plumbing

The 60s reconcile tick clears dead bindings, archives finished devs (zombies are impossible), reconciles teams, and recycles stale-waiting sessions on timeout. You never babysit tmux, archival, or stale task_ids. Details: `bot-squad-lifecycle`.

## Find your role contract

| You are the… | Trigger / who you are | Load skill | Doc |
|---|---|---|---|
| **Operator** | project dispatcher; human-facing; coordinates all work | `bot-squad-operator` | `vision/roles/operator.md` |
| **Team-lead** | transient lead the operator spawned for an initiative | `bot-squad-team-lead` | `vision/roles/teamlead.md` |
| **Dev** | assigned one task to build/test/commit | `bot-squad-dev` | `vision/roles/dev.md` |
| **User-conversation** | user-initiated; unrestricted; can spawn any of the above | — (see initiative M5) | — |

The canonical role/permission hierarchy (global/server/project admin & member) is in `vision/roles/role-hierarchy.md`.

## Two cross-cutting principles bind EVERY role

1. **Provenance / no invented work** — link every action to something the stakeholder asked. Load `bot-squad-provenance`.
2. **Autonomous when grounded** — decide sub-questions yourself when grounded; ask only when truly necessary. Load `autonomous-when-grounded`.

How you interact with the system day-to-day: `bot-squad-cli`.
