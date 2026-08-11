---
name: bot-squad-lifecycle
description: Use when you need to understand what the bot-squad system does FOR your session automatically — the ~1h cache-timeout recycle + compact-to-artifact, the 60s reconcile tick (session GC, dead-binding clearing, dev-zombie archival, team reconcile), single-source-of-truth renaming, and which sessions run non-stop vs. exit when done. So you don't hand-manage tmux, archival, or stale task bindings. ALSO use when you are about to write or boot from a handoff artifact and must decide what stays in it — "my forward-state file is bloated", "what do I keep at compact time", "where does this lesson go", "this artifact has sections that are not forward state" — see "Pruning the handoff artifact" below.
---

# bot-squad Session Lifecycle (the mechanics you rely on)

## The universal lifecycle

> "Whenever this session hits timeout, or when this session hits the token limit... it's always the same compact operation... this session is asked by the system to write down all the progress and terminate itself." — voice-03 (verbatim)

Every session — operator, TL, dev — shares ONE lifecycle. Sessions are transient work-execution processes, not persistent conversations (see `bot-squad-session-lifecycle-roles` for the paradigm). What that means operationally:

- **Finish → exit.** If the work the session was created to do is done, the session exits. Don't idle indefinitely.
- **Stale-wait → recycle on timeout.** A session blocked waiting past the **~1h cache-invalidation timeout** is asked to record its results and exit. You may **postpone** to the next timeout (repeatably) if you're actively waiting on a long, known-bounded process (e.g. a build).
- **Compact = write-to-artifact → terminate → relaunch.** "This compact just takes all the context, writes it, and starts a new session with this summary" (voice-03). On exit, state whether/when resuming this session beats starting fresh from the documented progress; full `--resume` reread is discouraged.
- **The handoff has a DEFINED home, and which one turns on whether you own a TASK** (T-0567, T-0863).
  - **Task-bound (a dev, a TL on a subtask) → that ticket's `## Context`.** `bsq ticket context <id> --file <f>` REPLACES the section; it is the WHOLE handoff. **One task, one artifact** — no `artifacts/<task_id>.md`, no `bsq compact-save` (the worker refuses it for you), no progress note about compacting. It is also what the board already renders, so your successor and the stakeholder read the same thing. Because it replaces rather than appends, write what is TRUE NOW: goal, done, in progress, exact next steps, key paths, decisions, gotchas — carry forward what still holds and drop what doesn't. **Finalize is TWO writes** (T-0863): also `bsq ticket summary <id> "<one paragraph>"`, which REPLACES `## Executive summary` — progress made + what remains, for the STAKEHOLDER to read, not your successor. Only the Context write releases the wait, so a summary alone does not count as having handed off.
  - **Task-less (the operator, any other role with no assignment) → the role artifact**, system-resolved via `bsq compact-save`: operator → `artifacts/operator-state.md`; other roles → `artifacts/role-<role>-<sid>.md`. Overwrite in place, don't mint variants. These sessions have no ticket to write a Context onto, which is the only reason the artifact seam still exists.
  - **Either way, task-relevant detail belongs on the TASK, not smuggled into a file**: his words → `bsq ticket quote`, a new ask → `bsq task new`, durable state → `bsq ticket context`, one-paragraph status → `bsq ticket summary`. An unanswered stakeholder question parked in a handover md is a stranded request. Full kind→home map: project doc `docs/architecture/D-0045`.

**Which run non-stop:** the operator "is always on when the work is on until the user paused it or it stalled itself and ran out of time" and "always drives the backlog"; TLs "always drive the initiative" until stopped; devs exit when their task is done (voice-09).

## Pruning the handoff artifact (a lifecycle rule, not a tidy-up)

A handoff artifact is **forward state for one successor**. It is **not** one of the durable homes for operating knowledge (project doc `docs/architecture/D-0066` owns that rule and the three-question routing test — Q1 durable past this initiative? Q2 portable to another install? Q3 must it fire without being looked up?). **Anything in the artifact that would still be true after the current initiative ends is misfiled** — and because the artifact accepts anything and nobody reviews it, that is where knowledge goes when no home is obvious.

(Task-bound sessions: this section is about the **role artifact**, which since T-0863 only task-LESS sessions write. Your ticket `## Context` is replace-on-write, so it cannot accumulate the same way — but the routing test below still applies to what you keep in it.)

- **Prune at WRITE, audit at BOOT.** The prune is part of `bsq compact-save`, every recycle — not a size threshold. The write is happening anyway, so pruning modifies an act you must perform; a threshold makes it a separate optional act, which is what already fails. Then the *incoming* incarnation checks the file before acting: the outgoing one is context-full and degraded, the incoming one has the only fresh context.
- **The check is one grep, against a closed section list.** An operator artifact's sanctioned sections are fixed in code (`assignment.OPERATOR_STATE_SECTIONS`, T-0473: Priorities · What's happening now · Delivered · Next · Tracked issues) and the compact prompt already ships them. `grep -n '^## ' <artifact>` — **anything off-schema is an unfiled-knowledge alarm**, not untidiness.
- **★ A prune not paired with a WRITE somewhere else is a DELETION.** Afterwards the two are indistinguishable — both leave a shorter, better-looking file — so name the destination at prune time. Measured on 2026-07-30: an operator artifact went 263 → 104 lines with nothing written to receive the ~160 removed lines, and since `data/` is not git-tracked the text is unrecoverable. Two durable sections (a role-contract one and a project-doc one) were lost that way.
- **"Rewrite, don't append" is NOT this process — it is what fails.** Same file, 11 minutes later: the successor rewrote from scratch, with the complaint quoted in its own header, and the file went 104 → 234 lines with the misfiled mass back to its exact pre-tidy size — including a new "WHAT I DID THIS SESSION" log. Within the hour it reached **295 lines, larger than the 263-line version the stakeholder complained about**, having been rewritten from scratch twice in between. The tidies punctuated the growth; they did not slow it. A rewrite keeps whatever the writer judges valuable, and a hard-won lesson always feels valuable. **Routing is what shrinks the file; rewriting only reorders it.** The tell that you are about to do this: you are asking *"what did I learn that my successor needs?"* instead of *"which of these lines is already a ticket, a doc, a skill or a memory?"*
- **From a degraded context, you are NOT required to author the new home.** Either route it now if it is a one-liner into a home that exists, or **`bsq task new` it verbatim and then delete it from the artifact.** A ticket is the one home that takes arbitrary un-routed content with an owner and a timestamp (same rule D-0045 already applies to stranded user requests). That is the step whose absence turns a prune into a loss.
- **Check the home before you write** — several bullets in a bloated artifact are usually *already* homed, and re-authoring them creates the divergence D-0066 forbids. Verify-and-delete beats write-again.
- **Numbers that change every commit are forward state**, not knowledge: test baselines, the live sha, host headroom. They belong in the artifact and in no durable home.

This project's worked routing table — every section of `artifacts/operator-state.md` classified, with what stays, what is already homed, and where the rest goes — is the runbook `docs/runbook/D-0068`.

## What the 60s reconcile tick guarantees (you don't babysit this)

The dispatch tick runs five ordered reconcilers (`binding_gc_tick`), so a TL/operator never hand-manages tmux/archival/stale ids (see `$BOT_SQUAD/api/app/resources/specs/session-lifecycle-contract.md`):

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

Full contract: `$BOT_SQUAD/api/app/resources/specs/session-lifecycle-contract.md`. The paradigm behind it: `bot-squad-session-lifecycle-roles`.
