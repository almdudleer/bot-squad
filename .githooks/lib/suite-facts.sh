#!/usr/bin/env bash
#
# suite-facts.sh — the per-REPOSITORY facts the two guard suites need, so that
# ONE copy of those suites runs in every clone that carries the guards. (T-0660)
#
# WHY THIS EXISTS
# ---------------
# T-0657/T-0658 shipped the guards into signal-tracker, and both suites were
# written against that repository's own facts: its git identity, and two commits
# of its history used as the "before" arm. Measured 2026-08-15 on a throwaway
# clone of my-avi with the guards ported and nothing else changed:
#
#   test_commit_policy.sh   17 passed, 17 FAILED
#   test_push_policy.sh     53 passed,  1 FAILED
#
# Every one of those failures was the suite asserting a signal-tracker fact, not
# the gate misbehaving. Copying the suites and hand-editing each copy is exactly
# the "two diverging sets of gates" T-0660 was opened to avoid, so the facts come
# out of the code and into this file instead.
#
# WHAT IS DERIVED AND WHAT IS CONFIGURED
# --------------------------------------
# The identities are DERIVED, not configured — from `.githooks/allowed-identities`
# itself, through the very function the gate uses (`cp__allowed_emails_for`). A
# suite that read its fixture identity from a second, hand-maintained place would
# drift out of agreement with the list it is testing, and the drift would show up
# as "the whole suite reds in this clone", which is exactly what it did.
#
# Only what genuinely cannot be derived is configured, in `.githooks/repo-facts`:
# the commit-ishes carrying the PRE-guard versions of the hook and library, and
# the real incident commit. Those are historical facts of one repository. A
# repository that has none — my-avi, where the guards are arriving for the first
# time — leaves them empty, and the sections that need them SKIP.
#
# A SKIP IS NOT A PASS
# --------------------
# `skip` has its own counter and its own line, and the RESULT line prints it
# separately. A guard suite has two ways to be green — to check and to be
# switched off — and a section that silently vanished into the pass count is the
# second one wearing the first one's clothes.

# ---------------------------------------------------------------------------
# sf_load <repo>  — sets SUITE_IDENTITY, SUITE_FOREIGN_IDENTITY and everything
# named in repo-facts. Call once, after $REPO is known.
# ---------------------------------------------------------------------------
sf_load() {
    local repo="${1:?repo}"
    local facts

    # --- configured facts -------------------------------------------------
    # Overridable so a candidate set can be proven outside the clone it will be
    # installed in — .githooks/ is live for every session the moment it is
    # written, before any commit (T-0657).
    facts="${T0660_FACTS:-$repo/.githooks/repo-facts}"
    SUITE_PRE_GUARD_REF=""      # commit-ish carrying .githooks/pre-commit as it stood BEFORE the guards
    SUITE_PRE_LIB_REF=""        # commit-ish carrying lib/commit-policy.sh BEFORE the T-0658 reader fix
    SUITE_PRE_T0662_LIB_REF=""  # commit-ish carrying lib/commit-policy.sh BEFORE T-0662 closed the "no policy, no gate" branch
    SUITE_PRE_DONOR_REPO=""     # another clone to read those two from, when this repo has no such history
    SUITE_LIVE_BRANCH=""        # a branch of THIS repo whose real history is the live input
    SUITE_LIVE_SECRET_COMMIT="" # a real commit of THIS repo that introduces a secret-like path
    SUITE_LIVE_SECRET_PATH=""   # the path that commit introduces
    # Paths that are secret-like, still IN the published tip, and deliberately
    # NOT in this repository's exception list — a decision, not an oversight.
    # Section 6a would otherwise red on them forever, and a permanently red
    # suite gets switched off. Declaring one is a REVIEWED act: it shows up in
    # the diff of repo-facts, and it does NOTHING without the companion _WHY, so
    # the reason sits next to the permission instead of in someone's head.
    SUITE_KNOWN_UNLISTED=""
    SUITE_KNOWN_UNLISTED_WHY=""
    SUITE_FACTS_FILE="$facts"
    if [ -f "$facts" ]; then
        # shellcheck disable=SC1090
        . "$facts"
        SUITE_FACTS_FOUND=1
    else
        SUITE_FACTS_FOUND=0
    fi

    # --- derived facts ----------------------------------------------------
    # Read through the gate's OWN allowlist reader, in the target repo, so the
    # fixture identity cannot disagree with the list under test.
    SUITE_IDENTITY="$(sf__emails_for "$repo" "")"
    SUITE_IDENTITY="${SUITE_IDENTITY%%,*}"
    SUITE_IDENTITY="$(printf '%s' "$SUITE_IDENTITY" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

    # An address that the `*` fallback allows but this clone's own pinned line
    # does not. Section 3's per-clone-pinning case needs one: without it, "the
    # list is per clone" is satisfied equally by one flat global allowlist. A
    # repository whose fallback holds a single address has no such value, and
    # the case skips rather than testing something weaker under the same name.
    SUITE_FOREIGN_IDENTITY=""
    local e
    local fallback
    fallback="$(sf__emails_for "$repo" "/nonexistent-path-that-matches-only-the-fallback")"
    # printf '%s\n', NOT the bare producer: `cp__allowed_emails_for` ends its
    # output with `printf '%s'`, so the last field arrives without a newline and
    # `read` fills the variable and THEN returns non-zero — the loop body never
    # runs for it. Measured here on 2026-08-15: the fallback resolved correctly
    # to two addresses and this loop still came back empty, which silently turned
    # the per-clone-pinning case into a SKIP in a clone that could run it. This
    # is the same defect T-0658 fixed one file over, arriving in new code because
    # the idiom, not the file, is what carries it.
    while IFS= read -r e; do
        [ -n "$e" ] || continue
        [ "$e" = "$SUITE_IDENTITY" ] && continue
        SUITE_FOREIGN_IDENTITY="$e"
        break
    done < <(printf '%s\n' "$fallback" | tr ',' '\n' \
             | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

    # Derivation self-check. "No second address" and "the reader lost the second
    # address" produce the same empty string and the same quiet SKIP, and only
    # one of them is a fact about the repository. So the contradiction is made
    # fatal here rather than left to be read as a property of the clone.
    case "$fallback" in
        *,*)
            if [ -z "$SUITE_FOREIGN_IDENTITY" ]; then
                printf '\nFATAL: suite-facts: the fallback lists more than one address (%s)\n' "$fallback" >&2
                printf 'but no foreign identity was derived — the reader dropped a field.\n' >&2
                printf 'Fix suite-facts.sh; do NOT let this read as "this clone has one address".\n' >&2
                exit 98
            fi ;;
    esac
}

