#!/usr/bin/env bash
#
# test_worktree_guard.sh — selftest for the worktree guard and its PreToolUse
# hook (T-0826; lifted from watchrobot's `.githooks/test_stash_guard.sh`,
# T-0433, and widened to the five verb families operator p502 scoped in).
#
# "A guard nobody has watched fail is not a guard." Every case here builds a
# throwaway clone, reproduces a real shape of the 2026-07-30 incident in it, and
# asserts the guard's actual exit code and output. NOTHING IN THIS FILE TOUCHES
# THE SHARED WORKING TREE, and nothing here calls the live worker socket — the
# roster arrives through the `BOT_SQUAD_GUARD_ROSTER_FILE` seam.
#
# It tests BOTH directions, because a guard that refuses everything gets turned
# off within a day:
#   REFUSE — the incident shapes across all five families
#   ALLOW  — the diagnosis paths (stash list/show, clean -n), a clean tree,
#            narrow own-path operations, index-only operations, the override,
#            `git checkout <branch>` (the near-miss), and prose that merely
#            MENTIONS one of the verbs.
#
# FOUR ARMS THAT ARE NOT OPTIONAL:
#
#   CONTROL ARM — for EACH of the five families, the same command run WITHOUT
#   the guard must destroy the fixtures. Proving the arms differ is the only way
#   to know the guard, and not the fixture, is doing the work.
#
#   BLAST RADIUS — the risk unique to our side. A PreToolUse hook intercepts
#   EVERY Bash call in EVERY session in this clone, and `checkout`/`reset` are
#   everyday commands. Ordinary git, non-git commands, and every way the hook
#   itself can fail are pinned to PASS THROUGH.
#
#   SHARED-VS-SOLO — the wide refusal must NOT fire in a single-tenant clone,
#   because `git reset --hard <prev-tag>` is the documented prod-rollback step.
#
#   COMMIT GUARD — `bsq commit`'s co-edit audit (`_coedit_audit` /
#   `_report_coedit`, T-0752) is the guard that already worked; adding more must
#   not disarm it.
#
# Run:  bash scripts/hooks/test_worktree_guard.sh

set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLONE_ROOT="$(cd "${HOOK_DIR}/../.." && pwd)"
GUARD="${HOOK_DIR}/bsq-worktree-guard.sh"
PRETOOL="${HOOK_DIR}/bsq-pretooluse-git.py"
BSQ="${CLONE_ROOT}/scripts/cli/bsq"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/worktree-guard-selftest.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Scratch repos must run NO hooks of their own — point core.hooksPath at an
# empty dir so this selftest can never be reading the real clone's .githooks.
EMPTY_HOOKS="$WORK/_nohooks"; mkdir -p "$EMPTY_HOOKS"

