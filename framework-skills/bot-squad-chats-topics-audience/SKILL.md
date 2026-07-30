---
name: bot-squad-chats-topics-audience
description: Use when a Telegram message has arrived and you must decide WHOSE it is and whether it is an instruction — which project a chat or forum topic belongs to, whether a post in a topic is addressed to you at all, whether to escalate it or only record it, and who wrote an outbound message. Triggers on "he wrote this in a topic", "which project is this about?", "should I file and dispatch this?", "is this a directive or an observation?", "who sent this message?", a message whose content names a project other than the one you are running in, and any relay from an attendant to an operator.
---

# Chats, topics and audience — whose message is this, and is it an instruction?

## The failure this exists to prevent (stakeholder verbatim)

> «и это не было адресовано оператору, я просто написал это в топик»
> — "and it was not addressed to the operator, I just wrote it in the topic"

> «весь мой вопрос про вотчробот, это ты почему-то начал говорить про бот-сквод»
> — "my whole question is about watchrobot; it is you who for some reason started
> talking about bot-squad"

> «почему они вообще "ушли оператору", когда я писал это просто в general и
> должны были уйти usersession?»
> — "why did they 'go to the operator' at all, when I just wrote this in general
> and they should have gone to the usersession?"
> (stakeholder, 2026-07-30)

Within one hour, three sessions mis-modelled the same thing: a stakeholder post
in a forum feed was relayed to an operator as if addressed to it, filed as a
directive, and dispatched against the **wrong project**. Every fact needed to get
it right was already on disk in a runtime store. Nothing was missing except the
reading guide — this is it.

## 1. Chat vs topic

