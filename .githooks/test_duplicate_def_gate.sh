#!/usr/bin/env bash
#
# test_duplicate_def_gate.sh — positive AND negative control for GATE 3
# (duplicate top-level def/class on scripts/cli/bsq, T-1017).
#
# Same harness discipline as test_commit_policy.sh, and for the same reason:
# every case here runs a REAL `git commit` against the REAL hook file, the
# REAL library, and the REAL census script, then asserts on the commit's exit
# code, on whether a commit object actually appeared, and on WHAT the refusal
# names. It never calls `cp_check_duplicate_defs` or the census module
# directly — a gate proven only through its own constructor is proven only to
# be a function that returns 1, not to be wired into git. See
# test_commit_policy.sh's header for why the sandbox guard below is not
# decoration; the guard itself is the ONE copy in lib/suite-facts.sh, shared
# with that suite so it cannot drift out of sync the way two hand-copies did
# before T-0660.
#
# WHY A SEPARATE FILE rather than adding sections to test_commit_policy.sh:
# that file is 46KB and already covers two unrelated gates end to end; a third
# gate's cases interleaved into it would make an unrelated future edit to
# either gate a collision risk in a file every session in this shared clone
# can commit to. Keeping this gate's suite in its own file is the same
# modularity test_commit_policy.sh / test_push_policy.sh already demonstrate
# for identity+secrets vs. push policy.
#
# Run:  .githooks/test_duplicate_def_gate.sh          (from anywhere in the clone)

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=lib/suite-facts.sh
. "$REPO/.githooks/lib/suite-facts.sh"
sf_load "$REPO"

