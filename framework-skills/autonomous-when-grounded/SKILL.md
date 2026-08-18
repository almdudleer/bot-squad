---
name: autonomous-when-grounded
description: Use whenever you hit a sub-question, ambiguity, or decision point in a bot-squad session and feel the urge to stop and ask the stakeholder/operator. Applies the exact 4-criteria ask rule + cost-of-mistake gate so you decide and keep moving when grounded, and ask only when genuinely necessary. Triggers on "should I check with them first?", "I'm not sure which way they want this", "let me wait for confirmation".
---

# Autonomous when grounded — decide, don't block

## The principle (stakeholder verbatim)

> "you don't really need my decisions, I gave you full understanding of what I want, expansive guidance on that... so drive the team autonomously — this concept on guidance should be one of the core concepts for bot-squad, because often claude starts asking clarifying questions even though it has full objectives picture... these questions are not important, you can make them yourself; important that the option that you choose is grounded on some of my direct guidance, quotes." — clarification-04 (verbatim)

> "if any agent comes to conclusion that this hole is real, the decision can't really be grounded in quotes, AND the decision is not trivial AND an important one, then by all means it should ask. The problem is right now you tend to ask obvious choice or unimportant stuff." — clarification-05 (verbatim)

> "on this project the cost of mistake on your side is low... we will just fix it if you make a wrong call, so don't worry, drive on full speed [...] maybe we need to teach bot-squad agents to clarify the cost of mistake beforehand, to choose autonomous vs cautious." — clarification-05 (verbatim)

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

**A restart or a deploy is NOT in that bucket on his projects — he took it out himself.** 2026-08-18 (T-0903): «хватит ждать моих разрешений на рестарт … bot-squad должен рестартить и как можно скорее до меня докатывать все изменения что я прошу, я единственный пользователь пока что», and when asked whether that meant only low-risk restarts or literally any change including DB schema and prod data, «про любые!». So a change that is ready by its own bar (review, green tests, DoD) ships now — no «можно рестартить?», and **silence is not a hold**: there is no answer coming, and a session that pings once and then waits is the failure this rule names (T-0895 sat finished for a day that way). The bar itself, backups/rollback, the destroy-guard on unpushed commits, and his authority over what gets BUILT are all unchanged; report what you shipped afterwards. Full text: the same section in `operator.md` / `teamlead.md` / `prod-teamlead.md`. Where a project's own config reserves deploys to its owner (`deploy_targets = []`), this grants you nothing — it removes a waiting habit, not a project's rule.

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
