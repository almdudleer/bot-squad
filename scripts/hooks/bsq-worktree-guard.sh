#!/usr/bin/env bash
#
# bsq-worktree-guard.sh — refuse the commands that destroy a peer's uncommitted
# work in this shared clone (T-0826).
#
# PROVENANCE — LIFTED, THEN WIDENED
# ---------------------------------
# The stash half of this file is watchrobot's `.githooks/bsq-stash-guard.sh`
# (their T-0433, eeacb17e), taken wholesale at 2026-07-30T13:41Z and offered for
# exactly that: a second independent implementation of the same guard IS the
# duplicate divergence this repo suffers from (T-0818: solve it once). It was
# renamed from `bsq-stash-guard.sh` when operator p502 widened the scope at
# 13:52Z, because a file that guards five verb families and is named for one of
# them is a lie the next reader pays for. Everything else is structurally theirs
# and stays diffable against it. Found a defect in the lifted parts? Tell their
# T-0433; do not fork it.
#
# THE INCIDENT (2026-07-30T12:55:49Z, watchrobot's dev clone)
# ---------------------------------------------------------
# A release TL ran a BARE `git stash` in a shared clone with EIGHT live
# sessions. One command swept the entire working tree into stash@{0}: 24 files
# across six tickets, at least FIVE sessions' uncommitted work. The tree then
# read CLEAN — no error, no warning, nothing pointing at the cause, exit 0.
#
# Nothing was ultimately lost, but only narrowly: two sessions noticed within
# ~2 minutes and both REFUSED the obvious recovery, because `git stash pop`
# would have restored three peers' files underneath them mid-edit. They
# extracted single paths read-only with `git show` instead.
#
# ★ THE MECHANISM, which is worse than "a stash takes your dirty files": the
# stash object had only TWO parents, so it was NOT `stash -u` — yet brand-new
# UNTRACKED files went in anyway, because a DIFFERENT session had `git add`ed
# them. THE INDEX IS SHARED. A bare stash takes whatever any peer happens to
# have STAGED. That is not a discipline problem; it is a property of one index.
#
# WHY FIVE FAMILIES AND NOT ONE (operator p502, 13:52Z, on p531's enumeration)
# ---------------------------------------------------------------------------
# "Guarding verbs one at a time is a losing game" is the obvious objection, and
# it was MEASURED rather than argued. Exactly five families destroy a peer's
# uncommitted work in a shared clone, and the set is CLOSED:
#
#   git stash        (bare)   tracked worktree edits + anything peer-STAGED
#   git reset --hard          tracked worktree edits + anything peer-STAGED
#   git checkout -- .         tracked worktree edits
#   git restore .    (wide)   tracked worktree edits (+ staged, with --source)
#   git clean -fd             untracked files
#
# `git checkout <branch>` destroys NOTHING and must keep working — it is the
# near-miss this guard is most likely to get wrong, so it is pinned in the
# selftest. One predicate covers all five: does this command revert or remove
# paths in the SHARED worktree. Shipping a stash-only guard would have left four
# equivalent holes open AND taught every session the tree is protected, which is
# worse than no guard at all.
#
# WHY A GUARD AND NOT AN INDEX FIX — DO NOT RE-DERIVE THIS
# -------------------------------------------------------
# The obvious root fix — give every session its own GIT_INDEX_FILE, killing the
# shared index that was the vector — was evaluated by watchrobot's p531 against
# four pre-registered falsifiers, all measured in scratch clones, all survived:
#
#   · A bare stash with a FULLY PRIVATE index still destroyed both peers'
#     TRACKED worktree edits. `git stash` resets the WORKTREE, and the worktree
#     is shared no matter whose index is active. It would have saved 1 of 24.
#   · A long-lived private index REVERTED a peer's LANDED COMMIT at HEAD while
#     the worktree kept their line — so `git status` looks innocent and their
#     committed work reappears as merely uncommitted. Every peer commit landed
#     since the index was seeded, not just one.
#   · A private index DISARMS `bsq commit`'s own T-0732 refusal, which treats an
#     isolated index as proof of safety. The check that blocked a 24-file commit
#     that same morning would not have fired.
#
# So a session-wide private index removes the guard that works and installs a
# silent HEAD-level failure in its place. It IS right as an EPHEMERAL,
# per-commit index — which is exactly what `bsq commit --hunks` already does.
# Leave that alone; do not introduce a session-wide one.
#
# THE SAFE IDIOM, also measured: `git stash push -- <paths>` scoped PERFECTLY —
# a peer's dirty tracked file, a peer's staged+dirty file, and a peer's staged
# brand-new module all survived and stayed staged. Scoping is FILE-granular, not
# hunk-granular, so a peer co-editing the SAME named file still loses their
# hunks — the same caveat safe-commit documents for pathspec commits.
#
# EXIT CODE CONTRACT — EXACTLY TWO MEANINGFUL CODES, AND THAT IS DELIBERATE
# (watchrobot p531, found by sabotage after they had shipped and verified):
#
#   0            allow
#   3            refuse
#   anything else  THE GUARD FAILED. The caller must fail CLOSED for a guarded
#                  verb, because a broken check is not evidence the command is
#                  safe. Their hook only caught Python-level exceptions, so a
#                  MISSING guard exited 127, read as "not a refusal", and
#                  silently allowed `git stash` — i.e. deleting or RENAMING this
#                  file disarmed every session with no signal anywhere. They had
#                  renamed it themselves earlier in the same ticket, which is
#                  how plausible that is.
#
# THERE IS DELIBERATELY NO THIRD OPINION CODE. "Not inside a git repo" used to
# be exit 2 — but BASH ITSELF EXITS 2 ON A SYNTAX ERROR, so "the guard is broken"
# and "the guard has an opinion" would be the same code and a syntax error here
# would read as a verdict. It returns 0 instead: outside a repo there is no
# shared worktree to protect, so there is nothing to refuse.
#
# OVERRIDE (you own every pending change in this tree), either name, all five
# families — kept identical to watchrobot's so the two copies stay diffable:
#   BOT_SQUAD_ALLOW_WORKTREE_WIPE=1     canonical
#   BOT_SQUAD_ALLOW_WIDE_STASH=1        the original name, still honoured
# Deliberately NOT documented as routine: the incident was committed by a
# session that had already been told the rule, so documentation is not the
# control here. It does NOT waive the stash-label requirement below.
#
# ENTRY POINTS (see scripts/hooks/README-git-guards.md):
#   1. Claude Code PreToolUse hook (scripts/hooks/bsq-pretooluse-git.py), wired
#      in this clone's .claude/settings.json — the one that actually fires.
#   2. Directly:  bash scripts/hooks/bsq-worktree-guard.sh check <verb> [args…]
#      Read-only and side-effect free, so this is also the safe way to ask
#      "what would this take right now?" in the live tree.
#   NOT SHIPPED: watchrobot's opt-in PATH shim, which is the only layer that
#   catches one of these verbs reached from INSIDE a script. Measured on this
#   tree across all five families, no script under deploy-recipes/, scripts/,
#   worker/ or api/ EXECUTES one — the sole executed git-checkout in a recipe is
#   `git -C <install> checkout -B <branch> origin/<branch>`
#   (deploy-recipes/bot-squad/staging.sh:67), which is branch-level and
#   destroys nothing. So our entire exposure is agent-typed command strings,
#   which is what layer 1 catches. Re-measure before changing that.

