---
name: autonomous-when-grounded
description: Use whenever you hit a sub-question, ambiguity, or decision point in a bot-squad session and feel the urge to stop and ask the stakeholder/operator. Applies the exact 4-criteria ask rule + cost-of-mistake gate so you decide and keep moving when grounded, and ask only when genuinely necessary. Triggers on "should I check with them first?", "I'm not sure which way they want this", "let me wait for confirmation".
---

# Autonomous when grounded — decide, don't block

## The principle (clarification-04 / 05, verbatim)

> "Decide sub-questions yourself, ground in quotes, don't block on the user." — clarification-04

> "Ask ONLY if real AND ungroundable AND non-trivial AND important; weigh cost-of-mistake (this project = low → full speed)." — clarification-05

Blocking on the stakeholder for things you could ground or reverse is the failure mode. Default to deciding and moving.

## The 4-criteria ask rule (ALL four must hold)

Ask the human ONLY if the question is:

1. **Real** — an actual blocker, not a comfort-seeking confirmation.
2. **Ungroundable** — you cannot answer it from a quote, a ticket's verbatim ask, the docs, the code, or `bsq guidance search`. **Try grounding first.**
3. **Non-trivial** — the answer isn't obvious or conventional-default.
4. **Important** — getting it wrong actually matters.

If ANY one fails → **decide yourself and proceed.** Don't ask.

## Cost-of-mistake gate

Even when the four hold, weigh the cost of being wrong:

- **Reversible / cheap to fix** → decide, ship, see. This project is explicitly **low cost-of-mistake → full speed** (clarification-05; staging breakage is fine — constitution). Do NOT add "are you sure?" gates.
- **Irreversible** (data loss, destructive migration, credential change, prod-breaking-without-rollback) → that's the genuine ask. Page deliberately.

## Ground first — the moves

Before deciding "I need to ask," exhaust grounding:

1. `bsq guidance search <task-id> <keywords>` — buried stakeholder guidance (backlog verbatim + session logs + jsonl). Re-run with other terms.
2. Re-read the ticket's `## Verbatim request`, the initiative, the relevant role doc / `AGENT_INSTRUCTIONS.md` / constitution.
3. Look at the code. Most "which way do they want this" answers are a convention already in the repo.

A decision grounded in any of these is a grounded decision — proceed and cite the source (see `bot-squad-provenance`).

## If you DO need to ask

- Dev → `bsq peer send` your TL first (routine), `bsq tg ping` the stakeholder only for decisions outside your TL's scope.
- Operator/TL → `bsq tg ping` for genuine human decisions.
- Send ONE clear question; keep working on what you CAN unblock meanwhile. Don't sit idle.

## Crash recovery (clarification-04)

Don't rely only on a graceful compact-exit. If you resume from a crashed/ungracefully-terminated session, reconstruct state from the artifact/ticket/progress notes and re-drive — don't block waiting for a clean handoff that never came.

## Rationalizations — STOP

| Excuse | Reality |
|--------|---------|
| "Let me just confirm before I start." | Comfort-seeking. If it's groundable or reversible, decide. |
| "I'm not sure which approach they'd prefer." | Check the code/docs/guidance — the convention is usually there. Grounded → decide. |
| "Better safe than sorry, I'll ask." | This project is low cost-of-mistake. Safe = shipping and iterating, not blocking. |
| "It's a big decision." | Big ≠ irreversible. Only irreversibility justifies blocking. |
| "I'll wait for them to reply." | Don't idle. Decide, or work the unblocked parts while one explicit ask is pending. |

## Red flags

- About to send a question you haven't tried to `bsq guidance search` first.
- Asking for "confirmation" / "sign-off" rather than a genuine unknown.
- Stopping all work to wait for an answer you don't strictly need to proceed.
- Treating a reversible choice as if it were irreversible.

**All of these mean: ground it and decide. Reserve the human for real, ungroundable, non-trivial, important, irreversible-ish calls.**
