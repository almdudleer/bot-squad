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
#      hunks and hand it to `bsq commit --hunks --patch <patch> -m MSG`, which
#      applies it onto a TEMP index seeded from HEAD. Never stage the patch into
#      the shared index and commit that with no pathspec — see failure mode 4.
#      See AGENT_INSTRUCTIONS.md "Concurrent-commit safety" for the full recipe.
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
#   4. pathspec-LESS commit of the shared index (T-0732, 2026-07-27 p279/p280
#      incident). `safe-commit -m MSG` with no `-- <paths>` commits the shared
#      `.git/index` AS-IS. AGENT_INSTRUCTIONS' post-hoc hunk recipe told every
#      session to do exactly that ("your hunks only") — but the index is shared,
#      so anything a peer staged seconds earlier rides along: 0e20d32 swept 17
#      files of a peer's in-flight WIP. This wrapper only WARNED. Defence: with
#      a non-empty SHARED index and no pathspec, REFUSE (exit 3). The sanctioned
#      escape is an isolated index — `GIT_INDEX_FILE=<tmp> safe-commit -m MSG`
#      is allowed, because a peer's staging cannot reach that index at all.
#      That is what `bsq commit --hunks` does internally.
#
#   5. INVERSE-staged shared index after an isolated-index commit (T-0752,
#      2026-07-27 p299/p303 incident). Defence 4's own escape hatch left a
#      loaded gun behind. A commit built through GIT_INDEX_FILE advances HEAD
#      without ever touching the SHARED `.git/index`, and git only refreshes
#      the real index for paths it committed THROUGH it — so afterwards the
#      shared index still holds the PRE-commit blobs. `git diff --cached` then
#      reports the exact INVERSE of the commit that just landed, and `git
#      status` shows those paths "MM". That is armed, not cosmetic: the
#      ordinary `git add <my file> && git commit -m msg` idiom commits the
#      index as-is and so silently REVERTS the work that just landed, while
#      the peer's own commit looks entirely normal — no failing test, no
#      suspicious diff. On 2026-07-27 that state sat over the P1 fix in
#      d3de6cc. Defence: reconcile — see "Shared-index reconciliation" below.
#
# Overrides:
#   BOT_SQUAD_ALLOW_COMMIT_ALL=1   permit `-a`/`--all` AND a pathspec-less
#                                  commit of a non-empty shared index
#                                  (single-tenant clone, you own every
#                                  pending change).
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
# Shared-index guard (T-0732) — refuse a pathspec-less commit of a non-empty
# SHARED index.
#
# `git commit` with no `-- <paths>` commits whatever is in the index. In this
# clone that index is shared by every live session, so "commit the index as-is"
# means "commit whatever any peer staged in the last few seconds" — the
# 2026-07-27 incident, where a post-hoc hunk commit swept 17 files of a peer's
# WIP. Checked INSIDE the flock so a peer's `git add` can't land between the
# check and the commit.
#
# An ISOLATED index is the sanctioned way to do this: point GIT_INDEX_FILE at
# your own temp index (seeded `git read-tree HEAD`, then `git apply --cached`
# your hunks) and a peer's staging is unreachable by construction. That form is
# allowed through untouched — it is what `bsq commit --hunks` uses.
# ---------------------------------------------------------------------------
GIT_DIR_ABS="$(git rev-parse --absolute-git-dir 2>/dev/null || echo "${REPO_ROOT}/.git")"
SHARED_INDEX="${GIT_DIR_ABS}/index"
ACTIVE_INDEX="${GIT_INDEX_FILE:-$SHARED_INDEX}"
index_is_shared=0
if [ "$(readlink -f "$ACTIVE_INDEX" 2>/dev/null || echo "$ACTIVE_INDEX")" = \
     "$(readlink -f "$SHARED_INDEX" 2>/dev/null || echo "$SHARED_INDEX")" ]; then
    index_is_shared=1
fi

if [ "$saw_pathspec" = "0" ] && [ "$index_is_shared" = "1" ] \
   && [ "${BOT_SQUAD_ALLOW_COMMIT_ALL:-}" != "1" ]; then
    # Anything staged? (An empty index with no pathspec is harmless — that's
    # `--amend --no-edit` / `--allow-empty`, which absorb nothing.)
    if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
        git diff --cached --quiet HEAD -- 2>/dev/null
        index_dirty=$?
    else
        [ -n "$(git ls-files --cached 2>/dev/null)" ] && index_dirty=1 || index_dirty=0
    fi
    if [ "$index_dirty" != "0" ]; then
        staged_now="$(git diff --cached --name-only 2>/dev/null | sed 's/^/  /')"
        cat >&2 <<EOF
safe-commit: REFUSED — no '-- <paths>' given, and the SHARED index is not empty.

Committing the index as-is means committing whatever ANY live session staged in
this clone, not just your work. That is the T-0732 failure mode: on 2026-07-27
this exact form swept 17 files of a peer's in-flight WIP into one commit and
produced a false "my work is at HEAD" report for its real author.

