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
