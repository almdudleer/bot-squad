# Git guards in this shared clone (T-0826)

`/home/almdudleer/bot-squad-mgmt` (aka `bot-squad/dev`) has ONE working tree and
ONE `.git/index` shared by ~10 concurrent Claude sessions committing under one
identity. Every guard here exists because a specific incident already happened.

**Documentation is not the control.** The 2026-07-30 incident was caused by a
session that had already been told the rule. Everything below is enforced by
code and covered by `test_worktree_guard.sh`; this file only explains it.

**Provenance: this is watchrobot's implementation (their T-0433), lifted rather
than reimplemented** — a second independent copy of the same guard is the
duplicate divergence this repo already suffers from (T-0818: solve it once).
If you find a defect in it, tell watchrobot's T-0433 instead of forking it.

## What is guarded

| Verb | Behaviour |
| --- | --- |
| `git stash` (no pathspec) | refused, enumerates whose work it would take |
| `git stash pop` / `apply` / `drop` / `clear` / `branch` | refused, hands back the read-only extraction recipe |
| `git stash push -- <paths>` | allowed **with a label**, unless a named path resolves to another live session |
| `git stash push` / `save` with no message | refused — an anonymous stash is unattributable |
| `git reset --hard` / `--merge` / `--keep` | refused |
| `git checkout .` / `-- .` / `-- <peer path>` / `-f <branch>` | refused |
| `git restore .` (wide, worktree-touching) | refused |
| `git clean -f` (any cluster: `-fd`, `-fdx`) | refused |
| `git stash list` / `show`, `git checkout <branch>`, `git checkout -b/-B`, plain `git reset`, `git reset --soft`, `git restore --staged`, `git clean -n` | **always allowed** |

Override, when you genuinely own every uncommitted path in the tree:
`BOT_SQUAD_ALLOW_WORKTREE_WIPE=1` (or the original `BOT_SQUAD_ALLOW_WIDE_STASH=1`;
both work for every verb). It does **not** waive the stash-label requirement.

## Why exactly these five verbs — the set is closed

"Guarding verbs one at a time is a losing game; stash was just the verb nobody
had thought of yet" is a reasonable objection, so watchrobot **measured** it. A
scratch clone was built with a peer's dirty tracked file, a peer's brand-new
untracked file, and a peer's staged new module, then each verb was run at it:

| Command | Destroys |
| --- | --- |
| `git stash` (bare) | tracked, staged-new |
| `git reset --hard` | tracked, staged-new |
| `git checkout -- .` | tracked |
| `git restore .` (wide) | tracked, staged-new (with `--staged`/`--source`) |
| `git clean -fd` | untracked |
| `git checkout <branch>` | **nothing** |

Five families, one predicate — *does this command revert or remove paths in the
shared worktree?* — and one guard covers all of them. It is a bounded problem.
`git clean` is the only one whose damage is truly unrecoverable: untracked files
exist in no index, no commit and no stash.

## Every stash must self-identify

A stash created here must carry a message; the guard refuses an anonymous one.
The reason is forensic: the only thing that made the damaging stash instantly
distinguishable on 2026-07-30 was that the *other* stash in the list names
itself (`pre-deploy compose <id>`), while the damaging one read `WIP on
bot_squad/dev: <sha>` — naming neither an owner nor a reason. Reconstructing
ownership took two sessions and an operator.

The guard prints a ready-made label (`bsq:<sid>:<task>:<utc>`) in the refusal.
It can only *refuse*: a `PreToolUse` hook can allow or deny a tool call, it
cannot rewrite the command, so it cannot inject the label for you.

## Shared vs solo — why the wide refusal is conditional

The wide refusal fires when **≥2 live sessions have their cwd inside the repo
being guarded**, or when the roster cannot be read at all (an unknown roster
counts as shared — refusing costs a re-run, permitting cost five sessions their
working trees).