# sf__emails_for <repo> <toplevel override>  -> the allowed address list
# An empty override means "ask about the repo itself".
# The list is read HERE, through the gate's own reader, and handed to the
# lookup — because since T-0662 `cp__allowed_emails_for` takes the already-read
# list as its second argument. That change exists so the GATE can tell "absent"
# from "empty" from "no line for this clone" and refuse instead of going quiet,
# and it must not be undone by giving the function an optional read of its own:
# a second, silent path to the policy is the defect this whole file is about.
# Calling it with one argument here would have handed the lookup an EMPTY list,
# resolved SUITE_IDENTITY to the empty string, and degraded every section that
# uses it — quietly, which is worse than reding.
sf__emails_for() {
    local repo="$1" as="$2"
    (
        cd "$repo" 2>/dev/null || exit 0
        # shellcheck disable=SC1091
        . "$repo/.githooks/lib/commit-policy.sh" 2>/dev/null || exit 0
        [ -n "$as" ] || as="$(git rev-parse --show-toplevel 2>/dev/null)"
        local raw
        local list
        raw="$(cp__policy_read "$CP_IDENTITY_ALLOWLIST")"
        list="$(printf '%s\n' "$raw" | cp__strip)"
        cp__allowed_emails_for "$as" "$list"
    )
}

# ---------------------------------------------------------------------------
# sf_have <fact name>...  -> 0 if every named fact is non-empty
# ---------------------------------------------------------------------------
sf_have() {
    local n v
    for n in "$@"; do
        eval "v=\${$n:-}"
        [ -n "$v" ] || return 1
    done
    return 0
}

# ---------------------------------------------------------------------------
# sf_resolve_pre <repo> <ref var> <path in tree> <dest file>
#   -> 0 written, 1 unavailable (caller must skip, never pass)
#
# Resolution order, each step a fact and not a guess:
#   1. the ref, in THIS repo's history            (signal-tracker: it is there)
#   2. the ref, in SUITE_PRE_DONOR_REPO           (a clone that does carry it)
#   3. unavailable                                 -> the caller SKIPS
# ---------------------------------------------------------------------------
sf_resolve_pre() {
    local repo="$1" ref="$2" path="$3" dest="$4"
    [ -n "$ref" ] || return 1
    if git -C "$repo" cat-file -e "$ref:$path" 2>/dev/null; then
        git -C "$repo" show "$ref:$path" > "$dest" 2>/dev/null && return 0
    fi
    if [ -n "${SUITE_PRE_DONOR_REPO:-}" ] \
       && git -C "$SUITE_PRE_DONOR_REPO" cat-file -e "$ref:$path" 2>/dev/null; then
        git -C "$SUITE_PRE_DONOR_REPO" show "$ref:$path" > "$dest" 2>/dev/null && return 0
    fi
    return 1
}

