#!/usr/bin/env bash
# NEGATIVE TEST for run-recipe.sh (T-0427 DoD: "prove it by making the recipe
# exit non-zero deliberately and confirming the wrapper says so").
#
# A wrapper that claims to report exit codes honestly, and has never been made
# to report a failure, is an untested claim — which is precisely the state the
# original hand-rolled pipeline was in when it printed `DEPLOY_RC=0` for a
# deploy that had exited 22.
#
# Every case below asserts TWO things, because the ticket's defect had two
# halves: the wrapper's own exit status, and — separately — that the literal
# string `DEPLOY_RC=0` never appears in the output of a failing run. The second
# matters because a human or an agent reads the output, not `$?`.
#
#   A  recipe exits 0                          -> rc 0, says DEPLOY_RC=0
#   B  recipe exits 22, silently               -> rc 22, never says DEPLOY_RC=0
#   C  recipe prints FATAL then exits 22       -> rc 22 (the original case)
#   D  recipe prints FATAL then exits 0        -> NON-ZERO (the falsified one:
#                                                a green rc contradicted by the
#                                                log must not be believed)
#   E  recipe emits 50k lines then exits 22    -> rc 22 (a long/noisy run cannot
#                                                lose the status, and nothing is
#                                                filtered out of the log)
#   F  recipe is killed by SIGKILL             -> non-zero
#   G  recipe path does not exist              -> non-zero, no run
#   H  target name is unknown                  -> non-zero, no run
#   I  `prod` without WR_PROD_CONFIRM          -> refuses (the freeze guard)
#   J  `staging --dry-run`                     -> rc 0, resolves to the LIVE
#                                                tracked recipe, deploys nothing
#                                                (the only way to exercise
#                                                target-NAME resolution without
#                                                running a deploy)
#   K  `prod --dry-run`                        -> rc 0 without WR_PROD_CONFIRM,
#                                                because it cannot deploy
#
# L-O cover T-0654: the wrapper used to take its log from a bare `mktemp`, i.e.
# from /tmp — which is NOT writable in the mount namespace a deploy job runs in
# (`/` is mounted ro there). The log name became the empty string, `tee ""`
# wrote nothing, and the FATAL cross-check then grepped a file that did not
# exist, got zero, and printed `DEPLOY_RC=0 — deploy OK`. Cases A-K could not
# see any of it: they run from an ordinary agent shell, where /tmp is writable.
#
#   L  unwritable TMPDIR, FATAL then exit 0    -> NON-ZERO. Before the fix this
#                                                printed DEPLOY_RC=0 — measured.
#   M  unwritable TMPDIR, clean recipe         -> rc 0 AND a real log with the
#                                                recipe's output in it (a green
#                                                that skipped the check is the
#                                                defect, not the fix)
#   N  no writable log dir anywhere            -> refuses loudly, runs nothing
#   O  log deleted before the cross-check      -> NON-ZERO. The same blindness
#                                                reached without mktemp: "no
#                                                FATAL found" and "nowhere to
#                                                look" must not be one answer.
#
#   P  THIS script with an unwritable TMPDIR   -> refuses at the mktemp with
#                                                rc 91, never reaches a case, so
#                                                nothing is written to / (T-0675)
#
# Runs in ~5s, builds nothing, deploys nothing: every recipe here is a scratch
# file in a temp dir.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$HERE/run-recipe.sh"

