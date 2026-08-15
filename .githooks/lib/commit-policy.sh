#!/usr/bin/env bash
#
# commit-policy.sh — the two REFUSING gates of this clone's pre-commit hook.
# Sourced by `.githooks/pre-commit`. (T-0657)
#
# WHY THESE EXIST, AND WHY THEY REFUSE INSTEAD OF WARNING
# ------------------------------------------------------
# 2026-08-14 22:49Z an unidentified actor committed `init` as `t <t@t>` into TWO
# live clones 13s apart. In this one (`b465362f`) it cut `Dockerfile` from 120
# lines to a single `y`, and dragged `.env.bak-ifx-2026-08-07` — with LIVE
# `TELEGRAM_BOT_TOKEN`, `JWT_SECRET`, `SMTP_PASS`, `MONITORING_SECRET` — into the
# index and into the history of a shared tree.
#
# `.githooks/pre-commit` existed and RAN. It let all of it through, because it
# only ever did one thing: enumerate peer activity on the committed paths and
# ask for a re-run with BOT_SQUAD_ACK_PEERS=1. It looked at neither the identity
# nor the CONTENT of the index, and by construction it did not block — it asked
# for a repeat, which any automatic actor performs for free.
#
# The asymmetry is what justifies the strictness here:
#
#   cost of a FALSE POSITIVE — one repeated command with an explicit variable.
#   cost of a MISS           — IRREVERSIBLE. A secret that reaches history does
#                              not leave it: the commit stays reachable BY SHA
#                              after a force-push until gc, and `.env.bak` /
#                              `data/` are outside git entirely, so there is
#                              nothing to restore from.
#
# So both gates below exit NON-ZERO and let no commit happen. Neither is
# released by BOT_SQUAD_ACK_PEERS: that variable is habitual — it is typed on
# ordinary commits all day — and one habitual variable must not disarm two
# unrelated defences. Each gate has its OWN key, and neither key is a bare `=1`:
# the operator has to name the exact address or the exact path they are letting
# through, so the release cannot become muscle memory.

# ---------------------------------------------------------------------------
# Presentation helpers. `pa_bold` / `pa_yellow` come from peer-activity.sh when
# the hook sources both; define fallbacks so this file also works standalone
# (the test harness sources it directly).
# ---------------------------------------------------------------------------
if ! declare -F pa_bold >/dev/null 2>&1; then
    pa_bold()   { printf '%s' "$*"; }
    pa_yellow() { printf '%s' "$*"; }
fi
if ! declare -F pa_red >/dev/null 2>&1; then
    pa_red() { if [ -t 2 ]; then printf '\033[31m%s\033[0m' "$*"; else printf '%s' "$*"; fi; }
fi

# ---------------------------------------------------------------------------
# cp__policy_read <repo-relative path>
#
# Read a policy file as it exists in HEAD, NOT in the working tree, and report
# on the RETURN CODE where the answer came from:
#
#   0  the blob at HEAD:<path> — the normal, reviewed source
#   3  this repo has no commits at all, so the worktree copy was used
#      (the genuine bootstrap: a brand-new repo installing its own hooks)
#   4  no usable policy anywhere
#
# Reading HEAD is deliberate and it is the point. The policy files are lists
# that name what MAY pass. Reading them from the working tree would make the
# gates self-disarming: a single `git add -A` that sweeps up a secret also
# sweeps up any edit to the list that permits it, and the gate would consult the
# attacker's own copy. Reading HEAD means an exception must already BE committed
# and reviewed before it has any effect, so adding one is a separate, visible act.
#
# WHY THE CALLER IS TOLD APART "ABSENT" FROM "EMPTY" (T-0662). Until T-0662 this
# function returned text and nothing else, and an unreadable policy was
# indistinguishable from a permissive one. The identity gate treated the empty
# string as "no policy, gate INACTIVE" and returned 0 — so a commit that DELETED
# `.githooks/allowed-identities` passed legitimately (the list was still in HEAD
# while it was being checked), and every commit after it sailed through with the
# gate switched off. Measured on the shipped library by HEAD movement: two
# commits, author `t <t@t>`, no override key and no `--no-verify`. An empty file,
# a comments-only file, and a DIRECTORY at that path all reached the same branch.
#
# WHY THE BOOTSTRAP FALLBACK NARROWED TO "NO HEAD AT ALL". It used to fire
# whenever the file was missing from HEAD, which handed the same self-disarm
# back: delete the list in one commit, write your own copy into the worktree,
# and the gate reads YOUR policy. Now an existing repo that has no list in HEAD
# gets a REFUSAL, and the commit that first installs one names it in
# BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST — once, visibly, on the record.
#
# `git cat-file -t` rather than `-e`: `-e` answers yes for a TREE as happily as
# for a blob, and `git show` on a tree prints a directory listing that parses as
# policy lines matching nothing.
# ---------------------------------------------------------------------------
cp__policy_read() {
    local rel
    local top
    rel="$1"
    if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
        if [ "$(git cat-file -t "HEAD:$rel" 2>/dev/null)" = "blob" ]; then
            git show "HEAD:$rel" 2>/dev/null
            return 0
        fi
        return 4
    fi
    top=$(git rev-parse --show-toplevel 2>/dev/null) || return 4
    if [ -n "$top" ] && [ -f "$top/$rel" ] && [ -r "$top/$rel" ]; then
        cat "$top/$rel"
        return 3
    fi
    return 4
}

