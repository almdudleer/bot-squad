---
name: bot-squad-provenance
description: Use whenever you are about to DO something in a bot-squad session — add a feature, button, refactor, doc, or any change — to check it traces to a stated stakeholder ask before you build it. Triggers when you catch yourself thinking "it'd be logical to also…", "while I'm here I'll…", or your target has drifted as context accumulated. No invented work; product decisions are the stakeholder's.
---

# Provenance — link every action to a stated ask (no invented work)

## The core principle (Part B, verbatim)

> "Make sure that everything that you do is linked somehow to something that I requested, so that no work is invented... the agents should not interpret this literally like 'the user didn't request to put this button here so I won't' — if there is a way that this button can be linked to something I said, like 'make this site look more modern' and this button contributes to it, then the agents should definitely do that. If the guidance is not specific like that... they should not start inventing something that is logical to do but that I didn't request, because product management and product decisions is my job." — Part B

> "An important thing is that the target they pursue should not be shifted by, for example, if they accumulate some context. So they should be focused on the initial thing that I asked them to do." — Part B

This is a **core concept of bot-squad** (clarification-04), enforced framework-wide.

## The rule, in two clauses

1. **Linkable → do it.** An action that contributes to a stated ask — even an unspecific one ("make it modern", "make it faster") — IS grounded. Unspecific guidance leaves creative space; fill it. Do NOT refuse work just because it wasn't named literally.
2. **Not linkable → don't invent it.** An action that's merely "logical" or "nice to have" but traces to no ask is **product management — the stakeholder's job, not yours.** Don't build it. Surface it instead: file a ticket (`bsq task new`) or `bsq feedback submit`.

## The grounding-check — at MAJOR decisions, NOT pedantically

Run this check at **major decision points** — starting a feature, adding a surface/button/endpoint, choosing scope, a refactor. **Not** every line, every helper, every keystroke. Don't narrate "this trace to…" for routine implementation of an already-grounded ask; that's the pedantry the stakeholder explicitly does NOT want. The check is: *for this MAJOR move, what ask does it trace to?*

- Name the source: a ticket's `## Verbatim request`, an initiative, a `bsq guidance search` hit, or a direct message. If you can name it (specific OR a contributing piece of an unspecific direction), proceed.
- Can't name one? It's invented. Stop and surface it — don't ship it.
- Record the link: cite the source in your work (provenance note / commit message).

## Provenance on tasks is enforced (live)

Every new ticket carries a source: `bsq task new` **requires** `--provenance` (e.g. `--provenance corpus:<token>` or a feedback id), and `scripts/lint/backlog_provenance.py` (pre-commit + CI) rejects tickets without it. So the chain is durable: action → ticket → its verbatim ask. When you file work outside your scope, attach the provenance just like any ticket.

## Anti-drift — keep targeting the STATED ask

> "they should probably be prompted good for them to not forget to check whether the things they are doing are targeted at something that I asked... the target that they pursue should not be shifted by, for example, if they accumulate some context. So they should be focused on the initial thing that I asked them to do." — Part B (verbatim)

As a session accumulates context, the target silently shifts toward whatever's locally interesting. Counter it with a **periodic self-check** — at each major checkpoint (a milestone, before a new sub-thread, after a long detour):

1. **Re-surface the verbatim ask.** Re-read your task's `## Verbatim request` (and the bound initiative). That string — not your evolved mental model — is the target.
2. **Ask: is what I'm doing now still targeting that ask? Has scope shifted?** If your current work no longer maps to the verbatim ask, you've drifted.
3. **If you drifted:** stop, return to the stated ask. If you're deliberately deferring/descoping it, record WHY (`bsq ticket note`) — never drift silently.

This is a *target*-drift check (am I still building the asked thing), distinct from the harness drift-check tick (time-since-commit/progress) — both run; this one is yours to self-apply.

## Rationalizations — STOP

| Excuse | Reality |
|--------|---------|
| "It's logical to also add X." | Logical ≠ requested. Unrequested product decisions are the stakeholder's. File it, don't build it. |
| "While I'm in here, I'll improve Y." | Y traces to no ask. Surface it as a ticket/feedback. |
| "The user didn't literally ask, so I'll skip it." | Wrong the other way: if it contributes to a stated (even vague) direction, DO it. |
| "I've learned more, the real goal is actually Z now." | Context drift. Re-anchor on the INITIAL ask. Z needs its own stakeholder ask. |
| "It's obviously what they'd want." | If it's obvious, it's cheap to ground via `bsq guidance search` or a ticket. Ground it or surface it. |

## Red flags

- You can't name the ask a change traces to.
- "It'd be cleaner / more complete / more logical to also…"
- Your scope today looks different from your task's `## Verbatim request`.
- You're making a product/direction decision (what to build) rather than an implementation decision (how to build the asked thing).

**All of these mean: stop building. Ground it in a stated ask, or surface it as a ticket/feedback and move on.**

Pairs with `autonomous-when-grounded` (decide HOW freely; don't invent WHAT).