set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/peer-activity.sh
. "${HOOK_DIR}/lib/peer-activity.sh"

LOOKBACK_HOURS=${BOT_SQUAD_LOOKBACK_HOURS:-24}

# `check` is accepted (and ignored) so the entry points can call this script
# with an argv that starts at the git subcommand.
[ "${1:-}" = "check" ] && shift
family="${1:-}"; shift 2>/dev/null || true

if ! REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    # 0, not 2 — see the exit-code contract above. Nothing to protect here.
    echo "bsq-worktree-guard: not inside a git repo — nothing to protect" >&2
    exit 0
fi

case "$family" in
    stash|reset|checkout|restore|clean) ;;
    *) exit 0 ;;   # not one of the five — nothing to say
esac

# ---------------------------------------------------------------------------
# CLASSIFY. Walk argv the way git does, just enough to tell a subcommand from a
# flag and a flag from a pathspec, and set:
#
#   destructive   1 if this command reverts/removes worktree paths at all
#   wide          1 if it names no bounded pathspec (`.` counts as wide)
#   pathspecs[]   the bounded paths, when there are any
#   want_staged   include staged (index) changes in the sweep set
#   want_tracked  include tracked worktree modifications
#   want_untracked include untracked files
#   want_ignored  include ignored files (git clean -x/-X)
#   trap_verb     non-empty for the stash-recovery verbs, which get their own
#                 refusal and the read-only extraction recipe
# ---------------------------------------------------------------------------
destructive=0
wide=1
want_staged=0
want_tracked=0
want_untracked=0
want_ignored=0
trap_verb=""
subcmd=""
saw_message=0
pathspecs=()
verb_text="git $family"

# `arg_is_path <arg>` — a bare argument that names something in the tree rather
# than a rev. `.` is a path AND is the wide form.
arg_is_path() {
    local a="$1"
    [ "$a" = "." ] || [ "$a" = "./" ] || [ "$a" = ":/" ] && return 0
    [ -e "$a" ] && return 0
    git ls-files --error-unmatch -- "$a" >/dev/null 2>&1 && return 0
    return 1
}

arg_is_rev() {
    git rev-parse --verify --quiet "$1^{commit}" >/dev/null 2>&1
}

# Anything that names `.` (or the repo root) is a WIDE op wearing a pathspec.
pathspec_is_wide() {
    local p
    for p in "$@"; do
        case "$p" in
            .|./|:/|"$REPO_ROOT"|"$REPO_ROOT"/) return 0 ;;
        esac
    done
    return 1
}