pass=0; fail=0
ok()   { pass=$((pass+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

# ---------------------------------------------------------------------------
# Fixture. Three sessions' uncommitted work in one tree, one of it a brand-new
# module that a DIFFERENT session has already `git add`ed — the vector that let
# a two-parent stash sweep untracked files.
#
#   app.py          T-0430  tracked worktree edit, unstaged     (peer P)
#   admin.py        T-0420  tracked worktree edit, unstaged     (peer R)
#   tg_outbound.py  T-0393  brand-new module, STAGED            (peer Q)
#   scratch.py      T-0393  brand-new module, NOT added         (peer Q)
# ---------------------------------------------------------------------------
make_repo() {
    local d="$WORK/$1"; rm -rf "$d"; mkdir -p "$d"
    (
        cd "$d" || exit 1
        git init -q .
        git config user.email dev@bot-squad.local
        git config user.name bsq-selftest
        git config core.hooksPath "$EMPTY_HOOKS"
        printf 'base\n' > app.py
        printf 'base\n' > admin.py
        git add app.py admin.py
        git commit -qm "T-0001 base"
        printf '# T-0430 outage marker\n' >> app.py
        printf '# T-0420 amend gap\n' >> admin.py
        printf '# T-0393 outbound sender\n' > tg_outbound.py
        git add tg_outbound.py
        printf '# T-0393 scratch notes\n' > scratch.py
    ) >/dev/null 2>&1
    write_roster "$d"
}

# The roster the guard would otherwise fetch from the worker socket. Three
# ACTIVE sessions whose cwd is this repo — so it reads as a shared clone — and
# a ticket→SID map that lets attribution resolve real peer SIDs.
write_roster() {
    local repo="$1"
    ROSTER="$WORK/roster.json"
    python3 - "$repo" "$ROSTER" <<'PY'
import json, sys
repo, out = sys.argv[1], sys.argv[2]
rows = [
    {"sid": "S-peer-P-p1", "status": "active", "cwd": repo,
     "task_id": "T-0430", "extra_task_ids": []},
    {"sid": "S-peer-R-p2", "status": "active", "cwd": repo,
     "task_id": "T-0420", "extra_task_ids": []},
    {"sid": "S-peer-Q-p3", "status": "active", "cwd": repo,
     "task_id": "T-0393", "extra_task_ids": []},
]
open(out, "w").write(json.dumps(rows))
PY
    export BOT_SQUAD_GUARD_ROSTER_FILE="$ROSTER"
}

run_guard() { # run_guard <repo> <verb> <args...> -> sets RC / OUT
    local repo="$1"; shift
    OUT="$(cd "$WORK/$repo" && bash "$GUARD" check "$@" 2>&1)"
    RC=$?
}

hook_json() { # hook_json <cwd> <command> [hook] -> sets RC / OUT / OUT_STDOUT
    local cwd="$1" cmd="$2" hook="${3:-$PRETOOL}"
    local payload
    payload="$(python3 -c '
import json,sys
print(json.dumps({"tool_name":"Bash","cwd":sys.argv[1],"tool_input":{"command":sys.argv[2]}}))
' "$cwd" "$cmd")"
    OUT_STDOUT="$(printf '%s' "$payload" | python3 "$hook" 2>"$WORK/.stderr")"
    RC=$?
    OUT="$(cat "$WORK/.stderr")"
}

hook_raw() { # hook_raw <raw stdin> -> sets RC / OUT
    OUT="$(printf '%s' "$1" | python3 "$PRETOOL" 2>&1)"
    RC=$?
}

# survivors <repo> -> how many of the four fixture files are still intact
survivors() {
    local d="$WORK/$1" n=0
    grep -q 'T-0430 outage marker' "$d/app.py" 2>/dev/null && n=$((n+1))
    grep -q 'T-0420 amend gap' "$d/admin.py" 2>/dev/null && n=$((n+1))
    [ -f "$d/tg_outbound.py" ] && n=$((n+1))
    [ -f "$d/scratch.py" ] && n=$((n+1))
    echo "$n"
}

echo "── REFUSE: git stash, the incident itself ──"

make_repo r1
run_guard r1 stash
if [ "$RC" = "3" ]; then ok "bare 'git stash' with peers' work is refused (rc=3)"
else bad "bare 'git stash' was NOT refused (rc=$RC)" "$OUT"; fi

# The whole point: the peer-STAGED untracked module must be NAMED, since that is
# the file class the incident showed vanishing silently out of a two-parent stash.
if printf '%s' "$OUT" | grep -q 'tg_outbound.py'; then ok "the peer-staged untracked module is enumerated by name"
else bad "tg_outbound.py missing from the refusal" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'app.py' && printf '%s' "$OUT" | grep -q 'admin.py'; then
    ok "both peers' tracked worktree edits are enumerated"
else bad "tracked peer edits missing from the refusal" "$OUT"; fi

# Attribution must come from CONTENT — the ticket tags in the diff — and must
# resolve to the peer SID that owns the ticket.
if printf '%s' "$OUT" | grep -q 'T-0430' && printf '%s' "$OUT" | grep -q 'T-0393'; then
    ok "paths are attributed by ticket tag found in the diff (content, not adjacency)"
else bad "no content-based attribution in the refusal" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'S-peer-P-p1' && printf '%s' "$OUT" | grep -q 'S-peer-Q-p3'; then
    ok "tags resolve to the live peer SIDs that own them"
else bad "ticket tags did not resolve to owning sessions" "$OUT"; fi
# Line counts appear as damage size, never as the attribution.
if printf '%s' "$OUT" | grep -q 'UNATTRIBUTED — open the diff, do not guess' \
   || ! printf '%s' "$OUT" | grep -q 'scratch.py'; then
    ok "an untracked file a bare stash would NOT take is not listed as at risk"
else bad "bare stash claimed an untracked file it does not sweep" "$OUT"; fi

make_repo r2; run_guard r2 stash push
if [ "$RC" = "3" ]; then ok "'git stash push' (no pathspec) is refused"
else bad "'git stash push' was not refused (rc=$RC)" "$OUT"; fi

make_repo r3; run_guard r3 stash save "wip on backend/app.py"
if [ "$RC" = "3" ]; then ok "'git stash save <message>' is refused (message is not a pathspec)"
else bad "'git stash save' slipped through — message read as a pathspec? (rc=$RC)" "$OUT"; fi

make_repo r4; run_guard r4 stash -u
if [ "$RC" = "3" ]; then ok "'git stash -u' is refused"
else bad "'git stash -u' was not refused (rc=$RC)" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'scratch.py'; then ok "'-u' widens the sweep set to the un-added file too"
else bad "'git stash -u' did not enumerate the untracked file" "$OUT"; fi

make_repo r5; run_guard r5 stash -m "backend/app.py tweak"
if [ "$RC" = "3" ]; then ok "'git stash -m <msg>' is refused (-m value not read as a pathspec)"
else bad "'git stash -m' slipped through (rc=$RC)" "$OUT"; fi

echo "── REFUSE: the stash-recovery traps ──"

for verb in pop apply drop clear branch; do
    make_repo "rv_$verb"; run_guard "rv_$verb" stash "$verb"
    if [ "$RC" = "3" ]; then ok "'git stash $verb' is refused"
    else bad "'git stash $verb' was not refused (rc=$RC)" "$OUT"; fi
    # A guard that blocks without offering the safe path gets worked around, so
    # every one of these must hand back the read-only extraction recipe.
    if printf '%s' "$OUT" | grep -q "git show 'stash@{0}:<path>'"; then
        ok "the $verb refusal hands back the read-only extract recipe"
    else bad "$verb refusal lacks the extract-one-path recovery recipe" "$OUT"; fi
done

echo "── REFUSE: the other four families (scope widened 13:52Z) ──"

make_repo f1; run_guard f1 reset --hard
if [ "$RC" = "3" ]; then ok "'git reset --hard' is refused"
else bad "'git reset --hard' was not refused (rc=$RC)" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'tg_outbound.py'; then ok "reset --hard names the peer-STAGED new module it would delete"
else bad "reset --hard did not enumerate the staged new file" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'no undo'; then ok "the reset refusal says there is no stash to recover from"
else bad "reset refusal reused the stash wording" "$OUT"; fi

make_repo f2a; run_guard f2a reset --merge
if [ "$RC" = "3" ]; then ok "'git reset --merge' is refused (it rewrites the worktree too)"
else bad "'git reset --merge' was not refused (rc=$RC)" "$OUT"; fi
make_repo f2b; run_guard f2b reset --keep
if [ "$RC" = "3" ]; then ok "'git reset --keep' is refused"
else bad "'git reset --keep' was not refused (rc=$RC)" "$OUT"; fi

make_repo f2; run_guard f2 reset --hard HEAD~1
if [ "$RC" = "3" ]; then ok "'git reset --hard HEAD~1' is refused"
else bad "'git reset --hard <rev>' was not refused (rc=$RC)" "$OUT"; fi

make_repo f3; run_guard f3 checkout -- .
if [ "$RC" = "3" ]; then ok "'git checkout -- .' is refused"
else bad "'git checkout -- .' was not refused (rc=$RC)" "$OUT"; fi

make_repo f4; run_guard f4 checkout .
if [ "$RC" = "3" ]; then ok "'git checkout .' (no --) is refused"
else bad "'git checkout .' was not refused (rc=$RC)" "$OUT"; fi

make_repo f5; run_guard f5 restore .
if [ "$RC" = "3" ]; then ok "'git restore .' is refused"
else bad "'git restore .' was not refused (rc=$RC)" "$OUT"; fi

make_repo f6; run_guard f6 restore --source=HEAD --staged --worktree .
if [ "$RC" = "3" ]; then ok "'git restore --source=HEAD --staged --worktree .' is refused"
else bad "wide restore with --source was not refused (rc=$RC)" "$OUT"; fi

make_repo f7; run_guard f7 clean -fd
if [ "$RC" = "3" ]; then ok "'git clean -fd' is refused"
else bad "'git clean -fd' was not refused (rc=$RC)" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'scratch.py'; then ok "clean names the never-added peer file — invisible to every other guard"
else bad "clean did not enumerate the untracked peer file" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'never in git at all'; then ok "the clean refusal says the loss is unrecoverable"
else bad "clean refusal reused the stash wording" "$OUT"; fi

make_repo f8; run_guard f8 clean -f -d
if [ "$RC" = "3" ]; then ok "'git clean -f -d' (split flags) is refused"
else bad "split-flag clean was not refused (rc=$RC)" "$OUT"; fi

make_repo f9
(cd "$WORK/f9" && git branch -q other) >/dev/null 2>&1
run_guard f9 checkout -f other
if [ "$RC" = "3" ]; then ok "'git checkout -f <branch>' is refused (-f discards local modifications)"
else bad "forced branch switch was not refused (rc=$RC)" "$OUT"; fi
# … and the same switch WITHOUT -f stays allowed. Asserting only the refusal
# would pass just as well if the guard blocked every branch switch in the clone.
run_guard f9 checkout other
if [ "$RC" = "0" ]; then ok "…while the same switch without -f is still allowed"
else bad "the -f arm over-blocked plain 'git checkout <branch>' (rc=$RC)" "$OUT"; fi

echo "── ALLOW: must not over-block ──"

make_repo a1
run_guard a1 stash list
if [ "$RC" = "0" ]; then ok "'git stash list' is allowed (diagnosis path)"
else bad "'git stash list' was blocked (rc=$RC)" "$OUT"; fi
run_guard a1 stash show
if [ "$RC" = "0" ]; then ok "'git stash show' is allowed (diagnosis path)"
else bad "'git stash show' was blocked (rc=$RC)" "$OUT"; fi

# ★ THE NEAR-MISS. `git checkout <branch>` destroys nothing and is an everyday
# command; blocking it would break all work in the clone.
(cd "$WORK/a1" && git branch -q other) >/dev/null 2>&1
run_guard a1 checkout other
if [ "$RC" = "0" ]; then ok "'git checkout <branch>' is ALLOWED (destroys nothing — the near-miss)"
else bad "branch switch was blocked (rc=$RC)" "$OUT"; fi
run_guard a1 checkout -b feature/new
if [ "$RC" = "0" ]; then ok "'git checkout -b <new>' is allowed"
else bad "branch creation was blocked (rc=$RC)" "$OUT"; fi
# The exact shape deploy-recipes/bot-squad/staging.sh:67 runs.
run_guard a1 checkout -B other master
if [ "$RC" = "0" ]; then ok "'git checkout -B <branch> <start>' is allowed (the deploy-recipe shape)"
else bad "recipe-shaped checkout -B was blocked (rc=$RC)" "$OUT"; fi

run_guard a1 reset
if [ "$RC" = "0" ]; then ok "'git reset' (index-only) is allowed"
else bad "plain reset was blocked (rc=$RC)" "$OUT"; fi
run_guard a1 reset --soft HEAD~1
if [ "$RC" = "0" ]; then ok "'git reset --soft' is allowed"
else bad "soft reset was blocked (rc=$RC)" "$OUT"; fi
run_guard a1 reset -- tg_outbound.py
if [ "$RC" = "0" ]; then ok "'git reset -- <path>' (unstage) is allowed — content survives"
else bad "unstage was blocked (rc=$RC)" "$OUT"; fi

run_guard a1 restore --staged tg_outbound.py
if [ "$RC" = "0" ]; then ok "'git restore --staged <path>' (index-only) is allowed"
else bad "index-only restore was blocked (rc=$RC)" "$OUT"; fi

run_guard a1 clean -nd
if [ "$RC" = "0" ]; then ok "'git clean -nd' (dry run) is allowed — the diagnosis path"
else bad "clean --dry-run was blocked (rc=$RC)" "$OUT"; fi
run_guard a1 clean -d
if [ "$RC" = "0" ]; then ok "'git clean -d' without -f is allowed (git refuses it itself)"
else bad "clean without -f was blocked (rc=$RC)" "$OUT"; fi

# Clean tree: nothing to destroy, so nothing to say.
make_repo a2
(cd "$WORK/a2" && git reset -q --hard HEAD && rm -f tg_outbound.py scratch.py) >/dev/null 2>&1
for verb in "stash -m label" "reset --hard" "checkout -- ." "restore ." "clean -fd"; do
    # shellcheck disable=SC2086
    run_guard a2 $verb
    if [ "$RC" = "0" ]; then ok "'git $verb' on a CLEAN tree is allowed (no work at risk)"
    else bad "clean-tree 'git $verb' was blocked (rc=$RC)" "$OUT"; fi
done
# … and even there, an anonymous stash is still refused: an unattributable
# artifact is unattributable whether or not a peer had work at risk.
run_guard a2 stash
if [ "$RC" = "3" ] && printf '%s' "$OUT" | grep -q 'would be anonymous'; then
    ok "…but an anonymous stash on a clean tree is still refused"
else bad "an anonymous clean-tree stash was allowed (rc=$RC)" "$OUT"; fi

# Narrow operations on a path that is nobody else's: bounded and declared.
make_repo a3
(cd "$WORK/a3" && git reset -q && printf '# T-9999 my own work\n' > mine.py && git add mine.py \
  && git checkout -q -- app.py admin.py && rm -f tg_outbound.py scratch.py) >/dev/null 2>&1
run_guard a3 stash push -m "bsq:S-me-p1:T-0826:now" -- mine.py
if [ "$RC" = "0" ]; then ok "narrow labelled 'git stash push -- <own path>' is allowed"
else bad "narrow own-path stash was blocked (rc=$RC)" "$OUT"; fi
run_guard a3 clean -f -- mine.py
if [ "$RC" = "0" ]; then ok "narrow 'git clean -f -- <own path>' is allowed"
else bad "narrow own-path clean was blocked (rc=$RC)" "$OUT"; fi

# … but a narrow operation naming a PEER's path is still refused: bounded is not
# the same as harmless.
make_repo a4
run_guard a4 checkout -- app.py
if [ "$RC" = "3" ]; then ok "narrow 'git checkout -- <PEER's path>' is refused"
else bad "narrow checkout of a peer's file slipped through (rc=$RC)" "$OUT"; fi
run_guard a4 stash push -m "bsq:S-me-p1:T-0826:now" -- app.py
if [ "$RC" = "3" ]; then ok "narrow labelled 'git stash push -- <PEER's path>' is refused"
else bad "narrow stash of a peer's file slipped through (rc=$RC)" "$OUT"; fi

# Overrides.
make_repo a5; run_guard a5 stash; rc_before=$RC
for var in BOT_SQUAD_ALLOW_WORKTREE_WIPE BOT_SQUAD_ALLOW_WIDE_STASH; do
    OUT="$(cd "$WORK/a5" && env "$var=1" bash "$GUARD" check stash -m "bsq:me:T-0826:now" 2>&1)"; RC=$?
    if [ "$rc_before" = "3" ] && [ "$RC" = "0" ]; then ok "$var=1 overrides a stash refusal"
    else bad "$var override did not work (before=$rc_before after=$RC)" "$OUT"; fi
    for verb in "reset --hard" "checkout -- ." "restore ." "clean -fd"; do
        # shellcheck disable=SC2086
        OUT="$(cd "$WORK/a5" && env "$var=1" bash "$GUARD" check $verb 2>&1)"; RC=$?
        if [ "$RC" = "0" ]; then ok "$var=1 overrides 'git $verb'"
        else bad "$var did not open 'git $verb' (rc=$RC)" "$OUT"; fi
    done
done

echo "── Every stash must SELF-IDENTIFY ──"

# The only reason the damaging stash was distinguishable from the pre-deploy one
# during recovery is that the other one named itself. So an anonymous stash is
# refused even where the peer-activity check would have allowed it.
make_repo l1
(cd "$WORK/l1" && git reset -q && printf '# T-9999 my own work\n' > mine.py && git add mine.py \
  && git checkout -q -- app.py admin.py && rm -f tg_outbound.py scratch.py) >/dev/null 2>&1
run_guard l1 stash push -- mine.py
if [ "$RC" = "3" ] && printf '%s' "$OUT" | grep -q 'would be anonymous'; then
    ok "an ANONYMOUS narrow stash is refused — it would be unattributable"
else bad "anonymous narrow stash was allowed (rc=$RC)" "$OUT"; fi
if printf '%s' "$OUT" | grep -qE 'bsq:[^:]+:[^:]+:[0-9]{4}-'; then
    ok "…and the refusal hands back a ready-made label to re-run with"
else bad "the label refusal did not offer a label" "$OUT"; fi
run_guard l1 stash push -m "bsq:S-me-p1:T-0826:2026-07-30T14:00:00Z" -- mine.py
if [ "$RC" = "0" ]; then ok "the same stash WITH a label is allowed"
else bad "a labelled narrow own-path stash was blocked (rc=$RC)" "$OUT"; fi
# `git stash save <msg>` carries its message POSITIONALLY. Read as a pathspec it
# would both narrow the sweep set and look anonymous; assert it counts as a
# label, behind the override so the wide-stash refusal is not what answers.
OUT="$(cd "$WORK/l1" && BOT_SQUAD_ALLOW_WORKTREE_WIPE=1 bash "$GUARD" check stash save "bsq:S-me-p1:T-0826:now" 2>&1)"; RC=$?
if [ "$RC" = "0" ]; then ok "'git stash save <label>' counts as labelled (message is positional)"
else bad "a labelled save was read as anonymous (rc=$RC)" "$OUT"; fi
OUT="$(cd "$WORK/l1" && BOT_SQUAD_ALLOW_WORKTREE_WIPE=1 bash "$GUARD" check stash save 2>&1)"; RC=$?
if [ "$RC" = "3" ]; then ok "…and a bare 'git stash save' is still anonymous"
else bad "an unlabelled save slipped through (rc=$RC)" "$OUT"; fi

# Short-option clusters (p531's finding: '-fd' and '-uq' are what people type).
make_repo l3
run_guard l3 stash push -qm "bsq:S-me-p1:T-0826:now" -- app.py
if [ "$RC" = "3" ] && printf '%s' "$OUT" | grep -q 'app.py'; then
    ok "'-qm <msg>' is parsed as a message: the sweep set is not narrowed to nothing"
else bad "a clustered -qm let its message be read as a pathspec (rc=$RC)" "$OUT"; fi
run_guard l3 stash -uq
if [ "$RC" = "3" ] && printf '%s' "$OUT" | grep -q 'scratch.py'; then
    ok "'-uq' is recognised as the untracked-widening form"
else bad "a clustered -uq lost its -u meaning (rc=$RC)" "$OUT"; fi
# The override buys past the peer-activity refusal, never past the label.
make_repo l2
OUT="$(cd "$WORK/l2" && BOT_SQUAD_ALLOW_WORKTREE_WIPE=1 bash "$GUARD" check stash 2>&1)"; RC=$?
if [ "$RC" = "3" ] && printf '%s' "$OUT" | grep -q 'would be anonymous'; then
    ok "the override does NOT license an anonymous stash"
else bad "override waived the label requirement (rc=$RC)" "$OUT"; fi

echo "── SHARED vs SOLO: the wide refusal must not block a prod rollback ──"

# `git reset --hard <prev-tag>` is the documented rollback step in the release
# TL's role contract, run in a single-tenant deploy clone. A guard that blocks
# an incident response is worse than the incident.
make_repo s1
python3 - "$WORK/s1" "$WORK/roster_solo.json" <<'PY'
import json, sys
repo, out = sys.argv[1], sys.argv[2]
open(out, "w").write(json.dumps([
    {"sid": "S-me-p1", "status": "active", "cwd": repo, "task_id": "T-0826",
     "extra_task_ids": []},
    {"sid": "S-elsewhere-p2", "status": "active", "cwd": "/home/other/clone",
     "task_id": "T-0001", "extra_task_ids": []},
]))
PY
OUT="$(cd "$WORK/s1" && BOT_SQUAD_GUARD_ROSTER_FILE="$WORK/roster_solo.json" \
        bash "$GUARD" check reset --hard 2>&1)"; RC=$?
if [ "$RC" = "0" ]; then ok "solo clone (1 session here): 'git reset --hard' is ALLOWED"
else bad "the guard would block a prod rollback in a single-tenant clone (rc=$RC)" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'not a shared tree'; then ok "…and says why, plus how many paths go"
else bad "solo allowance was silent" "$OUT"; fi

# An unreadable roster means we cannot know. Fail CLOSED.
OUT="$(cd "$WORK/s1" && BOT_SQUAD_GUARD_ROSTER_FILE="$WORK/does-not-exist.json" \
        bash "$GUARD" check reset --hard 2>&1)"; RC=$?
if [ "$RC" = "3" ]; then ok "unreadable roster ⇒ assume shared ⇒ still refused (fails closed)"
else bad "an unreadable roster DISARMED the guard (rc=$RC)" "$OUT"; fi

printf 'not json at all' > "$WORK/roster_bad.json"
OUT="$(cd "$WORK/s1" && BOT_SQUAD_GUARD_ROSTER_FILE="$WORK/roster_bad.json" \
        bash "$GUARD" check reset --hard 2>&1)"; RC=$?
if [ "$RC" = "3" ]; then ok "unparseable roster ⇒ assume shared ⇒ still refused (fails closed)"
else bad "an unparseable roster DISARMED the guard (rc=$RC)" "$OUT"; fi

echo "── ATTRIBUTION BUCKETS: the reaped-session class ──"

# Roster fixture: READABLE, with live sessions, none of which owns the fixture's
# tickets — i.e. those sessions have been reaped. One row deliberately carries no
# task_id, the shape that made p531 print "(roster unavailable)" for a roster
# that was perfectly available.
write_reaped_roster() {
    python3 - "$1" "$2" "${3:-shared}" <<'PY2'
import json, sys
repo, out = sys.argv[1], sys.argv[2]
rows = [{"sid": "S-me-p532", "status": "active", "cwd": repo,
         "task_id": "T-0826", "extra_task_ids": []}]
if sys.argv[3] != "solo":
    rows.append({"sid": "S-other-p9", "status": "active", "cwd": repo,
                 "task_id": None, "extra_task_ids": []})
open(out, "w").write(json.dumps(rows))
PY2
}

# ★ THE CLASS THAT PRODUCES WORK WITH NO LIVING OWNER. watchrobot's p499 died on
# 2026-07-30 holding uncommitted work in backend/core/notify.py tagged T-0408;
# its operator restored it BY HAND from stash@{0} precisely because no session
# was left alive to do it. Such a path's tag resolves to no live session, so the
# old two-counter scheme put it in NEITHER bucket: three of them printed as
# "3 path(s) would be removed — 0 attributed to another live session, 0 not
# attributable at all", a headline saying nothing is at stake. Confirmed by
# execution on both sides before this fix.
make_repo b1
write_reaped_roster "$WORK/b1" "$WORK/roster_reaped.json"
OUT="$(cd "$WORK/b1" && BOT_SQUAD_GUARD_ROSTER_FILE="$WORK/roster_reaped.json" \
        BOT_SQUAD_TASK_ID=T-0826 bash "$GUARD" check reset --hard 2>&1)"; RC=$?
if [ "$RC" = "3" ]; then ok "a sweep of reaped-session work is still refused"
else bad "reaped-session sweep was not refused (rc=$RC)" "$OUT"; fi
if printf '%s' "$OUT" | grep -q '3 UNKNOWN'; then
    ok "…and the reaped paths land in the UNKNOWN bucket, not in neither"
else bad "reaped-tag paths were counted in no bucket — the headline undercounts" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'session reaped — nobody left to notice'; then
    ok "…and each names the owning session as gone"
else bad "a reaped tag did not say the session is gone" "$OUT"; fi
# The roster WAS readable, and one row carries no task_id — the exact shape that
# made p531 report an unknown as a specific WRONG known.
if ! printf '%s' "$OUT" | grep -q 'roster unreadable'; then
    ok "a readable roster with no matching rows is NOT reported as unreadable"
else bad "'roster unreadable' printed for a roster that was read fine" "$OUT"; fi
# THE INVARIANT: foreign + mine + unknown == total swept. Its absence is why both
# projects' suites stayed green through the miscount.
if printf '%s' "$OUT" | grep -q 'INTERNAL — bucket counts'; then
    bad "the bucket invariant tripped" "$OUT"
else ok "bucket invariant holds (foreign + mine + unknown == swept)"; fi
if printf '%s' "$OUT" | grep -q '0 proven ANOTHER LIVE' \
   && printf '%s' "$OUT" | grep -q '0 proven yours'; then
    ok "…and all three numbers are printed, zeros included"
else bad "the refusal headline does not name all three buckets" "$OUT"; fi

# ★ THE ALLOW BRANCH — the acute one. A solo clone full of a DEAD peer's work
# must not be described as "yours".
make_repo b2
write_reaped_roster "$WORK/b2" "$WORK/roster_reaped_solo.json" solo
OUT="$(cd "$WORK/b2" && BOT_SQUAD_GUARD_ROSTER_FILE="$WORK/roster_reaped_solo.json" \
        BOT_SQUAD_TASK_ID=T-0826 bash "$GUARD" check reset --hard 2>&1)"; RC=$?
if [ "$RC" = "0" ]; then ok "solo gate still ALLOWS (predicate deliberately unchanged)"
else bad "the solo predicate changed (rc=$RC)" "$OUT"; fi
if ! printf '%s' "$OUT" | grep -q 'they are yours'; then
    ok "…and no longer claims the work is yours — that was an UNKNOWN stated as a KNOWN"
else bad "the allow message still asserts ownership it did not establish" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'NOBODY IS LEFT TO' \
   && printf '%s' "$OUT" | grep -q 'could not be attributed to anyone LIVE'; then
    ok "…and names the unknown count with the reaped-session consequence"
else bad "the allow message hid the unknown bucket" "$OUT"; fi
if printf '%s' "$OUT" | grep -q 'app.py' && printf '%s' "$OUT" | grep -q 'tg_outbound.py'; then
    ok "…and LISTS the unattributable paths rather than only counting them"
else bad "the unknown paths were counted but not listed" "$OUT"; fi

# A path provably MINE must land in the mine bucket, or "unknown" is just a
# synonym for "everything" and the arms above prove nothing.
make_repo b3
(cd "$WORK/b3" && git reset -q --hard HEAD && rm -f tg_outbound.py scratch.py \
  && printf '# T-0826 my own edit\n' >> app.py) >/dev/null 2>&1
OUT="$(cd "$WORK/b3" && BOT_SQUAD_GUARD_ROSTER_FILE="$WORK/roster_reaped.json" \
        BOT_SQUAD_TASK_ID=T-0826 bash "$GUARD" check checkout -- . 2>&1)"; RC=$?
if [ "$RC" = "0" ] || printf '%s' "$OUT" | grep -q '1 proven yours'; then
    ok "a path tagged with MY ticket is bucketed as mine, not as unknown"
else bad "my own tagged path was not recognised as mine" "$OUT"; fi

echo "── PreToolUse hook: the entry point that actually fires ──"

make_repo h1
hook_json "$WORK/h1" "git stash"
if [ "$RC" = "2" ]; then ok "hook BLOCKS a bare 'git stash' (rc=2 = deny)"
else bad "hook did not block a bare stash (rc=$RC)" "$OUT"; fi

for cmd in "git reset --hard" "git checkout -- ." "git restore ." "git clean -fd"; do
    hook_json "$WORK/h1" "$cmd"
    if [ "$RC" = "2" ]; then ok "hook blocks '$cmd'"
    else bad "hook did not block '$cmd' (rc=$RC)" "$OUT"; fi
done

hook_json "$WORK/h1" "git stash list"
if [ "$RC" = "0" ]; then ok "hook allows 'git stash list'"
else bad "hook blocked 'git stash list' (rc=$RC)" "$OUT"; fi

hook_json "$WORK/h1" "/usr/bin/git stash"
if [ "$RC" = "2" ]; then ok "hook blocks an absolute-path 'git' invocation"
else bad "absolute-path git stash slipped through (rc=$RC)" "$OUT"; fi

hook_json "$WORK/h1" "true && git reset --hard"
if [ "$RC" = "2" ]; then ok "hook blocks a destructive verb after a shell operator"
else bad "'&& git reset --hard' slipped through (rc=$RC)" "$OUT"; fi

hook_json "$WORK/h1" "cd $WORK/h1 && git clean -fd"
if [ "$RC" = "2" ]; then ok "hook follows a leading 'cd' to judge the right repo"
else bad "'cd <repo> && git clean -fd' slipped through (rc=$RC)" "$OUT"; fi

# `git -C <dir>` acts on <dir>. Judging it as cwd guards the wrong tree in both
# directions — and it is the shape the deploy recipes use.
hook_json "$WORK/_nohooks" "git -C $WORK/h1 stash"
if [ "$RC" = "2" ]; then ok "hook follows 'git -C <dir>' to the repo actually targeted"
else bad "'git -C <shared clone> stash' escaped judgement (rc=$RC)" "$OUT"; fi

hook_json "$WORK/_nohooks" "git --work-tree=$WORK/h1 --git-dir=$WORK/h1/.git stash"
if [ "$RC" = "2" ]; then ok "hook follows '--work-tree' to the repo actually targeted"
else bad "'--work-tree <shared clone>' escaped judgement (rc=$RC)" "$OUT"; fi

# The reverse direction of the same bug: a harmless command aimed at a CLEAN
# repo must not be refused because the caller's cwd happens to be dirty.
make_repo h2
(cd "$WORK/h2" && git reset -q --hard HEAD && rm -f tg_outbound.py scratch.py) >/dev/null 2>&1
write_roster "$WORK/h1"
hook_json "$WORK/h1" "git -C $WORK/h2 stash -m lbl"
if [ "$RC" = "0" ]; then ok "…and does NOT refuse a stash aimed at a CLEAN repo from a dirty cwd"
else bad "'-C <clean repo>' was judged against the caller's dirty tree (rc=$RC)" "$OUT"; fi

# Which tree is about to be rewritten must be DERIVABLE. Guessing is the same
# error in a quieter form, so these fail closed.
hook_json "$WORK/h1" "git -C /nonexistent/definitely-not-here stash"
if [ "$RC" = "2" ]; then ok "an unresolvable '-C <dir>' fails CLOSED"
else bad "unresolvable -C fell back to the caller's cwd (rc=$RC)" "$OUT"; fi
hook_json "$WORK/h1" "git --git-dir=$WORK/h1/.git stash"
if [ "$RC" = "2" ]; then ok "'--git-dir' with no '--work-tree' fails CLOSED (target not derivable)"
else bad "--git-dir without --work-tree was guessed at (rc=$RC)" "$OUT"; fi

# A redirection is punctuation for the shell, not an argument to git. Read as an
# argument it becomes a bogus SUBCOMMAND and the classification is wrong.
hook_json "$WORK/h1" "git stash 2>&1"
if [ "$RC" = "2" ]; then ok "hook blocks 'git stash 2>&1' (redirection is not a subcommand)"
else bad "'git stash 2>&1' slipped through (rc=$RC)" "$OUT"; fi
hook_json "$WORK/h1" "git stash list 2>/dev/null"
if [ "$RC" = "0" ]; then ok "hook allows 'git stash list 2>/dev/null'"
else bad "redirected 'stash list' was blocked (rc=$RC)" "$OUT"; fi

# ★ A HEREDOC BODY IS NOT QUOTED, so `inside_quotes` cannot see it — and writing
# a test, a doc or a runbook ABOUT these verbs means putting them at command
# position inside one. Found the hard way: this guard refused the very Bash call
# that was adding the tests above. Same social failure as blocking prose, one
# quoting style over, and a guard people route around is worse than none.
hook_json "$WORK/h1" "cat > /tmp/doc.md <<'EOF'
Never run git reset --hard in the shared clone.
git clean -fd is unrecoverable.
EOF"
if [ "$RC" = "0" ]; then ok "a heredoc body naming the verbs passes through"
else bad "writing a doc ABOUT the guarded verbs was refused (rc=$RC)" "$OUT"; fi
# … but a real command AFTER the heredoc terminator must still be caught, or the
# fix is a bypass wearing a fix's clothes.
hook_json "$WORK/h1" "cat > /tmp/doc.md <<'EOF'
git reset --hard
EOF
git stash"
if [ "$RC" = "2" ]; then ok "…while a real command AFTER the terminator is still caught"
else bad "a heredoc was used to smuggle a live command past the hook (rc=$RC)" "$OUT"; fi

# Prose must survive. Agents write about this incident constantly; a hook that
# blocks the ticket note describing it is a hook everyone disables.
hook_json "$WORK/h1" "bsq ticket note T-0826 \"a bare git stash swept every session's work; git reset --hard would too\""
if [ "$RC" = "0" ]; then ok "hook allows the verbs quoted inside prose"
else bad "hook blocked a ticket note that merely mentions them (rc=$RC)" "$OUT"; fi

hook_json "$WORK/h1" "git show 'stash@{0}:backend/app.py' > /tmp/mine"
if [ "$RC" = "0" ]; then ok "hook allows the read-only extract recipe it recommends"
else bad "hook blocked its own recommended recovery command (rc=$RC)" "$OUT"; fi

# An inline env assignment sits between the command position and `git`. If the
# pattern misses it the command runs UNGUARDED — a silent bypass, and the worse
# direction of the two, so both are asserted.
hook_json "$WORK/h1" "FOO=1 git stash"
if [ "$RC" = "2" ]; then ok "hook blocks an env-prefixed stash (no bypass via 'VAR=1 git stash')"
else bad "'FOO=1 git stash' BYPASSED the hook (rc=$RC)" "$OUT"; fi

hook_json "$WORK/h1" "BOT_SQUAD_ALLOW_WORKTREE_WIPE=1 git clean -fd"
if [ "$RC" = "0" ]; then ok "hook honours an inline BOT_SQUAD_ALLOW_WORKTREE_WIPE=1 override"
else bad "inline override not honoured (rc=$RC)" "$OUT"; fi

# A compound command must be judged as a whole: an override on one verb does not
# license a different, unoverridden one later in the same line.
hook_json "$WORK/h1" "BOT_SQUAD_ALLOW_WORKTREE_WIPE=1 git stash -m lbl && git reset --hard"
if [ "$RC" = "2" ]; then ok "hook still blocks an unoverridden verb later in the same command"
else bad "second verb escaped via the first one's override (rc=$RC)" "$OUT"; fi

echo "── BLAST RADIUS: this hook sees EVERY Bash call in EVERY session here ──"

# ★ The everyday commands. `checkout` and `reset` are now in the predicate, so
# these matter more than they did when this guarded stash alone.
(cd "$WORK/h1" && git branch -q other) >/dev/null 2>&1
for cmd in "git status --short" "git log --oneline -3" "git diff HEAD" \
           "git add -- app.py" "git commit -m 'T-0001 wip'" "git show HEAD:app.py" \
           "git checkout other" "git checkout -b feature/x" "git reset --soft HEAD~1" \
           "git reset -- app.py" "git restore --staged app.py" "git clean -nd" \
           "git stash list" "git fetch origin" "git rev-parse HEAD"; do
    hook_json "$WORK/h1" "$cmd"
    if [ "$RC" = "0" ] && [ -z "$OUT_STDOUT" ]; then ok "ordinary git passes through untouched: $cmd"
    else bad "hook interfered with '$cmd' (rc=$RC stdout='$OUT_STDOUT')" "$OUT"; fi
done

# Non-git commands entirely — the overwhelming majority of Bash calls here.
for cmd in "ls -la" "python3 -m pytest -q" "grep -rn stash ." "rm -rf /tmp/scratch" \
           "echo 'git stash is what broke it'" "npm run clean" "make clean" \
           "bsq team status" "docker restore-something" "cat scripts/hooks/bsq-worktree-guard.sh"; do
    hook_json "$WORK/h1" "$cmd"
    if [ "$RC" = "0" ] && [ -z "$OUT_STDOUT" ]; then ok "non-git command passes through untouched: $cmd"
    else bad "hook interfered with '$cmd' (rc=$RC stdout='$OUT_STDOUT')" "$OUT"; fi
done

# A payload for any other tool must be ignored without inspection.
OUT_STDOUT="$(printf '%s' '{"tool_name":"Read","tool_input":{"file_path":"/tmp/git stash"}}' \
    | python3 "$PRETOOL" 2>"$WORK/.stderr")"; RC=$?
if [ "$RC" = "0" ]; then ok "a non-Bash tool payload is ignored (rc=0)"
else bad "hook acted on a non-Bash tool (rc=$RC)" "$(cat "$WORK/.stderr")"; fi

# Every way the hook itself can fail must fail OPEN.
hook_raw 'this is not json'
if [ "$RC" = "0" ]; then ok "malformed stdin fails OPEN (rc=0)"
else bad "malformed stdin wedged the tool (rc=$RC)" "$OUT"; fi
hook_raw ''
if [ "$RC" = "0" ]; then ok "empty stdin fails OPEN (rc=0)"
else bad "empty stdin wedged the tool (rc=$RC)" "$OUT"; fi
hook_raw '{"tool_name":"Bash"}'
if [ "$RC" = "0" ]; then ok "a Bash payload with no command fails OPEN (rc=0)"
else bad "missing command wedged the tool (rc=$RC)" "$OUT"; fi
hook_raw '{"tool_name":"Bash","tool_input":{"command":null}}'
if [ "$RC" = "0" ]; then ok "a null command fails OPEN (rc=0)"
else bad "null command wedged the tool (rc=$RC)" "$OUT"; fi
hook_raw '{"tool_name":"Bash","tool_input":{"command":"git stash '"'"'unterminated}}'
if [ "$RC" = "0" ]; then ok "unparseable JSON around a real stash fails OPEN (rc=0)"
else bad "a malformed payload wedged the tool (rc=$RC)" "$OUT"; fi

# ★ THE EXIT-CODE CONTRACT, BY SABOTAGE. A guard that did not run is not a guard
# that said yes: for a GUARDED verb a broken guard must REFUSE, while everything
# else in the clone keeps working. watchrobot shipped the opposite — their guard
# exited 127 when missing, which read as "not a refusal" — so deleting or
# RENAMING the guard silently disarmed every session, and they had renamed it
# themselves earlier in the same ticket. Each arm gets a positive control first,
# proving the copied hook really does reach its neighbour; without that, "it
# refused" could just mean "the harness never ran anything".
for scenario in missing syntax_error bogus_code crashing hanging; do
    d="$WORK/hook_$scenario"; rm -rf "$d"; mkdir -p "$d"
    cp "$PRETOOL" "$d/bsq-pretooluse-git.py"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$d/bsq-worktree-guard.sh"
    hook_json "$WORK/h1" "git stash" "$d/bsq-pretooluse-git.py"
    if [ "$RC" = "0" ]; then ok "positive control ($scenario): a working stand-in guard ALLOWS (rc=0)"
    else bad "positive control failed — a clean 'exit 0' guard did not allow (rc=$RC)" "$OUT"; fi

    case "$scenario" in
        missing)      rm -f "$d/bsq-worktree-guard.sh" ;;
        # bash exits 2 on a syntax error, which is exactly why the guard has no
        # third opinion code — otherwise this would read as a verdict.
        syntax_error) printf '#!/usr/bin/env bash\nif then fi (\n' > "$d/bsq-worktree-guard.sh" ;;
        bogus_code)   printf '#!/usr/bin/env bash\nexit 42\n' > "$d/bsq-worktree-guard.sh" ;;
        crashing)     printf '#!/usr/bin/env bash\nnot_a_command_at_all\nexit 1\n' > "$d/bsq-worktree-guard.sh" ;;
        hanging)      printf '#!/usr/bin/env bash\nsleep 600\n' > "$d/bsq-worktree-guard.sh" ;;
    esac

    # The timeout branch is provable in seconds via the env seam rather than by
    # waiting out the 90s production value — an untimed timeout branch is where
    # a "fails closed" claim goes stale.
    export BOT_SQUAD_GUARD_TIMEOUT=2
    t0=$(date +%s)
    hook_json "$WORK/h1" "git stash" "$d/bsq-pretooluse-git.py"
    t1=$(date +%s)
    if [ "$RC" = "2" ]; then ok "SABOTAGE $scenario guard — a GUARDED verb is REFUSED (fails closed)"
    else bad "SABOTAGE $scenario guard SILENTLY DISARMED the guard (rc=$RC)" "$OUT"; fi
    if [ "$scenario" = "hanging" ]; then
        if [ "$((t1 - t0))" -lt 10 ]; then ok "…and returned in $((t1 - t0))s rather than hanging the Bash tool"
        else bad "the hanging guard held the tool for $((t1 - t0))s"; fi
    fi

    # THE OTHER HALF, and the one that matters more: the same broken guard must
    # be invisible to everything that is not a guarded verb. A hook that sees
    # every Bash call has to degrade PERMISSIVE outside its own remit.
    broke=0
    for cmd in "ls -la" "npm test" "git status --short" "git log --oneline -3" \
               "python3 -m pytest -q" "git add -- app.py" "git commit -m wip" \
               "echo 'a bare git stash swept the tree'"; do
        hook_json "$WORK/h1" "$cmd" "$d/bsq-pretooluse-git.py"
        [ "$RC" = "0" ] || broke=$((broke+1))
    done
    if [ "$broke" = "0" ]; then ok "…while non-guarded commands still run untouched with the guard $scenario"
    else bad "a $scenario guard blocked $broke/8 non-guarded commands — the whole clone stops"; fi

    # The cost of the contract, asserted rather than discovered later: a broken
    # guard also refuses the HARMLESS forms of the five verbs, because only the
    # guard knows `git checkout <branch>` destroys nothing. That is the right
    # trade — a broken check is not evidence of safety — but it means a missing
    # guard is loud, not silent, which is the whole point.
    hook_json "$WORK/h1" "git checkout other" "$d/bsq-pretooluse-git.py"
    if [ "$RC" = "2" ]; then ok "…and even a harmless 'git checkout <branch>' is refused: the failure is LOUD"
    else bad "a $scenario guard silently permitted a guarded verb (rc=$RC)" "$OUT"; fi
    unset BOT_SQUAD_GUARD_TIMEOUT
