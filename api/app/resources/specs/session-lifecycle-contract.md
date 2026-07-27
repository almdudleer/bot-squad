# Session lifecycle & hierarchical-delegation contract (T-0142 / T-0144)

The operator → TL → dev chain and the session lifecycle it runs on. This is
the *contract*: what each layer can assume the system does for it, so nobody
has to hand-manage tmux, archival, or stale bindings. Read alongside
the role contracts under `$BOT_SQUAD/api/app/resources/roles/` (`operator.md`,
`teamlead.md`, `dev.md`) — the git SSOT, D-0043.

## The Team entity

A **Team** is a persisted, first-class bot-squad object keyed by **tmux session
name** (e.g. `bot-squad`, `bot-squad-operator-ux-and-session-mgmt`). It lives at
`data/<slug>/teams/<name>.md` and records:

- `tl` — the lead slot (the operator/teamlead-role session for that tmux
  session)
- `teammates` — live/suspended dev members
- `archived_members` — members that have been archived
- `archived` — whole-team archive flag

Teams are a **projection of the SessionMd registry**, rebuilt every 60s by the
`reconcile_teams` pass of the dispatch tick, so they **survive a worker reload**
(the md is durable and always re-derivable from the sessions on disk). You never
hand-edit a team md — change the underlying sessions and the next tick reconciles.

Surfaces:
- `bsq team list` — the persisted Team rosters (TL / teammate / archived counts)
- `bsq team status [--all]` — the live session list
- worker actions: `list_teams`, `reconcile_teams`, `archive_team`,
  `resurrect_team` (all coordinator-only)

## What the lifecycle guarantees (you can rely on these)

Every 60s the dispatch tick runs five ordered reconcilers
(`binding_gc_tick`). As a TL/operator you no longer babysit any of this:

1. **`gc_sessions`** — a SessionMd marked `active` whose pane is gone is
   flipped to `suspended` (needs a recorded `pane_id`; legacy mds are left
   alone — T-0134).
2. **`gc_dead_bindings`** (T-0142) — a session's `task_id` is **cleared** when
   that task is `closed` or its md is missing; `initiative` cleared when its
   file is missing; closed/missing `extra_task_ids` pruned. *This is why a
   suspended dev no longer shows a stale task_id in the sessions UI.* Bindings
   for `open` / `in_progress` / `totest` / `reopened` tasks are preserved.
3. **`gc_stale_bindings`** — when two sessions claim the same `task_id`, the
   live + latest one wins; losers are stripped (old value kept as
   `last_task_id`).
4. **`archive_dead_teammates`** (T-0142 / T-0144) — **dev zombies are
   impossible**. A dev whose pane exited AND whose task is `totest` / `closed`
   / missing is auto-flipped `suspended` + `archived`, its binding cleared, and
   any lingering tmux window killed. A *live* dev whose task is `closed`
   (verified-done) is suspended then archived — the aggressive working-set
   trim. **A live `totest` dev is never force-killed** (the TL may still be
   iterating review with it); a crashed in-progress dev (pane gone, task still
   open) is left *resumable*, only marked suspended.
5. **`reconcile_teams`** — rebuilds the team rosters from the now-reconciled
   registry.

Net: cleanly-exited post-totest teammates archive themselves; bindings refresh
from disk; the working set trims to verified-done; nothing leaks.

## Naming is single-source-of-truth

A session's name appears in three surfaces — the UI/sessions row label, the
tmux window name, and (cosmetically) the claude tab title. The **tmux window
name is the source of truth**. Rename through the one mutation point and all
surfaces converge within a tick:

- `bsq team rename <sid> <new-name>` → `sync_session_name` worker action

For a *live* session this renames the tmux window, rotates the pane-derived
SID, migrates the SessionMd, and rebinds the peer-bus inbox. For a *suspended*
session it updates the registry `window` field in place (the SID settles on the
next resume). Never hand-rename a tmux window and a session md separately — use
the action so they can't drift.

## Archival

- `bsq team archive <sid>` — **true archive**: suspend the pane (idempotent if
  already suspended) then `archive_session`, so the session leaves the default
  roster and its binding is released. (Replaces the old suspend-only interim.)
- `bsq team resurrect <sid>` — unarchive + resume (rotates SID, respawns the
  window via `claude --resume`).
- `archive_team` / `resurrect_team` worker actions do the same for a whole
  tmux-session-keyed team.

## Operator → TL: structured dispatch envelope (T-0144)

Operator → TL hand-offs are a **typed envelope**, not free text the TL parses by
eye. Send with:

```
bsq peer dispatch <to> <action> [--field k=v ...] [--json '<obj>'] [--note ...]
```

It rides the normal peer bus (so `check mail` still nudges the TL) but the body
is a one-line marker + JSON the TL parses deterministically:

```
[[bsq-dispatch v1]]{"action":"assign","payload":{"ticket":"T-0150","bundle":["T-0151"]},"note":"..."}
```

The TL surfaces these with `bsq inbox check` (envelope is pretty-printed under
the raw line; `--envelopes` shows only the typed dispatches).

**Action vocabulary** (TL-interpreted; the worker transports, it does not
enforce semantics):

| action        | payload                                  | TL does |
|---------------|------------------------------------------|---------|
| `assign`      | `ticket`, optional `bundle[]`, `initiative` | spawn a dev for the ticket(s) (`bsq spawn`) |
| `archive_team`| `name`                                   | `archive_team` that tmux-session team |
| `status`      | *(none)*                                 | reply with `bsq team status` / `team list` |
| `message`     | `text`                                   | plain relay / acknowledgement |

Extend the vocabulary by documenting new actions here — keep them small and
typed so parsing stays reliable.

## TL pre-approval (no permission prompts blocking dispatch)

A TL must not stall on permission prompts for routine tool calls. Two layers:

1. **Default (this install):** bot-squad sessions launch with
   `--dangerously-skip-permissions` (see the install's
   `vision/team_protocol.md`), so routine tool
   calls are already auto-allowed. Permission relays are rare.
2. **Stricter hosts / per-team policy:** drop the curated allow-list at
   `scripts/templates/teamlead.settings.local.json` (in the bot-squad repo)
   into the TL session's `.claude/settings.local.json`. It pre-approves the
   routine surface — `bsq …`, read-only git, `pytest`, `npm run build`, the
   deploy CLI — so the TL never blocks dispatch waiting for an operator to
   approve a `bsq peer send` or a `git log`.

Either way the guarantee is the same: a TL dispatching work does not generate
operator-facing approval prompts for its routine bus / spawn / read operations.

## Dev exit is always clean (zombie-impossible)

When a dev's claude exits, the next tick guarantees: SessionMd → `suspended`,
tmux window killed, task binding cleared (when delivered/closed). A dev can
therefore exit by simply finishing — no manual `suspend_session` needed, and it
will never linger as an `active` md with a stale task_id. Verified-done workers
are archived rather than resumed into a new task's context.
