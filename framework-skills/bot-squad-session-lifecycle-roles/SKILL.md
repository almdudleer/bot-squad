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

## Five cross-cutting principles bind EVERY role

1. **Provenance / no invented work** — link every action to something the stakeholder asked. Load `bot-squad-provenance`.
2. **Autonomous when grounded** — decide sub-questions yourself when grounded; ask only when truly necessary. Load `autonomous-when-grounded`.
3. **Every lifecycle artifact has a defined home — no random mds** (T-0567). Before you `Write` any md, its path must be one of the defined homes (map: project doc `docs/architecture/D-0045`). Above all: **user/stakeholder requests, clarifications, and decisions NEVER strand in a session/handover/scratch md — they go on the relevant TASK** (`bsq task new` verbatim for new asks; `bsq ticket note <id>` for clarifications/decisions on existing work). Handover/compact artifacts carry context GOTCHAS only; task-relevant detail (requirements, progress, decisions, follow-ups) lives on the task or becomes a task. Scratch (probe scripts, logs, dumps) goes in your session scratchpad, never the code tree or data dir.
4. **"The concept" — look it up, never treat it as unknown (T-0595).** When the stakeholder references "the concept", the original framing, or the one-brain idea — it IS recorded; failing to recall it is a system defect ("вот то, что ты не помнишь эту концепцию, это как раз тоже минус системы"). Before answering or acting, look it up (paths relative to the project data dir): `vision/INI-XX-process-paradigm-SOURCE-VERBATIM.md` (Part A = his original structured ENGLISH concept message, verbatim; Part C indexes the raw voice transcripts), `docs/raw-user-input/process-paradigm-initiative/` (the raw transcripts themselves), and `bsq guidance search "<terms>"` (prior stakeholder comments) plus existing tasks under `data/<slug>/backlog/`.
5. **Feedback is welcome and expected.** If you hit product friction, a confusing flow, a missing capability, or a broken process/recipe, run `bsq feedback submit "<your note>"` to send it upstream to the operator/stakeholder. You don't need permission, and small notes are valuable — it lands in the project feedback queue. This is how the process improves; don't silently absorb friction.

## Two more bind every role that RECEIVES stakeholder steering directly

Operator, team-lead, user-conversation — any session the stakeholder talks *into* (pane drops, TG, voice notes, peer relays carrying his words). Your role doc states your own delta on top of these; the rule itself lives here.

6. **Dictated priorities — take up vs clarify (stakeholder 2026-07-05, T-0595).** When the stakeholder dictates new PRIORITIES, recording them is not enough — JUDGE them against in-flight work, don't just queue them.
   - **Take up directly** — the DEFAULT posture — when in-flight work is light or the dictation clearly outranks it ("сейчас вроде текущих задач особо нет, поэтому вот то, что я сейчас наговорил, нужно принять к сведению прямо").
   - **Ask to clarify** only when current in-flight tasks may legitimately outrank the new dictation and the trade-off is genuinely not obvious ("иногда текущие задачи … могут быть важнее, чем то, что я наговорил") — ONE focused question naming the competing in-flight work, not a re-litigation of the whole board.
   - **Never silently ignore** a dictated priority. Every one is either taken up (visibly moving) or explicitly queried back — there is no third state.

   He should not have to dictate the ops moves themselves ("нужно, чтобы я вот этого не говорил") — the system makes this judgement itself.

7. **Steering comments — every one lands in a durable home (stakeholder 2026-07-05, T-0590).** "Чтобы у каждого моего комментария который я оставляю было своё место в этой системе … и чтобы она реально слушалась" (T-0587 #4). Some comments don't ask for work — they steer HOW the system works (framing, standing rules). For those, capturing verbatim on a ticket is step one, not the whole job: a standing rule that lives only on a ticket dies when the ticket closes. Full recipe: `docs/architecture/D-0050`.
   1. **Capture verbatim** on a source ticket (principle 3 — unchanged).
   2. **Classify + split.** One voice note usually carries several directions; route each piece by what it steers: *how a ROLE must behave, across tasks* → role doc SSOT (`api/app/resources/roles/<role>.md`), via a build ticket quoting his words + date + source ticket (deploy-gated; T-0597 is the pattern); *how work is done in THIS project* (recipes, constraints, gotchas) → `AGENT_INSTRUCTIONS.md` (constitution for governance); *what the product IS / the concept* → vision docs (SOURCE-VERBATIM addendum, initiative md, or `bsq doc new`); *framework how-to, any project* → framework skill (D-0040); *one task's scope/decisions* → that ticket (`bsq ticket note`); *priorities* → principle 6 above.
   3. **Record the landing on the source ticket** — one note per routed piece: `routed: <piece> -> <home path / ticket id>`. A steering comment with no recorded landing is a defect, same as a dropped user request (D-0045).

   Never leave steering only in the conversation thread, a pane, or your own context — those evaporate. The durable homes are the ones the next session is automatically served: role docs ride the spawn brief, `AGENT_INSTRUCTIONS.md` rides orientation, vision rides the concept-lookup rule (principle 4), tickets ride `bsq guidance search` (which since T-0590 also indexes tickets' harvested-comments sections).

How you interact with the system day-to-day: `bot-squad-cli`.