It is conditional because `git reset --hard <prev-tag>` is the documented **prod
rollback** step in the release TL's role contract
(`api/app/resources/roles/prod-teamlead.md`), run in a single-tenant deploy
clone. A guard that blocks an incident response is worse than the incident.

## The two ways into the guard

1. **Claude Code `PreToolUse` hook** — `bsq-pretooluse-git.py`, wired in this
   clone's `.claude/settings.json`. **This is the layer that actually fires.**
   Git has NO pre-stash hook — nothing in `.git/hooks` can see a `git stash`
   coming, which is why nothing fired at 12:55:49Z and why this guard cannot
   live in `.githooks/`. Every session here is a Claude Code session and every
   git command goes through the Bash tool, so the hook sees the command before
   the shell does: no PATH setup, nothing to remember. An allow-list would not
   do this; only a `PreToolUse` hook can refuse a tool call.
2. **Directly** — `bash scripts/hooks/bsq-worktree-guard.sh check <verb> [args]`.
   Read-only and side-effect free, so this is also the safe way to ask "what
   would this command take right now?" in the live tree.

**The PATH shim is deliberately NOT shipped here.** A shim intercepts the git
*binary*, so it is the only layer that catches a destructive verb reached from
INSIDE a script, and it is the only layer that can *inject* a stash label rather
than merely refusing an anonymous one (a `PreToolUse` hook can allow or deny, it
cannot rewrite a command).

This was initially framed as "watchrobot need it and we do not, because their
stash list holds `pre-deploy compose …` entries created that way by tooling."
**p531 retracted that at 14:15Z and the retraction is worth keeping:** the
artifact is genuinely script-generated — `pre-deploy compose 1779118215` decodes
to the stash's own creation second — but there is **no such call in watchrobot's
scripts or recipes today either**, and their five labelled stashes are all from
May. Whatever wrote them is gone. So the asymmetry does not exist; both installs
are agent-typed-only right now, and that is a fact with a shelf life rather than
a property of either repo.

Measured on this tree, across all five families: **no script under
`deploy-recipes/`, `scripts/`, `worker/` or `api/` EXECUTES one.** The only
executed git-checkout in a recipe is
`git -C "$INSTALL_DIR" checkout -B "$DEPLOY_BRANCH" "origin/$DEPLOY_BRANCH"`
(`deploy-recipes/bot-squad/staging.sh:67`), which is branch-level and destroys
nothing; every other hit is prose in a comment, a role doc, or a help string.
So our entire exposure is agent-typed command strings, which is what layer 1
catches. **Re-measure before concluding otherwise, and raise it rather than
enabling a shim on your own judgement** — it is a per-install question, not a
general truth.

## Known limits — read these before trusting the guard

- **A destructive verb inside a script is not caught** (that is what the shim
  would be for). See above for why that is currently an empty set here.
- **Attribution is best-effort and says so.** A path is attributed only from
  ticket tags in its own uncommitted diff. No tag means `UNATTRIBUTED — open the
  diff, do not guess`, never a guess.
- **Scoping is file-granular, not hunk-granular.** `git stash push -- <paths>`
  protects every *other* file but takes the whole worktree version of each named
  path — so a peer co-editing the *same* file still loses their hunks. Same
  caveat `safe-commit` documents for pathspec commits.
- **The exit-code contract has exactly two meaningful codes**, and that is load-
  bearing: `0` allow, `3` refuse, **anything else means the guard did not run**
  and the caller fails CLOSED for that verb. There is deliberately no third
  opinion code — "not inside a git repo" returns 0, because *bash itself exits 2
  on a syntax error*, so a third code would make "the guard is broken" and "the
  guard has an opinion" indistinguishable. watchrobot shipped and verified the
  permissive version of this; sabotage found that a **missing** guard exited 127,
  read as "not a refusal", and silently allowed `git stash` — so deleting or
  *renaming* the guard disarmed every session with no signal anywhere, and they
  had renamed it themselves earlier in the same ticket.
