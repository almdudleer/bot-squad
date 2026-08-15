#!/usr/bin/env bash
#
# check-guard-parity.sh — are two clones running the SAME gate? (T-0660)
#
# WHY THIS EXISTS
# ---------------
# The guards are code, and they now live in two repositories: signal-tracker,
# where T-0657/T-0658 built them, and my-avi, which had no git gate of any kind
# — measured 2026-08-15: no `.githooks/` directory, `core.hooksPath` unset, and
# an incident on 2026-08-14 that touched BOTH clones 13 seconds apart.
#
# Copying the files is the only way to gate a second repository — `core.hooksPath`
# can point out of the tree, but the two policy lists are read from HEAD by
# design, so the guarded repository has to carry them, and a hooks directory that
# lives in some other clone silently stops existing when that clone moves. The
# cost of copying is the thing T-0660 named: two sets that drift, which is how
# one defect came to exist in two deploy recipes at once.
#
# Drift cannot be prevented by asking people to be careful. It CAN be made
# visible in one command, which is this file. It checks THREE kinds of file,
# and the split is the whole design:
#
#   CODE      the gates and their suites — must be BYTE-IDENTICAL.
#   WRAPPERS  the hook files git actually runs — checked by PROPERTY, not bytes.
#   POLICY    the identity and exception lists — must DIFFER, never compared.
#
# WHY WRAPPERS ARE NOT COMPARED BYTE FOR BYTE (T-0659)
# ----------------------------------------------------
# `pre-commit` and `pre-push` are per-repository by nature: bot-squad's
# pre-commit carries its own peer-activity report inline, naming its own tools
# (`bsq commit --ack`, `ops/bot-squad-bin/safe-commit`), and its pre-push runs
# that repository's own lint gates. Demanding byte equality there could only be
# satisfied by deleting true text from one of the repositories, and it would
# leave this script permanently red — and a permanently red instrument stops
# being read within a week. An instrument that lies towards alarm ends the same
# way as one that lies towards calm.
#
# So the wrappers are checked for the property that actually matters, which is
# also stronger than equality: does the hook SOURCE the gate library, does it
# CALL the gate functions, and — for `pre-commit`, where this is the T-0657
# lesson itself — does it call them BEFORE any early `exit 0`? Byte equality
# catches a difference in text. This catches a gate that is present in the file
# and placed after the return, which is the failure that actually happened.
#
#   ./.githooks/check-guard-parity.sh <other clone>
#   ./.githooks/check-guard-parity.sh <clone A> <clone B>
#   ./.githooks/check-guard-parity.sh --selftest
#
# Exit 0 identical, 1 drifted, 2 misuse. Reads only; changes nothing.

set -uo pipefail

# Byte-identical across every clone. Adding a file to the gate means adding it
# here, or it drifts unwatched — which is the failure this script exists for.
CODE=(
    "install-hooks.sh"
    "lib/commit-policy.sh"
    "lib/push-policy.sh"
    "lib/suite-facts.sh"
    "test_commit_policy.sh"
    "test_push_policy.sh"
    "check-guard-parity.sh"
)

# The hook files git runs. Checked by PROPERTY (see the header), never by bytes.
# Format:  <file>|<library it must source>|<functions it must call>|ordered|unordered
#
# `ordered` means every call must come BEFORE the first early `exit 0`. It is
# asserted for pre-commit and NOT for pre-push, and the asymmetry is measured,
# not stylistic: pre-push legitimately exits 0 early when stdin carries no ref
# lines — there is nothing to push and nothing to check — whereas every early
# exit in pre-commit is an answer about PEERS, and letting one short-circuit the
# identity and secret gates is exactly the 2026-08-14 hole.
WRAPPERS=(
    "pre-commit|lib/commit-policy.sh|cp_check_identity cp_check_secret_paths|ordered"
    "pre-push|lib/push-policy.sh|pp_check_push|unordered"
)

# Per repository ON PURPOSE. Listed so the split is stated rather than implied
# by absence, and so a reader can see that "these differ" is the design.
POLICY=(
    "allowed-identities"
    "allowed-lookalike-paths"
    "repo-facts"
    # The peer-activity report is the WRAPPER's own business, not the gate's.
    # watchrobot factors it into a library; bot-squad keeps it inline in the
    # hook. Comparing it would red a difference that is by design.
    "lib/peer-activity.sh"
    # README-git-guards.md is prose about one repository's incident and its
    # clones. Named here rather than left out silently, so that "the README is
    # not compared" is a decision on the page instead of an omission somebody
    # later reads as an oversight and half-fixes.
    "README-git-guards.md"
)