Currently staged in the shared index (this is what would be committed):
${staged_now}

Commit by explicit pathspec:
  bsq commit -m "msg" path/to/file.py [more/files ...]

Co-editing ONE file with a peer, so a pathspec would sweep their hunks?
Use an ISOLATED index — a peer's staging cannot reach it:
  bsq commit --hunks -- <files>              # after 'bsq edit-begin <files>'
  bsq commit --hunks --patch <patch> -m MSG  # post-hoc, no edit-begin needed
Raw equivalent, if you must build it by hand:
  idx=\$(mktemp); export GIT_INDEX_FILE=\$idx
  git read-tree HEAD && git apply --cached --recount my-hunks.patch
  BOT_SQUAD_ACK_PEERS=1 safe-commit -m "msg"   # allowed: the index is yours

Override (single-tenant clone, you own every pending change):
  BOT_SQUAD_ALLOW_COMMIT_ALL=1 safe-commit ...
EOF
        exec 9>&-
        exit 3
    fi
fi

# ---------------------------------------------------------------------------
# Shared-index reconciliation, part 1 of 2 (T-0752) — remember which paths the
# SHARED index already disagreed with HEAD about BEFORE we commit.
#
# After the commit we bring the shared index back in line with HEAD for the
# paths we touched. For the overwhelming case (index entry == HEAD blob, i.e.
# nobody had staged anything there) that is a pure staleness repair and there
# is nothing to say about it. But if a peer had genuinely `git add`ed one of
# those paths, our repair drops their staged entry — so we have to be able to
# tell the two apart afterwards and name the blob they can recover from.
# Captured inside the flock, so it is a true pre-image of our own commit.
# ---------------------------------------------------------------------------
pre_staged_raw=""
if [ "$index_is_shared" = "0" ] && [ -f "$SHARED_INDEX" ] \
   && git rev-parse --verify -q HEAD >/dev/null 2>&1; then
    pre_staged_raw="$(GIT_INDEX_FILE="$SHARED_INDEX" \
        git diff --cached --raw --abbrev=40 --no-renames HEAD 2>/dev/null)"
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
stage_paths=()
prior_index_entries=""
if [ -n "${BOT_SQUAD_STAGE_PATHS_FILE:-}" ]; then
    while IFS= read -r -d '' p; do
        stage_paths+=("$p")
    done < "$BOT_SQUAD_STAGE_PATHS_FILE"
    if [ "${#stage_paths[@]}" -gt 0 ]; then
        # Remember what the shared index held for these paths BEFORE we stage,
        # so a failed commit can put it back (T-0732 / p279's feedback: the
        # pre-commit hook blocks the first attempt by design and tells you to
        # review, which used to leave your files staged in the shared index —
        # a loaded index sitting unattended for the whole review window).
        prior_index_entries="$(git ls-files --stage -- "${stage_paths[@]}" 2>/dev/null)"
        if ! git add -- "${stage_paths[@]}"; then
            echo "safe-commit: git add failed for staged paths" >&2
            exec 9>&-
            exit 1
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Base compare-and-swap (T-0970) — refuse a commit whose index was seeded from a
# HEAD that has since moved.
#
# An isolated-index caller seeds `git read-tree <base>` and only reaches here
# some seconds later, after its own audit and this script's peer-activity
# report. A peer commit landing in that gap is NOT caught by git: `git commit`
# CAS-checks HEAD only from its own start, so it happily records the new HEAD as
# parent while writing the tree we seeded from the old one — a well-formed
# commit that silently reverts everything the peer landed (15 files / 1453
# deletions on 2026-09-06, again on 2026-09-07).
#
# Checked HERE, inside the flock, because that is the only race-free place: every
# lane that commits through safe-commit is serialized on this lock, so a peer's
# commit either precedes our lock (we refuse, having committed nothing) or
# queues behind it. Opt-in via the env var, so no other caller changes.
# ---------------------------------------------------------------------------
if [ -n "${BOT_SQUAD_EXPECT_HEAD:-}" ]; then
    head_now="$(git rev-parse HEAD 2>/dev/null || echo '')"
    if [ "$head_now" != "$BOT_SQUAD_EXPECT_HEAD" ]; then
        cat >&2 <<EOF
safe-commit: REFUSED (T-0970) — HEAD moved while your commit was being prepared.

  your index was seeded from: ${BOT_SQUAD_EXPECT_HEAD}
  HEAD is now:                ${head_now:-<unborn>}

Committing now would record the NEW head as this commit's parent while writing a
tree built from the OLD one — which silently reverts everything that landed in
between. The parent pointer would look correct and the commit would succeed;
only the tree would be wrong. NOTHING HAS BEEN COMMITTED.

Re-extract your hunks against the current HEAD and retry:
  git diff ${head_now} -- <your files>   # rebuild the patch on the new base
EOF
        exec 9>&-
        exit 4
    fi
fi

git commit "$@"
rc=$?