# ---------------------------------------------------------------------------
# cp__strip <text>  — drop CRs, comments and blank lines from a policy file
#
# The `tr -d` is not cosmetic (T-0662). An editor that saves the exception list
# with CRLF used to silently un-list every entry, because that side compares with
# `grep -qxF` — exact, no trim — so `.env.example\r` never equals `.env.example`.
# The refusal that followed then told the reader to "add it to the list and
# commit THAT first" for a path that was already in the list, sending them to
# fix a file that was not broken. The identity side survived the same input only
# by accident: its per-address trim is `s/[[:space:]]*$//`, and that class
# happens to include CR. Handling it once, here, removes the accident.
# ---------------------------------------------------------------------------
cp__strip() { tr -d '\r' | sed -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d'; }

# ---------------------------------------------------------------------------
# cp__lines <text>  — emit <text> as lines that `read` can consume ALL of.
#
# `while read -r … done < <(producer)` silently DROPS the final line when the
# producer's output does not end in a newline: `read` fills the variables and
# THEN returns non-zero, so the loop body never runs for it. Every policy file
# here is a list whose LAST line is load-bearing — `allowed-identities` ends
# with the `*` fallback, which is the only line an unusual clone path (a
# worktree, a throwaway, /tmp) can match. Losing it makes the gate consult an
# EMPTY allowlist, and an empty allowlist takes the "no policy, gate INACTIVE"
# branch — so a file saved by an editor that omits the trailing newline turns
# the identity gate OFF, silently, while every commit still succeeds and so
# looks like proof that nothing is wrong.
#
# Measured on 2026-08-15 against the T-0657 file: with the trailing newline the
# `*` line resolves; without it the lookup returns the empty string.
#
# Command substitution strips trailing newlines and `printf '%s\n'` puts back
# exactly one, so the last line survives whether or not the file had one. This
# is the same idiom the email loop in cp_check_identity already documents; the
# outer loop simply never got it.
# ---------------------------------------------------------------------------
cp__lines() { printf '%s\n' "$1"; }

# ===========================================================================
# GATE 1 — IDENTITY
# ===========================================================================
#
# Refuses a commit whose author/committer email is not allowed FOR THIS CLONE,
# and names both the found and the expected value.
#
# It cannot be one hardcoded address: the identities below are all LEGITIMATE,
# each in its own clone — `dev` commits as almdudleer@dev.local while
# `master` / `deploy` / `my-avi` commit as leshaserdyukov@gmail.com. A single
# address would red every prod release.
#
# The allowlist is a tracked file, `.githooks/allowed-identities`, matching on
# the repository's toplevel PATH. An unrecognised path (a new clone, a
# worktree, a throwaway) falls to the `*` line, which carries the UNION of the
# known-legitimate addresses — so an unknown clone is still default-deny against
# an unknown identity like `t@t`, it is merely not pinned to one address.
# ---------------------------------------------------------------------------
CP_IDENTITY_ALLOWLIST=".githooks/allowed-identities"

# cp__allowed_emails_for <toplevel path> <stripped list> -> comma-separated
# emails (first match wins). The list is passed IN rather than read here: the
# caller has to distinguish "absent", "empty" and "no line for this clone", and
# a function that returns only text cannot tell it any of that (T-0662).
cp__allowed_emails_for() {
    local top
    local list
    local pattern emails fallback=""
    top="$1"
    list="$2"
    while read -r pattern emails; do
        [ -n "$pattern" ] || continue
        [ -n "$emails" ] || continue
        if [ "$pattern" = "*" ]; then
            fallback="$emails"
            continue
        fi
        # shellcheck disable=SC2254  # pattern is a glob on purpose
        case "$top" in
            $pattern) printf '%s' "$emails"; return 0 ;;
        esac
    done < <(cp__lines "$list")
    printf '%s' "$fallback"
}