case "$family" in

# ── stash ───────────────────────────────────────────────────────────────────
stash)
    after_dashdash=0; skip_next=0; saw_pathspec=0
    for arg in "$@"; do
        if [ "$after_dashdash" = "1" ]; then
            saw_pathspec=1; pathspecs+=("$arg"); continue
        fi
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        case "$arg" in
            --) after_dashdash=1 ;;
            --include-untracked|--all) want_untracked=1 ;;
            # Value-taking options: skip the value so a stash message like
            # `-m "backend/app.py wip"` is never misread as a pathspec.
            -m|--message) saw_message=1; skip_next=1 ;;
            --message=*) saw_message=1 ;;
            --*) : ;;
            -*)
                # A SHORT CLUSTER. `-uq` and `-au` are the wide forms an exact
                # `-u` match misses, and `-qm` takes the next argv element as its
                # message — left unskipped it reads as a pathspec and narrows the
                # sweep set to nothing, which waves the command through. Neither
                # is exotic; they are what people actually type.
                case "$arg" in
                    -*[ua]*) want_untracked=1 ;;
                esac
                case "$arg" in
                    -*m*)
                        saw_message=1
                        # `-m` must be last in a cluster (or carry its value
                        # inline, `-mfoo`). Only the former eats the next argv.
                        [ "${arg%m}" != "$arg" ] && skip_next=1
                        ;;
                esac
                ;;
            *)
                if [ -z "$subcmd" ]; then
                    subcmd="$arg"
                else
                    # Only `push` takes PATHSPECS as bare arguments. `save` takes
                    # a MESSAGE, and pop/apply/drop take a stash ref — reading
                    # either as a pathspec would silently narrow the sweep set to
                    # nothing and wave the command through.
                    case "$subcmd" in
                        push) saw_pathspec=1; pathspecs+=("$arg") ;;
                        save) saw_message=1 ;;
                    esac
                fi
                ;;
        esac
    done
    [ -z "$subcmd" ] && subcmd="push"
    verb_text="git stash $subcmd"

    case "$subcmd" in
        list|show)
            exit 0 ;;   # read-only: the diagnosis path, never blocked
        pop|apply|drop|clear|branch)
            trap_verb="$subcmd"; destructive=1 ;;
        push|save|create|store)
            destructive=1
            want_staged=1
            want_tracked=1
            if [ "$saw_pathspec" = "1" ] && [ "${#pathspecs[@]}" -gt 0 ]; then
                wide=0
                pathspec_is_wide "${pathspecs[@]}" && wide=1
            fi
            ;;
        *)
            # An unrecognised stash subcommand. It is still `git stash`, so
            # treat it as the destructive default rather than waving it through.
            destructive=1; want_staged=1; want_tracked=1 ;;
    esac
    ;;

# ── reset ───────────────────────────────────────────────────────────────────
# Only `--hard` touches the worktree. `--soft`/`--mixed`/a pathspec reset move
# the INDEX only: a peer's staged brand-new file becomes untracked, but its
# CONTENT survives, so it is not in the closed set and is not guarded here.
# `git reset --hard` takes no pathspec, so it is always wide.
reset)
    hard=0; mode=""
    for arg in "$@"; do
        case "$arg" in
            # --merge and --keep also rewrite worktree files, they are merely
            # politer about it: both abort on some conflicts and revert the
            # rest. watchrobot guard all three; so do we.
            --hard|--merge|--keep) hard=1; mode="$arg" ;;
        esac
    done
    [ "$hard" = "1" ] || exit 0
    destructive=1; wide=1; want_staged=1; want_tracked=1
    verb_text="git reset $mode"
    ;;