- The asymmetry that follows: a broken guard **must not** block `ls`, `npm test`
  or `git status` — this hook sees every Bash call, so a bug in it has to
  degrade permissive — and **must** still refuse `git stash` / `reset --hard` /
  `clean -fd`, because a broken check is not evidence the command is safe. A
  failure inside the *hook itself* fails open, because at that point it knows
  nothing about the command. Both directions are pinned by sabotage.

## Why the shared index was NOT "fixed at the root" instead

The obvious root fix — one `GIT_INDEX_FILE` per session, removing the shared
index that was the vector — was **measured** by watchrobot's p531, with the
falsifying conditions written down before the experiments ran. It fails for
three independent reasons:

1. **It cannot touch the worktree class.** A bare `git stash` with a fully
   private index *still* reverted two peers' tracked worktree edits to HEAD;
   only the staged-untracked file survived. `git stash` resets the **worktree**,
   which is shared no matter whose index is active. Index isolation would have
   saved 1 of the 24 files.
2. **A long-lived private index is a worse footgun than the one it removes.** An
   index seeded at session start goes stale against HEAD, and committing it
   **reverted a peer's already-landed commit** — while the worktree kept the
   peer's line, so `git status` looks innocent and their committed work reappears
   as merely "uncommitted". Every peer commit landed since the seed, not just one.
3. **It disarms the guard that does work.** `bsq commit`'s T-0732 refusal treats
   an isolated index as *proof* of safety and skips its check.

In fairness to it: a private index **does** stop the commit-absorption class. It
is right as an **ephemeral, per-commit** index — exactly what `bsq commit
--hunks` already does — and wrong as a session-wide default. Do not introduce
one.

## Attribution rule (non-negotiable — it is what nearly compounded the incident)

Attribute work by **content**: the ticket tag in the diff, the lines a session
actually wrote. **Never** by adjacency in a `git status` listing, by line count,
or by position in any output. During the incident an owner was inferred from
`+130/+55` line counts and from two files sitting next to each other in a status
listing; the conclusion was wrong, and acting on it would have committed a peer's
unfinished feature — killing HEAD for every session, invisibly, since everyone
runs from the tree rather than a fresh clone. A session opened the diff, found
the mismatch and refused. That refusal is the only reason it did not happen.

## Recovering from a wipe that already happened

Do **not** `pop`. The tree is usually partially restored by the time anyone
looks, so a pop dumps peers' files onto live edits and conflicts against paths
already back. Extract single paths, read-only:

```bash
git stash show --name-only stash@{0}       # what is in there
git show 'stash@{0}:<path>' > /tmp/mine    # one path, read-only
diff /tmp/mine <path>                      # compare before overwriting
```

That leaves the stash intact for sessions whose work is still only in it. For
`reset --hard` / `checkout` / `restore` there is no stash to extract from —
recover from `git fsck --lost-found` if the content was ever staged, and from
nothing at all if it was not. For `git clean`, there is no recovery.

## Running the tests

```bash
bash scripts/hooks/test_worktree_guard.sh
```

Builds throwaway clones, reproduces each incident shape, and asserts both
directions — refuse AND allow — including:

- a **control arm per family**, showing the same command destroying the fixtures
  *without* the guard;
- **blast-radius pins**: ordinary git and non-git commands pass through
  untouched, and the hook is **sabotaged on purpose** (a raise at the top of
  `main()`, a `NameError` in the verb loop, a syntax error, a missing / crashing
  / hanging guard script) to show a broken hook cannot wedge the Bash tool;
- a **cost budget**, since the hook runs before every Bash call in every session;
- a re-check that `bsq commit`'s co-edit audit is still armed, with a green
  control so "refuses everything" cannot pass as "refuses correctly".

Nothing it does touches the shared working tree, and it never calls the live
worker socket — the session roster arrives through the
`BOT_SQUAD_GUARD_ROSTER_FILE` test seam. Run it after editing any file here.