done

# ★ SABOTAGE THE HOOK ITSELF — the OTHER direction from the block above. When
# the GUARD fails we know the command is a guarded verb, so we refuse. When THIS
# FILE fails we know nothing at all, and blocking on nothing would take out every
# session's Bash. `main()` is wrapped in `except Exception: exit(0)` for that,
# which is right and, read alone, proves nothing — a later refactor can move a
# statement outside the try and the symptom is EVERY SESSION'S BASH FAILING in
# this repo. So break it on purpose and watch work continue. Each arm gets a
# positive control first, because an unmodified copy that already passed
# everything through would "prove" fail-open without running any of it.
sab="$WORK/sabotage"; mkdir -p "$sab"
sabotage_arm() { # sabotage_arm <label> <sed program> <command to try>
    local label="$1" prog="$2" cmd="$3"
    cp "$PRETOOL" "$sab/hook.py"
    hook_json "$WORK/h1" "$cmd" "$sab/hook.py"
    local ctl_rc=$RC
    sed -i "$prog" "$sab/hook.py"
    hook_json "$WORK/h1" "$cmd" "$sab/hook.py"
    if [ "$RC" = "0" ]; then ok "SABOTAGE $label — the tool still runs (rc=0)"
    else bad "SABOTAGE $label WEDGED the tool (rc=$RC, unsabotaged rc=$ctl_rc)" "$OUT"; fi
}
# A hard throw on the very first line of main().
sabotage_arm "raise at the top of main()" \
    's/^def main():$/def main():\n    raise RuntimeError("sabotage")/' "ls -la"
