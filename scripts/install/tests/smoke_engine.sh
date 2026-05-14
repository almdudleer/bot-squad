#!/usr/bin/env bash
# Focused smoke for the install.sh checkpoint engine.
#
# Sources install.sh in a mode where every step_* function is a no-op,
# then asserts the state file and idempotency behavior. Designed to run
# on the dev box (or in CI) without touching apt / docker / sudo.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Drive the script with a state dir under WORK and bypass the
# mothership + any prompts. The step_* functions are stubbed below by
# re-defining them after sourcing.
export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_OPERATOR_SESSION="bs-smoke-engine-$$"

# Sourcing install.sh would run main(); guard with a sentinel.
# Trick: rewrite main to a no-op via env, then call run_checkpoint directly.
# Simpler: extract just the engine into a fixture. But we want to test the
# real engine, not a copy. Solution: use a sentinel env var that install.sh
# checks before running main.
#
# install.sh does `main "$@"` unconditionally. To run it without
# triggering main, copy into a temp file with the last line patched.

TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"

# shellcheck disable=SC1090
source "$TMP_SH"

# Re-stub all step_* to no-ops.
for step in "${STEPS[@]}"; do
  eval "step_${step}() { :; }"
done

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

ensure_state_dir

# --- Test 1: clean run completes all checkpoints in order ----------------
log() { :; }  # quiet
for step in "${STEPS[@]}"; do
  run_checkpoint "$step"
done
expected_lines="${#STEPS[@]}"
actual_lines="$(wc -l < "$STATE_FILE")"
[[ "$actual_lines" -eq "$expected_lines" ]] \
  || fail "expected $expected_lines completed checkpoints, got $actual_lines"

# State file content matches STEPS in order.
diff <(printf '%s\n' "${STEPS[@]}") "$STATE_FILE" \
  || fail "state file content does not match STEPS order"
pass "clean run completes all ${#STEPS[@]} checkpoints, order preserved"

# --- Test 2: re-run is a no-op (skips everything) ------------------------
SKIPS=0
for step in "${STEPS[@]}"; do
  if checkpoint_done "$step"; then
    SKIPS=$((SKIPS+1))
  fi
done
[[ "$SKIPS" -eq "${#STEPS[@]}" ]] \
  || fail "expected all ${#STEPS[@]} steps to be done, only $SKIPS are"
pass "re-run sees all checkpoints as done (idempotency holds)"

# --- Test 3: partial failure + resume ------------------------------------
rm -f "$STATE_FILE"; touch "$STATE_FILE"
# Mark first 5 checkpoints as done.
for step in "${STEPS[@]:0:5}"; do
  checkpoint_mark "$step"
done

# Make the 7th step fail (index 6).
fail_step="${STEPS[6]}"
eval "step_${fail_step}() { return 1; }"

# Drive the runner manually, catching the failure.
failed_at=""
set +e
trap - ERR  # disable on_err for this test (it would exit + emit struct block)
for step in "${STEPS[@]}"; do
  run_checkpoint "$step" 2>/dev/null
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    failed_at="$step"
    break
  fi
done
set -e
[[ "$failed_at" = "$fail_step" ]] \
  || fail "expected failure at $fail_step, got '$failed_at'"
# State file should have the first 5 (pre-existing) + step #6 (which
# passed cleanly because we stubbed it earlier and re-stubbed only #7).
# Critically: the failing step #7 must NOT be marked done.
done_count="$(wc -l < "$STATE_FILE")"
[[ "$done_count" -eq 6 ]] \
  || fail "expected 6 checkpoints recorded as done after fail at index 6, got $done_count"
grep -qxF "$fail_step" "$STATE_FILE" \
  && fail "failing step $fail_step must NOT be in state file but is"
pass "partial failure at step #7 leaves state file with first 6 checkpoints (failing step not marked done)"

# --- Test 4: post-fix re-run resumes from the failed checkpoint ----------
# Restore the failing step.
eval "step_${fail_step}() { :; }"
trap on_err ERR  # re-enable error trap
for step in "${STEPS[@]}"; do
  run_checkpoint "$step" >/dev/null
done
done_count="$(wc -l < "$STATE_FILE")"
[[ "$done_count" -eq "${#STEPS[@]}" ]] \
  || fail "after fix + re-run, expected ${#STEPS[@]} done, got $done_count"
pass "post-fix re-run resumes from failed checkpoint to completion"

echo ""
echo "ENGINE SMOKE: all assertions passed"
