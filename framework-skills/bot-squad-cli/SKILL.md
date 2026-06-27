---
name: bot-squad-cli
description: Use when you are a bot-squad session and need to interact with the system — send/receive peer messages, spawn sessions, manage tickets/docs, search prior stakeholder guidance, page the stakeholder, or commit. The bsq CLI is the canonical surface; this covers which verb to use when, the peer-bus "check mail" signal, and the ~1h cache expiry.
---

# bot-squad CLI (`bsq`) — how & when

> "Eventually the agents should use the CLI... skills are most important in the places where agents need to use bot-squad CLI — to know when to use them and how to use them well." — voice-06 (verbatim)

`bsq` (`~/.local/bin/bsq`) wraps worker-socket actions as kubectl-style verbs and is the **canonical day-to-day surface** — use it instead of raw `curl` to the worker socket or manual `tmux send-keys`. It resolves your slug from `$PWD` and your SID from tmux automatically. **`bsq --help` and `bsq <verb> --help` are the source of truth** — run them; don't memorize flags.

## Which verb when

| Need | Verb |
|------|------|
| Drain mail addressed to you | `bsq inbox check` |
| Long-poll for mail (opt-in) | `bsq inbox wait [--timeout N]` (cap 7200s) |
| Message a peer / broadcast | `bsq peer send <SID\|teamlead\|dev> "<text>"` |
| Typed operator→TL hand-off | `bsq peer dispatch <to> <action> ...` |
| DM the stakeholder | `bsq tg ping "<message>"` |
| Spawn a dev/TL session | `bsq spawn <ticket> [--role tl] [--bundle ids] [--initiative x.md]` |
| Find a resumable expert | `bsq expert find <ticket>` |
| Session roster | `bsq team status [--all]` / `bsq team list` |
| Search buried stakeholder guidance | `bsq guidance search <keywords>` |
| Ticket status/update/note | `bsq ticket status\|update\|note <id> ...` |
| New ticket (atomic id) | `bsq task new "<title>"` (never hand-pick `T-NNNN`) |
| Manual-test scenario template | `bsq scenario new <ticket>` |
| Report process/product friction | `bsq feedback submit "<note>"` |
| Docs / use cases / flows | `bsq doc\|uc\|flow new ...` |
| Commit your work | `bsq commit -m MSG --ack <explicit files>` |
| Full orientation on demand | `bsq brief` |

## The "check mail" signal (primary channel)

`bsq peer send` writes to the recipient's inbox AND injects the text `check mail` into each live recipient pane. **When you see `check mail` in your composer, run `bsq inbox check`.** This send-keys nudge replaced the always-armed background long-poll; `bsq inbox wait` is now opt-in. Suspended/non-tmux peers pick the message up on their next `bsq inbox check`.

## On a new task: search prior guidance FIRST

Stakeholder guidance gets buried in suspended sessions' logs. Before designing an approach: `bsq guidance search <task-id> <title-keywords>`. It greps backlog Verbatim sections, session bodies, and `~/.claude/projects` jsonl for stakeholder-attributed comments. Re-run with other terms before committing to a design. (See `autonomous-when-grounded` — guidance search is how you ground a decision instead of asking.)

## The ~1h cache expiry & which sessions run non-stop

Your session is transient. On the Claude-subscription **~1h cache-invalidation timeout** (or token-limit / context-full), the system asks a stale waiting session to record results and exit; you may postpone if actively waiting on a known-bounded long process (e.g. a build). **Operators and TLs "run non-stop"** while work is on (they always drive the backlog/initiative) until the user pauses or they stall out of time; devs exit when their task is done. The full mechanics are in `bot-squad-lifecycle`.

The raw worker socket remains for advanced/debug use only — `bsq` is the documented surface. Full recipes: `AGENT_INSTRUCTIONS.md`.