# The same, on a command that WOULD have been refused: a broken hook must not
# block, even when the thing it was about to block was real.
sabotage_arm "raise on a command that would be refused" \
    's/^def main():$/def main():\n    raise RuntimeError("sabotage")/' "git stash"
# A NameError deep in the classification path — the shape a bad refactor makes.
sabotage_arm "NameError inside the verb loop" \
    's/        verb = m.group("verb")/        verb = undefined_symbol_from_a_bad_refactor/' "git stash"
# A syntax error: python never even reaches the try block. This one exits 1, not
# 0 — what matters is that Claude Code treats any non-2 exit as non-blocking, so
# assert the code is NOT the deny code.
cp "$PRETOOL" "$sab/hook.py"
printf 'def (\n' >> "$sab/hook.py"
hook_json "$WORK/h1" "git stash" "$sab/hook.py"
if [ "$RC" != "2" ]; then ok "SABOTAGE syntax error — exits $RC, which is not the deny code"
else bad "a hook that cannot even parse still DENIED the tool call" "$OUT"; fi
rm -rf "$sab"

# Cost. The hook runs before EVERY Bash call, so its pass-through cost is paid
# by every session all day. Bound it rather than trusting it — and measure the
# hook alone: building the payload costs a python startup of its own, which is
# the test harness's bill, not the hook's. Reported against the bare-interpreter
# floor, because that part is unavoidable for any python hook.
PAYLOAD="$(python3 -c '
import json
print(json.dumps({"tool_name":"Bash","cwd":"/tmp","tool_input":{"command":"ls -la"}}))')"
t0=$(date +%s%N)
for _ in $(seq 20); do printf '%s' "$PAYLOAD" | python3 "$PRETOOL" >/dev/null 2>&1; done
t1=$(date +%s%N)
hook_ms=$(( (t1 - t0) / 20000000 ))
t0=$(date +%s%N)
for _ in $(seq 20); do python3 -c pass; done
t1=$(date +%s%N)
floor_ms=$(( (t1 - t0) / 20000000 ))
if [ "$hook_ms" -lt 100 ]; then
    ok "pass-through cost ${hook_ms}ms per Bash call (python floor ${floor_ms}ms; bound: <100ms)"