# ── checkout ────────────────────────────────────────────────────────────────
# The near-miss family. `git checkout <branch>` destroys nothing and MUST pass;
# `git checkout -- .` reverts every tracked worktree edit in the tree.
checkout)
    after_dashdash=0; skip_next=0; saw_pathspec=0
    force=0; branchish=0; patch=0; rev=""
    positionals=()
    for arg in "$@"; do
        if [ "$after_dashdash" = "1" ]; then
            saw_pathspec=1; pathspecs+=("$arg"); continue
        fi
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        case "$arg" in
            --) after_dashdash=1 ;;
            -b|-B|--orphan) branchish=1; skip_next=1 ;;
            -t|--track|--conflict|--pathspec-from-file) skip_next=1 ;;
            -f|--force) force=1 ;;
            -p|--patch) patch=1 ;;
            -*) : ;;
            *) positionals+=("$arg") ;;
        esac
    done

    # Interactive: git prompts per hunk, so it cannot silently sweep a tree.
    [ "$patch" = "1" ] && exit 0
    # Creating a branch carries the worktree over; nothing is reverted.
    [ "$branchish" = "1" ] && exit 0

    if [ "$saw_pathspec" = "1" ]; then
        # Anything before `--` is the source rev, so staged content goes too.
        [ "${#positionals[@]}" -gt 0 ] && rev="${positionals[0]}"
    elif [ "${#positionals[@]}" -eq 0 ]; then
        exit 0                              # `git checkout` alone: a no-op
    elif arg_is_path "${positionals[0]}" && ! arg_is_rev "${positionals[0]}"; then
        saw_pathspec=1; pathspecs=("${positionals[@]}")
    elif arg_is_rev "${positionals[0]}"; then
        # A branch/rev switch. Harmless — UNLESS -f, which discards local
        # modifications on the way, which is the whole tree.
        if [ "$force" = "1" ]; then
            destructive=1; wide=1; want_tracked=1
            verb_text="git checkout -f ${positionals[0]}"
        else
            exit 0
        fi
    else
        exit 0                              # git will error on this itself
    fi

    if [ "$saw_pathspec" = "1" ]; then
        destructive=1
        want_tracked=1
        [ -n "$rev" ] && want_staged=1      # `git checkout HEAD -- x` takes staged too
        wide=0
        pathspec_is_wide "${pathspecs[@]}" && wide=1
        verb_text="git checkout ${rev}${rev:+ }-- ${pathspecs[*]}"
    fi
    ;;

# ── restore ─────────────────────────────────────────────────────────────────
# `--staged` alone is index-only and survives; the default (and `--worktree`)
# reverts worktree content.
restore)
    after_dashdash=0; skip_next=0
    staged=0; worktree=0; source_given=0; patch=0
    positionals=()
    for arg in "$@"; do
        if [ "$after_dashdash" = "1" ]; then pathspecs+=("$arg"); continue; fi
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        case "$arg" in
            --) after_dashdash=1 ;;
            -S|--staged) staged=1 ;;
            -W|--worktree) worktree=1 ;;
            --source=*) source_given=1 ;;
            -s|--source) source_given=1; skip_next=1 ;;
            -p|--patch) patch=1 ;;
            --pathspec-from-file) skip_next=1 ;;
            -*) : ;;
            *) positionals+=("$arg") ;;
        esac
    done
    [ "${#positionals[@]}" -gt 0 ] && pathspecs+=("${positionals[@]}")

    [ "$patch" = "1" ] && exit 0
    # Index-only: content survives as an unstaged change.
    [ "$staged" = "1" ] && [ "$worktree" = "0" ] && exit 0
    # No pathspec at all: git errors ("you must specify path(s)").
    [ "${#pathspecs[@]}" -eq 0 ] && exit 0

    destructive=1
    want_tracked=1
    { [ "$staged" = "1" ] || [ "$source_given" = "1" ]; } && want_staged=1
    wide=0
    pathspec_is_wide "${pathspecs[@]}" && wide=1
    verb_text="git restore ${pathspecs[*]}"
    ;;

# ── clean ───────────────────────────────────────────────────────────────────
# The only family whose damage is to UNTRACKED files — a peer's brand-new module
# that has not been added yet is invisible to every other guard here.
clean)
    after_dashdash=0
    force=0; dry=0; interactive=0
    positionals=()
    for arg in "$@"; do
        if [ "$after_dashdash" = "1" ]; then pathspecs+=("$arg"); continue; fi
        case "$arg" in
            --) after_dashdash=1 ;;
            -n|--dry-run) dry=1 ;;
            -i|--interactive) interactive=1 ;;
            --force) force=1 ;;
            -x) want_ignored=1 ;;
            -X) want_ignored=1 ;;
            -*)
                # Short flags cluster: -fd, -fdx, -ndx …
                case "$arg" in
                    -*f*) force=1 ;;
                esac
                case "$arg" in
                    -*n*) dry=1 ;;
                esac
                case "$arg" in
                    -*x*|-*X*) want_ignored=1 ;;
                esac
                case "$arg" in
                    -*i*) interactive=1 ;;
                esac
                ;;
            *) positionals+=("$arg") ;;
        esac
    done
    [ "${#positionals[@]}" -gt 0 ] && pathspecs+=("${positionals[@]}")

    [ "$dry" = "1" ] && exit 0          # --dry-run: read-only, the diagnosis path
    [ "$interactive" = "1" ] && exit 0  # git prompts before each removal
    [ "$force" = "1" ] || exit 0        # without -f git refuses on its own

    destructive=1
    want_untracked=1
    wide=1
    if [ "${#pathspecs[@]}" -gt 0 ]; then
        wide=0
        pathspec_is_wide "${pathspecs[@]}" && wide=1
    fi
    verb_text="git clean ${pathspecs[*]:-(whole tree)}"
    ;;
esac

[ "$destructive" = "1" ] || exit 0

override_var="BOT_SQUAD_ALLOW_WORKTREE_WIPE"
override_on=0
if [ "${BOT_SQUAD_ALLOW_WORKTREE_WIPE:-}" = "1" ] || [ "${BOT_SQUAD_ALLOW_WIDE_STASH:-}" = "1" ]; then
    override_on=1