# A failed `mktemp -d` is a REFUSAL here, not a continuation (T-0675; same form
# and same rc as the guard T-0655 put on prod-rollback-selftest.sh).
#
# `set -u` does not catch this and never could: the variable IS assigned — to
# the empty string. And this script is deliberately without `set -e`, so the
# failure carried straight on. Every scratch path below is built as "$TMP/…",
# so an EMPTY $TMP turns a sandbox-relative path into an ABSOLUTE one and the
# writes land in the FILESYSTEM ROOT of whatever box this runs on.
#
# Measured before this guard existed, with `mktemp` shimmed to fail and the run
# confined to a bwrap sandbox whose / was a scratch dir: the script ran to
# completion (9 passed, 9 failed) and left NINE entries at the sandbox root —
# ok.sh, silentfail.sh, fatal0.sh, fatal22.sh, noisy.sh, suicide.sh, eatlog.sh,
# noisy-run.log and the directory fake-bot-squad-root/. Under uid 1000 on a real
# box those writes are Permission denied and the suite merely reddens, which is
# exactly why it survived; run once as root and it litters /.
TMP="$(mktemp -d -t wr-run-recipe-selftest-XXXXXX)"
if [ -z "${TMP:-}" ] || [ ! -d "$TMP" ]; then
    echo "FATAL: mktemp -d produced no usable directory (TMP='${TMP:-}', TMPDIR='${TMPDIR:-<unset>}')." >&2
    echo "       Refusing to run. Every scratch path here is built as \"\$TMP/<name>\", so an empty" >&2
    echo "       TMP makes them absolute (/ok.sh, /fatal0.sh, /fake-bot-squad-root/, …) and this" >&2
    echo "       selftest would write into the filesystem root." >&2
    echo "       A deploy namespace mounts / read-only, so /tmp is not writable there — set TMPDIR" >&2
    echo "       to a writable directory and re-run." >&2
    exit 91
fi

# The trap gets the same question asked of it. `rm -rf ""` is harmless today —
# and that is the problem: a silent no-op is indistinguishable from a cleanup
# that worked. It says which one happened instead.
_cleanup() {
    if [ -n "${TMP:-}" ] && [ -d "$TMP" ]; then
        rm -rf "$TMP"
    else
        echo "WARN: no cleanup performed — TMP was '${TMP:-}', which is not a directory." >&2
    fi
}
trap _cleanup EXIT

pass=0
fail=0

mkrecipe() { printf '%s\n' "$2" > "$TMP/$1.sh"; chmod +x "$TMP/$1.sh"; }

# assert <name> <expected_rc> <deploy_rc_0: yes=must be absent | no=must be
#                              present | skip=don't care> <cmd...>
# "skip" is for --dry-run, which exits 0 but deliberately does NOT print
# DEPLOY_RC=0 — nothing was deployed, so claiming a deploy rc would be the same
# species of false report this wrapper exists to prevent.
assert() {
    local name="$1" want="$2" forbid0="$3"; shift 3
    local out rc ok=1
    out=$("$@" 2>&1)
    rc=$?
    if [ "$want" = "nonzero" ]; then
        [ "$rc" -ne 0 ] || { echo "FAIL $name: expected a non-zero rc, got 0"; ok=0; }
    else
        [ "$rc" -eq "$want" ] || { echo "FAIL $name: expected rc=$want, got rc=$rc"; ok=0; }
    fi
    if [ "$forbid0" = "yes" ] && grep -q 'DEPLOY_RC=0' <<<"$out"; then
        echo "FAIL $name: output contains 'DEPLOY_RC=0' for a FAILED run — this is the exact defect"
        ok=0
    fi
    if [ "$forbid0" = "no" ] && ! grep -q 'DEPLOY_RC=0' <<<"$out"; then
        echo "FAIL $name: a successful run did not report DEPLOY_RC=0"
        ok=0
    fi
    if [ "$ok" -eq 1 ]; then
        echo "ok   $name (rc=$rc)"
        pass=$((pass + 1))
    else
        sed 's/^/      | /' <<<"$out" | tail -20
        fail=$((fail + 1))
    fi
}

echo "== run-recipe.sh selftest =="
echo "wrapper: $WRAPPER"
[ -x "$WRAPPER" ] || { echo "FAIL: $WRAPPER is not executable"; exit 1; }

# ── A ───────────────────────────────────────────────────────────────────────
mkrecipe ok 'echo "[fake] all good"; exit 0'
assert "A clean recipe reports DEPLOY_RC=0" 0 no \
    bash "$WRAPPER" "$TMP/ok.sh"

# ── B: the plain case the one-liner already got wrong ───────────────────────
mkrecipe silentfail 'echo "[fake] doing things"; exit 22'
assert "B exit 22 is reported as 22" 22 yes \
    bash "$WRAPPER" "$TMP/silentfail.sh"

# ── C: the original observed case, verbatim shape ──────────────────────────
mkrecipe fatal22 'echo "[staging] FATAL: /api/version never answered within ~90s of recreate" >&2; exit 22'
assert "C FATAL + exit 22 is reported as 22" 22 yes \
    bash "$WRAPPER" "$TMP/fatal22.sh"