else bad "pass-through cost ${hook_ms}ms per Bash call — too slow for every-call use (floor ${floor_ms}ms)"; fi

echo "── END-TO-END + CONTROL ARM: the work actually survives, and would not have ──"

# The only assertion that matters in the end: with the hook in front of them,
# the peers' files are still in the tree afterwards. And WITHOUT the guard the
# same command destroys them — proving the arms differ is the only way to know
# the guard, and not the fixture, is doing the work.
#
#   family                     fixture files destroyed with no guard
#   git stash                  app.py, admin.py, tg_outbound.py       (3 of 4)
#   git reset --hard           app.py, admin.py, tg_outbound.py       (3 of 4)
#   git checkout -- .          app.py, admin.py                       (2 of 4)
#   git restore .              app.py, admin.py                       (2 of 4)
#   git clean -fd              scratch.py                             (1 of 4)
run_e2e() { # run_e2e <label> <command> <expected survivors after control run>
    local label="$1" cmd="$2" want="$3" g c

    make_repo "e2e_g"
    hook_json "$WORK/e2e_g" "$cmd"
    g="$(survivors e2e_g)"
    if [ "$RC" = "2" ] && [ "$g" = "4" ]; then
        ok "GUARDED   '$label' blocked; all 4 fixture files intact"
    else bad "'$label' did not protect the tree (rc=$RC survivors=$g/4)" "$OUT"; fi

    make_repo "e2e_c"
    (cd "$WORK/e2e_c" && eval "$cmd") >/dev/null 2>&1
    c="$(survivors e2e_c)"
    if [ "$c" = "$want" ]; then
        ok "CONTROL   '$label' WITHOUT the guard destroyed $((4 - c)) of 4 (arms differ)"
    else bad "control arm for '$label' destroyed $((4 - c)), expected $((4 - want))"; fi
}

