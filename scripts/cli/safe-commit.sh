#!/usr/bin/env bash
#
# safe-commit.sh — flock + absorb-proof wrapper around `git commit` for shared
# bot-squad clones.
#
# Usage (via symlink at ops/bot-squad-bin/safe-commit):
#   safe-commit -m MSG -- backend/foo.py [more/files ...]
#   BOT_SQUAD_ACK_PEERS=1 safe-commit -m MSG -- backend/foo.py
#
# This clone is shared by ~10 concurrent Claude sessions, all committing under
# one git identity, against ONE working tree and ONE `.git/index`. Three
# distinct failure modes follow; safe-commit layers a defence against each:
#
#   1. index.lock collision (T-0093). git takes `.git/index.lock` atomically via
#      O_EXCL; concurrent `git commit`s don't queue — losers get
#      `fatal: Unable to create '.git/index.lock': File exists` and exit 128.
#      Defence: take our own advisory flock *before* git, so callers queue.
#
#   2. absorbed-peer-WIP (T-0068, 2026-05-23 incident). Because the index is
#      shared, `git commit -a` / `-am` / `--all` stages EVERY tracked
#      modification in the tree — including a peer's in-flight edits — and sweeps
#      them into your commit, mis-attributed and out of their author's control.
#      Defence (ported from ~/watchrobot's shared-tree operating model, whose
#      rule is "stage by explicit path, never `git commit -a`"): refuse the
#      `-a`/`--all` family. The safe primitive is a PATHSPEC commit
#      (`git commit -- <paths>`): git builds a temp index of HEAD + those paths
#      from the worktree, so whatever else is staged is ignored — absorb-proof
#      by construction. `bsq commit` always uses this form.
#
#      CAVEAT — same-FILE co-edit (the partial-hunk case): a pathspec commit
#      protects OTHER files, but it snapshots the WHOLE worktree version of each
#      named path. So if a peer is editing the SAME file you are, your
#      `safe-commit -- that_file.py` still sweeps THEIR hunks in it. The fix is
#      the per-session hunk-isolated commit (T-0215), which commits only your
#      own baseline→current hunks via `git apply --cached` onto a temp index:
#          bsq edit-begin <files>          # snapshot baseline BEFORE you edit
#          ...edit...
#          bsq commit --hunks -- <files>   # commits ONLY your hunks
#      Post-hoc fallback (you forgot edit-begin): build a patch of only your
#      hunks and `git apply --cached --recount` it, then run THIS wrapper with
#      no pathspec to commit the staged index as-is. See AGENT_INSTRUCTIONS.md
#      "Concurrent-commit safety" for the full recipe.
#
#   3. unlocked-stage race (T-0648, 2026-07-18 p31/p34 incident). Pathspec
#      commit is only absorb-proof from the *commit* step onward; the earlier
#      `git add` that puts your paths in the index was, until this fix, run
#      by the CALLER before ever reaching safe-commit's flock. A concurrent
#      lane's locked `git commit` and your unlocked `git add` both touch the
#      one shared `.git/index` and can collide on `.git/index.lock` —
#      reproduced under stress as either a hard EEXIST failure or a lane
#      believing its commit landed when the add silently lost the race.
#      Defence: `bsq commit` now hands its path list to safe-commit via
#      BOT_SQUAD_STAGE_PATHS_FILE (a NUL-separated temp file) instead of
#      staging itself; safe-commit stages AFTER taking the flock, so
#      add+commit is one lock-scoped critical section per lane. On success,
#      safe-commit also echoes the resulting commit's file list so
#      attribution is verifiable at a glance.
#
# Overrides:
#   BOT_SQUAD_ALLOW_COMMIT_ALL=1   permit `-a`/`--all` (single-tenant clone,
#                                  you own every pending change).
#   BOT_SQUAD_COMMIT_TIMEOUT=N     flock wait seconds (default 120).
#
# Pass-through: every arg goes straight to `git commit`; exit code is git's
# (or 3 on a refused wide-stage, 2 when not inside a git repo).

set -uo pipefail

# ---------------------------------------------------------------------------
# Absorb-proof guard (T-0145) — refuse `-a`/`--all`/`-am` … wide staging.
#
# Walk argv the way git does *just enough* to tell a wide-stage flag from a
# value: skip the argument that follows a value-taking option (so a commit
# message like `-m "-a tweak"` is never mistaken for `-a`), and ignore the
# attached forms (`-mMSG`, `-Ccommit`). Anything after a literal `--` is a
# pathspec, never a flag.
# ---------------------------------------------------------------------------
saw_all=0
saw_pathspec=0
after_dashdash=0
skip_next=0
for arg in "$@"; do
    if [ "$after_dashdash" = "1" ]; then
        saw_pathspec=1
        continue
    fi
    if [ "$skip_next" = "1" ]; then
        skip_next=0
        continue
    fi
    case "$arg" in
        --)
            after_dashdash=1
            ;;
        # Options whose VALUE is the next argv element — skip that value so it
        # can't be misread as a flag.
        -m|--message|-c|-C|-F|--file|--author|--date|-t|--template|\