- **Chat** — one Telegram conversation the bot can reach. A private DM (its
  `chat_id` *is* that user's TG user id, positive) or a group/supergroup
  (negative). **A chat is an address, not a project.**
- **Topic** — a named thread inside a forum-enabled supergroup. Telegram calls
  the field `message_thread_id`; bot-squad calls it the topic or thread id. A
  message posted in a topic carries it; a message posted in the same chat
  *outside* any topic — the **General** feed — carries none, and every bot-squad
  store keys that as thread `None`.
- **General is bound like a topic and is not a topic.** It can carry a binding of
  its own, and that binding can point at a **different project** than the
  forum's real topics do. Treat "no thread id" as its own routing case, never as
  "the chat's default".

Two other things are also called topics and answer a different question: the
per-project message-**class** topics (`tg_topics.py` — feedback / deploy-logs /
team-queries) say *where deploy logs go*, and a per-**task** topic is an ordinary
topic binding that also carries a `ticket_id`/`session_id`. Neither tells you
whose a message is.

## 2. A topic binds to a PROJECT — not to a role, not to a session

The runtime binding store maps `(chat_id, thread_id)` → `{slug, ticket_id,
session_id, pinned_message_id}`. `slug` is a **project**. Act on these:

- **A binding names a subject, never an audience.** A topic bound to project P
  means "messages here are recorded against P". It does **not** mean "addressed
  to P's operator", and it does not mean addressed to whoever reads it.
- **One chat can serve several projects, and one project several chats** — this
  is deliberate design, not an accident to be tidied up. So a chat id alone can
  never name the project. On a real install two different projects share one DM
  chat id, which is why a chat-level lookup answers confidently and wrongly.
- A binding may additionally carry a `session_id` (direct mode, or a per-task
  topic). That is the **only** case where a topic names a recipient — and even
  then it names one session, never a role.
- **Bindings are runtime state.** They change with no deploy and no commit.
  Re-read the store; never hardcode a binding, never memorise one, never carry
  one across a compact.

## 3. Writing in a topic is not addressing you, and is not a directive

Two independent questions. Collapsing them into one is the documented failure:

| Question | Answered by |
|---|---|
| Which PROJECT is this about? | the binding — §4 |
| Who is it ADDRESSED to, and does it authorize work? | the message's own content — §5 |

**A post in a topic is an observation by default** — and General counts as a topic
for this rule. A topic is a place the stakeholder thinks out loud in; arriving in a
thread you attend is not an act of addressing you. Specifically:

- A post that *describes* a problem is not an instruction to fix it.
- A post that names another project's behaviour is not an instruction to change
  this one.
- "It landed in the thread I attend" answers only which project's record it goes
  into — it never converts to authorization.

The system already encodes the distinction: `drive_mode` (`record_only` /
`bounded` / `do_all` / `ambiguous`, from the worker's `placement_decision` action
/ `dispatch.classify_drive_mode`). Its default is `ambiguous`, and `ambiguous`
means **ask**, not run.

## 4. Determine the project BEFORE you act — the ladder, in order

1. **Allowlist.** The chat must be some project's static `tg_chat` *or* have at
   least one binding. Anything else is dropped and logged as an unknown chat.
2. **The binding for this exact `(chat_id, thread_id)`.** Strongest,
   zero-ambiguity signal, and it wins over everything below.
3. **No binding in a chat that already does per-topic routing → HELD, not
   guessed.** The message is not routed, the sender is told the topic is
   unbound, and it is recorded as an unbound topic. Do not "helpfully" pick a
   project for it — the guess is exactly the incident this rung was added for.
4. **Static fallback:** the first project whose `tg_chat` matches. In an install
   where projects share a chat that is **config-file order, not meaning** — the
   code itself calls this slug *incidental*. Never treat it as an answer to
   "whose is this".
5. **For an unquoted DM** the project-of-record is the user's sticky pin
   (`/project`), and the incidental slug from rung 4 is used only to *record*
   the message while the system asks which project. Unpinned means ask.

How to read the store, rather than remembering it:

- `bsq topic list` — every current binding, one line each. This is your first
  move, not your last resort.
- Direct read of the worker's binding store (`data/_worker/tg_bindings.json`) if
  the CLI is unavailable. **Numbers live in your install's project docs, never in
  this skill** — the ids here would be wrong on any other install. (On the
  bot-squad install itself: `docs/reference/D-0067`.)

And the rule that closes the loop:

> **The content is not the binding.** A message routed to project P whose text is
> about project Q means: the record belongs to P, and the **subject** is Q. Both
> are true at once. Say both. Filing it as work on P because it arrived on P is
> the mis-dispatch this skill exists to stop.

If the store says nothing about a `(chat, thread)`, **you do not know** which
project it is. Say so and ask. "The project I happen to be running in" is not a
fallback.

### Forwarded messages: the routing says nothing about the subject

A message the stakeholder **forwards** from one project's conversation into
another chat enters as an ordinary inbound message and is recorded against the
destination's project like any other. It is not a new request, and it is usually
not even recent.

- The record carries `forwarded_from` (`bot`, or `user:<tg_user_id>`) and the
  timestamp of the **forward**, not of the original. A batch of forwards all
  share that one timestamp.
- Its **subject project** is the project it came FROM. When the forwarded text is
  a bot message it usually still carries its own `[<slug> <role>]` tag inside the
  body — read that (§6); it is the evidence.
- So: never date a directive by a forwarded record's timestamp, never read a
  forwarded batch as a burst of live messages, and never file a forwarded
  instruction as work on the project it landed in. It is context being shown to
  you, and the thing it asks for may already be done, stale, or another project's.

## 5. Escalate or not — and what an escalation must carry

**Do NOT escalate as work:**

- A post whose scope cue is `record_only` («просто заметка», "just a note", "не
  делай пока"). Record it verbatim on a ticket and **stop** — say on the ticket
  that it is record-only.
- An observation about **another** project, escalated as work on this one. Hand it
  to whoever owns the project it is about — never re-label it as this project's
  work because it arrived on this project's feed.
- `ambiguous` scope. Ask the stakeholder to pick (do everything / just this /
  just record it). Guessing and running is the failure; asking is cheap.
- Something already owned by a live session. Deliver it there instead of minting
  a second owner.

**Route each point separately — the boundary can run through the batch.** Several
points arriving together are not one routing decision. Measured on the 2026-07-30
incident: an operator first filed **all three** of the stakeholder's points as this
project's work, then, corrected, dropped **all three** — and the real boundary ran between
them (the incident belonged to one project, the requests to another). Its own
summary of the failure: *"ошибся в обе стороны"* — wrong in both directions.
Over-correcting a wrong attribution by flipping the whole batch is the same
mistake mirrored, and it loses the points that were right.

**DO escalate:** a direct question or an explicit instruction; anything blocked
on a decision only the stakeholder or operator can make; and anything that must
not be lost — file the ticket first, then escalate, so the escalation can be
ignored without the ask evaporating.

**An escalation must carry five things.** A relay missing 3, 4 or 5 is a
broken-telephone hop — the receiver cannot tell a directive from an observation,
and defaults to "it reached me, therefore it is work":

1. **The verbatim words.** Not your paraphrase. Paraphrase is the loss channel.
2. **Where it arrived** — chat id and thread id (or "General"), plus the project
   the binding resolved to.
3. **The subject project**, when it differs from 2 — named as a difference, out
   loud: "arrived on P's feed, the content is about Q."
4. **The audience** — "written in a topic, not addressed to anyone" vs "asked the
   operator a question directly". If you are inferring this, say you are inferring
   it. Read it off the `author="user"` record itself (§6), never off another
   session's relay of it.
5. **The drive mode** and the cue that fired.

## 6. Who wrote what

**Outbound.** The sender tag `[<slug> <role>]` is applied at the transport, and
it is derived from the **sending session**, never from the destination chat:
the session's own on-disk record says which project it belongs to. Read it as
"this project's session speaking", and read its absence as "the send named no
sender" (an interactive command reply, a voice-note echo) — never as "no
project". A wrong tag is corrected, not doubled, when a session hand-types one;
so a hand-typed tag is never evidence of who actually sent it. A destination
lookup is used *only* as a last-resort fallback for the slug, because a shared
chat cannot identify a project.

**Inbound and stored.** In the conversation store, **`author` is the
discriminator, and it is a field to read rather than an inference to make:**
`"user"` is the human himself, `"session:<sid>"` is an attendant writeback, and
`system:*` is machine-composed. This is what lets you tell a **relay from an
original** — an attendant's relay of his words and his own message are two
different records with two different authors, and only the `author="user"` one
tells you whether he addressed anyone. Reading the relay alone is how a directive
and an observation become indistinguishable.

Other fields worth knowing: `fyi: true` marks a record deliberately needing no
answer; `general_feed: true` marks one that arrived via an explicit General-feed
binding rather than a DM — those two `thread_id: None` cases are otherwise
indistinguishable, and that flag is the only thing separating them;
`forwarded_from` marks a forward (above); `reply_to` carries a quoted original.

**Record keys are additive, so an absent field is not a negative.** Fields were
added by successive tickets and older records simply lack them — a record written
before `general_feed` existed carries no flag even if it came from General. Check
when a field started being written before reading its absence as evidence.

## Red flags

- You know which thread a message arrived in but have not read the binding.
- You are about to dispatch work whose subject project you never named.
- You are relaying to an operator and have not said whether he addressed anyone.
- You inferred a project from the chat id, from the content, or from "the
  project I'm running in".
- You are treating a description of a problem as an instruction to fix it.
- You are acting on an attendant's relay without having read the `author="user"`
  record it relays.
- You are treating a forwarded message as a live request, or dating it by the
  forward's timestamp.
- You corrected a wrong project attribution by flipping the whole batch to the
  other project.
- You are quoting a binding or a chat id from memory or from a handoff artifact
  instead of the store.

Pairs with `bot-squad-provenance` (a topic post is not an ask you can build
from) and `autonomous-when-grounded` (decide freely once you know whose it is).