run_e2e "git stash"         "git stash"         1
run_e2e "git reset --hard"  "git reset --hard"  1
run_e2e "git checkout -- ." "git checkout -- ." 2
run_e2e "git restore ."     "git restore ."     2
run_e2e "git clean -fd"     "git clean -fd"     3

# The property that made the real incident silent: afterwards the tree reads
# CLEAN. No error, no warning, nothing pointing at the cause.
make_repo c1
(cd "$WORK/c1" && git stash) >/dev/null 2>&1
if [ -z "$(cd "$WORK/c1" && git status --porcelain | grep -v '^??')" ]; then
    ok "CONTROL   the swept tree reads CLEAN — the silence is reproduced too"
else bad "control tree was not clean after the sweep"; fi

echo "── COMMIT guard (bsq co-edit audit, T-0752) still armed ──"

# Our commit guard is NOT .githooks/pre-commit — that file ships to managed
# project clones but is inert in this one (core.hooksPath is unset here and
# .git/hooks holds only samples). The live one is `bsq commit`'s co-edit audit,
# so that is what gets re-verified. It is driven directly rather than through
# `bsq commit`, because every real bsq verb hits the live worker socket.
commit_guard_out="$(BSQ_PATH="$BSQ" WORK="$WORK" python3 - <<'PY' 2>&1
import importlib.machinery, importlib.util, os, subprocess, sys
from pathlib import Path

