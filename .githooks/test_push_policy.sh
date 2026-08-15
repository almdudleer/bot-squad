#!/usr/bin/env bash
#
# test_push_policy.sh — negative control, BOTH WAYS, for the refusing pre-push
# gate added by T-0658.
#
# WHAT COUNTS AS A REFUSAL HERE
# -----------------------------
# Three things at once, and a case that checks fewer is not checking this gate:
#   1. a non-zero exit code, taken from `git push` DIRECTLY — never through a
#      pipe, because a pipeline's `$?` is the LAST command's;
#   2. the remote ref DID NOT MOVE, read out of the bare repo afterwards — the
#      only statement about publication that is not the pusher's own opinion;
#   3. the refusal text names this gate, so pre-commit's banner or an unrelated
#      failure cannot masquerade as it.
#
# THE SANDBOX GUARD IS NOT DECORATION — READ THIS BEFORE EDITING
# --------------------------------------------------------------
# Inherited from T-0657, where it was learned expensively. That harness computed
#     local name="$1" mode="${2:-new}" d="$WORK/$name"
# and bash expands every RHS on a `local` line BEFORE assigning any of them, so
# under `set -u` `$name` was unbound when `$WORK/$name` was expanded. `D` came
# back empty and every `git -C "$D"` after it ran against
# /home/almdudleer/watchrobot/dev — the SHARED tree, where other sessions work.
# It committed a live peer's uncommitted work to the shared branch, and the
# suite still printed PASS lines, because a gate refusing in the wrong repo
# looks exactly like a gate refusing in the right one.
#
# So: every git invocation goes through `sbx`, which hard-exits 99 on any path
# outside $WORK — including the empty string. Assignments are one per line.
# `sbx_selftest` proves the guard fires, so it cannot rot into a wrapper that
# always says yes. This suite additionally never has a remote pointing anywhere
# but $WORK: the live-fact clone below has its `origin` REMOVED before anything
# else happens, so no push in this file can reach github even in principle.
#
# Run:  .githooks/test_push_policy.sh          (from anywhere in the clone)

set -uo pipefail

# The clone whose .githooks/ and history are the material under test. Defaults
# to this file's own clone; T0658_REPO exists only so the hook can be developed
# and proven OUTSIDE the shared tree before it is installed there, because an
# edit to .githooks/ in the shared tree is live for every peer immediately —
# T-0657 rejected a neighbouring session's commits for ~10 minutes that way.
REPO="${T0658_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Per-REPOSITORY facts — one copy of this suite runs in every clone that carries
# the guards (T-0660). The identity it commits as is derived from that clone's
# own allowlist; the history it uses as a live input comes from
# `.githooks/repo-facts`. See `.githooks/lib/suite-facts.sh`, and note that a
# fact this clone cannot supply produces a SKIP with its own counter, never a
# pass.
# shellcheck source=lib/suite-facts.sh
. "$REPO/.githooks/lib/suite-facts.sh"
sf_load "$REPO"

# The name planted in the SANDBOX repos below. Not a fact about any real
# repository — the sandboxes are `git init`-fresh — so it stays a constant.
SECRET=".env.bak-ifx-2026-08-07"

# A secret-like path the SUITE owns, appended to every sandbox's exception list
# by new_clone/new_pair below. Cases that need "a path this clone excepts" use
# THIS one rather than borrowing an entry out of the repository's real list:
# which paths a clone excepts is a fact about that clone, and a case leaning on
# one silently tests nothing in a repository that excepts something else — the
# T-0660 failure, one layer down. The repository's real list is still copied in
# alongside it, so the sandbox runs the real policy plus one known entry.
SUITE_PLANTED_EXCEPTION="config/planted.key"

# touch_listed <dir> — make a REAL change to the listed exception path.
#
# It lives in the base commit now, so writing the same bytes over it produces no
# commit and an empty pushable range — and an empty range passes for a reason
# that has nothing to do with the exception list. Every write is unique.
TOUCH_N=0
touch_listed() {
    need_sandbox "$1"
    TOUCH_N=$((TOUCH_N + 1))
    mkdir -p "$1/$(dirname "$SUITE_PLANTED_EXCEPTION")"
    printf 'public-material\nrevision=%s\n' "$TOUCH_N" > "$1/$SUITE_PLANTED_EXCEPTION"
}