# ── D: rc and log disagree — the wrapper must not pick the comfortable one ──
mkrecipe fatal0 'echo "[staging] FATAL: something broke" >&2; exit 0'
assert "D FATAL with a zero exit is NOT reported as success" nonzero yes \
    bash "$WRAPPER" "$TMP/fatal0.sh"

# ── E: volume must not swallow the status, and nothing may be filtered ──────
mkrecipe noisy 'i=0; while [ $i -lt 50000 ]; do echo "#$i building layer"; i=$((i+1)); done; exit 22'
assert "E 50k lines then exit 22 is still 22" 22 yes \
    bash "$WRAPPER" "$TMP/noisy.sh"
# and the log kept every line — the companion defect was `grep -v` in the
# pipeline hiding all 50k buildkit progress lines.
NOISY_LOG="$TMP/noisy-run.log"
WR_RUN_LOG="$NOISY_LOG" bash "$WRAPPER" "$TMP/noisy.sh" >/dev/null 2>&1
lines=$(wc -l < "$NOISY_LOG")
if [ "$lines" -ge 50000 ]; then
    echo "ok   E2 log kept all output ($lines lines, unfiltered)"
    pass=$((pass + 1))
else
    echo "FAIL E2: log has only $lines lines, expected >=50000 — output is being dropped"
    fail=$((fail + 1))
fi

# ── F: killed by a signal ──────────────────────────────────────────────────
mkrecipe suicide 'echo "[fake] about to die"; kill -9 $$'
assert "F SIGKILLed recipe is a failure" nonzero yes \
    bash "$WRAPPER" "$TMP/suicide.sh"

# ── G/H/I: refusals ────────────────────────────────────────────────────────
assert "G missing recipe path is refused" nonzero yes \
    bash "$WRAPPER" "$TMP/does-not-exist.sh"
assert "H unknown target is refused" nonzero yes \
    bash "$WRAPPER" not-a-target
assert "I prod without WR_PROD_CONFIRM is refused" nonzero yes \
    env -u WR_PROD_CONFIRM bash "$WRAPPER" prod

# ── J/K: --dry-run resolves the real targets and deploys nothing ────────────
# This is the only way to exercise the `staging` / `prod` NAME resolution — the
# path that an agent actually types — without running a deploy. It must resolve
# to the LIVE tracked recipe, since that is the file the worker executes.
assert "J --dry-run staging resolves and deploys nothing" 0 skip \
    bash "$WRAPPER" staging --dry-run
J_OUT=$(bash "$WRAPPER" staging --dry-run 2>&1)
if grep -q "deploy-recipes/watchrobot/staging.sh" <<<"$J_OUT" \
   && grep -q "DRY RUN" <<<"$J_OUT"; then
    echo "ok   J2 dry run named the tracked recipe"
    pass=$((pass + 1))
else
    echo "FAIL J2: dry run did not resolve to deploy-recipes/watchrobot/staging.sh"
    sed 's/^/      | /' <<<"$J_OUT"
    fail=$((fail + 1))
fi
assert "K --dry-run prod needs no WR_PROD_CONFIRM (it cannot deploy)" 0 skip \
    env -u WR_PROD_CONFIRM bash "$WRAPPER" prod --dry-run

# ── L-O: an UNWRITABLE TMPDIR (T-0654) ──────────────────────────────────────
# The condition every case above is blind to, because they inherit a writable
# /tmp from the shell running them. /proc/nonexistent is the stand-in for the
# ro-mounted / of a deploy namespace: mktemp cannot create anything under it.
# FAKE_ROOT gives the wrapper's fallback somewhere writable that is inside this
# selftest's own temp dir, so the run leaves nothing behind in the data dir.
NOTMP=/proc/nonexistent
FAKE_ROOT="$TMP/fake-bot-squad-root"

assert "L unwritable TMPDIR: FATAL with a zero exit is still NOT success" nonzero yes \
    env TMPDIR="$NOTMP" BOT_SQUAD_ROOT="$FAKE_ROOT" bash "$WRAPPER" "$TMP/fatal0.sh"