work = Path(os.environ["WORK"]) / "coedit"
slug = "selftest"
sid = "S-selftest-dev-p1"
repo = work / "repo"
bsq_root = work / "bsq_root"
repo.mkdir(parents=True, exist_ok=True)

def git(*a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)

git("init", "-q", ".")
git("config", "user.email", "dev@bot-squad.local")
git("config", "user.name", "bsq-selftest")
(repo / "mine.py").write_text("base\n")
(repo / "peer.py").write_text("peer base\n")
git("add", "mine.py", "peer.py")
git("commit", "-qm", "T-0001 base")
head = git("rev-parse", "HEAD").stdout.strip()

# A peer is demonstrably mid-edit in this tree (a dirty file I never baselined).
(repo / "mine.py").write_text("base\n# T-0826 my edit\n")
(repo / "peer.py").write_text("peer base\n# T-0430 a peer is editing right now\n")

os.environ["BOT_SQUAD"] = str(bsq_root)
# `bsq` has no .py suffix, so spec_from_file_location returns None unless the
# loader is named explicitly.
loader = importlib.machinery.SourceFileLoader("_bsq_selftest", os.environ["BSQ_PATH"])
spec = importlib.util.spec_from_file_location("_bsq_selftest", os.environ["BSQ_PATH"], loader=loader)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