fi

# ---------------------------------------------------------------------------
# Every stash this tooling creates must SELF-IDENTIFY.
#
# The one reason the bare stash was instantly distinguishable on 2026-07-30 is
# that the OTHER stash in the list names itself — `pre-deploy compose <id>`. The
# anonymous one read as `WIP on bot_squad/dev: <sha>`, which says nothing about
# who made it or why, so nobody could claim or disclaim it and reconstructing
# ownership took two sessions and an operator. An unlabelled stash in a shared
# clone is an unattributable artifact by construction. This is our ticket's
# third ask ("carry p525's GOOD half forward") and watchrobot's secondary one,
# enforced rather than documented — both of today's incidents came from sessions
# that had already been told the rule.
# ---------------------------------------------------------------------------
require_stash_label() {
    local why="$1"
    cat >&2 <<EOF

$(pa_red "bsq-worktree-guard: REFUSED — this stash would be anonymous.")

${why}

A stash in a shared clone with no message is unattributable: it lands as
"WIP on $(git rev-parse --abbrev-ref HEAD 2>/dev/null): $(git rev-parse --short HEAD 2>/dev/null)", which names neither an owner nor a
reason. That is exactly why reconstructing the 2026-07-30 incident took two
sessions and an operator — while the pre-deploy stash sitting next to it in the
same list was identifiable at a glance, because it says so.

Re-run with a label. Yours would be:

  git stash ${subcmd} -m "$(pa_stash_label)"$([ "${#pathspecs[@]}" -gt 0 ] && printf ' -- %s' "${pathspecs[*]}")

EOF
    exit 3
}

# Called at EVERY point that would otherwise permit a stash to be created — the
# override, an empty sweep set, a solo clone, and the narrow-pathspec allowance.
# Putting it at only one of them is how an anonymous stash still gets made.
maybe_require_label() {
    [ "$family" = "stash" ] || return 0
    [ "$saw_message" = "0" ] || return 0
    case "$subcmd" in
        push|save) require_stash_label "$1" ;;
    esac
    return 0
}

# The override does not license an ANONYMOUS stash. An overridden stash is the
# case most likely to hold several sessions' work, so it is the one that most
# needs a name.
if [ "$override_on" = "1" ]; then
    maybe_require_label "The override lets you past the peer-activity refusal. It does not license an
anonymous stash."
fi

if [ "$override_on" = "1" ]; then
    echo "bsq-worktree-guard: override set — guard bypassed for \`${verb_text}\`." >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# The stash-recovery verbs. During the incident stash@{0} was the only copy of
# several sessions' work, and `pop` was the obvious-but-wrong recovery. These
# destroy the STASH rather than the worktree, so they get their own refusal —
# and every one of them hands back the read-only extraction recipe, because a
# guard that blocks without offering the safe path gets worked around.
# ---------------------------------------------------------------------------
if [ -n "$trap_verb" ]; then
    live_sessions=$(pa_ticket_owner_map | wc -l)
    cat >&2 <<EOF

$(pa_red "bsq-worktree-guard: REFUSED — \`git stash $trap_verb\` in a SHARED clone.")

EOF
    case "$trap_verb" in
        pop|apply)
            cat >&2 <<EOF
This is the TRAP, not the recovery (T-0826). A stash taken in this clone holds
SEVERAL sessions' files, and by the time anyone reaches for \`$trap_verb\` the tree
is usually PARTIALLY restored already. So this would:
  - restore peers' files underneath them while they are mid-edit, and
  - conflict against the paths that have already been put back.
On 2026-07-30 two sessions each had this command queued and each refused it.
That refusal is the only reason nothing was lost.
EOF
            ;;
        drop|clear)
            cat >&2 <<EOF
A stash in this clone can be the ONLY remaining copy of a peer's work — that
was literally true of stash@{0} on 2026-07-30, which is why the operator's
instruction was "stash@{0} MUST NOT BE DROPPED". Dropping is unrecoverable
once the reflog expires, and you cannot tell from the outside whether the
owning session is still alive to notice.
EOF
            ;;
        branch)
            cat >&2 <<EOF