# Overridable so a candidate library/script can be proven outside the shared
# tree — .githooks/ and scripts/lint/ are both live for every peer the moment
# they are written, before any commit.
CP_LIB="${CP_LIB:-$REPO/.githooks/lib/commit-policy.sh}"
CENSUS_SCRIPT="${CENSUS_SCRIPT:-$REPO/scripts/lint/duplicate_def_census.py}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/t1017-dupdef-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0; SKIP=0
ok()    { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()   { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
skip()  { SKIP=$((SKIP+1)); printf '  \033[33mSKIP\033[0m  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
# new_clone <name> — a disposable repo whose HEAD carries .githooks/ + the
# census script, so both are read from HEAD/the index exactly as in a real
# clone. Gates 1/2's policy files (allowed-identities, allowed-lookalike-paths)
# are copied from the REAL repo too, and the commit identity is derived
# through the gate's own reader (sf_load) — this suite exercises GATE 3 only,
# and a case tripping gate 1 or 2 by accident would be a false read on gate 3.
# ---------------------------------------------------------------------------
new_clone() {
    local name d
    name="$1"
    d="$WORK/$name"

    mkdir -p "$d/.githooks/lib" "$d/scripts/lint" "$d/scripts/cli" || exit 99
    cp "$REPO/.githooks/pre-commit"              "$d/.githooks/pre-commit"
    cp "$CP_LIB"                                 "$d/.githooks/lib/commit-policy.sh"
    cp "$CENSUS_SCRIPT"                          "$d/scripts/lint/duplicate_def_census.py"
    cp "$REPO/.githooks/allowed-identities"      "$d/.githooks/allowed-identities"
    cp "$REPO/.githooks/allowed-lookalike-paths" "$d/.githooks/allowed-lookalike-paths"
    chmod +x "$d/.githooks/pre-commit"

    git -C "$d" init -q
    D="$d"
    need_sandbox "$D"
    sbx "$D" config user.name  "guard-suite"
    sbx "$D" config user.email "$SUITE_IDENTITY"
    sbx "$D" config commit.gpgsign false

    # A tiny but syntactically real "bsq" — two unique top-level defs, no
    # duplicates. Real enough for the census to read as Python, small enough
    # that every case below can state its own diff instead of parsing 9K lines.
    cat > "$d/scripts/cli/bsq" <<'BSQ'
#!/usr/bin/env python3
def main():
    return 0


def helper():
    return 1
BSQ
    echo "seed" > "$d/seed.txt"
    sbx "$D" add -A
    # The seed commit predates hook installation on purpose: bootstrapping
    # through the gate would mean testing the gate with the gate.
    sbx "$D" commit -qm seed
    sbx "$D" config core.hooksPath .githooks
}

# commit_in <dir> <message> [VAR=val ...] -> $LAST_OUT $LAST_RC $LAST_COUNT
commit_in() {
    local d msg
    d="$1"; shift
    msg="$1"; shift
    need_sandbox "$d"
    LAST_OUT="$(cd "$d" && env "$@" git commit -m "$msg" 2>&1)"
    LAST_RC=$?
    LAST_COUNT="$(sbx "$d" rev-list --count HEAD)"
}

count_in() { need_sandbox "$1"; sbx "$1" rev-list --count HEAD; }

# assert_refused — same three-way check as test_commit_policy.sh: nonzero rc,
# no new commit object, AND the refusal naming the right cause. Checking only
# the rc would let gate 1/2 (or the unchanged peer-activity warning, which also
# exits 1) masquerade as gate 3.
assert_refused() {
    local label="$1" pat="$2" before="$3" why=""
    [ "$LAST_RC" -ne 0 ]              || why="$why rc=0(expected nonzero)"
    [ "$LAST_COUNT" = "$before" ]     || why="$why commit-was-created"
    grep -qi -- "$pat" <<<"$LAST_OUT" || why="$why no-match:/$pat/"
    if [ -z "$why" ]; then ok "$label"; else bad "$label —$why"; printf '%s\n' "$LAST_OUT" | sed 's/^/        /'; fi
}

assert_passed() {
    local label="$1" before="$2" why=""
    [ "$LAST_RC" -eq 0 ]                  || why="$why rc=$LAST_RC(expected 0)"
    [ "$LAST_COUNT" = "$((before + 1))" ] || why="$why no-commit-created"
    if [ -z "$why" ]; then ok "$label"; else bad "$label —$why"; printf '%s\n' "$LAST_OUT" | sed 's/^/        /'; fi
}

# ===========================================================================
head_ "0. THE HARNESS'S OWN GUARD"
# ===========================================================================
sbx_selftest

# ===========================================================================
head_ "1. POSITIVE CONTROL — a change that does not touch bsq is untouched"
# ===========================================================================
new_clone ordinary
echo "unrelated" > "$D/other.txt"
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "chore: unrelated file"
assert_passed "unrelated file, bsq untouched -> commits" "$BEFORE"
if grep -qi "duplicate" <<<"$LAST_OUT"; then
    bad "gate 3 produced output for a commit that never staged bsq"
else
    ok "gate 3 is silent when bsq is not staged"
fi

# ===========================================================================
head_ "2. POSITIVE CONTROL — a CLEAN edit to bsq itself passes"
# ===========================================================================
new_clone clean_edit
cat >> "$D/scripts/cli/bsq" <<'BSQ'


def another_helper():
    return 2
BSQ
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "feat: add another_helper" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "a third unique top-level def -> commits" "$BEFORE"

# ===========================================================================
head_ "3. THE GUARD CAN FAIL — a genuine duplicate top-level def is REFUSED"
# ===========================================================================
new_clone duplicate
cat >> "$D/scripts/cli/bsq" <<'BSQ'


def helper():
    return 99
BSQ
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "feat: (accidentally) redefine helper"
assert_refused "duplicate top-level def 'helper' -> REFUSED" \
               "COMMIT REFUSED: duplicate top-level def" "$BEFORE"
grep -q "DUPLICATE def 'helper'" <<<"$LAST_OUT" \
    && ok "refusal NAMES the duplicated identifier (not just a nonzero exit)" \
    || bad "refusal does not name 'helper' — sees a one and leaks"

# --- and the SAME defect, once fixed, is no longer refused. Renamed (not
# reverted to HEAD's exact bytes) so there is a genuine diff to commit — a
# revert identical to HEAD would leave nothing staged and prove nothing about
# the gate.
cat > "$D/scripts/cli/bsq" <<'BSQ'
#!/usr/bin/env python3
def main():
    return 0


def helper():
    return 1


def helper_v2():
    return 99
BSQ
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "fix: rename the second helper instead of redefining it" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "renaming away the duplicate -> the fix commits" "$BEFORE"

# ===========================================================================
head_ "4. THE NESTED-CASE CONTROL (operator's ticket: the half a naive census skips)"
# ===========================================================================
# A def NESTED inside another def, sharing a name with a real top-level def,
# must NOT be treated as a duplicate — this is the ast.walk-vs-tree.body
# traversal bug named in T-1017's own Context.
new_clone nested
cat >> "$D/scripts/cli/bsq" <<'BSQ'


def wrapper():
    def helper():
        return "shadowed, not a duplicate"
    return helper()
BSQ
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "feat: nested helper() shares a name with the top-level one" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "nested def sharing a top-level name -> commits (not a false positive)" "$BEFORE"

# ===========================================================================
head_ "5. THE COMMENT/DOCSTRING TRAP (operator's ticket, second named hazard)"
# ===========================================================================
# Text that QUOTES a duplicate-looking def twice, inside a comment or a
# docstring, must not trip the detector — ast reads syntax, not text.
new_clone commented_trap
cat >> "$D/scripts/cli/bsq" <<'BSQ'


# Example of a mistake NOT to make:
#   def helper():
#       return 1
#   def helper():
#       return 2
BSQ
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "docs: comment quoting the exact defect this gate exists for" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "a comment quoting 'def helper()' twice -> commits (text, not syntax)" "$BEFORE"

# ===========================================================================
head_ "6. NAMED RELEASE — same discipline as gates 1/2"
# ===========================================================================
new_clone release
cat >> "$D/scripts/cli/bsq" <<'BSQ'


def helper():
    return 99
BSQ
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "feat: intentional (for this test) redefinition"
assert_refused "duplicate -> refused before any release" \
               "COMMIT REFUSED: duplicate top-level def" "$BEFORE"

commit_in "$D" "feat: intentional (for this test) redefinition" \
    BOT_SQUAD_ALLOW_DUP_DEFS="scripts/cli/other-file.py"
assert_refused "a release naming the WRONG path does NOT disarm it" \
               "COMMIT REFUSED: duplicate top-level def" "$BEFORE"

commit_in "$D" "feat: intentional (for this test) redefinition" \
    BOT_SQUAD_ALLOW_DUP_DEFS="scripts/cli/bsq" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "a release NAMING scripts/cli/bsq lets this exact commit through" "$BEFORE"
grep -qi "released by BOT_SQUAD_ALLOW_DUP_DEFS" <<<"$LAST_OUT" \
    && ok "release is announced on stderr, not silent" \
    || bad "release did not announce itself"

# ===========================================================================
head_ "7. DELETING bsq is not refused (same posture as the secret-path gate)"
# ===========================================================================
new_clone deletion
git -C "$D" rm -q scripts/cli/bsq
BEFORE=$(count_in "$D")
commit_in "$D" "chore: remove bsq entirely" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "deleting scripts/cli/bsq -> commits (nothing left to census)" "$BEFORE"

# ===========================================================================
head_ "RESULT"
# ===========================================================================
printf '\n%d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
