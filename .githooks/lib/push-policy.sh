#!/usr/bin/env bash
#
# push-policy.sh — the REFUSING gate of this clone's pre-push hook. (T-0658)
# Sourced by `.githooks/pre-push`.
#
# WHY A SECOND GATE EXISTS AT ALL
# -------------------------------
# T-0657 put two refusing gates in `pre-commit`. They close exactly the path the
# 2026-08-14 incident took and nothing else, because `pre-commit` is only run by
# `git commit`. Measured on throwaway clones, a secret reaches history through
# every one of these without the hook being invoked once:
#
#   git commit --no-verify                  git cherry-pick
#   git -c core.hooksPath=… commit          git merge
#   git commit-tree + git update-ref        git rebase
#   a clone with core.hooksPath unset       git am
#
# The cherry-pick line is the one that matters here: **a prod release is a
# cherry-pick onto `master` by construction.** So a secret committed on a branch
# rides to `master` having never met a commit-time gate — and `pre-commit`
# cannot ever close that, because it is not called.
#
# `pre-push` is. It is the only hook that sees the whole set of commits
# regardless of the verb that produced them, and it sits immediately before the
# irreversible event: publication to a remote.
#
# THE RANGE, NOT THE TIP — AND NOT THE ENDPOINT DIFF EITHER
# ---------------------------------------------------------
# This gate walks EVERY commit being pushed and looks at what each one
# introduces. Two cheaper things were considered and are both blind:
#
#   * the tip's tree — a secret added and then removed one commit later is not
#     in the tip at all, and still travels to the remote in full;
#   * `git diff <remote-sha>..<local-sha>` — an ENDPOINT diff cancels exactly
#     the same case out. This is not hypothetical: in this clone on 2026-08-15,
#     `b465362f` adds `.env.bak-ifx-2026-08-07` and `41e0b726` deletes it, both
#     unpushed. The endpoint diff over the pushable range reports ZERO hits; the
#     per-commit walk reports the one that matters.
#
# ADDITIONS ONLY, PER COMMIT
# --------------------------
# Deletions are ignored, for the same reason `pre-commit` ignores them: removing
# a secret-like path is the thing we want people to do. The commit that ADDED it
# is in the same range and is what reds.
#
# Merges are read as a COMBINED diff — only what the merge itself introduces
# relative to ALL of its parents (an "evil merge"). Diffing a merge against each
# parent in turn would report every path the merged branch ever added, which
# would red the ordinary `merge(release): master → dev` shape that publishes
# this project, and DoD 1 of T-0658 is that legitimate releases keep working.
# Nothing is lost: the commits the merge brings in are themselves in the range
# and are scanned individually, unless they are already on the remote — in which
# case they are already published and this gate is not the instrument for them.

# ---------------------------------------------------------------------------
# Presentation fallbacks (see commit-policy.sh — same reasoning).
# ---------------------------------------------------------------------------
if ! declare -F pa_bold >/dev/null 2>&1; then
    pa_bold()   { printf '%s' "$*"; }
    pa_yellow() { printf '%s' "$*"; }
fi
if ! declare -F pa_red >/dev/null 2>&1; then
    pa_red() { if [ -t 2 ]; then printf '\033[31m%s\033[0m' "$*"; else printf '%s' "$*"; fi; }
fi

PP_SECRET_EXCEPTIONS=".githooks/allowed-lookalike-paths"

# ---------------------------------------------------------------------------
# pp__is_zero <sha> — git's all-zero sentinel (ref creation / ref deletion).
# Length-agnostic so it holds under sha256 too. An EMPTY string is NOT zero:
# an unset variable must never read as "nothing to push".
# ---------------------------------------------------------------------------
pp__is_zero() {
    local s
    s="${1-}"
    [ -n "$s" ] || return 1
    case "$s" in
        *[!0]*) return 1 ;;
        *)      return 0 ;;
    esac
}

# ---------------------------------------------------------------------------
# pp__rev_args <local_sha> <remote_sha> <remote>  -> sets PP_REV_ARGS
#
# "Everything reachable from what I am pushing that this remote does not
# already have." `<remote_sha>` is authoritative and fresh — git obtained it
# from the actual connection for THIS ref during THIS push, so it cannot be
# stale the way a remote-tracking ref can. `--remotes=<remote>` is added on top
# so commits already published on some OTHER branch of the same remote are not
# re-scanned; it is an extra exclusion only, and a remote with no tracking refs
# yet simply excludes nothing.
# ---------------------------------------------------------------------------
pp__rev_args() {
    local local_sha
    local remote_sha
    local remote
    local_sha="${1-}"
    remote_sha="${2-}"
    remote="${3-}"

    PP_REV_ARGS=("$local_sha" --not)
    pp__is_zero "$remote_sha" || PP_REV_ARGS+=("$remote_sha")
    [ -n "$remote" ] && PP_REV_ARGS+=("--remotes=$remote")
    return 0
}