# M is the control in the other direction: an unwritable TMPDIR must not turn a
# healthy deploy into a refusal. And the rc alone would not prove it — a wrapper
# that silently skipped the cross-check would also pass — so this asserts the
# log EXISTS and holds the recipe's output.
assert "M unwritable TMPDIR: a clean recipe still succeeds" 0 no \
    env TMPDIR="$NOTMP" BOT_SQUAD_ROOT="$FAKE_ROOT" bash "$WRAPPER" "$TMP/ok.sh"
M_LOG=$(find "$FAKE_ROOT" -name 'wr-deploy-ok.sh-*.log' 2>/dev/null | head -1)
if [ -n "$M_LOG" ] && grep -q '\[fake\] all good' "$M_LOG"; then
    echo "ok   M2 the fallback log is real and holds the recipe's output"
    pass=$((pass + 1))
else
    echo "FAIL M2: no fallback log with the recipe's output — the run reported a green without recording one"
    fail=$((fail + 1))
fi

# N: nowhere writable at all. The wrapper may not quietly carry on with an empty
# log name; it has to refuse before running anything.
N_OUT=$(env TMPDIR="$NOTMP" BOT_SQUAD_ROOT="$NOTMP" bash "$WRAPPER" "$TMP/ok.sh" 2>&1)
N_RC=$?
if [ "$N_RC" -ne 0 ] && ! grep -q 'DEPLOY_RC=0' <<<"$N_OUT" && ! grep -q '\[fake\] all good' <<<"$N_OUT"; then
    echo "ok   N no writable log dir is refused before the recipe runs (rc=$N_RC)"
    pass=$((pass + 1))
else
    echo "FAIL N: expected a refusal with no recipe output, got rc=$N_RC"
    sed 's/^/      | /' <<<"$N_OUT" | tail -20
    fail=$((fail + 1))
fi

# O: the cross-check's own blind spot, reached without touching mktemp — the log
# is created fine and then disappears. "grep found no FATAL" and "grep had no
# file" must not collapse into the same green.
mkrecipe eatlog 'echo "[fake] working"; rm -f "$WR_RUN_LOG"; exit 0'
assert "O a missing log at cross-check time is a refusal, not 'no FATALs'" nonzero yes \
    env WR_RUN_LOG="$TMP/eaten.log" bash "$WRAPPER" "$TMP/eatlog.sh"


# ── P: an unwritable TMPDIR refuses instead of writing into / (T-0675) ───────
# The guard at the top of this file is the only thing between a failed
# `mktemp -d` and nine files in the filesystem root, and a guard that has never
# been made to fire is an untested claim. So: run THIS script again with TMPDIR
# unwritable. The child must refuse with the guard's OWN rc, name the reason,
# and never reach the case list. It exits at the guard, so it cannot recurse;
# WR_RRS_NO_RECURSE is the backstop for the day the guard regresses.
#
# The sentinel is deliberately NOT the name prod-rollback-selftest.sh uses: one
# shared mute switch would silently skip this case in both files the day
# anything exports it.
#
# "any non-zero rc" would be VACUOUS here — a child that ran the whole suite and
# merely failed it also exits non-zero. It must be 91, the guard's own code.
if [ -z "${WR_RRS_NO_RECURSE:-}" ]; then
    P_OUT="$(env TMPDIR=/proc/nonexistent WR_RRS_NO_RECURSE=1 \
                 bash "${BASH_SOURCE[0]}" 2>&1)"
    P_RC=$?
    if [ "$P_RC" = "91" ]; then
        echo "ok   P unwritable TMPDIR: refused with the mktemp guard's own rc=91"
        pass=$((pass + 1))
    else
        echo "FAIL P: expected rc 91 from the mktemp guard, got $P_RC"
        sed 's/^/      | /' <<<"$P_OUT" | tail -10
        fail=$((fail + 1))
    fi
    if grep -qF 'mktemp -d produced no usable directory' <<<"$P_OUT"; then
        echo "ok   P2 the refusal names the failed mktemp"
        pass=$((pass + 1))
    else
        echo "FAIL P2: refused without naming the reason"
        fail=$((fail + 1))
    fi
    if grep -qF '== run-recipe.sh selftest ==' <<<"$P_OUT"; then
        echo "FAIL P3: the child started the case list anyway — the guard let it through"
        fail=$((fail + 1))
    else
        echo "ok   P3 the child never reached a single case (nothing was written)"
        pass=$((pass + 1))
    fi
fi

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