--fixup|--squash|--reuse-message|--reedit-message|--trailer)
            skip_next=1
            ;;
        # Attached value forms (`-mMSG`, `-cREF`, `-CREF`, `-FFILE`) — self
        # contained, nothing to skip, never a wide-stage.
        -m?*|-c?*|-C?*|-F?*)
            ;;
        --all)
            saw_all=1
            ;;
        --*)
            # other long option (e.g. --amend, --message=foo) — not -a/--all
            ;;
        -?*)
            # single-dash short cluster (`-a`, `-am`, `-sa`, …). Flag it if it
            # contains an 'a' — that's the --all short form.
            if [ "${arg#*a}" != "$arg" ]; then
                saw_all=1
            fi
            ;;
    esac
done

if [ "$saw_all" = "1" ] && [ "${BOT_SQUAD_ALLOW_COMMIT_ALL:-}" != "1" ]; then
    cat >&2 <<'EOF'
safe-commit: REFUSED — `git commit -a/--all` stages every tracked modification
in this shared clone's single index, including peers' in-flight work. That is
the T-0068 "absorbed another dev's WIP" failure mode.

Commit by explicit pathspec instead (a partial commit ignores other staged work):
  safe-commit -m "msg" -- path/to/file.py [more/files ...]
  # or, preferred:  bsq commit -m "msg" path/to/file.py

Override (single-tenant clone, you own every pending change):
  BOT_SQUAD_ALLOW_COMMIT_ALL=1 safe-commit ...
EOF
    exit 3
fi

if [ "$saw_pathspec" = "0" ] && [ "${BOT_SQUAD_ALLOW_COMMIT_ALL:-}" != "1" ]; then
    # No `-- <paths>`: the commit takes the shared index AS-IS, which may hold a
    # peer's staged files. Warn but don't block — legacy callers and `--amend`
    # fixups land here, and the pre-commit peer-activity hook is the backstop.
    # `bsq commit` always passes a pathspec, so it never trips this.
    echo "safe-commit: WARNING — no '-- <paths>' given; committing the current shared index as-is. If a peer staged files here they'll be absorbed. Prefer: safe-commit -m MSG -- <paths>" >&2
fi

# ---------------------------------------------------------------------------
# flock serialization (T-0093)
# ---------------------------------------------------------------------------
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "safe-commit: not inside a git repo" >&2
    exit 2
}

LOCK="${REPO_ROOT}/.git/.bot-squad-commit.lock"
TIMEOUT="${BOT_SQUAD_COMMIT_TIMEOUT:-120}"

# Touch the lockfile so flock has a fd to open. Harmless if it exists.
: > "$LOCK" 2>/dev/null || true

# fd 9 holds the advisory lock for the duration of the stage + commit.
exec 9>"$LOCK"
if ! flock --timeout="$TIMEOUT" 9; then
    echo "safe-commit: could not acquire commit lock in ${TIMEOUT}s — running git commit anyway, expect possible EEXIST" >&2
fi

# ---------------------------------------------------------------------------
# Locked staging (T-0648) — `bsq commit` hands us its explicit path list here
# (NUL-separated, in a temp file named by BOT_SQUAD_STAGE_PATHS_FILE) instead
# of running `git add` itself before calling this wrapper. Staging AFTER the
# flock makes add+commit one lock-scoped critical section: a concurrent
# lane's `git add`/`git commit` can no longer interleave with ours on the one
# shared `.git/index` (the 2026-07-18 p31/p34 incident — an unlocked `git add`
# racing a locked commit for `.git/index.lock`).
# ---------------------------------------------------------------------------
if [ -n "${BOT_SQUAD_STAGE_PATHS_FILE:-}" ]; then
    stage_paths=()
    while IFS= read -r -d '' p; do
        stage_paths+=("$p")
    done < "$BOT_SQUAD_STAGE_PATHS_FILE"
    if [ "${#stage_paths[@]}" -gt 0 ]; then
        if ! git add -- "${stage_paths[@]}"; then
            echo "safe-commit: git add failed for staged paths" >&2
            exec 9>&-
            exit 1
        fi
    fi
fi

git commit "$@"
rc=$?

# Echo the resulting commit's file list (T-0648) so attribution is verifiable
# at a glance — a dev can see at commit time exactly what landed, instead of
# trusting that the pathspec they passed is what actually got committed.
if [ "$rc" = "0" ]; then
    committed_files=()
    while IFS= read -r -d '' cf; do
        committed_files+=("$cf")
    done < <(git diff-tree --no-commit-id --name-only -r -z HEAD)
    echo "safe-commit: committed ${#committed_files[@]} file(s):"
    printf '  %s\n' "${committed_files[@]}"
fi

exec 9>&-
exit $rc