# ===========================================================================
# THE SHARED-TREE LEAK GUARD
# ===========================================================================
#
# ONE copy, for every harness that carries these guards. It used to be two, and
# they had already drifted before anyone looked: `need_sandbox` in the commit
# suite accepted only a directory `.git`, the push suite's also accepted a
# `.git` FILE and a bare repo's `objects/`; `need_under_work` existed in one
# suite only; and the selftest had two arms in one and four in the other. Two
# copies of the guard that catches a run being redirected into a live tree drift
# exactly like everything else does — and the one that drifts is the one nobody
# has watched fire. This file is what both suites already source, so it is where
# the single copy belongs.
#
# WHAT IT IS FOR. On 2026-08-15 a helper written as
#     local n="$1" src="$2" inst="$3" d="$W/$n"
# died on its first call: bash expands every right-hand side on a `local` line
# BEFORE assigning any of them, so under `set -u` `$n` was still unbound. The
# path came back EMPTY, and every `git -C ""` and `cd ""` after it ran against
# the shared watchrobot clone — unsetting core.hooksPath for a moment and
# writing `user.name=t` / `user.email=t@t` into its config. What stopped it
# becoming a commit was the identity gate, not the harness.
#
# AND THE PART WORTH REMEMBERING: this guard already existed that day, with
# these same self-tests, since T-0657. It was not missing — it simply was not
# used, because the run was "just a quick probe" and a quick probe did not feel
# worth a harness. That is the whole lesson. A one-off probe against a live tree
# is exactly the run that has no reviewer, no second pair of eyes and no test
# around it, which makes it the run that most needs the guard, not the least.
#
# `$WORK` is read at CALL time, so a suite may set it after sourcing this file;
# it must be set before the first call.

# sbx <dir> <git args…>  — git, but only ever inside the sandbox.
sbx() {
    local d
    d="${1-}"
    shift
    case "$d" in
        "$WORK"/*) ;;
        *) printf '\n\033[31mFATAL\033[0m: refusing git outside the sandbox: %q\n' "$d" >&2
           printf 'sandbox is %q — this is the shared-tree leak guard, do not remove it.\n' "$WORK" >&2
           exit 99 ;;
    esac
    git -C "$d" "$@"
}

# need_sandbox <dir> — abort unless <dir> is a live sandbox repo. Accepts a
# `.git` directory, a `.git` FILE (a worktree) and a bare repo's `objects/`:
# the push suite needs the last two for its remotes, and a guard that is stricter
# in one harness than another is two guards.
need_sandbox() {
    local d
    d="${1-}"
    case "$d" in
        "$WORK"/*) [ -e "$d/.git" ] || [ -d "$d/objects" ] && return 0 ;;
    esac
    printf '\n\033[31mFATAL\033[0m: sandbox path is not usable: %q\n' "$d" >&2
    exit 99
}

# need_under_work <dir> — abort unless a WRITE target is inside the sandbox.
# Separate from need_sandbox because a directory that does not exist yet is a
# legitimate write target and not yet a repo.
need_under_work() {
    local d
    d="${1-}"
    case "$d" in
        "$WORK"/*) return 0 ;;
    esac
    printf '\n\033[31mFATAL\033[0m: refusing to write outside the sandbox: %q\n' "$d" >&2
    exit 99
}

# sbx_selftest — prove the guard fires, BEFORE anything relies on it.
#
# It must be the first thing a suite runs. A guard checked after it has already
# been used is not a guard, it is a report on damage. Needs `ok`/`bad` from the
# calling suite; both define them before section 0.
sbx_selftest() {
    local rc
    ( sbx "$REPO" status >/dev/null 2>&1 ) ; rc=$?
    [ "$rc" -eq 99 ] && ok "sandbox guard refuses a git call aimed at the real clone" \
                     || bad "sandbox guard did NOT fire on the real clone (rc=$rc) — harness is unsafe"
    ( sbx "" status >/dev/null 2>&1 ) ; rc=$?
    [ "$rc" -eq 99 ] && ok "sandbox guard refuses an EMPTY path (the exact 2026-08-15 failure)" \
                     || bad "sandbox guard did NOT fire on an empty path (rc=$rc) — harness is unsafe"
    ( need_sandbox "$REPO" >/dev/null 2>&1 ) ; rc=$?
    [ "$rc" -eq 99 ] && ok "need_sandbox refuses the real clone too" \
                     || bad "need_sandbox did NOT fire on the real clone (rc=$rc)"
    ( need_under_work "$REPO/.githooks" >/dev/null 2>&1 ) ; rc=$?
    [ "$rc" -eq 99 ] && ok "need_under_work refuses writing into the real clone's .githooks" \
                     || bad "need_under_work did NOT fire on the real .githooks (rc=$rc) — an edit there is live for every peer"
}