WORK="$(mktemp -d "${TMPDIR:-/tmp}/t0658-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0; SKIP=0
ok()    { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()   { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
# A section this clone cannot supply the input for. Its own counter and its own
# line: a case that disappeared into the pass count is a check that was switched
# off wearing the clothes of one that ran.
skip()  { SKIP=$((SKIP+1)); printf '  \033[33mSKIP\033[0m  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }
info()  { printf '        %s\n' "$1"; }

# ---------------------------------------------------------------------------
# sbx / need_sandbox / need_under_work / sbx_selftest now live ONE copy over in
# `.githooks/lib/suite-facts.sh`, which this suite already sources above. They
# were two copies here and in the sibling suite, and they had already drifted —
# different `need_sandbox`, `need_under_work` in only one of them, two selftest
# arms against four. A guard that catches a run being redirected into a live
# tree is the last thing that should exist in two versions. Read that file for
# what the guard is for and why a "quick probe" is the run that needs it most.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# install_hooks <repo dir> <with|without> [hooks dir]
#
# `without` is the BEFORE arm: this clone genuinely has no pre-push today, so
# the control is the real prior state, not a disabled copy of the new one.
#
# `hooks dir` defaults to <repo dir>/.githooks. The live-fact clone in section 6
# passes an OUT-OF-TREE directory instead: it is a copy of the real repository,
# where .githooks/ is TRACKED, and overwriting tracked files there makes every
# later checkout/cherry-pick in that case fail for a reason that has nothing to
# do with the gate.
install_hooks() {
    local d
    local mode
    local hd
    d="${1-}"
    mode="${2-with}"
    hd="${3-}"
    need_sandbox "$d"
    [ -n "$hd" ] || hd="$d/.githooks"
    need_under_work "$hd"
    mkdir -p "$hd/lib" || exit 99
    cp "$REPO/.githooks/pre-commit"                "$hd/pre-commit"
    grep -q 'peer-activity\.sh' "$REPO/.githooks/pre-commit" \
        && cp "$REPO/.githooks/lib/peer-activity.sh" "$hd/lib/peer-activity.sh"
    cp "$CP_LIB"                                   "$hd/lib/commit-policy.sh"
    cp "$REPO/.githooks/allowed-identities"        "$hd/allowed-identities"
    cp "$REPO/.githooks/allowed-lookalike-paths"   "$hd/allowed-lookalike-paths"
    printf '%s\n' "$SUITE_PLANTED_EXCEPTION" >> "$hd/allowed-lookalike-paths"
    chmod +x "$hd/pre-commit"
    rm -f "$hd/pre-push"
    if [ "$mode" = "with" ]; then
        cp "$PP_HOOK" "$hd/pre-push"
        cp "$PP_LIB"  "$hd/lib/push-policy.sh"
        chmod +x "$hd/pre-push"
    fi
}

# Where the hook under test comes from. Defaults to the clone's own .githooks/,
# so the suite tests the SHIPPED files; overridable while developing them
# outside the shared tree (T-0658 DoD 6 — an edit to .githooks/ in the shared
# tree is live for every peer instantly, before any commit).
PP_HOOK="${PP_HOOK:-$REPO/.githooks/pre-push}"
PP_LIB="${PP_LIB:-$REPO/.githooks/lib/push-policy.sh}"
CP_LIB="${CP_LIB:-$REPO/.githooks/lib/commit-policy.sh}"

# ---------------------------------------------------------------------------
# new_pair <name> [with|without]  — sets globals D (work clone) and B (bare remote)
#
# Assignments deliberately one per line; see the header for what sharing a
# `local` line cost on T-0657.
# ---------------------------------------------------------------------------
new_pair() {
    local name
    local mode
    local d
    local b
    name="${1-}"
    mode="${2:-with}"
    [ -n "$name" ] || { printf 'FATAL: new_pair with empty name\n' >&2; exit 99; }
    d="$WORK/$name"
    b="$WORK/$name.git"

    git init -q --bare "$b" || exit 99
    mkdir -p "$d" || exit 99
    git -C "$d" init -q
    D="$d"
    B="$b"
    need_sandbox "$D"
    need_sandbox "$B"
    sbx "$D" config user.name  "guard-suite"
    sbx "$D" config user.email "$SUITE_IDENTITY"
    sbx "$D" config commit.gpgsign false
    sbx "$D" remote add sandbox "$B"

    install_hooks "$D" "$mode"
    mkdir -p "$d/backend"
    echo "def a(): return 1" > "$d/backend/thing.py"
    # A LISTED secret-like path (new_pair appends it to the sandbox's exception
    # list above) and nothing unlisted. The base commit is the floor every
    # section stands on, and a section that pushes the WHOLE history — 9d's
    # empty-remote bootstrap — would otherwise red on the floor rather than on
    # its own input. Whether `.env.example` is excepted is a fact about one
    # repository; this path is a fact about this suite (T-0659).
    mkdir -p "$d/$(dirname "$SUITE_PLANTED_EXCEPTION")"
    printf 'public-material\n' > "$d/$SUITE_PLANTED_EXCEPTION"
    sbx "$D" add -A
    # The base commit predates hook installation: bootstrapping through the gate
    # would be testing the gate with the gate.
    sbx "$D" commit -qm "base"
    sbx "$D" branch -M main
    sbx "$D" push -q sandbox main
    sbx "$D" config core.hooksPath .githooks
}

# ---------------------------------------------------------------------------
# push_in <workdir> <refspec> [VAR=val …]
#   -> LAST_OUT LAST_RC REMOTE_BEFORE REMOTE_AFTER LOCAL_SHA
#
# `git push` is run bare — no pipe anywhere near it. T-0658 DoD 7: a return code
# read through a pipeline is the pipeline's, and says nothing about the command.
# ---------------------------------------------------------------------------
push_in() {
    local d
    local refspec
    local branch
    d="${1-}"; shift
    refspec="${1-}"; shift
    need_sandbox "$d"
    branch="${refspec##*:}"
    branch="${branch#refs/heads/}"

    REMOTE_BEFORE="$(sbx "$B" rev-parse --verify -q "refs/heads/$branch" 2>/dev/null || echo NONE)"
    LAST_OUT="$(cd "$d" && env "$@" git push sandbox "$refspec" 2>&1)"
    LAST_RC=$?
    REMOTE_AFTER="$(sbx "$B" rev-parse --verify -q "refs/heads/$branch" 2>/dev/null || echo NONE)"
    LOCAL_SHA="$(sbx "$d" rev-parse "${refspec%%:*}" 2>/dev/null || echo UNKNOWN)"
}

# refusal = non-zero rc AND the remote ref did not move AND the right text
assert_push_refused() {
    local label
    local pat
    local why
    label="${1-}"
    pat="${2-}"
    why=""
    [ "$LAST_RC" -ne 0 ]                    || why="$why rc=0(expected nonzero)"
    [ "$REMOTE_AFTER" = "$REMOTE_BEFORE" ]  || why="$why REMOTE-REF-MOVED($REMOTE_BEFORE->$REMOTE_AFTER)"
    grep -qi -- "$pat" <<<"$LAST_OUT"       || why="$why no-match:/$pat/"
    if [ -z "$why" ]; then ok "$label"; else bad "$label —$why"; printf '%s\n' "$LAST_OUT" | sed 's/^/        /'; fi
}

assert_push_passed() {
    local label
    local why
    label="${1-}"
    why=""
    [ "$LAST_RC" -eq 0 ]                 || why="$why rc=$LAST_RC(expected 0)"
    [ "$REMOTE_AFTER" = "$LOCAL_SHA" ]   || why="$why remote-ref-did-NOT-reach-local($REMOTE_AFTER vs $LOCAL_SHA)"
    grep -q "PUSH REFUSED" <<<"$LAST_OUT" && why="$why printed-a-refusal-banner"
    if [ -z "$why" ]; then ok "$label"; else bad "$label —$why"; printf '%s\n' "$LAST_OUT" | sed 's/^/        /'; fi
}

# ---------------------------------------------------------------------------
# The seven ways a secret reaches history without pre-commit ever running.
# Each plants $SECRET on the CURRENT branch of <dir> by a different verb.
# ---------------------------------------------------------------------------
plant_file() {
    cat > "${1}/$SECRET" <<'EOF'
TELEGRAM_BOT_TOKEN=123456:REDACTED-TEST-VALUE
JWT_SECRET=redacted-test-value
SMTP_PASS=redacted-test-value
MONITORING_SECRET=redacted-test-value
EOF
}

# a side branch carrying the secret, built with hooks bypassed
side_branch_with_secret() {
    local d
    d="${1-}"
    need_sandbox "$d"
    sbx "$d" checkout -q -b sidebranch
    plant_file "$d"
    sbx "$d" add -A
    sbx "$d" -c core.hooksPath=/dev/null commit -qm "side: add backup"
    sbx "$d" checkout -q main
}

mk_noverify()   { plant_file "$1"; sbx "$1" add -A; sbx "$1" commit -q --no-verify -m "no-verify"; }
mk_hookspath()  { plant_file "$1"; sbx "$1" add -A; sbx "$1" -c core.hooksPath=/dev/null commit -qm "hookspath override"; }
mk_committree() {
    local d
    local tree
    local commit
    d="${1-}"
    need_sandbox "$d"
    plant_file "$d"
    sbx "$d" add -A
    tree=$(sbx "$d" write-tree)
    commit=$(sbx "$d" commit-tree "$tree" -p HEAD -m "commit-tree")
    sbx "$d" update-ref HEAD "$commit"
}
mk_cherrypick() { side_branch_with_secret "$1"; sbx "$1" cherry-pick sidebranch >/dev/null 2>&1; }
mk_merge()      { side_branch_with_secret "$1"; sbx "$1" merge -q --no-ff -m "merge side" sidebranch >/dev/null 2>&1; }
mk_rebase()     {
    side_branch_with_secret "$1"
    sbx "$1" checkout -q sidebranch
    sbx "$1" rebase -q main >/dev/null 2>&1
    sbx "$1" checkout -q main
    sbx "$1" merge -q --ff-only sidebranch >/dev/null 2>&1
}
mk_am()         {
    local d
    d="${1-}"
    side_branch_with_secret "$d"
    sbx "$d" format-patch -1 --stdout sidebranch > "$d/../patch.mbox"
    sbx "$d" am "$d/../patch.mbox" >/dev/null 2>&1
}
mk_nohooks()    {
    # a clone with core.hooksPath unset — no gate of any kind at commit time
    local d
    d="${1-}"
    need_sandbox "$d"
    sbx "$d" config --unset core.hooksPath
    plant_file "$d"
    sbx "$d" add -A
    sbx "$d" commit -qm "from a clone with no hooksPath"
    sbx "$d" config core.hooksPath .githooks
}

MAKERS="noverify hookspath committree cherrypick merge rebase am nohooks"

# ===========================================================================
head_ "0. THE HARNESS'S OWN GUARD"
# ===========================================================================
sbx_selftest

# ===========================================================================
head_ "1. DoD 1 — THE PROD RELEASE SHAPE STILL PUSHES (this is the first check, not the last)"
# ===========================================================================
# pre-push sits before an IRREVERSIBLE event. A false positive here stops a
# release. Prod is published from the `master` clone by CHERRY-PICK — the exact
# verb this gate intercepts — so the release shape is measured, not assumed.
new_pair release
# a dev branch with three ordinary commits, as bot_squad/dev would carry
sbx "$D" checkout -q -b devwork
for i in 1 2 3; do
    echo "step $i" > "$D/backend/step$i.py"
    sbx "$D" add -A
    sbx "$D" commit -qm "feat: step $i" BOT_SQUAD_SKIP_PEER_CHECK=1 2>/dev/null \
      || sbx "$D" -c core.hooksPath=/dev/null commit -qm "feat: step $i"
done
sbx "$D" checkout -q main
CP_START=$SECONDS
for c in $(sbx "$D" rev-list --reverse main..devwork); do
    sbx "$D" cherry-pick "$c" >/dev/null 2>&1 || bad "cherry-pick of $c failed in the harness"
done
push_in "$D" main
assert_push_passed "a release built by CHERRY-PICK pushes, and the remote ref moves"
info "cherry-pick+push wall clock: $((SECONDS - CP_START))s; pushed $(sbx "$D" rev-list --count "$REMOTE_BEFORE..$LOCAL_SHA") commits"

# the other real release shape on this project: merge(release) master <- dev
sbx "$D" checkout -q devwork
echo "step 4" > "$D/backend/step4.py"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "feat: step 4"
sbx "$D" checkout -q main
sbx "$D" merge -q --no-ff -m "merge(release): master приводится к dev" devwork >/dev/null 2>&1
push_in "$D" main
assert_push_passed "a release built by MERGE pushes, and the remote ref moves"

# a push that only DELETES a secret-like path must not red — that is the cleanup
# we want people to do. The path is planted and published HERE rather than taken
# from the base commit, for two reasons: the base must stay free of unlisted
# secret-like paths (see new_pair), and deleting a path that was excepted anyway
# would not have tested anything — this one is unlisted, so it had to be pushed
# through the gate's own named key to get onto the remote in the first place.
printf 'JWT_SECRET=live\n' > "$D/config/to-be-deleted.key"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "chore: plant an UNLISTED secret-like path"
push_in "$D" main BOT_SQUAD_ALLOW_SECRET_PUSH=config/to-be-deleted.key
assert_push_passed "setup: the named key publishes one unlisted secret-like path"
# `git rm`, not `rm --cached`: the latter clears the INDEX and leaves the file in
# the working tree, where the very next `add -A` picks it straight back up — the
# following case then reds on a path this one believed it had removed.
sbx "$D" -c core.hooksPath=/dev/null rm -q config/to-be-deleted.key >/dev/null 2>&1
sbx "$D" -c core.hooksPath=/dev/null commit -qm "chore: delete it again"
push_in "$D" main
assert_push_passed "a push whose only secret-like change is a DELETION passes"

# an already-tracked, HEAD-listed exception must not red when it is re-touched
touch_listed "$D"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "docs: a listed exception"
push_in "$D" main
assert_push_passed "a listed exception pushes ($SUITE_PLANTED_EXCEPTION)"

# deleting a remote ref pushes no objects and must not red
sbx "$D" push -q sandbox main:refs/heads/scratch
push_in "$D" ":refs/heads/scratch"
[ "$LAST_RC" -eq 0 ] && ok "deleting a remote ref is not refused" \
                     || { bad "deleting a remote ref was refused (rc=$LAST_RC)"; printf '%s\n' "$LAST_OUT" | sed 's/^/        /'; }

# ===========================================================================
head_ "2. DoD 2/7 — EVERY VERB THAT BYPASSES pre-commit, BOTH ARMS"
# ===========================================================================
for m in $MAKERS; do
    # --- BEFORE: no pre-push. The secret must actually LAND, or the "after"
    #     arm proves nothing: a case that could not pass before is not a control.
    new_pair "before_$m" without
    "mk_$m" "$D"
    push_in "$D" main
    if [ "$LAST_RC" -eq 0 ] && [ "$REMOTE_AFTER" = "$LOCAL_SHA" ] \
       && sbx "$B" cat-file -e "$REMOTE_AFTER:$SECRET" 2>/dev/null; then
        ok "BEFORE/$m: the secret reaches the remote — the hole is real"
    else
        bad "BEFORE/$m: did not reproduce (rc=$LAST_RC) — the AFTER arm below is unproven"
        printf '%s\n' "$LAST_OUT" | sed 's/^/        /'
    fi

    # --- AFTER: same verb, same sequence, pre-push installed
    new_pair "after_$m" with
    "mk_$m" "$D"
    push_in "$D" main
    assert_push_refused "AFTER/$m: the push is REFUSED and the remote ref does not move" \
                        "PUSH REFUSED: a pushed commit introduces a secret-like path"
    sbx "$B" cat-file -e "refs/heads/main:$SECRET" 2>/dev/null \
        && bad "AFTER/$m: the secret is present in the remote's ref anyway" \
        || ok "AFTER/$m: the remote's ref does not contain the secret"
done

# the ticket's central claim, asserted rather than assumed: the cherry-pick that
# publishes prod never invokes pre-commit at all.
new_pair cpquiet with
side_branch_with_secret "$D"
CP_OUT="$(cd "$D" && git cherry-pick sidebranch 2>&1)"
if grep -q "COMMIT REFUSED" <<<"$CP_OUT"; then
    bad "cherry-pick DID hit a pre-commit gate — the premise of this ticket is wrong"
else
    ok "cherry-pick never invokes pre-commit (no COMMIT REFUSED) — only pre-push can see it"
fi
push_in "$D" main
assert_push_refused "…and pre-push catches exactly that commit" "PUSH REFUSED"

# ===========================================================================
head_ "3. DoD 3 — THE WHOLE RANGE, NOT THE TIP, AND NOT THE ENDPOINT DIFF"
# ===========================================================================
new_pair midrange with
BASE_SHA="$(sbx "$D" rev-parse main)"
plant_file "$D"                                 # N-2 adds it
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "N-2: add the backup"
sbx "$D" rm -q "$SECRET"                        # N-1 removes it again
sbx "$D" -c core.hooksPath=/dev/null commit -qm "N-1: remove the backup"
echo "later" > "$D/backend/later.py"            # N is ordinary
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "N: unrelated work"

sbx "$D" cat-file -e "HEAD:$SECRET" 2>/dev/null \
    && bad "harness error: the secret is still in the tip" \
    || ok "setup: the tip does NOT contain the secret (a tip scan would see nothing)"
if [ -z "$(sbx "$D" diff --name-only "$BASE_SHA..HEAD" | grep -F "$SECRET")" ]; then
    ok "setup: the ENDPOINT diff ${BASE_SHA:0:8}..HEAD reports nothing — it is blind by construction"
else
    bad "setup: the endpoint diff saw it — this case no longer proves what it is for"
fi
push_in "$D" main
assert_push_refused "a secret added in N-2 and removed in N-1 STILL reds the push" "PUSH REFUSED"
grep -qF "$SECRET" <<<"$LAST_OUT" && ok "the refusal names the offending path" \
                                  || bad "the refusal does not name the offending path"

# ===========================================================================
head_ "4. DoD 4 — THE RELEASE IS ITS OWN NAMED KEY"
# ===========================================================================
new_pair keys with
mk_cherrypick "$D"

push_in "$D" main BOT_SQUAD_ACK_PEERS=1
assert_push_refused "BOT_SQUAD_ACK_PEERS=1 does NOT release the push gate" "PUSH REFUSED"

push_in "$D" main "BOT_SQUAD_ALLOW_SECRET_PATHS=$SECRET"
assert_push_refused "pre-commit's key BOT_SQUAD_ALLOW_SECRET_PATHS does NOT release it (different radius)" \
                    "PUSH REFUSED"

push_in "$D" main BOT_SQUAD_ALLOW_SECRET_PUSH=1
assert_push_refused "a bare BOT_SQUAD_ALLOW_SECRET_PUSH=1 does NOT release it (must name the path)" \
                    "PUSH REFUSED"

push_in "$D" main "BOT_SQUAD_ALLOW_SECRET_PUSH=$SECRET"
assert_push_passed "naming the exact path in BOT_SQUAD_ALLOW_SECRET_PUSH releases it"

# ===========================================================================
head_ "5. THE EXCEPTION LIST CANNOT BE CARRIED BY THE PUSH IT PERMITS"
# ===========================================================================
# pre-commit reads its lists from HEAD so one `git add -A` cannot sweep in both a
# secret and its permission. For a PUSH, HEAD is the tip being published — it IS
# the attacker's copy — so the list is read from what the remote already has.
new_pair selfdisarm with
mk_cherrypick "$D"
echo "$SECRET" >> "$D/.githooks/allowed-lookalike-paths"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "chore: permit the backup path"
push_in "$D" main
assert_push_refused "adding the exception INSIDE the pushed range does NOT release it" "PUSH REFUSED"

# …and once the exception is genuinely published first, the second push passes.
new_pair twostep with
echo "$SECRET" >> "$D/.githooks/allowed-lookalike-paths"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "chore: permit the backup path"
push_in "$D" main
assert_push_passed "step 1 — publishing the exception list alone passes"
mk_cherrypick "$D"
push_in "$D" main
assert_push_passed "step 2 — with the exception already on the remote, the path pushes"
# ===========================================================================
head_ "6. THE LIVE FACT — THIS CLONE'S OWN PUBLISHED HISTORY"
# ===========================================================================
# Everything above this line runs against sandboxes built by `git init`, and a
# gate proven only against inputs the suite itself planted is proven only to
# recognise the suite's own fixtures. This section uses the repository the suite
# is installed in.
#
# It is in two halves, because two different repositories can supply two
# different amounts of it (T-0660):
#
#   6a  the published history baseline — every clone can supply this, and it is
#       the question that decides whether arming the gate stops legitimate work:
#       does anything already on the remote match the gate's own test without
#       being in this repository's exception list?
#   6b  a real commit of this repository that introduces a secret-like path.
#       signal-tracker has one (the 2026-08-14 incident commit, named in
#       repo-facts, with a later commit deleting the file — which is exactly
#       what makes an endpoint diff blind to it). A repository that has none
#       leaves it empty and this half SKIPS. It does not quietly pass.
#
# Measured on a THROWAWAY CLONE whose `origin` is REMOVED before anything else
# runs, so nothing here can reach the real remote.
if [ -z "${SUITE_LIVE_BRANCH:-}" ]; then
    skip "the live-fact section: no SUITE_LIVE_BRANCH in ${SUITE_FACTS_FILE} — this clone's own history was NOT used as an input"
else
LIVE="$WORK/live"
git clone -q --no-hardlinks --branch "$SUITE_LIVE_BRANCH" "$REPO" "$LIVE" 2>/dev/null
if [ ! -d "$LIVE/.git" ]; then
    bad "could not clone $REPO at $SUITE_LIVE_BRANCH into the sandbox — the live-fact section is UNMEASURED"
else
    need_sandbox "$LIVE"
    sbx "$LIVE" remote remove origin 2>/dev/null
    ok "live clone made; its 'origin' removed — no push here can reach the real remote"
    D="$LIVE"
    B="$WORK/live.git"
    git init -q --bare "$B"
    sbx "$D" config user.name  "guard-suite"
    sbx "$D" config user.email "$SUITE_IDENTITY"
    sbx "$D" remote add sandbox "$B"
    LIVE_HOOKS="$WORK/live-hooks"
    install_hooks "$D" with "$LIVE_HOOKS"

    ORIGIN_TIP="$(git -C "$REPO" rev-parse "refs/remotes/origin/$SUITE_LIVE_BRANCH" 2>/dev/null || echo '')"

    # ---- 6a. the published baseline -------------------------------------
    # Asked with the gate's OWN test, not with a regex written next to it. A
    # second copy of "what looks like a secret" is a second thing to keep in
    # step, and the moment the two disagree this assertion starts answering a
    # question the gate is not asking.
    if [ -z "$ORIGIN_TIP" ]; then
        skip "6a published baseline: $REPO has no refs/remotes/origin/$SUITE_LIVE_BRANCH — nothing to read the published history from"
    else
        # shellcheck disable=SC1091
        . "$REPO/.githooks/lib/commit-policy.sh"
        LISTED="$(cp__strip < "$REPO/.githooks/allowed-lookalike-paths" 2>/dev/null)"
        BLOCKING=""   # secret-like, AT the published tip, and NOT listed
        HISTORIC=""   # secret-like, in history, gone from the tip
        SEEN=0
        HITS=0
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            SEEN=$((SEEN+1))
            cp__is_secretlike "$p" || continue
            HITS=$((HITS+1))
            printf '%s\n' "$LISTED" | grep -qxF -- "$p" && continue
            # The distinction that matters. A path still IN the published tip is
            # one this repository commits to as ordinary work: unlisted, the gate
            # reds that work, and arming it would be a mistake. A path that is
            # only in HISTORY is not touched by anyone today — an ordinary push
            # never rescans commits the remote already has — so requiring it to
            # be listed would mean writing a standing permission to re-introduce
            # a file nobody needs. It is reported by name instead of waved
            # through silently, which is the whole of what is owed here.
            if sbx "$D" cat-file -e "$ORIGIN_TIP:$p" 2>/dev/null; then
                BLOCKING="$BLOCKING $p"
            else
                HISTORIC="$HISTORIC $p"
            fi
        done < <(sbx "$D" log --no-merges --root --no-renames --diff-filter=ACMR \
                       --name-only --format='' "$ORIGIN_TIP" 2>/dev/null | sort -u)
        # Non-vacuity: a walk that produced no paths at all would report "no
        # leaks" for a reason that has nothing to do with the history.
        [ "$SEEN" -gt 0 ] && ok "6a: the published history walk produced $SEEN distinct paths, $HITS secret-like (not an empty read)" \
                          || bad "6a: the walk produced NO paths — this baseline would be vacuous"
        # A path may be unlisted ON PURPOSE — bot-squad keeps `.env.example` out
        # of its exception list because that file currently carries a live token,
        # so reddening a push that touches it is the CORRECT outcome, not a
        # false positive. Such a path has to be DECLARED in repo-facts, with a
        # reason; an undeclared one still reds, and a declaration with no reason
        # is not a declaration.
        DECLARED=""
        UNDECLARED=""
        for p in $BLOCKING; do
            if [ -n "$SUITE_KNOWN_UNLISTED_WHY" ] && printf '%s\n' $SUITE_KNOWN_UNLISTED | grep -qxF -- "$p"; then
                DECLARED="$DECLARED $p"
            else
                UNDECLARED="$UNDECLARED $p"
            fi
        done
        [ -z "$UNDECLARED" ] && ok "6a: every secret-like path still IN origin/$SUITE_LIVE_BRANCH's tip is either in this repo's exception list or declared unlisted on purpose — arming the gate stops no unplanned work" \
                             || { bad "6a: the published TIP carries UNDECLARED unlisted secret-like paths — arming the gate WOULD red legitimate work"; printf '        %s\n' $UNDECLARED; }
        if [ -n "$DECLARED" ]; then
            info "6a: unlisted ON PURPOSE, declared in ${SUITE_FACTS_FILE##*/} — a push touching one of these reds, and that is the intended outcome:"
            printf '        %s\n' $DECLARED
            info "        why: $SUITE_KNOWN_UNLISTED_WHY"
        fi
        if [ -n "$HISTORIC" ]; then
            info "6a: secret-like paths in the published history but NOT in the tip, deliberately unlisted so re-introducing one is a decision, not a default:"
            printf '        %s\n' $HISTORIC
        fi
    fi

    # ---- 6b. a real commit of this repository that adds a secret-like path --
    #
    # THE DEGENERATE INPUT THIS SECTION MUST RECOGNISE (operator, 2026-08-15).
    # The range under test is `merge-base(branch, origin/branch)..branch`. In a
    # clone whose origin ref already points AT the branch tip — a fresh
    # `git clone` of another local clone, which is exactly how anyone reproduces
    # this suite — that merge base IS the tip, the range is ZERO commits, there
    # is nothing to scan, and the push passes lawfully. Measured: two cases red
    # in that clone, and the same reds reappear if the gate is genuinely broken.
    #
    # So it is detected and SKIPPED by name, with its own counter. A red here
    # would say "the gate failed"; silence would say "the gate held". Neither is
    # true, and the difference between them is what the reader needs.
    LIVE_BASE=""
    LIVE_RANGE_N=0
    if [ -n "$ORIGIN_TIP" ]; then
        LIVE_BASE="$(sbx "$D" merge-base "$SUITE_LIVE_BRANCH" "$ORIGIN_TIP" 2>/dev/null || echo '')"
        [ -n "$LIVE_BASE" ] && LIVE_RANGE_N="$(sbx "$D" rev-list --count "$LIVE_BASE..$SUITE_LIVE_BRANCH" 2>/dev/null || echo 0)"
    fi

    if ! sf_have SUITE_LIVE_SECRET_COMMIT SUITE_LIVE_SECRET_PATH; then
        skip "6b live positive input: ${SUITE_FACTS_FILE} names no real commit of this repository that introduces a secret-like path — 'an ordinary push of this branch reds' is UNPROVEN here, not proven"
    elif [ -z "$ORIGIN_TIP" ]; then
        skip "6b live positive input: no refs/remotes/origin/$SUITE_LIVE_BRANCH to take the pushable range from"
    elif [ "${LIVE_RANGE_N:-0}" -eq 0 ]; then
        skip "6b live positive input: the pushable range ${LIVE_BASE:0:8}..$SUITE_LIVE_BRANCH is EMPTY — this clone's origin ref already points at the branch tip, so there is nothing for the gate to scan and a passing push proves nothing. Point refs/remotes/origin/$SUITE_LIVE_BRANCH at what the real origin holds and re-run"
    elif ! sbx "$D" merge-base --is-ancestor "$SUITE_LIVE_SECRET_COMMIT" "$SUITE_LIVE_BRANCH" 2>/dev/null \
         || sbx "$D" merge-base --is-ancestor "$SUITE_LIVE_SECRET_COMMIT" "$ORIGIN_TIP" 2>/dev/null; then
        skip "6b live positive input: $SUITE_LIVE_SECRET_COMMIT is not INSIDE the pushable range ($LIVE_RANGE_N commits) — it is either off this branch or already published, so an ordinary push would not scan it and the arm would pass for the wrong reason"
    else
    BASE="$LIVE_BASE"
    info "6b: pushable range is $LIVE_RANGE_N commit(s), and ${SUITE_LIVE_SECRET_COMMIT} is inside it"
    # Seed the sandbox remote with what this clone and its origin genuinely
    # SHARE — their merge base. Seeding origin's own tip instead would model a
    # push git refuses as non-fast-forward before any hook runs, which would
    # make this case vacuous: it would prove git rejects a stale push, not that
    # the gate saw the secret.
    sbx "$D" push -q sandbox "$BASE:refs/heads/$SUITE_LIVE_BRANCH"
    sbx "$D" config core.hooksPath "$LIVE_HOOKS"

    LIVE_SHA="$(sbx "$D" rev-parse "$SUITE_LIVE_SECRET_COMMIT" 2>/dev/null || echo '')"
    if [ -n "$LIVE_SHA" ] && sbx "$D" cat-file -e "$LIVE_SHA:$SUITE_LIVE_SECRET_PATH" 2>/dev/null; then
        ok "the live positive input is present: ${LIVE_SHA:0:8} carries $SUITE_LIVE_SECRET_PATH"
    else
        bad "$SUITE_LIVE_SECRET_COMMIT no longer carries $SUITE_LIVE_SECRET_PATH — the live input named in repo-facts has changed"
    fi
    if [ -z "$(sbx "$D" diff --name-only "$BASE..$SUITE_LIVE_BRANCH" | grep -F "$SUITE_LIVE_SECRET_PATH")" ]; then
        ok "the endpoint diff over the REAL pushable range reports nothing — it is blind by construction"
    else
        info "the endpoint diff over the real range DOES see it; the per-commit walk below is still the thing under test"
    fi

    push_in "$D" "$SUITE_LIVE_BRANCH"
    assert_push_refused "an ORDINARY push of $SUITE_LIVE_BRANCH from this clone's real history is REFUSED" \
                        "PUSH REFUSED"
    grep -qF "${LIVE_SHA:0:8}" <<<"$LAST_OUT" && ok "the refusal names the real offending commit ${LIVE_SHA:0:8}" \
                                              || bad "the refusal does not name ${LIVE_SHA:0:8}"

    # The publishing route this work is itself released by must NOT red: cut a
    # branch from the state the REMOTE actually has (not from this clone's tip,
    # which carries the offending commit), put the shipped guard files on it,
    # and push THAT.
    sbx "$D" fetch -q sandbox
    sbx "$D" checkout -q -b publish "$ORIGIN_TIP"
    mkdir -p "$D/.githooks/lib"
    cp "$PP_HOOK" "$D/.githooks/pre-push"
    cp "$PP_LIB"  "$D/.githooks/lib/push-policy.sh"
    cp "$CP_LIB"  "$D/.githooks/lib/commit-policy.sh"
    sbx "$D" add -A .githooks
    sbx "$D" -c core.hooksPath=/dev/null commit -qm "feat(guards): the shipped gate files"
    push_in "$D" publish:refs/heads/publish
    assert_push_passed "the publishing route (branch cut from the remote's state) pushes clean"
    info "range scanned: $(sbx "$D" rev-list --count "$ORIGIN_TIP..publish") commits"
    fi
fi
fi

# ===========================================================================
head_ "7. WHAT THIS GATE DOES NOT CLOSE — measured, so nobody has to guess"
# ===========================================================================
# A gate that is silent about its bypasses reads as impassable. These are not
# failures; each one is asserted to PASS, so the honest list stays true.
new_pair gaps with
mk_cherrypick "$D"
LAST_OUT="$(cd "$D" && git push --no-verify sandbox main 2>&1)"; LAST_RC=$?
REMOTE_AFTER="$(sbx "$B" rev-parse --verify -q refs/heads/main || echo NONE)"
LOCAL_SHA="$(sbx "$D" rev-parse main)"
if [ "$LAST_RC" -eq 0 ] && [ "$REMOTE_AFTER" = "$LOCAL_SHA" ]; then
    ok "GAP CONFIRMED: 'git push --no-verify' publishes the secret — no hook runs at all"
else
    bad "the --no-verify gap did not reproduce; the honest list in the README is now wrong"
fi

new_pair gaps2 with
mk_cherrypick "$D"
sbx "$D" config --unset core.hooksPath
push_in "$D" main
if [ "$LAST_RC" -eq 0 ] && [ "$REMOTE_AFTER" = "$LOCAL_SHA" ]; then
    ok "GAP CONFIRMED: a clone with core.hooksPath unset has no gate — it publishes"
else
    bad "the unset-hooksPath gap did not reproduce; the honest list in the README is now wrong"
fi

# ===========================================================================
head_ "8. A POLICY LIST WITH NO TRAILING NEWLINE MUST NOT CHANGE THE VERDICT"
# ===========================================================================
# An editor that omits the final newline is a one-keystroke way to delete the
# LAST line of a list from the reader's point of view, and the last line is
# always the load-bearing one: `*` in allowed-identities, and whatever exception
# was most recently added here. The case is written so it cannot pass vacuously
# — a second, UNLISTED secret-like path must still red in the same push, which
# proves the gate was consulted at all rather than switched off.
OTHER=".env.bak-other-2026-08-15"

new_pair nonewline with
# the list, ending on the path under test, with NO trailing newline, published
# first (the list is read from what the remote already has)
printf '# exceptions\n.env.example\n%s' "$SECRET" > "$D/.githooks/allowed-lookalike-paths"
[ -n "$(tail -c1 "$D/.githooks/allowed-lookalike-paths")" ] \
    && ok "setup: the list genuinely has no trailing newline and ends on $SECRET" \
    || bad "setup: the list still ends in a newline — this case would be vacuous"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "chore: exception list, no trailing newline"
push_in "$D" main
assert_push_passed "publishing the newline-less list itself passes"

mk_cherrypick "$D"
push_in "$D" main
assert_push_passed "the LAST line of a newline-less exception list is still honoured"

plant_file "$D"
mv "$D/$SECRET" "$D/$OTHER" 2>/dev/null || printf 'X=1\n' > "$D/$OTHER"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "add an UNLISTED secret-like path"
push_in "$D" main
assert_push_refused "…and an UNLISTED path in the same clone still reds — the gate was not disarmed" \
                    "PUSH REFUSED"
grep -qF "$OTHER" <<<"$LAST_OUT" && ok "the refusal names $OTHER, not the listed path" \
                                 || bad "the refusal does not name $OTHER"

# ===========================================================================
head_ "9. THE GATE'S OWN POLICY SOURCE (T-0662)"
# ===========================================================================
# Everything above attacks the RANGE — which verb carried the secret, which
# commit inside it, which endpoint diff hides it. This section attacks the
# EXCEPTION LIST, i.e. the gate's own input, because until T-0662 that list had
# two fallbacks and either of them handed the policy to the pusher:
#
#   remote side  ->  HEAD  ->  the WORKING TREE
#                    ^^^^      ^^^^^^^^^^^^^^^^
#                    the tip being pushed, i.e. the copy under suspicion
#                              never committed at all
#
# Measured before the fix on a throwaway remote, by whether the REMOTE REF moved
# and whether the blob arrived: two pushes, no override key, no --no-verify, and
# the secret landed. Every case here is measured the same way — assert_push_*
# compares the remote ref, never the text alone.

# ---------------------------------------------------------------------------
# 9a. THE WORKTREE FALLBACK. Remove the list from the remote in one push, then
# write your own copy into the working tree and push the secret.
# ---------------------------------------------------------------------------
new_pair own_worktree
sbx "$D" rm -q .githooks/allowed-lookalike-paths
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: drop the exception list"
push_in "$D" main
assert_push_passed "the push that REMOVES the exception list is allowed (it can only tighten the gate)" \

printf '# my own policy\n%s\n' "$SECRET" > "$D/.githooks/allowed-lookalike-paths"   # worktree only
printf 'JWT_SECRET=live\n' > "$D/$SECRET"
sbx "$D" add "$SECRET"
sbx "$D" -c core.hooksPath=/dev/null commit -qm "init"
push_in "$D" main
assert_push_refused "an exception list that exists ONLY in the working tree does not permit its own author" \
                    "PUSH REFUSED"
[ "$(sbx "$B" cat-file -t "main:$SECRET" 2>/dev/null || echo NONE)" = "NONE" ] \
    && ok "…and the secret blob never reached the remote" \
    || bad "…but the secret blob IS on the remote — the refusal did not hold"

# ---------------------------------------------------------------------------
# 9b. THE HEAD FALLBACK — the same defect one step shorter. For a push, HEAD is
# the copy under suspicion, so committing your own exception list must not
# permit the secret that travels with it.
# ---------------------------------------------------------------------------
new_pair own_head
sbx "$D" rm -q .githooks/allowed-lookalike-paths
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: drop the exception list"
push_in "$D" main
assert_push_passed "setup: the remote no longer carries an exception list"

printf '# my own policy\n%s\n' "$SECRET" > "$D/.githooks/allowed-lookalike-paths"
printf 'JWT_SECRET=live\n' > "$D/$SECRET"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "init"
push_in "$D" main
assert_push_refused "an exception list COMMITTED in the same push does not permit its own secret" \
                    "PUSH REFUSED"

# ---------------------------------------------------------------------------
# 9c. THE CONTROL, in the same shape, or 9a/9b prove only that this gate reds.
# With the list where it belongs — on the side the remote already holds — the
# listed path pushes.
# ---------------------------------------------------------------------------
new_pair own_control
touch_listed "$D"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "docs: a listed exception"
push_in "$D" main
assert_push_passed "a path listed on the REMOTE side still pushes — the gate reads, it does not merely red"

# ---------------------------------------------------------------------------
# 9d. THE ONE GENUINE BOOTSTRAP: a remote with NO refs at all. There is no
# published history to protect, and `pp__policy_commit` hands this function the
# literal "HEAD" — so reading HEAD there is reading the argument, not falling
# back to it. Both arms, because "it publishes" alone would go green on a gate
# that had simply given up.
# ---------------------------------------------------------------------------
new_pair own_bootstrap
sbx "$D" push -q --delete sandbox main 2>/dev/null || true
[ "$(sbx "$B" for-each-ref --count=1 --format='%(refname)')" = "" ] \
    && ok "setup: the sandbox remote genuinely has no refs" \
    || bad "setup: the remote still has refs — case 9d would not be the bootstrap"
touch_listed "$D"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "docs: a listed exception"
push_in "$D" main
assert_push_passed "a first publication to an EMPTY remote honours the list it is publishing"

printf 'JWT_SECRET=live\n' > "$D/$SECRET"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "init"
push_in "$D" main:refs/heads/other
assert_push_refused "…and an UNLISTED secret in that same first publication is still refused" \
                    "PUSH REFUSED"

# ---------------------------------------------------------------------------
# 9e. THE LIST IS A DIRECTORY on the policy commit. `git cat-file -e` says yes
# for a tree, and `git show` on a tree prints a listing that parses as entries
# matching nothing — fail-closed here, since an unusable list means zero
# exceptions, but pinned so it stays that way.
# ---------------------------------------------------------------------------
new_pair own_isdir
sbx "$D" rm -q .githooks/allowed-lookalike-paths
mkdir -p "$D/.githooks/allowed-lookalike-paths"
printf 'not a policy\n' > "$D/.githooks/allowed-lookalike-paths/inner.txt"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: exception list replaced by a directory"
push_in "$D" main
assert_push_passed "setup: the directory reaches the remote"
[ "$(sbx "$B" cat-file -t "main:.githooks/allowed-lookalike-paths" 2>/dev/null)" = "tree" ] \
    && ok "setup: the policy path really is a tree on the remote side" \
    || bad "setup: it is not a tree — case 9e would be vacuous"
touch_listed "$D"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "docs: a formerly-listed path"
push_in "$D" main
assert_push_refused "a DIRECTORY where the exception list belongs means zero exceptions, not all-permitted" \
                    "PUSH REFUSED"

# ---------------------------------------------------------------------------
# 9f. CRLF ON THE EXCEPTION LIST. The comparison on this side is `grep -qxF` —
# exact, no trim — so before T-0662 a list saved with CRLF silently un-listed
# every entry, and the refusal then told the reader to add a path that was
# already there. Fixed once, in cp__strip, which both hooks share.
# ---------------------------------------------------------------------------
new_pair own_crlf
printf '# exceptions\r\n%s\r\n' "$SUITE_PLANTED_EXCEPTION" > "$D/.githooks/allowed-lookalike-paths"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "policy: CRLF exception list"
push_in "$D" main
assert_push_passed "setup: the CRLF list reaches the remote"
grep -q $'\r' "$D/.githooks/allowed-lookalike-paths" \
    && ok "setup: the exception list genuinely carries CRLF" \
    || bad "setup: no CRLF — case 9f would be vacuous"
touch_listed "$D"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "docs: a listed exception"
push_in "$D" main
assert_push_passed "a CRLF exception list still honours its entries"
printf 'JWT_SECRET=live\n' > "$D/$SECRET"
sbx "$D" add -A
sbx "$D" -c core.hooksPath=/dev/null commit -qm "init"
push_in "$D" main
assert_push_refused "…and an UNLISTED path in that same clone still reds — the list was read, not skipped" \
                    "PUSH REFUSED"

# ===========================================================================
printf '\n\033[1m%s\033[0m\n' "RESULT: $PASS passed, $FAIL failed, $SKIP skipped"
printf '        clone %s\n' "$REPO"
printf '        identity under test: %s\n' "$SUITE_IDENTITY"
printf '        repo-facts: %s\n' "$( [ "$SUITE_FACTS_FOUND" = 1 ] && echo "$SUITE_FACTS_FILE" || echo "ABSENT ($SUITE_FACTS_FILE)" )"
[ "$SKIP" -gt 0 ] && printf '        \033[33m%s section(s) had no input in this clone and were NOT checked.\033[0m\n' "$SKIP"
[ "$FAIL" -eq 0 ] || exit 1