# ---------------------------------------------------------------------------
# pp__policy_commit <remote_sha>  -> a commit-ish to read the exception list from
#
# NOT the tip being pushed. `pre-commit` reads its lists from HEAD so that one
# `git add -A` cannot sweep in both a secret and the line permitting it; the
# push-side equivalent of "already committed and reviewed" is "already on the
# remote", because for a push, HEAD *is* the attacker's copy — the tip being
# published can carry its own exception. So the list is read from the state the
# remote already has, and only falls back to HEAD when the remote side has no
# such file at all (a remote that predates T-0657, or a brand-new ref).
#
# Cost of this choice, stated plainly: introducing a NEW legitimate exception
# takes two pushes — the list first, the file second. That is the property, not
# a bug.
# ---------------------------------------------------------------------------
pp__policy_commit() {
    local remote_sha
    local boundary
    remote_sha="${1-}"

    if ! pp__is_zero "$remote_sha" && git cat-file -e "${remote_sha}^{commit}" 2>/dev/null; then
        printf '%s' "$remote_sha"
        return 0
    fi
    # New ref on the remote: the boundary of the range is the newest commit the
    # remote already has (via some other branch). `-` prefixes boundary commits.
    boundary=$(git rev-list --boundary "${PP_REV_ARGS[@]}" 2>/dev/null \
                 | sed -n 's/^-//p' | head -1)
    if [ -n "$boundary" ]; then
        printf '%s' "$boundary"
        return 0
    fi
    printf '%s' "HEAD"
}

# ---------------------------------------------------------------------------
# pp__exceptions <policy commit>  -> the exception list, comments stripped
#
# ONE source, and it is <policy commit> — nothing else. There is no fallback to
# HEAD and no fallback to the working tree, and removing those two was the whole
# of T-0662 on this side.
#
# WHAT THE FALLBACKS COST, MEASURED 2026-08-15 on a throwaway remote by whether
# the REMOTE REF MOVED and whether the blob arrived:
#
#   push 1  a commit REMOVING .githooks/allowed-lookalike-paths — passes, and
#           legitimately so: at that moment it can only make the gate stricter.
#   push 2  write your own exception list into the WORKING TREE (never commit
#           it), commit a secret-like path, push. The chain fell through the
#           remote side, through HEAD, and landed on YOUR uncommitted file.
#           rc=0, the remote ref moved, and `.env.bak-probe` arrived as a blob.
#
# Two pushes, no override key, no --no-verify. The control arm — same secret,
# list intact — was refused with the remote ref unmoved, so the gate was armed
# and the fallback was what opened it.
#
# The HEAD fallback is the same defect one step shorter: for a push, HEAD IS the
# copy under suspicion, so consulting it after the remote side comes up empty
# lets push 2 commit its own permission instead of merely writing it.
#
# The genuine bootstrap survives without either: `pp__policy_commit` returns the
# literal string "HEAD" only when the remote has NO refs at all — nothing
# published, nothing to protect — so reading HEAD there is this function reading
# its argument, not falling back to it. Once the remote has any history, an
# exception must be present in the commit that remote already holds, which is
# the one copy the pusher cannot have edited.
#
# `cat-file -t … = blob`, not `-e`: `-e` answers yes for a TREE as happily as
# for a blob, and `git show` on a tree prints a directory listing that parses as
# exception lines matching nothing. Fail-closed either way here — an unusable
# list means zero exceptions — but the check says what it means.
# ---------------------------------------------------------------------------
pp__exceptions() {
    local at
    at="${1-HEAD}"

    if [ "$(git cat-file -t "$at:$PP_SECRET_EXCEPTIONS" 2>/dev/null)" = "blob" ]; then
        git show "$at:$PP_SECRET_EXCEPTIONS" 2>/dev/null | cp__strip
    fi
    return 0
}

# ---------------------------------------------------------------------------
# pp__introduced  -> lines "<sha> <path>" for everything the range ADDS.
#
# Two single-process walks; a per-commit shell loop over this repo's history
# costs 25s, these cost 0.2s together, and a gate slow enough to be disabled is
# not a gate.
#   pass 1  non-merge commits, additions/modifications only
#   pass 2  merge commits, COMBINED diff (what the merge itself introduces).
#           `--diff-filter` does not apply to a combined diff, so a path the
#           merge DELETES would show up here too; it is filtered out by asking
#           whether the path exists in the merge's own tree.
# ---------------------------------------------------------------------------
# A commit line is tagged with a byte that cannot occur in a git path, rather
# than being recognised by "looks like 40 hex chars" — a path CAN look like that.
PP_MARK=$'\001'