# Failed commit (usually the peer-activity hook's deliberate first-attempt
# block) — roll the shared index back to what it held for our paths before we
# staged, so nothing of ours is left loaded in it while we review. Still inside
# the flock, so no peer can observe the half-state. `bsq commit` re-stages on
# the --ack re-run.
if [ "$rc" != "0" ] && [ "${#stage_paths[@]}" -gt 0 ]; then
    if git update-index --force-remove -- "${stage_paths[@]}" 2>/dev/null; then
        if [ -n "$prior_index_entries" ]; then
            printf '%s\n' "$prior_index_entries" | git update-index --index-info 2>/dev/null || true
        fi
    else
        echo "safe-commit: WARNING — could not unstage after a failed commit; your paths are still in the shared index" >&2
    fi
fi

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

# ---------------------------------------------------------------------------
# Shared-index reconciliation, part 2 of 2 (T-0752) — leave the SHARED index
# consistent with the commit we just made.
#
# Only for an ISOLATED-index commit (failure mode 5 above): a pathspec commit
# through the shared index already refreshes its own entries, and a commit OF
# the shared index obviously does. Here git has no reason to touch the shared
# index at all, so it keeps the pre-commit blobs and reads back as the exact
# inverse of what landed.
#
# Scope is exactly the paths THIS commit touched: surviving paths get HEAD's
# blob, paths the commit deleted are removed from the index. Every other index
# entry, and every worktree file, is left alone — so a peer's dirty hunks and
# their staging in other files survive untouched. Renames are disabled so the
# name-status stream is a flat STATUS/PATH pair sequence. Still inside the
# flock, so no peer ever observes the inverse-staged half-state.
# ---------------------------------------------------------------------------
if [ "$rc" = "0" ] && [ "$index_is_shared" = "0" ] && [ -f "$SHARED_INDEX" ]; then
    reco_alive=()
    reco_gone=()
    while IFS= read -r -d '' _st && IFS= read -r -d '' _path; do
        case "$_st" in
            D*) reco_gone+=("$_path") ;;
            *)  reco_alive+=("$_path") ;;
        esac
    done < <(git diff-tree --no-commit-id --name-status -r -z --root --no-renames HEAD)

    reco_failed=0
    if [ "${#reco_alive[@]}" -gt 0 ]; then
        # --index-info ONLY, deliberately. Do NOT follow this with
        # `git update-index --refresh -- <paths>` to re-warm the stat cache:
        # trailing paths are update-index's *primary* argument, not a refresh
        # scope, so that form silently re-stages the WORKTREE blob — i.e. it
        # is `git add` wearing a refresh flag, and in a co-edited file it
        # would stage the peer's dirty hunks we just took care not to commit.
        # (Measured: it replaced HEAD's blob with the worktree's.) Leaving the
        # stat cache cold only costs `git status` one re-hash of these paths.
        git ls-tree -r -z HEAD -- "${reco_alive[@]}" \
            | GIT_INDEX_FILE="$SHARED_INDEX" git update-index -z --index-info \
            || reco_failed=1
    fi
    if [ "${#reco_gone[@]}" -gt 0 ]; then
        printf '%s\0' "${reco_gone[@]}" \
            | GIT_INDEX_FILE="$SHARED_INDEX" git update-index --force-remove -z --stdin \
            || reco_failed=1
    fi

    if [ "$reco_failed" != "0" ]; then
        cat >&2 <<EOF
safe-commit: WARNING (T-0752) — could not reconcile the SHARED index after this
isolated-index commit. It may now hold the INVERSE of what just landed, which a
peer's \`git add <file> && git commit\` would silently revert. Clear it yourself,
scoped to YOUR paths only (never a bare \`git restore --staged .\`):
  git restore --staged -- $(printf '%s ' "${committed_files[@]}")
  git diff --cached --stat        # must be empty for those paths afterwards
EOF
    else
        # Did the repair overwrite a peer's genuinely staged entry (as opposed
        # to a stale one that merely echoed the old HEAD)? Say so and name the
        # blob — the object is still in the odb, so their staging is recoverable.
        clobbered=""
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            p="${line#*$'\t'}"
            blob="$(printf '%s' "${line%%$'\t'*}" | awk '{print $4}')"
            for c in "${committed_files[@]}"; do
                if [ "$c" = "$p" ]; then
                    clobbered="${clobbered}  ${p}  (was staged as blob ${blob})"$'\n'
                    break
                fi
            done
        done <<< "$pre_staged_raw"
        if [ -n "$clobbered" ]; then
            cat >&2 <<EOF

safe-commit: NOTE (T-0752) — the shared index held STAGED content for path(s)
this commit also touched. They have been reset to the new HEAD, because leaving
the index disagreeing with HEAD is what silently reverts landed work. Nothing in
any worktree was modified, and the staged blobs are still in the object store:
${clobbered}Recover one with:
  git cat-file blob <blob> > /tmp/recovered && diff /tmp/recovered <path>
EOF
        fi
    fi
fi

exec 9>&-
exit $rc
