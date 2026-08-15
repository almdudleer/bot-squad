#!/usr/bin/env bash
#
# test_commit_policy.sh — negative control for the two REFUSING pre-commit
# gates added by T-0657 (identity, secret-like paths).
#
# HOW THIS TESTS, AND WHY IT MATTERS THAT IT TESTS THIS WAY
# --------------------------------------------------------
# Every case below runs a REAL `git commit` against the REAL hook file, and then
# asserts on the commit's exit code, on whether a commit object actually
# appeared, and on WHICH refusal text came out. It never calls the check
# functions directly: a gate proven only through its own constructor is proven
# only to be a function that returns 1, not to be wired into git.
#
# THE SANDBOX GUARD IS NOT DECORATION — READ THIS BEFORE EDITING
# --------------------------------------------------------------
# The first version of this harness computed its throwaway repo path with
#     local name="$1" mode="${2:-new}" d="$WORK/$name"
# Bash expands every RHS on a `local` line BEFORE assigning any of them, so
# under `set -u` `$name` was still unbound when `$WORK/$name` was expanded. The
# function died on its first call, `D` came back empty, and every subsequent
# `git -C "$D" add -A && git commit` ran with -C "" — that is, against
# /home/almdudleer/watchrobot/dev itself. It committed a live peer session's
# uncommitted work to the shared branch before anything noticed, and the suite
# still printed PASS lines, because a gate refusing a commit in the WRONG repo
# looks exactly like a gate refusing a commit in the right one.
#
# Hence `sbx`: every git invocation and every file write goes through a path
# that is checked to live under $WORK, and anything else is a hard exit. An
# empty or wrong path is now a loud abort instead of a silent redirection onto
# the shared tree. `sbx_selftest` below proves the guard actually fires, so it
# cannot rot into a decorative wrapper that always says yes.
#
# Run:  .githooks/test_commit_policy.sh          (from anywhere in the clone)

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Per-REPOSITORY facts. This file is one copy running in every clone that
# carries the guards (T-0660), so the identity it commits as and the commits it
# reads its "before" arms from come from `.githooks/repo-facts` and from the
# clone's own allowlist — never from a constant in here. Read
# `.githooks/lib/suite-facts.sh` for why, and for why an unavailable fact SKIPS
# out loud instead of quietly counting as a pass.
# shellcheck source=lib/suite-facts.sh
. "$REPO/.githooks/lib/suite-facts.sh"
sf_load "$REPO"

# The hook as it stood DURING the incident, and the T-0657 library as it shipped
# before T-0658 fixed the allowlist reader. Pinned by sha so the "it used to
# pass" / "it used to be broken" arms keep being real before/afters and do not
# quietly start testing the new code against itself.
PRE_T0657_REF="${SUITE_PRE_GUARD_REF:-}"
PRE_T0658_LIB_REF="${SUITE_PRE_LIB_REF:-}"

# The library as it stood before T-0662 closed the "no policy means no gate"
# branch. Section 6 pins it for the same reason: once the fix is committed the
# claim "the gate used to switch itself off" is only a claim unless the code
# that did it is still being run against the same input. Per repository, like
# the two above — a clone whose history predates the guards has no such commit,
# and that arm SKIPS out loud rather than quietly counting as a pass.
PRE_T0662_LIB_REF="${SUITE_PRE_T0662_LIB_REF:-}"

# An address that belongs to NO clone, used where a case needs the `*` fallback
# to be something the identity under test is not. A constant, not a repo fact:
# the sandboxes are `git init`-fresh and .invalid can never resolve anywhere.
SUITE_ALIEN="nobody@guard-suite.invalid"

# A secret-like path the SUITE owns, appended to every sandbox's exception list
# by new_clone/new_pair below. Cases that need "a path this clone excepts" use
# THIS one rather than borrowing an entry out of the repository's real list:
# which paths a clone excepts is a fact about that clone, and a case leaning on
# one silently tests nothing in a repository that excepts something else — the
# T-0660 failure, one layer down. The repository's real list is still copied in
# alongside it, so the sandbox runs the real policy plus one known entry.
SUITE_PLANTED_EXCEPTION="config/planted.key"