pp__introduced() {
    local sha=""
    local line

    git log --no-merges --root --no-renames --diff-filter=ACMR \
            --name-only --format="%x01%H" "${PP_REV_ARGS[@]}" 2>/dev/null \
    | while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            "$PP_MARK"*) sha="${line#"$PP_MARK"}" ;;
            *)           printf '%s %s\n' "$sha" "$line" ;;
        esac
    done

    git log --merges -c --name-only --format="%x01%H" "${PP_REV_ARGS[@]}" 2>/dev/null \
    | while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            "$PP_MARK"*) sha="${line#"$PP_MARK"}"; continue ;;
        esac
        # only paths the merge introduces or changes, never ones it removes
        git cat-file -e "$sha:$line" 2>/dev/null && printf '%s %s\n' "$sha" "$line"
    done
}

# ---------------------------------------------------------------------------
# pp_check_push <remote> <local_ref> <local_sha> <remote_ref> <remote_sha>
#   -> 0 clean / allowed, 1 refused (message already on stderr)
# ---------------------------------------------------------------------------
pp_check_push() {
    local remote
    local local_ref
    local local_sha
    local remote_ref
    local remote_sha
    remote="${1-}"
    local_ref="${2-}"
    local_sha="${3-}"
    remote_ref="${4-}"
    remote_sha="${5-}"

    # Deleting a remote ref pushes no objects.
    pp__is_zero "$local_sha" && return 0
    [ -n "$local_sha" ] || return 0

    pp__rev_args "$local_sha" "$remote_sha" "$remote"

    local policy_at
    local exceptions
    local allowed_env
    local base_hint
    policy_at=$(pp__policy_commit "$remote_sha")
    base_hint="$policy_at"
    [ "$base_hint" = "HEAD" ] && base_hint="<base>"
    exceptions=$(pp__exceptions "$policy_at")
    allowed_env="${BOT_SQUAD_ALLOW_SECRET_PUSH:-}"

    local hits=()
    local released=()
    local sha
    local path
    while read -r sha path; do
        [ -n "$path" ] || continue
        cp__is_secretlike "$path" || continue
        if printf '%s\n' "$exceptions" | grep -qxF -- "$path"; then
            continue
        fi
        if printf '%s\n' "$allowed_env" | tr ',' '\n' \
             | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -qxF -- "$path"; then
            released+=("${sha:0:8} $path")
            continue
        fi
        hits+=("${sha:0:8} $path")
    done < <(pp__introduced | sort -u)

    if [ "${#released[@]}" -gt 0 ]; then
        printf '%s\n' "── push check: released by BOT_SQUAD_ALLOW_SECRET_PUSH ──" >&2
        printf '     %s\n' "${released[@]}" >&2
    fi
    [ "${#hits[@]}" -eq 0 ] && return 0

    local uniq_paths
    uniq_paths=$(printf '%s\n' "${hits[@]}" | awk '{ $1=""; sub(/^ /,""); print }' | sort -u | paste -sd, -)

    cat >&2 <<EOF

$(pa_red "── PUSH REFUSED: a pushed commit introduces a secret-like path ──")

  remote:    $remote
  ref:       ${local_ref:-?}  ->  ${remote_ref:-?}
  range:     $(printf '%s ' "${PP_REV_ARGS[@]}")

$(printf '  %s\n' "${hits[@]}")

These commits are in the set this push would publish. Note the commit shas: the
offending path does NOT have to be present in the tip — a path added in one
commit and deleted in the next is invisible to the tip and to an endpoint diff,
and still travels to the remote in full.

Publication is the irreversible step. After a force-push the commit stays
reachable BY SHA until garbage collection, and on a remote you do not control
it may be fetched, mirrored or indexed before then.

WHY THIS FIRES WHEN pre-commit DID NOT: pre-commit is only run by 'git commit'.
cherry-pick, merge, rebase, am, --no-verify and a clone with core.hooksPath
unset never invoke it — and a prod release is a cherry-pick by construction.

What to do — rewrite the range so the path is not in ANY commit:
  git rebase -i $base_hint
  # or, to drop one path from every commit in the range:
  git filter-branch --index-filter 'git rm --cached --ignore-unmatch <path>' ...

If a path is genuinely publishable and recurring (a template, a public cert),
add it to $PP_SECRET_EXCEPTIONS and PUSH THAT FIRST — the list is
read from the state the remote already has ($policy_at), never from the tip
being pushed, so a tip that carries its own permission does not get it.

To let exactly these through once, name every path (not a bare 1):
  BOT_SQUAD_ALLOW_SECRET_PUSH='$uniq_paths' git push ...

This key is deliberately NOT BOT_SQUAD_ACK_PEERS and NOT the pre-commit key
BOT_SQUAD_ALLOW_SECRET_PATHS: the two gates have different radii — one clears a
single commit, this one clears an entire published range.

EOF
    return 1
}
