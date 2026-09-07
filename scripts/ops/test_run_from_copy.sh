#!/usr/bin/env bash
# Test for run_from_copy.sh (T-1067).
#
# Test 1 is the POSITIVE CONTROL for the hazard itself: a plain `bash` run
# of a script that is rewritten IN PLACE (same inode) mid-run diverges to
# the NEW file's bytes at exit, with no error — proving there was something
# to fix and that this test can actually go red. Test 2 proves run_from_copy
# is immune to the identical rewrite. Test 1 must PASS (i.e. the hazard must
# reproduce) for test 2 to mean anything.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$SCRIPT_DIR/run_from_copy.sh"
FAIL=0

if [ ! -x "$WRAPPER" ]; then
  echo "FAIL: $WRAPPER not executable" >&2
  exit 2
fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# Build two byte-identical-up-to-the-tail variants: same padding (so the
# byte OFFSET of the trailing echo is identical in both), different final
# message. A script large enough that bash's own read buffering cannot have
# already pulled the tail into memory before the sleep executes.
build() {
  local out="$1" tail_msg="$2" i
  { printf '#!/usr/bin/env bash\n'
    for i in $(seq 1 400); do
      printf '# pad line %04d filler filler filler filler\n' "$i"
    done
    printf 'sleep 3\n'
    printf 'echo "%s"\n' "$tail_msg"
  } > "$out"
}
build "$SCRATCH/v_before.sh" "BEFORE-REWRITE"
build "$SCRATCH/v_after.sh" "AFTER-REWRITE"

# --- Test 1: unprotected `bash` run — the hazard must reproduce ---
LIVE="$SCRATCH/live.sh"
cp "$SCRATCH/v_before.sh" "$LIVE"
OUT1="$SCRATCH/out1.log"
bash "$LIVE" > "$OUT1" 2>&1 &
PID1=$!
sleep 1
cp "$SCRATCH/v_after.sh" "$LIVE"   # in-place rewrite, same inode, mid-sleep
wait "$PID1"
GOT1="$(cat "$OUT1")"
if [ "$GOT1" = "AFTER-REWRITE" ]; then
  echo "PASS: unprotected run diverged to the rewritten bytes (hazard reproduced, got '$GOT1')"
elif [ "$GOT1" = "BEFORE-REWRITE" ]; then
  echo "FAIL: unprotected run did NOT diverge (got '$GOT1') — this environment's bash buffering makes the control invalid; do not trust test 2 below" >&2
  FAIL=1
else
  echo "FAIL: unprotected run produced neither expected value: '$GOT1'" >&2
  FAIL=1
fi

# --- Test 2: run_from_copy — identical rewrite, must NOT diverge ---
LIVE2="$SCRATCH/live2.sh"
cp "$SCRATCH/v_before.sh" "$LIVE2"
OUT2="$SCRATCH/out2.log"
"$WRAPPER" "$LIVE2" > "$OUT2" 2>&1 &
PID2=$!
sleep 1
cp "$SCRATCH/v_after.sh" "$LIVE2"  # rewrite the SOURCE, not the copy
wait "$PID2"
GOT2="$(tail -1 "$OUT2")"
if [ "$GOT2" = "BEFORE-REWRITE" ]; then
  echo "PASS: run_from_copy did not diverge under the identical rewrite (got '$GOT2')"
else
  echo "FAIL: run_from_copy diverged (got '$GOT2') — the mitigation did not hold" >&2
  FAIL=1
fi

# --- Test 3: the sha256 exported/announced matches the source at cp time ---
EXPECT_SHA="$(sha256sum "$SCRATCH/v_before.sh" | awk '{print $1}')"
if grep -q "sha256=$EXPECT_SHA" "$OUT2"; then
  echo "PASS: wrapper announced the correct sha256 of the snapshot"
else
  echo "FAIL: expected sha256=$EXPECT_SHA in wrapper output, got:" >&2
  cat "$OUT2" >&2
  FAIL=1
fi

# --- Test 4: exit code propagates ---
FAILING="$SCRATCH/failing.sh"
printf '#!/usr/bin/env bash\nexit 7\n' > "$FAILING"
if "$WRAPPER" "$FAILING" >/dev/null 2>&1; then
  echo "FAIL: expected nonzero exit to propagate" >&2
  FAIL=1
else
  rc=$?
  if [ "$rc" -eq 7 ]; then
    echo "PASS: exit code 7 propagated"
  else
    echo "FAIL: expected rc 7, got rc $rc" >&2
    FAIL=1
  fi
fi

# --- Test 5: missing target is a clean, early failure ---
if "$WRAPPER" "$SCRATCH/does-not-exist.sh" >/dev/null 2>&1; then
  echo "FAIL: expected nonzero exit for a missing target" >&2
  FAIL=1
else
  echo "PASS: missing target fails cleanly"
fi

if [ "$FAIL" -eq 0 ]; then
  echo "ALL PASS"
else
  exit 2
fi