red()  { if [ -t 1 ]; then printf '\033[31m%s\033[0m' "$*"; else printf '%s' "$*"; fi; }
grn()  { if [ -t 1 ]; then printf '\033[32m%s\033[0m' "$*"; else printf '%s' "$*"; fi; }

sha_of() { [ -f "$1" ] && sha256sum "$1" 2>/dev/null | cut -c1-16 || printf 'ABSENT'; }

# ---------------------------------------------------------------------------
# THE WRAPPER PROPERTY CHECK
#
# Comment handling is deliberately blunt: a line whose first non-blank character
# is `#` is not code. That catches the case worth catching — a gate call left in
# the file behind a `#` — and it does not pretend to parse shell. A call hidden
# in a trailing comment on a code line would be counted as a call; if that ever
# matters, the fix is a real parser, not a cleverer regex.
# ---------------------------------------------------------------------------

# wp__first <file> <ERE>  -> line number of the first NON-COMMENT line that
# matches, or "" if none.
#
# awk, not `grep -n | grep`, and the reason is a bug this very check shipped
# with for one run: `grep -n` prefixes each line with "<n>:", so an anchor like
# `(^|[[:space:]])` could never match a command sitting at column 1 — the
# character before it was the colon. Every wrapper came back BROKEN, and because
# every negative arm of the selftest expects BROKEN, all four of them passed. It
# was the CONTROL arm — an honest wrapper must read PARITY — that caught it.
# That is the whole argument for keeping a control next to negative arms.
#
# The patterns use bracket expressions ([.] rather than \.) on purpose: awk
# processes escape sequences in a -v assignment before the string ever becomes a
# regex, so a backslash here would mean something different from what it reads.
wp__first() {
    awk -v re="$2" '
        /^[[:space:]]*#/ { next }
        $0 ~ re          { print NR; exit }
    ' "$1" 2>/dev/null
}