\`git stash branch\` checks out a new branch and applies the stash — in a clone
where ~${live_sessions} sessions share this ONE working tree, switching branches
under them is a wider version of the same damage. This clone is pinned to
bot_squad/dev for exactly that reason: no worktree, no branch switch.
EOF
            ;;
    esac
    cat >&2 <<EOF

Recover YOUR OWN paths read-only instead — this is what worked on 2026-07-30:

  git stash show --name-only stash@{0}          # what is in there
  git show 'stash@{0}:<path>' > /tmp/mine       # extract ONE path, read-only
  diff /tmp/mine <path>                         # compare before overwriting

That touches nobody else's file and leaves the stash intact for the sessions
whose work is still only in it. Identify which paths are yours by CONTENT — the
ticket tag in the diff — never by adjacency in a status listing or by line count.

Override (you are certain you own every path in the stash):
  ${override_var}=1 git stash $trap_verb

EOF
    exit 3
fi

# ---------------------------------------------------------------------------
# Compute the SWEEP SET: what this command would remove from the working tree.
#
# The classes come from the family. Note `want_staged`: an untracked file a peer
# has `git add`ed is a STAGED change and goes regardless of -u. That is exactly
# how the incident swept brand-new modules out of a stash with only two parents.
# ---------------------------------------------------------------------------
status_args=(--porcelain -z)
[ "$want_ignored" = "1" ] && status_args+=(--ignored)
if [ "$wide" = "0" ] && [ "${#pathspecs[@]}" -gt 0 ]; then
    status_args+=(-- "${pathspecs[@]}")
fi

sweep=()
while IFS= read -r -d '' entry; do
    xy="${entry:0:2}"
    path="${entry:3}"
    [ -n "$path" ] || continue
    x="${xy:0:1}"; y="${xy:1:1}"
    case "$xy" in
        '??') [ "$want_untracked" = "1" ] && sweep+=("$path") ;;
        '!!') [ "$want_ignored" = "1" ] && sweep+=("$path") ;;
        *)
            # X = index vs HEAD, Y = worktree vs index.
            if [ "$want_staged" = "1" ] && [ "$x" != " " ]; then
                sweep+=("$path")
            elif [ "$want_tracked" = "1" ] && [ "$y" != " " ]; then
                sweep+=("$path")
            fi
            ;;
    esac
done < <(git status "${status_args[@]}" 2>/dev/null)

# Nothing at risk → nothing to refuse. The command will no-op on its own … but a
# stash it does create still has to be attributable.
if [ "${#sweep[@]}" -eq 0 ]; then
    maybe_require_label "Nothing of anyone else's is at risk here, so this stash is allowed — but an
unnamed stash in a shared clone is unattributable the moment anyone else looks
at the list."
    exit 0
fi

# Is this clone actually shared? A wide refusal is right in a tree several
# sessions live in and wrong in a single-tenant deploy clone, where
# `git reset --hard <prev-tag>` is the documented PROD ROLLBACK step — a guard
# that blocks an incident response is worse than the incident. The decision
# itself is deferred until after attribution, because it takes TWO conditions.
here=$(pa_sessions_in_repo "$REPO_ROOT")

# ---------------------------------------------------------------------------
# Attribute each swept path BY CONTENT (the attribution rule): the ticket tags
# present in its UNCOMMITTED lines, mapped to the live session that owns that
# ticket. Line counts are shown as damage size and never as provenance.
# ---------------------------------------------------------------------------
# Resolve roster availability in THIS scope, before the subshell below: the
# helpers are read as `x=$(f)`, and a global set inside a command substitution
# never comes back out.
roster_ok=0
pa_roster_available && roster_ok=1
owner_map="$(pa_ticket_owner_map)"

# Which tickets are MINE. Two independent sources, because getting this wrong in
# either direction is bad: claiming a peer's path is mine invites me to destroy
# it, and failing to recognise my own path makes the guard nag about work only I
# can lose.
#   1. BOT_SQUAD_TASK_ID — exported into every dev session by the spawn.
#   2. the roster row for my SID (picks up bound/extra tasks too), resolved from
#      tmux the same way bot-squad's own hooks do it.
my_sid="${BOT_SQUAD_SID:-${BSQ_SID:-}}"
if [ -z "$my_sid" ] && [ -x /home/www/bot-squad/scripts/hooks/hook_my_sid.sh ]; then
    my_sid="$(/home/www/bot-squad/scripts/hooks/hook_my_sid.sh 2>/dev/null || echo "")"
fi
my_tasks="${BOT_SQUAD_TASK_ID:-}"
if [ -n "$my_sid" ] && [ -n "$owner_map" ]; then
    my_tasks="${my_tasks}
$(printf '%s\n' "$owner_map" | awk -F'\t' -v s="$my_sid" '$2==s {print $1}')"
fi

# ---------------------------------------------------------------------------
# THREE BUCKETS, MUTUALLY EXCLUSIVE, COVERING EVERY SWEPT PATH:
#
#   foreign  the tag resolves to a LIVE sid that is not mine  → proven a peer's
#   mine     the tag matches one of my task ids               → proven mine
#   unknown  EVERYTHING ELSE: untagged, or tagged to a ticket with NO live
#            session — which is work a REAPED session left behind
#
# and the invariant foreign + mine + unknown == ${#sweep[@]}, asserted below and
# pinned in the selftest. Its ABSENCE is why both projects' suites were green
# through this: the old code counted `foreign` and `unattributed` only, and a
# path whose tag resolved to no live session fell in NEITHER. Three such paths
# printed as "3 path(s) would be removed — 0 attributed to another live session,
# 0 not attributable at all" — a headline saying nothing is at stake, which is
# the number a human reads when deciding whether to override.
#
# ★ THE REAPED BUCKET IS NOT THE RARE CASE, IT IS THE ONE THAT MATTERS MOST.
# watchrobot's p499 died this morning holding uncommitted work in
# backend/core/notify.py tagged T-0408; its operator restored it by hand from
# stash@{0} precisely because NO SESSION WAS LEFT ALIVE TO DO IT. Work with no
# living owner is the work that cannot recover itself, and this message is the
# last thing anyone reads before destroying it.
# ---------------------------------------------------------------------------
manifest=""
foreign_count=0
mine_count=0
unknown_count=0
unknown_paths=()

for p in "${sweep[@]}"; do
    tags="$(pa_tags_in_uncommitted "$p")"
    churn="$(git diff --numstat HEAD -- "$p" 2>/dev/null | awk '{printf "+%s -%s", $1, $2}')"
    [ -n "$churn" ] || churn="new file"

    # The TAG is the attribution; the session lookup is an enrichment. If the
    # roster is unreachable we still print the tag we found in the diff —
    # discarding it and reporting UNATTRIBUTED is exactly the "throw away the
    # content evidence" mistake this rule exists to prevent.
    who=""
    mine=0
    proven_foreign=0
    for t in $tags; do
        is_mine=0
        printf '%s\n' "$my_tasks" | grep -qx -- "$t" && is_mine=1
        [ "$is_mine" = "1" ] && mine=1

        sid=""
        [ -n "$owner_map" ] && \
            sid=$(printf '%s\n' "$owner_map" | awk -F'\t' -v k="$t" '$1==k {print $2; exit}')
        if [ -n "$sid" ]; then
            who="${who}${who:+, }${t} ${sid}"
            [ "$is_mine" = "0" ] && proven_foreign=1
        elif [ "$roster_ok" = "1" ]; then
            # The roster WAS readable and this ticket is not on it: the owning
            # session has been reaped. That is a fact, and a different one from
            # "we could not look" — conflating them states an unknown as a
            # specific WRONG known. p531 hit this in a run where the roster
            # returned two sessions, neither carrying a task_id.
            who="${who}${who:+, }${t} (session reaped — nobody left to notice)"
        else
            who="${who}${who:+, }${t} (roster unreadable — ownership UNKNOWN)"
        fi
    done

    if [ -z "$who" ]; then
        who="UNATTRIBUTED — open the diff, do not guess"
    fi

    # Exactly one bucket per path. Precedence foreign > mine > unknown: if ANY
    # tag in a path is proven a live peer's, the path is theirs regardless of
    # what else is in it — the dangerous reading wins. Only a RESOLVED sid that
    # is not ours counts as foreign; an unresolvable tag is reported but never
    # ASSERTED to be a peer's, because guessing an owner is the failure this
    # rule exists to prevent.
    if [ "$proven_foreign" = "1" ]; then
        foreign_count=$((foreign_count + 1))
    elif [ "$mine" = "1" ]; then
        mine_count=$((mine_count + 1))
    else
        unknown_count=$((unknown_count + 1))
        unknown_paths+=("$p")
    fi

    marker="  "
    [ "$mine" = "1" ] && marker="· "
    manifest="${manifest}${marker}$(printf '%-58s %-12s %s' "$p" "$churn" "$who")"$'\n'
done

# The invariant. If it ever breaks, a path went uncounted and the headline lies
# about how much is at stake — which is precisely the defect this replaced, and
# it was invisible for exactly as long as nothing asserted the sum.
if [ "$((foreign_count + mine_count + unknown_count))" -ne "${#sweep[@]}" ]; then
    echo "bsq-worktree-guard: INTERNAL — bucket counts ($foreign_count+$mine_count+$unknown_count)" \
         "do not sum to ${#sweep[@]} swept paths. Refusing rather than reporting a wrong total." >&2
    exit 3
fi

# ---------------------------------------------------------------------------
# Decide.
#
# A WIDE command is refused whenever anything is at risk: it named nothing, so
# it cannot have meant "and also revert whatever four peers have open". That is
# the incident, and it is never the right command in this tree.
#
# A NARROW, explicitly-pathspec'd one is a declared, bounded action, so it is
# allowed unless a named path is attributed to ANOTHER live session. Refusing
# narrow operations on your own files would push people to the override, and an
# override people reach for daily protects nothing.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# SOLO CLONE. Two conditions, not one — p531's correction, and it is right: the
# roster can UNDER-report. A session operating on this tree via `git -C`, or a
# human in a plain shell, is invisible to it. Content-attributed foreign work is
# direct evidence a peer is here whatever the roster says, and it is free
# because the manifest above already computed it. An unreadable or unparseable
# roster reads as "unknown" and keeps the refusal.
# ---------------------------------------------------------------------------
if [ "$here" != "unknown" ] && [ "$here" -le 1 ] 2>/dev/null && [ "$foreign_count" = "0" ]; then
    maybe_require_label "This clone is not shared, so the peer-activity refusal does not apply — but an
unnamed stash is unattributable wherever it is made."
    cat >&2 <<EOF
bsq-worktree-guard: allowing \`${verb_text}\` — ${here} live session(s) work in
$REPO_ROOT and none of the ${#sweep[@]} affected path(s) is attributed to another
LIVE session, so this is not a shared tree.
EOF
    # What the guard PROVED is "not attributable to a live peer". It did NOT
    # prove "yours" — and the gap between those two claims is the unknown
    # bucket, which is where a reaped session's unrecoverable work lives. The
    # earlier wording closed that gap by assertion, in the one message a human
    # reads immediately before destroying work. Same defect as attributing by
    # proximity, wearing different clothes.
    if [ "$unknown_count" -gt 0 ]; then
        cat >&2 <<EOF

$(pa_yellow "⚠ ${unknown_count} of ${#sweep[@]} path(s) could not be attributed to anyone LIVE") — untagged, or
tagged to a ticket whose session has been reaped. That is NOT the same as "yours":
if any of it was left behind by a session that has since died, NOBODY IS LEFT TO
NOTICE IT IS GONE. Open them before you proceed:

$(printf '  %s\n' "${unknown_paths[@]}")
EOF
    fi
    exit 0
fi

if [ "$wide" = "0" ] && [ "$foreign_count" = "0" ]; then
    maybe_require_label "The paths you named are not attributed to another session, so the operation
itself is fine. The stash it creates still has to say whose it is."
    exit 0
fi

# The same enumeration the commit path produces, over the paths about to go.
pa_collect_report "worktree" "$LOOKBACK_HOURS" "${sweep[@]}"

cat >&2 <<EOF

$(pa_red "bsq-worktree-guard: REFUSED — \`${verb_text}\` would destroy other sessions' work.")

$(pa_bold "${#sweep[@]} path(s) would be removed from the working tree") — of those,
  ${foreign_count} proven ANOTHER LIVE SESSION'S (a ticket tag in the diff resolves to their SID)
  ${mine_count} proven yours
  ${unknown_count} UNKNOWN — untagged, or tagged to a ticket whose session has been REAPED.
     That last bucket is the dangerous one: work with no living owner is the
     work that cannot recover itself.
${here} live session(s) are working in this tree.

${manifest}
EOF

if [ "$PA_WARNINGS_FOUND" = "1" ]; then
    cat >&2 <<EOF
$(pa_yellow "── peer-activity check ──")
$PA_REPORT
EOF
fi

case "$family" in
    stash)
        why="A stash is not a save. It REVERTS the working tree to HEAD, and takes
whatever any peer has STAGED along with it — the index is shared."
        ;;
    reset)
        why="\`git reset --hard\` discards every uncommitted change in the tree and
deletes files a peer has staged but not committed. There is no stash to
recover from afterwards: this is the one on the list with no undo."
        ;;
    checkout|restore)
        why="This reverts tracked files to their committed content. Every uncommitted
line in the paths below — whoever wrote them — is gone, with no stash and no
error message."
        ;;
    clean)
        why="\`git clean\` DELETES untracked files. A peer's brand-new module that has
not been \`git add\`ed yet is invisible to every other safeguard here and is
unrecoverable once removed — it was never in git at all."
        ;;
esac

cat >&2 <<EOF
${why}
This tree is shared by every live session, so the work above becomes invisible
with no error and no attribution. On 2026-07-30 that took 24 files across six
tickets out of five sessions at once (T-0826, watchrobot T-0433). A wide COMMIT
is already refused here for the same reason (bsq's co-edit audit, T-0752); these
five verbs were simply never wired to a guard.

What to do instead:

  Wanted a clean tree for YOUR files only?
    bsq commit -m "msg" <your paths>      # land them; a commit cannot be lost
    git stash push -- <your paths>        # measured to scope perfectly: peers'
                                          # dirty, staged and staged-new files
                                          # all survive and stay staged
  Wanted to see the tree at HEAD without touching it?
    git show HEAD:<path>                  # read HEAD's version of one file
    git diff HEAD -- <path>               # what is uncommitted in one file
    git stash list / git stash show       # read-only, never blocked
    git clean -nd                         # what a clean WOULD delete

  Need a pristine tree to test against? Use a scratch copy, not this tree:
    git archive HEAD | (mkdir -p /tmp/pristine && tar -x -C /tmp/pristine)

Identify what is yours by CONTENT — the ticket tag in the diff — never by
adjacency in a status listing or by line count. Both misattributions during the
2026-07-30 incident came from exactly that. Note that pathspec scoping is
FILE-granular, not hunk-granular: a peer co-editing the SAME file still loses
their hunks.

Override (you own every pending change in this tree):
  ${override_var}=1 ${verb_text}

EOF
exit 3