# Place the baseline snapshot through bsq's OWN path helpers rather than
# re-deriving them here: `_sanitize_sid` keeps hyphens, and a hand-built store
# path that misses by one character makes the audit find no baseline and report
# nothing — which is indistinguishable from the guard having been removed.
store = mod.worktree_store(slug, sid)
snap = mod._snapshot_path(store, "mine.py")
snap.parent.mkdir(parents=True, exist_ok=True)
# Byte-identical to HEAD, so `--hunks` would isolate nothing. That plus the
# dirty peer file is the `✗` refusal, the one --ack cannot open.
snap.write_text("base\n")

meta = {"mine.py": {"head": head, "ts": "2026-07-30T13:00:00Z"}}
refusing, blocking, caution = mod._coedit_audit(str(repo), slug, sid, ["mine.py"], meta)
print("REFUSING:%d" % len(refusing))
print("HAS_X:%d" % int(any("✗" in ln for ln in refusing)))
print("NAMES_PEER:%d" % int(any("peer.py" in ln for ln in refusing)))

# The report must actually abort, not merely describe.
pid = os.fork()
if pid == 0:
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    try:
        mod._report_coedit(refusing, blocking, caution, False, "redo", ["mine.py"])
    except SystemExit as e:
        os._exit(e.code if isinstance(e.code, int) else 1)
    os._exit(0)
_, status = os.waitpid(pid, 0)
print("REPORT_EXIT:%d" % (status >> 8))

# GREEN CONTROL: with no peer mid-edit, the same call must NOT refuse — a guard
# that refuses unconditionally is indistinguishable from a broken one.
git("checkout", "-q", "--", "peer.py")
refusing2, _, _ = mod._coedit_audit(str(repo), slug, sid, ["mine.py"], meta)
print("GREEN_REFUSING:%d" % len(refusing2))
PY
)"
if printf '%s' "$commit_guard_out" | grep -q '^HAS_X:1'; then
    ok "commit guard still produces its ✗ refusal (co-edit audit armed)"
else bad "commit guard no longer refuses" "$commit_guard_out"; fi
if printf '%s' "$commit_guard_out" | grep -q '^NAMES_PEER:1'; then
    ok "commit guard still names the peer file dirty in the tree"
else bad "commit guard lost its peer enumeration" "$commit_guard_out"; fi
if printf '%s' "$commit_guard_out" | grep -q '^REPORT_EXIT:3'; then
    ok "commit guard still ABORTS the commit (exit 3), not just warns"
else bad "commit guard no longer aborts" "$commit_guard_out"; fi
if printf '%s' "$commit_guard_out" | grep -q '^GREEN_REFUSING:0'; then
    ok "green control: with no peer mid-edit the commit guard allows"
else bad "commit guard refuses unconditionally — green control failed" "$commit_guard_out"; fi

echo
printf 'worktree-guard selftest: \033[32m%d passed\033[0m, \033[31m%d failed\033[0m\n' "$pass" "$fail"
[ "$fail" = "0" ] || exit 1