# ---------------------------------------------------------------------------
# cp__refuse_no_policy <human reason> <toplevel>  -> 0 released, 1 refused
#
# The gate has no policy it can assert against. Before T-0662 this printed
# "gate INACTIVE" and returned 0 — which is the one answer a guard must never
# give, because it is indistinguishable from "checked, and fine". Deleting the
# list, emptying it, or replacing it with a directory all landed here, and all
# of them let an unknown identity commit.
#
# The release key is its own, and it NAMES THE PATH rather than being a bare 1 —
# same rule as the other two gates, and for the same reason: the commit that
# first installs the list into an existing repo needs exactly one use of it, and
# that use should be visible on the record rather than muscle memory.
# ---------------------------------------------------------------------------
cp__refuse_no_policy() {
    local reason
    local top
    reason="$1"
    top="$2"

    if [ "${BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST:-}" = "$CP_IDENTITY_ALLOWLIST" ]; then
        printf '%s\n' "── identity check: missing policy at $CP_IDENTITY_ALLOWLIST allowed by BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST ──" >&2
        return 0
    fi

    cat >&2 <<EOF

$(pa_red "── COMMIT REFUSED: the identity gate has no policy to check against ──")

  list:    $CP_IDENTITY_ALLOWLIST
  problem: $reason
  clone:   $top

A guard with no policy is not a guard that approved — it is a guard that is
switched OFF, and those two states used to be indistinguishable here: until
T-0662 this printed "gate INACTIVE" and let the commit through. One commit
removing $CP_IDENTITY_ALLOWLIST was enough to disarm the
identity check for every commit after it, with no override and no --no-verify.

Restore the list (it is read from HEAD, so it has to be committed to count):
  git checkout HEAD~1 -- $CP_IDENTITY_ALLOWLIST

If you are INSTALLING the list into this repo for the first time, that one
commit genuinely has no list in HEAD to read. Name the path (not a bare 1):
  BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST='$CP_IDENTITY_ALLOWLIST' git commit ...

Note this key is deliberately NOT BOT_SQUAD_ACK_PEERS, and NOT
BOT_SQUAD_ALLOW_IDENTITY: that one names an address to let through ONCE and
answers a different question — whether a known-unusual identity is acceptable —
which is not a question that can even be asked while the list is unreadable.

EOF
    return 1
}

# cp_check_identity -> 0 allowed, 1 refused (message already on stderr)
cp_check_identity() {
    # `allowed` starts EMPTY, not merely declared: under `set -uo pipefail` a
    # bare `local allowed` leaves it unset, and the release path below never
    # assigns it — so the guard died on "unbound variable" the moment its own
    # override key was used. It failed closed, which is the safe direction, but
    # the only way to use the key was to hit a crash.
    local top found_author found_committer bad=""
    local allowed=""
    local raw
    local st
    local list
    top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0

    raw=$(cp__policy_read "$CP_IDENTITY_ALLOWLIST")
    st=$?
    list=$(printf '%s\n' "$raw" | cp__strip)

    if [ "$st" -eq 3 ]; then
        printf '%s\n' "── identity check: $CP_IDENTITY_ALLOWLIST read from the WORKING TREE — this repo has no commits yet ──" >&2
    fi

    if [ "$st" -eq 4 ]; then
        cp__refuse_no_policy "not a readable file at HEAD:$CP_IDENTITY_ALLOWLIST (absent, or not a blob)" "$top" || return 1
    elif [ -z "$list" ]; then
        cp__refuse_no_policy "the file exists but carries no policy lines (empty, or comments only)" "$top" || return 1
    else
        allowed=$(cp__allowed_emails_for "$top" "$list")
        if [ -z "$allowed" ]; then
            cp__refuse_no_policy "no line matches this clone and there is no '*' fallback line" "$top" || return 1
        fi
    fi

    # Released by the named key above: there is nothing left to check against,
    # and the release already said so on stderr.
    [ -n "$allowed" ] || return 0

    found_author=$(git var GIT_AUTHOR_IDENT 2>/dev/null | sed -n 's/.*<\(.*\)>.*/\1/p')
    found_committer=$(git var GIT_COMMITTER_IDENT 2>/dev/null | sed -n 's/.*<\(.*\)>.*/\1/p')

    local e ok
    for found in "$found_author" "$found_committer"; do
        [ -n "$found" ] || continue
        ok=0
        while IFS= read -r e; do
            [ "$e" = "$found" ] && { ok=1; break; }
        # printf '%s\n', NOT '%s': without the trailing newline `read` returns
        # non-zero on the final field and the loop never sees it, which silently
        # drops the LAST allowed address from the list.
        done < <(printf '%s\n' "$allowed" | tr ',' '\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        [ "$ok" = "1" ] || bad="$found"
    done

    [ -n "$bad" ] || return 0

    # The explicit, non-habitual release: it must NAME the address.
    if [ "${BOT_SQUAD_ALLOW_IDENTITY:-}" = "$bad" ]; then
        printf '%s\n' "── identity check: '$bad' allowed by BOT_SQUAD_ALLOW_IDENTITY ──" >&2
        return 0
    fi

    cat >&2 <<EOF

$(pa_red "── COMMIT REFUSED: unexpected git identity ──")

  found:     $bad
  expected:  $allowed
  clone:     $top

An unexpected identity is how the 2026-08-14 incident signed itself: commits
authored as 't <t@t>' landed in two live clones after user.name/user.email were
overwritten in .git/config. Legitimate work in this clone never carries an
address outside the list above.

Fix the identity (this is almost always the right move):
  git config user.email <one of: $allowed>

If this address is genuinely new and legitimate, add it to
$CP_IDENTITY_ALLOWLIST and commit THAT first — the list is read from
HEAD, so an uncommitted edit to it has no effect.

To let exactly this one commit through, name the address (not a bare 1):
  BOT_SQUAD_ALLOW_IDENTITY='$bad' git commit ...

EOF
    return 1
}

# ===========================================================================
# GATE 2 — SECRET-LIKE PATHS IN THE INDEX
# ===========================================================================
#
# Refuses a commit that stages a path that LOOKS like a secret. Path-shaped, not
# content-shaped: `.env.bak-ifx-2026-08-07` was caught by nothing because
# nothing looked at the index at all.
# ---------------------------------------------------------------------------
CP_SECRET_EXCEPTIONS=".githooks/allowed-lookalike-paths"

# cp__is_secretlike <repo-relative path> -> 0 yes, 1 no
cp__is_secretlike() {
    local p="$1" base lower
    base="${p##*/}"
    lower=$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')

    case "$lower" in
        .env|.env.*|*.env)                       return 0 ;;  # .env, .env.bak-…, prod.env
        *.pem|*.key|*.p12|*.pfx|*.keystore|*.jks) return 0 ;;
        id_rsa|id_dsa|id_ecdsa|id_ed25519)       return 0 ;;
        *credentials*|*secret*)                  return 0 ;;
        .htpasswd|.pgpass|.netrc|.npmrc|.pypirc) return 0 ;;
    esac
    return 1
}