# wp_check <clone> <spec> [side label]  -> 0 ok, 1 broken (reasons printed)
wp_check() {
    local root="$1" spec="$2" side="${3:-}"
    local file lib funcs mode path fn n exit_n lib_re bad=""
    IFS='|' read -r file lib funcs mode <<<"$spec"
    path="$root/.githooks/$file"

    [ -f "$path" ] || { printf '  %s  %s%-26s %s\n' "$(red BROKEN)" "$side" "$file" "absent"; return 1; }

    # sources the gate library, matched on its exact tail path, so that
    # `lib/commit-policy-old.sh` or a copy under another directory does not
    # satisfy it. Dots in the path are made literal.
    lib_re="${lib//./[.]}"
    if [ -z "$(wp__first "$path" "(^|[[:space:]])([.]|source)[[:space:]]+[^#]*/${lib_re}([[:space:]]|\"|$)")" ]; then
        bad="$bad does-not-source-$lib"
    fi

    exit_n="$(wp__first "$path" "(^|[;&|[:space:]])exit[[:space:]]+0([[:space:]]|;|[)]|$)")"
    for fn in $funcs; do
        n="$(wp__first "$path" "(^|[[:space:];&|(])${fn}([[:space:];&|)]|$)")"
        if [ -z "$n" ]; then
            bad="$bad never-calls-$fn"
        elif [ "$mode" = "ordered" ] && [ -n "$exit_n" ] && [ "$n" -gt "$exit_n" ]; then
            bad="$bad calls-$fn-at-line-$n-AFTER-exit-0-at-line-$exit_n"
        fi
    done

    if [ -n "$bad" ]; then
        printf '  %s  %s%-26s%s\n' "$(red BROKEN)" "$side" "$file" "$bad"
        return 1
    fi
    printf '  %s  %s%-26s sources %s, calls %s%s\n' "$(grn gates)" "$side" "$file" "$lib" "$funcs" \
           "$([ "$mode" = ordered ] && printf ', before any early exit')"
    return 0
}

compare() {
    local a="$1" b="$2" f drift=0 sa sb
    local absent_a=0 absent_b=0 total=0
    printf 'A: %s\nB: %s\n\n' "$a" "$b"
    for f in "${CODE[@]}"; do
        sa=$(sha_of "$a/.githooks/$f")
        sb=$(sha_of "$b/.githooks/$f")
        total=$((total+1))
        [ "$sa" = "ABSENT" ] && absent_a=$((absent_a+1))
        [ "$sb" = "ABSENT" ] && absent_b=$((absent_b+1))
        if [ "$sa" = "$sb" ] && [ "$sa" != "ABSENT" ]; then
            printf '  %s  %-26s %s\n' "$(grn same)" "$f" "$sa"
        else
            drift=1
            printf '  %s  %-26s A=%s  B=%s\n' "$(red DRIFT)" "$f" "$sa" "$sb"
        fi
    done
    # A whole side missing is almost never drift; it is the wrong path. Said
    # here because the alternative — a column of ABSENT that is technically
    # correct — cost the operator five minutes on 2026-08-15, and would cost the
    # next reader the same five. The instrument was right and unhelpful.
    local side path n
    for side in A B; do
        [ "$side" = A ] && { path="$a"; n="$absent_a"; } || { path="$b"; n="$absent_b"; }
        [ "$n" -eq "$total" ] || continue
        printf '\n  %s  every code file is ABSENT on side %s.\n' "$(red HINT)" "$side"
        if [ -d "$path/.githooks" ]; then
            printf '      %s/.githooks exists but carries none of them — is it a partial copy?\n' "$path"
        else
            printf '      %s has no .githooks/ directory. The argument is a CLONE, not a hooks\n' "$path"
            printf '      directory: this script looks for <clone>/.githooks/… inside it.\n'
        fi
    done

    printf '\n  hook wrappers — per repository, checked by PROPERTY not by bytes:\n'
    local spec
    for spec in "${WRAPPERS[@]}"; do
        wp_check "$a" "$spec" "A " || drift=1
        wp_check "$b" "$spec" "B " || drift=1
    done

    printf '\n  per-repository by design, NOT compared:\n'
    for f in "${POLICY[@]}"; do
        printf '      %-26s A=%s  B=%s\n' "$f" \
               "$(sha_of "$a/.githooks/$f")" "$(sha_of "$b/.githooks/$f")"
    done
    return "$drift"
}

# ---------------------------------------------------------------------------
# --selftest — prove this script can say NO.
#
# A parity check that only ever prints "same" is indistinguishable from one that
# is broken, and it would be believed for exactly as long as it took someone to
# edit one of the two copies. So: build two throwaway trees, prove they compare
# equal, then change ONE BYTE of one code file and prove the answer flips. Also
# prove that changing a POLICY file does NOT flip it — a check that reds on the
# lists would be turned off within a week, because those lists must differ.
# ---------------------------------------------------------------------------
selftest() {
    local w a b rc fails=0
    w="$(mktemp -d "${TMPDIR:-/tmp}/parity-selftest-XXXXXX")"
    trap 'rm -rf "$w"' RETURN
    a="$w/a"; b="$w/b"
    mkdir -p "$a/.githooks/lib" "$b/.githooks/lib"
    local f
    for f in "${CODE[@]}" "${POLICY[@]}"; do
        mkdir -p "$(dirname "$a/.githooks/$f")" "$(dirname "$b/.githooks/$f")"
        printf 'content of %s\n' "$f" > "$a/.githooks/$f"
        printf 'content of %s\n' "$f" > "$b/.githooks/$f"
    done

    # Honest wrappers in both trees. Every wrapper arm below breaks ONE of them
    # and requires the verdict to flip; the pair above is the control that keeps
    # those arms from passing against a check that simply always says BROKEN.
    write_wrappers() {
        local t="$1"
        cat > "$t/.githooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
set -uo pipefail
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${HOOK_DIR}/lib/commit-policy.sh"
cp_check_identity || exit 1
cp_check_secret_paths "$@" || exit 1
[ "${BOT_SQUAD_SKIP_PEER_CHECK:-}" = "1" ] && exit 0
exit 0
HOOK
        cat > "$t/.githooks/pre-push" <<'HOOK'
#!/usr/bin/env bash
set -uo pipefail
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${HOOK_DIR}/lib/commit-policy.sh"
. "${HOOK_DIR}/lib/push-policy.sh"
ref_lines=()
while IFS= read -r line; do [ -n "$line" ] && ref_lines+=("$line"); done
[ "${#ref_lines[@]}" -eq 0 ] && exit 0
pp_check_push "$@" || exit 1
exit 0
HOOK
    }
    write_wrappers "$a"; write_wrappers "$b"

    compare "$a" "$b" >/dev/null; rc=$?
    [ "$rc" -eq 0 ] && printf '  %s  identical trees compare equal\n' "$(grn PASS)" \
                    || { printf '  %s  identical trees reported drift\n' "$(red FAIL)"; fails=1; }

    printf 'x' >> "$b/.githooks/lib/commit-policy.sh"
    compare "$a" "$b" >/dev/null; rc=$?
    [ "$rc" -eq 1 ] && printf '  %s  ONE BYTE of a code file flips the verdict\n' "$(grn PASS)" \
                    || { printf '  %s  a changed code file did NOT flip the verdict — this check is decorative\n' "$(red FAIL)"; fails=1; }
    truncate -s -1 "$b/.githooks/lib/commit-policy.sh"

    rm -f "$b/.githooks/lib/push-policy.sh"
    compare "$a" "$b" >/dev/null; rc=$?
    [ "$rc" -eq 1 ] && printf '  %s  a MISSING code file flips the verdict (absent is not equal)\n' "$(grn PASS)" \
                    || { printf '  %s  a missing code file did NOT flip the verdict\n' "$(red FAIL)"; fails=1; }
    printf 'content of lib/push-policy.sh\n' > "$b/.githooks/lib/push-policy.sh"

    printf 'another-identity@example.com\n' >> "$b/.githooks/allowed-identities"
    compare "$a" "$b" >/dev/null; rc=$?
    [ "$rc" -eq 0 ] && printf '  %s  a differing POLICY file does NOT flip the verdict (it must differ)\n' "$(grn PASS)" \
                    || { printf '  %s  a differing policy list reported drift — this check would be switched off\n' "$(red FAIL)"; fails=1; }

    # -----------------------------------------------------------------------
    # THE WRAPPER PROPERTY ARMS (T-0659). Each breaks the wrapper in exactly one
    # way and requires the verdict to FLIP. Without them the property check has
    # the same two ways of being green as the gate it is watching — working, and
    # switched off — and greping a function name out of a file is the cheapest
    # possible way to be the second one.
    # -----------------------------------------------------------------------
    wrapper_arm() {
        local label="$1" want="$2"
        compare "$a" "$b" >/dev/null; local r=$?
        if [ "$r" -eq "$want" ]; then
            printf '  %s  %s\n' "$(grn PASS)" "$label"
        else
            printf '  %s  %s (rc=%s, wanted %s)\n' "$(red FAIL)" "$label" "$r" "$want"; fails=1
        fi
        write_wrappers "$b"
    }

    wrapper_arm "an HONEST wrapper pair is PARITY (the control these arms need)" 0

    # 1. the gate is simply not called at all
    grep -v 'cp_check_' "$a/.githooks/pre-commit" > "$b/.githooks/pre-commit.tmp"
    mv "$b/.githooks/pre-commit.tmp" "$b/.githooks/pre-commit"
    wrapper_arm "a wrapper that never CALLS the gate flips the verdict" 1

    # 2. THE ONE THIS EXISTS FOR: the calls are present but placed after an
    #    early exit, so every commit that takes that branch is unchecked.
    cat > "$b/.githooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
set -uo pipefail
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${HOOK_DIR}/lib/commit-policy.sh"
[ "${BOT_SQUAD_SKIP_PEER_CHECK:-}" = "1" ] && exit 0
cp_check_identity || exit 1
cp_check_secret_paths "$@" || exit 1
HOOK
    wrapper_arm "a wrapper calling the gate AFTER an early exit 0 flips the verdict" 1

    # 3. sources something with a similar name from somewhere else
    sed 's#/lib/commit-policy\.sh#/lib/commit-policy-old.sh#' \
        "$a/.githooks/pre-commit" > "$b/.githooks/pre-commit"
    wrapper_arm "a wrapper sourcing the WRONG file (similar name) flips the verdict" 1

    # 4. the call is still in the file, behind a #
    sed 's#^cp_check_identity#\# cp_check_identity#' \
        "$a/.githooks/pre-commit" > "$b/.githooks/pre-commit"
    wrapper_arm "a wrapper whose gate call is COMMENTED OUT flips the verdict" 1

    return "$fails"
}

if [ "${1:-}" = "--selftest" ]; then
    printf 'check-guard-parity selftest\n'
    selftest; rc=$?
    [ "$rc" -eq 0 ] && printf '\nselftest OK\n' || printf '\n%s\n' "$(red 'selftest FAILED — do not trust this script')"
    exit "$rc"
fi

case "$#" in
    1) A="$(git rev-parse --show-toplevel 2>/dev/null)"; B="$1" ;;
    2) A="$1"; B="$2" ;;
    *) printf 'usage: %s <other clone> | <clone A> <clone B> | --selftest\n' "${0##*/}" >&2; exit 2 ;;
esac
[ -n "${A:-}" ] || { printf 'not inside a git clone, and only one path given\n' >&2; exit 2; }

compare "$A" "$B"; RC=$?
if [ "$RC" -eq 0 ]; then
    printf '\n%s\n' "$(grn 'PARITY: the gate code is identical in both clones.')"
else
    cat <<EOF

$(red 'DRIFT: the two clones are NOT running the same gate.')

This is the failure T-0660 was opened to make visible: one defect living in two
copies, fixed in one of them. Bring the older clone up to the newer file and
re-run; do not hand-merge the two, and do not "fix it in one place for now".
EOF
fi
exit "$RC"
