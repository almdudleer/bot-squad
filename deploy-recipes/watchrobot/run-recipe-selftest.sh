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
# Runs in ~5s, builds nothing, deploys nothing: every recipe here is a scratch
# file in a temp dir.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$HERE/run-recipe.sh"
TMP="$(mktemp -d -t wr-run-recipe-selftest-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

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

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