# cp_check_secret_paths <staged path>... -> 0 clean, 1 refused
cp_check_secret_paths() {
    local p hits=() exceptions allowed_env
    [ "$#" -gt 0 ] || return 0

    # Absent or empty is NOT an error on this side, and must not become one:
    # this list names what may PASS, so having none is the strictest policy
    # there is. That is the opposite of the identity list, which IS the policy —
    # hence the two are read the same way and judged differently (T-0662).
    exceptions=$(printf '%s\n' "$(cp__policy_read "$CP_SECRET_EXCEPTIONS")" | cp__strip)
    allowed_env="${BOT_SQUAD_ALLOW_SECRET_PATHS:-}"

    for p in "$@"; do
        cp__is_secretlike "$p" || continue

        # tracked, reviewed exception (exact path, from HEAD)
        if printf '%s\n' "$exceptions" | grep -qxF -- "$p"; then
            continue
        fi
        # one-commit release: must NAME the path, not a bare 1
        if printf '%s\n' "$allowed_env" | tr ',' '\n' \
             | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -qxF -- "$p"; then
            printf '%s\n' "── secret check: '$p' allowed by BOT_SQUAD_ALLOW_SECRET_PATHS ──" >&2
            continue
        fi
        hits+=("$p")
    done

    [ "${#hits[@]}" -eq 0 ] && return 0

    cat >&2 <<EOF

$(pa_red "── COMMIT REFUSED: secret-like file(s) staged ──")

$(printf '  %s\n' "${hits[@]}")

A secret that reaches git history CANNOT be taken back out: after a force-push
the commit stays reachable by its SHA until garbage collection, and this tree is
shared with other sessions and with origin. On 2026-08-14 exactly this shape —
'.env.bak-ifx-2026-08-07', swept in by a wide 'git add -A' — carried live
TELEGRAM_BOT_TOKEN / JWT_SECRET / SMTP_PASS / MONITORING_SECRET into the history
of this clone.

Unstage them (the tree keeps the files):
  git restore --staged $(printf '%s ' "${hits[@]}")

If a path is genuinely committable and recurring (a template, a public cert),
add it to $CP_SECRET_EXCEPTIONS and commit THAT first — the list
is read from HEAD, so an uncommitted edit to it has no effect.

To let exactly these through once, name every path (not a bare 1):
  BOT_SQUAD_ALLOW_SECRET_PATHS='$(printf '%s' "$(IFS=,; echo "${hits[*]}")")' git commit ...

Note this key is deliberately NOT BOT_SQUAD_ACK_PEERS: that one is typed on
ordinary commits all day, and one habitual variable must not disarm two
unrelated defences.

EOF
    return 1
}