# Overridable so the pair of libraries can be proven outside the shared tree —
# .githooks/ there is live for every peer the moment it is written, before any
# commit (T-0657 rejected a neighbouring session's commits for ~10 minutes).
CP_LIB="${CP_LIB:-$REPO/.githooks/lib/commit-policy.sh}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/t0657-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0; SKIP=0
ok()    { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()   { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
# A section this clone cannot supply the input for. It gets its OWN counter and
# its own line: a case that silently disappeared into the pass count is a check
# that was switched off wearing the clothes of one that ran.
skip()  { SKIP=$((SKIP+1)); printf '  \033[33mSKIP\033[0m  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
# sbx <dir> <git args...>  — git, but only ever inside the sandbox.
# ---------------------------------------------------------------------------
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

# need_sandbox <dir> — abort unless <dir> is a live sandbox repo
need_sandbox() {
    local d
    d="${1-}"
    case "$d" in
        "$WORK"/*) [ -d "$d/.git" ] && return 0 ;;
    esac
    printf '\n\033[31mFATAL\033[0m: sandbox path is not usable: %q\n' "$d" >&2
    exit 99
}

sbx_selftest() {
    local rc
    ( sbx "$REPO" status >/dev/null 2>&1 ) ; rc=$?
    [ "$rc" -eq 99 ] && ok "sandbox guard refuses a git call aimed at the real clone" \
                     || bad "sandbox guard did NOT fire on the real clone (rc=$rc) — harness is unsafe"
    ( sbx "" status >/dev/null 2>&1 ) ; rc=$?
    [ "$rc" -eq 99 ] && ok "sandbox guard refuses an EMPTY path (the exact 2026-08-15 failure)" \
                     || bad "sandbox guard did NOT fire on an empty path (rc=$rc) — harness is unsafe"
}

# ---------------------------------------------------------------------------
# new_clone <name> [old|new]   — sets the global D
#
# Builds a disposable repo whose HEAD already carries .githooks/, so the policy
# files are read from HEAD exactly as they are in a real clone. Assignments are
# deliberately one-per-line; see the header for what sharing a `local` line cost.
# ---------------------------------------------------------------------------
new_clone() {
    local name
    local mode
    local d
    name="$1"
    mode="${2:-new}"
    d="$WORK/$name"

    mkdir -p "$d/.githooks/lib" || exit 99
    if [ "$mode" = "old" ]; then
        # The BEFORE arm needs the code as it stood before the guards. Resolved
        # from this repo's history, else from a donor clone, else NOT AT ALL —
        # and "not at all" must reach the caller as a skip, never as a sandbox
        # holding two empty files, which would make the arm assert nothing while
        # still printing a verdict.
        sf_resolve_pre "$REPO" "$PRE_T0657_REF" ".githooks/pre-commit" \
                       "$d/.githooks/pre-commit" || { D=""; return 1; }
        # …and the library it sources, IF it sources one. A repository whose
        # pre-guard hook is a self-contained file has no lib/peer-activity.sh to
        # fetch, and demanding one there would skip the BEFORE arm in exactly
        # the repository that has the richest before-state to show. Asked of the
        # resolved file itself rather than assumed from this clone's layout.
        if grep -q 'peer-activity\.sh' "$d/.githooks/pre-commit"; then
            sf_resolve_pre "$REPO" "$PRE_T0657_REF" ".githooks/lib/peer-activity.sh" \
                           "$d/.githooks/lib/peer-activity.sh" || { D=""; return 1; }
        fi
    else
        cp "$REPO/.githooks/pre-commit"                   "$d/.githooks/pre-commit"
        # Same reasoning as the BEFORE arm: copy it only if the hook uses it.
        grep -q 'peer-activity\.sh' "$REPO/.githooks/pre-commit" \
            && cp "$REPO/.githooks/lib/peer-activity.sh" "$d/.githooks/lib/peer-activity.sh"
        cp "$CP_LIB"                                      "$d/.githooks/lib/commit-policy.sh"
        cp "$REPO/.githooks/allowed-identities"           "$d/.githooks/allowed-identities"
        cp "$REPO/.githooks/allowed-lookalike-paths" "$d/.githooks/allowed-lookalike-paths"
    printf '%s\n' "$SUITE_PLANTED_EXCEPTION" >> "$d/.githooks/allowed-lookalike-paths"
    fi
    chmod +x "$d/.githooks/pre-commit"

    git -C "$d" init -q
    D="$d"
    need_sandbox "$D"
    sbx "$D" config user.name  "guard-suite"
    sbx "$D" config user.email "$SUITE_IDENTITY"
    sbx "$D" config commit.gpgsign false
    echo "seed" > "$d/seed.txt"
    sbx "$D" add -A
    # The seed commit predates hook installation on purpose: bootstrapping
    # through the gate would mean testing the gate with the gate.
    sbx "$D" commit -qm seed
    sbx "$D" config core.hooksPath .githooks
}

# commit_in <dir> <message> [VAR=val ...] -> $LAST_OUT $LAST_RC $LAST_COUNT
commit_in() {
    local d
    local msg
    d="$1"; shift
    msg="$1"; shift
    need_sandbox "$d"
    LAST_OUT="$(cd "$d" && env "$@" git commit -m "$msg" 2>&1)"
    LAST_RC=$?
    LAST_COUNT="$(sbx "$d" rev-list --count HEAD)"
}

count_in() { need_sandbox "$1"; sbx "$1" rev-list --count HEAD; }

# assert_refused — refusal means ALL THREE: non-zero rc, no new commit object,
# and the refusal naming the right cause. Checking only the rc would let the
# peer-activity warning — which also exits 1 — masquerade as the gate under test.
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
head_ "1. POSITIVE CONTROL — ordinary work is untouched"
# ===========================================================================
# Without this the whole suite is satisfiable by a hook that refuses everything.
new_clone ordinary
mkdir -p "$D/backend" "$D/web/src"
echo "def a(): return 1"   > "$D/backend/thing.py"
echo "export const x = 1"  > "$D/web/src/thing.ts"
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "feat: two ordinary files"
assert_passed "two code files + legitimate identity -> commits" "$BEFORE"
if grep -q "COMMIT REFUSED" <<<"$LAST_OUT"; then
    bad "ordinary commit printed a refusal banner"
else
    ok "ordinary commit prints no refusal banner (behaviour unchanged)"
fi

# ===========================================================================
head_ "2. SECRET-LIKE PATH IN THE INDEX"
# ===========================================================================
new_clone secrets
cat > "$D/.env.bak-ifx-2026-08-07" <<'EOF'
TELEGRAM_BOT_TOKEN=123456:REDACTED-TEST-VALUE
JWT_SECRET=redacted-test-value
EOF
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "init"
assert_refused "staging .env.bak-... -> REFUSED, and the refusal names the secret gate" \
               "COMMIT REFUSED: secret-like" "$BEFORE"
grep -q '\.env\.bak-ifx-2026-08-07' <<<"$LAST_OUT" \
    && ok "refusal names the offending path" \
    || bad "refusal does not name the offending path"

# --- the whole point of a separate key ------------------------------------
commit_in "$D" "init" BOT_SQUAD_ACK_PEERS=1 BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "BOT_SQUAD_ACK_PEERS=1 + BOT_SQUAD_SKIP_PEER_CHECK=1 do NOT release it" \
               "COMMIT REFUSED: secret-like" "$BEFORE"

commit_in "$D" "init" BOT_SQUAD_ALLOW_SECRET_PATHS=1
assert_refused "a bare BOT_SQUAD_ALLOW_SECRET_PATHS=1 does NOT release it (must name the path)" \
               "COMMIT REFUSED: secret-like" "$BEFORE"

# --- self-disarm: permitting yourself in the same commit -------------------
echo ".env.bak-ifx-2026-08-07" >> "$D/.githooks/allowed-lookalike-paths"
sbx "$D" add -A
commit_in "$D" "init"
assert_refused "staging its own exception in the same commit does NOT release it (list read from HEAD)" \
               "COMMIT REFUSED: secret-like" "$BEFORE"

# --- the named release DOES work (or the gate is unusable) -----------------
# BOT_SQUAD_SKIP_PEER_CHECK=1 rides along here and in the deletion case below
# purely to isolate the variable: everything in this sandbox is 0 seconds old,
# so check 3 (peer activity, unchanged) fires on every re-commit of a file and
# would red these cases for a reason that has nothing to do with the gate under
# test. It cannot make them vacuous — the dedicated case above proves that this
# same variable does NOT release the secret gate.
commit_in "$D" "init" BOT_SQUAD_ALLOW_SECRET_PATHS=.env.bak-ifx-2026-08-07 BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "naming the exact path in BOT_SQUAD_ALLOW_SECRET_PATHS releases it" "$BEFORE"

# --- tracked exceptions and deletions must not red -------------------------
new_clone exceptions
mkdir -p "$D/$(dirname "$SUITE_PLANTED_EXCEPTION")"
printf 'public-material\n' > "$D/$SUITE_PLANTED_EXCEPTION"
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "docs: a listed exception"
assert_passed "a path in the HEAD exception list commits ($SUITE_PLANTED_EXCEPTION)" "$BEFORE"

printf 'x\n' > "$D/secrets.txt"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "plant a secret-like file"
sbx "$D" rm -q secrets.txt
BEFORE=$(count_in "$D")
commit_in "$D" "remove the planted file" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "DELETING a secret-like path is not refused" "$BEFORE"

# ===========================================================================
head_ "3. IDENTITY"
# ===========================================================================
new_clone identity
sbx "$D" config user.name t
sbx "$D" config user.email t@t
echo "y" > "$D/Dockerfile"
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "init"
assert_refused "committing as t@t -> REFUSED, and the refusal names the identity gate" \
               "COMMIT REFUSED: unexpected git identity" "$BEFORE"
grep -q "found:.*t@t" <<<"$LAST_OUT" \
    && ok "refusal names the FOUND identity" || bad "refusal does not name the found identity"
grep -q "expected:.*$SUITE_IDENTITY" <<<"$LAST_OUT" \
    && ok "refusal names the EXPECTED identity" || bad "refusal does not name the expected identity"

commit_in "$D" "init" BOT_SQUAD_ACK_PEERS=1
assert_refused "BOT_SQUAD_ACK_PEERS=1 does NOT release the identity gate" \
               "COMMIT REFUSED: unexpected git identity" "$BEFORE"

commit_in "$D" "init" BOT_SQUAD_ALLOW_IDENTITY=t@t
assert_passed "naming the exact address in BOT_SQUAD_ALLOW_IDENTITY releases it" "$BEFORE"

# --- per-clone pinning is REAL, not just the union fallback ----------------
# Without this case the "the list is per clone" claim would be vacuous: every
# case above passes equally against one flat global allowlist.
#
# It needs an address the `*` fallback allows but this clone's pinned line does
# not. A repository whose fallback carries a single address has no such value:
# there, "refused in a clone pinned to X" and "refused everywhere" are the same
# sentence, and a case that cannot tell them apart must say so rather than pass.
if [ -z "$SUITE_FOREIGN_IDENTITY" ]; then
    skip "per-clone pinning: $REPO/.githooks/allowed-identities has one address in its fallback, so no input can distinguish per-clone pinning from a flat allowlist"
else
new_clone perclone
sed -i "1i $D  $SUITE_IDENTITY" "$D/.githooks/allowed-identities"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "pin this clone to its own identity"
BEFORE=$(count_in "$D")
sbx "$D" config user.email "$SUITE_FOREIGN_IDENTITY"
echo "1" > "$D/a.txt"; sbx "$D" add -A
commit_in "$D" "commit as an address legal elsewhere"
assert_refused "an address legal in ANOTHER clone ($SUITE_FOREIGN_IDENTITY) is refused in a clone pinned to $SUITE_IDENTITY" \
               "COMMIT REFUSED: unexpected git identity" "$BEFORE"
sbx "$D" config user.email "$SUITE_IDENTITY"
commit_in "$D" "commit as this clone's own identity"
assert_passed "the pinned clone's own identity commits" "$BEFORE"
fi

# ===========================================================================
head_ "4. THE 2026-08-14 INCIDENT, REPLAYED WHOLE — before vs after"
# ===========================================================================
# git config user.email t@t  +  .env.bak in the tree  +  git add -A  +  commit
replay() {
    local d
    d="$1"
    need_sandbox "$d"
    sbx "$d" config user.name t
    sbx "$d" config user.email t@t
    cat > "$d/.env.bak-ifx-2026-08-07" <<'EOF'
TELEGRAM_BOT_TOKEN=123456:REDACTED-TEST-VALUE
JWT_SECRET=redacted-test-value
SMTP_PASS=redacted-test-value
MONITORING_SECRET=redacted-test-value
EOF
    echo "y" > "$d/Dockerfile"
    mkdir -p "$d/web"; echo "x" > "$d/web/f.ts"
    sbx "$d" add -A
}

if ! new_clone replay_old old; then
    skip "BEFORE arm: this clone has no pre-guard code to run (SUITE_PRE_GUARD_REF='${PRE_T0657_REF:-<unset>}', donor '${SUITE_PRE_DONOR_REPO:-<unset>}') — 'the gate used to be open here' is UNPROVEN, not proven"
else
echo "120 lines of real Dockerfile" > "$D/Dockerfile"
sbx "$D" add -A; sbx "$D" -c core.hooksPath=/dev/null commit -qm "real Dockerfile"
replay "$D"
BEFORE=$(count_in "$D")

# Step 1 — the bare attempt. The old hook DOES exit non-zero here, and it would
# be easy to mistake that for "the old gate stopped this". It did not: it says
# nothing about the identity and nothing about the staged secret, and the only
# thing it asks for is a repeat.
commit_in "$D" "init"
if grep -q "peer-activity check" <<<"$LAST_OUT" \
   && ! grep -q "COMMIT REFUSED"           <<<"$LAST_OUT" \
   && ! grep -q '\.env\.bak-ifx'           <<<"$LAST_OUT" \
   && ! grep -q 't@t'                      <<<"$LAST_OUT"; then
    ok "BEFORE step 1: the old hook never refuses, never names the staged secret, never names t@t"
else
    bad "BEFORE step 1: unexpected output from the pinned old hook"
    printf '%s\n' "$LAST_OUT" | sed 's/^/        /'
fi

# Step 2 — the repeat it asked for. This is the whole defect: the gate's single
# demand is satisfiable by one variable that any automatic actor either already
# has set or supplies on the retry.
commit_in "$D" "init" BOT_SQUAD_ACK_PEERS=1
if [ "$LAST_RC" -eq 0 ] && [ "$LAST_COUNT" = "$((BEFORE + 1))" ]; then
    ok "BEFORE step 2 (hook at ${PRE_T0657_REF:0:8}): the incident commit LANDS — the gate really was open"
    sbx "$D" show --stat --format='  landed as %an <%ae>  %s' HEAD | sed 's/^/        /' | head -9
else
    bad "BEFORE arm did not reproduce: rc=$LAST_RC — the 'it used to pass' claim is unproven"
    printf '%s\n' "$LAST_OUT" | sed 's/^/        /'
fi
fi

new_clone replay_new
echo "120 lines of real Dockerfile" > "$D/Dockerfile"
sbx "$D" add -A; sbx "$D" -c core.hooksPath=/dev/null commit -qm "real Dockerfile"
replay "$D"
BEFORE=$(count_in "$D")
commit_in "$D" "init"
assert_refused "AFTER: the same incident commit is REFUSED" "COMMIT REFUSED" "$BEFORE"
# ...and the repeat that used to be enough is no longer enough.
commit_in "$D" "init" BOT_SQUAD_ACK_PEERS=1
assert_refused "AFTER: the retry with BOT_SQUAD_ACK_PEERS=1 — which USED to land it — is still REFUSED" \
               "COMMIT REFUSED" "$BEFORE"
[ "$(sbx "$D" show -s --format=%s HEAD)" != "init" ] \
    && ok "AFTER: no 'init' commit object exists" \
    || bad "AFTER: an 'init' commit object was created"
[ "$(sbx "$D" show HEAD:Dockerfile)" = "120 lines of real Dockerfile" ] \
    && ok "AFTER: history still holds the real Dockerfile; the 'y' truncation stayed in the working tree" \
    || bad "AFTER: the truncated Dockerfile reached history"

# ===========================================================================
head_ "5. A POLICY LIST WITH NO TRAILING NEWLINE (T-0658)"
# ===========================================================================
# `while read -r … done < <(producer)` DROPS the producer's final line when the
# output does not end in a newline: read fills the variables and then returns
# non-zero, so the loop body never runs for it. Every list here is one whose
# LAST line is the load-bearing one — allowed-identities ends with the `*`
# fallback, the only line an unusual clone path (a worktree, a throwaway, /tmp)
# can match, and an exception list ends with whatever was added most recently.
#
# So each case below must (a) put the line under test LAST, (b) strip the
# trailing newline, and (c) use a clone path that matches ONLY that line —
# otherwise an earlier line answers and the case proves nothing.
#
# And each has a companion assertion that something is still REFUSED, because
# the failure mode here is NOT "everything is rejected". Measured 2026-08-15 on
# the shipped T-0657 library: losing the `*` line makes the lookup return the
# empty string, the empty string takes the "no allowlist found, gate INACTIVE"
# branch, and the gate turns ITSELF OFF while every commit still succeeds. A
# case that only checked "an ordinary commit passes" would go green on exactly
# that, which is how the behaviour came to look verified while being broken.

strip_final_newline() {
    local f
    local tmp
    f="${1-}"
    tmp="$(mktemp)"
    printf '%s' "$(cat "$f")" > "$tmp"
    mv "$tmp" "$f"
}

# --- 5a. IDENTITIES: the `*` fallback is the last line --------------------
new_clone nl_identity
strip_final_newline "$D/.githooks/allowed-identities"
[ -n "$(tail -c1 "$D/.githooks/allowed-identities")" ] \
    && ok "setup: allowed-identities genuinely ends without a newline" \
    || bad "setup: the file still ends in a newline — case 5a would be vacuous"
tail -1 "$D/.githooks/allowed-identities" | grep -q '^\*' \
    && ok "setup: the last line IS the '*' fallback (the one a sandbox path matches)" \
    || bad "setup: the last line is not the '*' fallback — case 5a tests the wrong line"
sbx "$D" add -A
# --no-verify, because the list is read from HEAD: it has to be committed to
# have any effect, and committing it through the gate would be circular.
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: list without a trailing newline"
BEFORE=$(count_in "$D")
echo "1" > "$D/ok.txt"; sbx "$D" add -A
commit_in "$D" "ordinary work" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "a legitimate identity still commits with a newline-less allowlist" "$BEFORE"
grep -q "gate INACTIVE" <<<"$LAST_OUT" \
    && bad "the gate reported itself INACTIVE — the last line was lost and the guard is OFF" \
    || ok "the gate did NOT go inactive — the '*' line survived the missing newline"

# the non-vacuous half: the gate must still be ARMED in this same clone
sbx "$D" config user.email t@t
echo "2" > "$D/bad.txt"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "as t@t"
assert_refused "…and t@t is STILL refused there — the guard is armed, not merely quiet" \
               "COMMIT REFUSED: unexpected git identity" "$BEFORE"
sbx "$D" config user.email "$SUITE_IDENTITY"

# --- 5b. the same input against the PRE-T-0658 library --------------------
# Without this the fix has no before/after and "it used to be broken" is a claim.
new_clone nl_identity_old
if ! sf_resolve_pre "$REPO" "$PRE_T0658_LIB_REF" ".githooks/lib/commit-policy.sh" \
                    "$D/.githooks/lib/commit-policy.sh"; then
    skip "BEFORE arm 5b: this clone has no pre-T-0658 library to run (SUITE_PRE_LIB_REF='${PRE_T0658_LIB_REF:-<unset>}', donor '${SUITE_PRE_DONOR_REPO:-<unset>}') — 5a stands on its own non-vacuity check, not on a before/after"
else
strip_final_newline "$D/.githooks/allowed-identities"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: list without a trailing newline"
sbx "$D" config user.email t@t
echo "2" > "$D/bad.txt"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "as t@t"
if [ "$LAST_RC" -eq 0 ] || grep -q "gate INACTIVE" <<<"$LAST_OUT"; then
    ok "BEFORE (lib at $PRE_T0658_LIB_REF): the same file turns the identity gate OFF — t@t is not refused"
else
    bad "BEFORE arm did not reproduce: the pre-T-0658 library refused t@t anyway, so 5a proves no fix"
    printf '%s\n' "$LAST_OUT" | sed 's/^/        /'
fi
fi

# --- 5c. EXCEPTION PATHS: the path under test is the last line ------------
new_clone nl_paths
printf '# exceptions\n.env.example\nconfig/deploy.key' > "$D/.githooks/allowed-lookalike-paths"
[ -n "$(tail -c1 "$D/.githooks/allowed-lookalike-paths")" ] \
    && ok "setup: allowed-lookalike-paths ends without a newline, on config/deploy.key" \
    || bad "setup: the file still ends in a newline — case 5c would be vacuous"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: exception list without a trailing newline"
mkdir -p "$D/config"
printf 'public-material\n' > "$D/config/deploy.key"
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "add the listed exception" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "the LAST line of a newline-less exception list is still honoured" "$BEFORE"

printf 'x\n' > "$D/config/other.key"
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "add an UNLISTED secret-like path" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "…and an UNLISTED path in the same clone still reds — the list was read, not skipped" \
               "COMMIT REFUSED: secret-like" "$BEFORE"

# ===========================================================================
head_ "6. THE GUARD'S OWN INPUTS (T-0662)"
# ===========================================================================
# Sections 1-5 attack the PAYLOAD: a bad identity, a secret-like path, a list
# saved without a trailing newline. This section attacks the GUARD, because a
# guard has two ways to be green — working, and switched OFF — and only one of
# them is worth anything. T-0657 shipped with the second: losing the last line
# of allowed-identities took the lookup to the empty string, the empty string
# took the "no allowlist found, gate INACTIVE" branch, and t@t committed freely
# while the suite stayed green. T-0658 fixed that ONE input. This section sweeps
# the rest of them, and every verdict is measured by whether HEAD MOVED — never
# by the text of the output, never by an exit code read through a pipe.
#
# The table this section pins, input by input, with the verdict BEFORE T-0662:
#
#   identity list absent from HEAD .............. REFUSE   (was PASS — gate off)
#   identity list empty / comments only ......... REFUSE   (was PASS — gate off)
#   identity list is a DIRECTORY in HEAD ........ REFUSE   (was PASS — gate off)
#   identity list re-created in the WORKTREE .... REFUSE   (was PASS — obeyed it)
#   identity list with CRLF ..................... unchanged verdicts (already so)
#   identity list, comments + blank lines ....... stripped (already so)
#   identity list unreadable in the worktree .... PASS — HEAD is the source
#   EXCEPTION list with CRLF .................... entries honoured (was: silently
#                                                 ignored, and the refusal told
#                                                 you to add what was listed)
#   EXCEPTION list absent / empty ............... zero exceptions, gate ARMED
#   .githooks/ absent entirely .................. PASS — no hook runs at all
#
# Why REFUSE is right for one list and "zero exceptions" for the other: they are
# opposite kinds of file. allowed-lookalike-paths names what may pass, so an
# empty one is the STRICTEST reading and fail-closed by construction.
# allowed-identities IS the policy, so an empty one is NO policy — and a gate
# with no policy must not answer "approved".

# fresh_change <dir> — stage a file nothing else has touched, so the next
# attempt can never be vacuous. Without it, an attempt that follows a commit
# which unexpectedly SUCCEEDED finds an empty index, and "nothing to commit"
# wears the same rc and the same unmoved HEAD as a refusal.
FRESH_N=0
fresh_change() {
    need_sandbox "$1"
    FRESH_N=$((FRESH_N + 1))
    printf '%s\n' "$FRESH_N" > "$1/fresh_$FRESH_N.txt"
    sbx "$1" add "fresh_$FRESH_N.txt"
}

# ---------------------------------------------------------------------------
# 6a. THE DEFECT AS FOUND: delete the list, then commit as t@t.
#
# Two commits, no override variable, no --no-verify. The first is legitimate on
# its face — at the moment it is checked the list is still in HEAD, so the gate
# is armed and it passes. It is the STATE it leaves behind that is the defect.
# ---------------------------------------------------------------------------
new_clone own_missing
BEFORE=$(count_in "$D")
sbx "$D" rm -q .githooks/allowed-identities
commit_in "$D" "policy: drop the identity allowlist" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "the commit that DELETES the allowlist passes (list still in HEAD when it is checked)" "$BEFORE"

sbx "$D" config user.name t
sbx "$D" config user.email t@t
fresh_change "$D"
BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "with the allowlist gone from HEAD, t@t is REFUSED (it used to commit)" \
               "COMMIT REFUSED" "$BEFORE"
if grep -q "COMMIT REFUSED" <<<"$LAST_OUT" && grep -q "allowed-identities" <<<"$LAST_OUT"; then
    ok "…and the refusal names the MISSING LIST, so the reader fixes the right thing"
else
    bad "…but the refusal does not name the missing list"
fi
# Matched on the old branch's own marker line, not on the words "gate
# INACTIVE": the replacement refusal quotes that phrase while explaining what
# it replaced, and a test that greps for the phrase would red on the fix.
grep -q "identity check: no allowlist found" <<<"$LAST_OUT" \
    && bad "the old 'gate INACTIVE' branch still fired — that branch is the T-0662 defect and must be gone" \
    || ok "the old 'gate INACTIVE' branch did not fire"

# The non-vacuous half: a LEGITIMATE identity is refused too, and for the same
# stated reason. A gate with no policy cannot approve anybody — passing the
# legitimate address here would be asserting something it cannot check.
sbx "$D" config user.name "guard-suite"
sbx "$D" config user.email "$SUITE_IDENTITY"
fresh_change "$D"
BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "a LEGITIMATE identity is refused too while the list is missing — no policy, no approval" \
               "COMMIT REFUSED" "$BEFORE"

# The habitual variables must not release it, exactly as for the other two gates.
fresh_change "$D"; BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_ACK_PEERS=1 BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "BOT_SQUAD_ACK_PEERS=1 does NOT release a missing allowlist" \
               "COMMIT REFUSED" "$BEFORE"
fresh_change "$D"; BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_ALLOW_IDENTITY="$SUITE_IDENTITY" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "BOT_SQUAD_ALLOW_IDENTITY does NOT release it — that key answers a different question" \
               "COMMIT REFUSED" "$BEFORE"
fresh_change "$D"; BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST=1 BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "a bare BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST=1 does NOT release it (must name the path)" \
               "COMMIT REFUSED" "$BEFORE"

# …and the named release DOES work, or the gate is unusable: this is the key the
# commit that first INSTALLS the list into an existing repo has to carry.
fresh_change "$D"; BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST=.githooks/allowed-identities BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "naming the exact path in BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST releases it" "$BEFORE"

# --- the same input against the PRE-T-0662 library -------------------------
# Without this arm, "one commit used to disarm the identity gate" is a claim
# about code nothing runs any more. With it, the defect stays reproducible.
new_clone own_missing_old
if ! sf_resolve_pre "$REPO" "$PRE_T0662_LIB_REF" ".githooks/lib/commit-policy.sh" \
                    "$D/.githooks/lib/commit-policy.sh"; then
    skip "BEFORE arm 6a: this clone has no pre-T-0662 library to run (SUITE_PRE_T0662_LIB_REF='${PRE_T0662_LIB_REF:-<unset>}', donor '${SUITE_PRE_DONOR_REPO:-<unset>}') — the cases above stand on their own, not on a before/after"
else
sbx "$D" rm -q .githooks/allowed-identities
commit_in "$D" "policy: drop the identity allowlist" BOT_SQUAD_SKIP_PEER_CHECK=1
sbx "$D" config user.name t
sbx "$D" config user.email t@t
fresh_change "$D"
BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_SKIP_PEER_CHECK=1
if [ "$LAST_RC" -eq 0 ] && [ "$LAST_COUNT" = "$((BEFORE + 1))" ]; then
    ok "BEFORE (lib at $PRE_T0662_LIB_REF): the same input lands a t@t commit — the gate really did switch itself off"
    sbx "$D" show -s --format='  landed as %an <%ae>  %s' HEAD | sed 's/^/        /'
else
    bad "BEFORE arm did not reproduce: rc=$LAST_RC — the 'it used to be open' claim is unproven"
    printf '%s\n' "$LAST_OUT" | sed 's/^/        /'
fi
fi

# ---------------------------------------------------------------------------
# 6b. THE RE-CREATE HOLE. Deleting the list from HEAD and writing your own copy
# into the WORKING TREE must not hand the gate your policy. This is the same
# self-disarm the read-from-HEAD rule exists to stop, and the bootstrap fallback
# is exactly where it can come back in.
# ---------------------------------------------------------------------------
new_clone own_recreate
sbx "$D" rm -q .githooks/allowed-identities
commit_in "$D" "policy: drop the identity allowlist" BOT_SQUAD_SKIP_PEER_CHECK=1
printf '*\tt@t\n' > "$D/.githooks/allowed-identities"
sbx "$D" config user.name t
sbx "$D" config user.email t@t
echo "y" > "$D/Dockerfile"
sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "an allowlist re-created in the WORKTREE does not permit its own author" \
               "COMMIT REFUSED" "$BEFORE"

# ---------------------------------------------------------------------------
# 6c. EMPTY and COMMENTS-ONLY lists. Both strip to nothing, which is no policy.
# ---------------------------------------------------------------------------
new_clone own_empty
: > "$D/.githooks/allowed-identities"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: empty allowlist"
sbx "$D" config user.email t@t
echo "y" > "$D/Dockerfile"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "a ZERO-BYTE allowlist is no policy -> REFUSED" "COMMIT REFUSED" "$BEFORE"

new_clone own_comments_only
printf '# only comments here\n\n#   and a blank line\n' > "$D/.githooks/allowed-identities"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: comments-only allowlist"
sbx "$D" config user.email t@t
echo "y" > "$D/Dockerfile"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "a COMMENTS-ONLY allowlist strips to nothing — same as empty -> REFUSED" \
               "COMMIT REFUSED" "$BEFORE"

# ---------------------------------------------------------------------------
# 6d. COMMENTS AND BLANK LINES around a real policy are stripped, not fatal.
# The companion arm keeps it non-vacuous: the gate must still be ARMED.
# ---------------------------------------------------------------------------
new_clone own_comments_ok
{ printf '# header comment\n\n'
  printf '%s\t%s\n' "$D" "$SUITE_IDENTITY"
  printf '\n#  trailing comment\n'
  printf '*\t%s\n' "$SUITE_ALIEN"; } > "$D/.githooks/allowed-identities"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: comments and blank lines"
sbx "$D" config user.email "$SUITE_IDENTITY"
echo "1" > "$D/ok.txt"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "ordinary work" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "comments and blank lines are stripped; the clone's pinned line still answers" "$BEFORE"
sbx "$D" config user.email t@t
echo "2" > "$D/bad.txt"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "as t@t" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "…and t@t is still refused in that same clone — armed, not merely quiet" \
               "COMMIT REFUSED: unexpected git identity" "$BEFORE"

# ---------------------------------------------------------------------------
# 6e. CRLF ON THE IDENTITY LIST — measured to be already correct, pinned so it
# stays that way. The trailing \r survives into the address field, and what
# saves it is that the per-address trim is `s/[[:space:]]*$//`, whose class
# includes CR. That is one sed away from being a defect, which is why the case
# is here rather than left implicit.
# ---------------------------------------------------------------------------
new_clone own_crlf
{ printf '# header\r\n'
  printf '%s\t%s\r\n' "$D" "$SUITE_IDENTITY"
  printf '*\t%s\r\n' "$SUITE_ALIEN"; } > "$D/.githooks/allowed-identities"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: CRLF allowlist"
grep -q $'\r' "$D/.githooks/allowed-identities" \
    && ok "setup: the allowlist genuinely carries CRLF" \
    || bad "setup: no CRLF in the file — case 6e would be vacuous"
sbx "$D" config user.email "$SUITE_IDENTITY"
echo "1" > "$D/ok.txt"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "ordinary work" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "a CRLF allowlist still admits the legitimate identity" "$BEFORE"
sbx "$D" config user.email t@t
echo "2" > "$D/bad.txt"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "as t@t" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "…and still refuses t@t — CRLF changes no verdict in either direction" \
               "COMMIT REFUSED: unexpected git identity" "$BEFORE"

# ---------------------------------------------------------------------------
# 6f. CRLF ON THE EXCEPTION LIST — measured BROKEN before T-0662.
#
# There is no per-entry trim on this side: the comparison is `grep -qxF`, which
# is exact, so every entry silently stopped matching and every excepted path was
# refused. The expensive part is not the refusal, it is the TEXT of it — it told
# the reader to "add it to the list and commit THAT first" for a path that was
# already listed, which sends them to fix a file that is not broken.
# ---------------------------------------------------------------------------
new_clone own_crlf_paths
printf '# exceptions\r\nconfig/planted.key\r\n' > "$D/.githooks/allowed-lookalike-paths"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: CRLF exception list"
grep -q $'\r' "$D/.githooks/allowed-lookalike-paths" \
    && ok "setup: the exception list genuinely carries CRLF" \
    || bad "setup: no CRLF in the file — case 6f would be vacuous"
mkdir -p "$D/config"; printf 'public-material\n' > "$D/config/planted.key"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "add the listed exception" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "a CRLF exception list still honours its entries" "$BEFORE"
printf 'x\n' > "$D/other.key"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "add an UNLISTED secret-like path" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "…and an UNLISTED path in that same clone still reds — the list was read, not skipped" \
               "COMMIT REFUSED: secret-like" "$BEFORE"

# ---------------------------------------------------------------------------
# 6g. THE PATH IS A DIRECTORY IN HEAD. `git cat-file -e HEAD:<path>` says yes
# for a TREE as happily as for a blob, and `git show` on a tree prints a
# directory listing — which parses as policy lines that match nothing. Before
# T-0662 that produced an empty lookup, and an empty lookup was the INACTIVE
# branch: swapping the file for a directory switched the gate off.
# ---------------------------------------------------------------------------
new_clone own_isdir
rm -f "$D/.githooks/allowed-identities"
mkdir -p "$D/.githooks/allowed-identities"
printf 'not a policy\n' > "$D/.githooks/allowed-identities/inner.txt"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: allowlist replaced by a directory"
[ "$(sbx "$D" cat-file -t HEAD:.githooks/allowed-identities)" = "tree" ] \
    && ok "setup: HEAD:.githooks/allowed-identities really is a tree, not a blob" \
    || bad "setup: the path is not a tree — case 6g would be vacuous"
sbx "$D" config user.email t@t
echo "y" > "$D/Dockerfile"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "init" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "an allowlist that is a DIRECTORY is not a policy -> REFUSED" \
               "COMMIT REFUSED" "$BEFORE"

# ---------------------------------------------------------------------------
# 6h. UNREADABLE IN THE WORKING TREE (chmod 000). This one must PASS, and the
# reason is the whole design: the gate reads HEAD, so the worktree copy's
# permissions — and its contents — are not an input at all. Pinning it stops a
# later "hardening" change from quietly making the worktree authoritative again.
#
# Staging is by explicit path here: `git add -A` would try to re-index the
# mode-changed policy file, fail on the unreadable copy, and red the case for a
# reason that has nothing to do with the gate.
# ---------------------------------------------------------------------------
new_clone own_chmod
chmod 000 "$D/.githooks/allowed-identities"
sbx "$D" config user.email "$SUITE_IDENTITY"
echo "1" > "$D/ok.txt"; sbx "$D" add ok.txt
BEFORE=$(count_in "$D")
commit_in "$D" "ordinary work" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "an UNREADABLE worktree copy changes nothing — HEAD is the source" "$BEFORE"
sbx "$D" config user.email t@t
echo "2" > "$D/bad.txt"; sbx "$D" add bad.txt
BEFORE=$(count_in "$D")
commit_in "$D" "as t@t" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "…and t@t is still refused through the unreadable copy" \
               "COMMIT REFUSED: unexpected git identity" "$BEFORE"
chmod 644 "$D/.githooks/allowed-identities"

# ---------------------------------------------------------------------------
# 6i. THE EXCEPTION LIST IS THE OPPOSITE CASE. It names what may pass, so absent
# or empty means ZERO exceptions — the strictest reading, and correct. Refusing
# on its absence would be wrong, so both arms are here.
# ---------------------------------------------------------------------------
# The excepted path is PLANTED by the case rather than borrowed from this
# repository's own list: which paths a clone excepts is a fact about that clone,
# and a case that leans on one silently tests nothing in a repository that
# excepts something else (T-0660).
new_clone own_exceptions_absent
printf '# exceptions\nconfig/planted.key\n' > "$D/.githooks/allowed-lookalike-paths"
mkdir -p "$D/config"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: an exception this case owns"
printf 'public-material\n' > "$D/config/planted.key"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "add the planted exception" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "setup: the planted exception is honoured while the list exists" "$BEFORE"

sbx "$D" rm -q .githooks/allowed-lookalike-paths
commit_in "$D" "policy: drop the exception list" BOT_SQUAD_SKIP_PEER_CHECK=1
echo "1" > "$D/ok.txt"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "ordinary work" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_passed "with NO exception list, an ordinary file still commits" "$BEFORE"
printf 'more-public-material\n' > "$D/config/planted.key"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "re-touch the formerly-excepted path" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "…and the SAME path, excepted a moment ago, is refused — no list means no exceptions" \
               "COMMIT REFUSED: secret-like" "$BEFORE"

new_clone own_exceptions_empty
: > "$D/.githooks/allowed-lookalike-paths"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: empty exception list"
mkdir -p "$D/config"; printf 'x\n' > "$D/config/planted.key"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "add a formerly-excepted path" BOT_SQUAD_SKIP_PEER_CHECK=1
assert_refused "an EMPTY exception list means zero exceptions, not all-permitted" \
               "COMMIT REFUSED: secret-like" "$BEFORE"

# ---------------------------------------------------------------------------
# 6j. NO .githooks/ AT ALL. Named here so the table is honest rather than
# flattering: git runs no hook it cannot find, so nothing in this library can
# refuse anything. Same family as core.hooksPath being unset and `--no-verify`,
# already recorded as NOT closed by pre-commit (T-0657); what covers it is
# pre-push (T-0658) and, before that, install-hooks.sh.
# ---------------------------------------------------------------------------
new_clone own_nohooks
rm -rf "$D/.githooks"
sbx "$D" config user.email t@t
echo "y" > "$D/Dockerfile"; sbx "$D" add -A
BEFORE=$(count_in "$D")
commit_in "$D" "init"
assert_passed "with .githooks/ deleted no hook runs at all — pre-commit cannot cover this (pre-push does)" "$BEFORE"

# ===========================================================================
printf '\n\033[1m%s\033[0m\n' "RESULT: $PASS passed, $FAIL failed, $SKIP skipped"
printf '        clone %s\n' "$REPO"
printf '        identity under test: %s%s\n' "$SUITE_IDENTITY" \
       "${SUITE_FOREIGN_IDENTITY:+  (foreign: $SUITE_FOREIGN_IDENTITY)}"
printf '        repo-facts: %s\n' \
       "$( [ "$SUITE_FACTS_FOUND" = 1 ] && echo "$SUITE_FACTS_FILE" || echo "ABSENT ($SUITE_FACTS_FILE)" )"
[ "$SKIP" -gt 0 ] && printf '        \033[33m%s section(s) had no input in this clone and were NOT checked.\033[0m\n' "$SKIP"
[ "$FAIL" -eq 0 ] || exit 1
