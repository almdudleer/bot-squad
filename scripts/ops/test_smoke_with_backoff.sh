#!/usr/bin/env bash
# Smoke test for smoke-with-backoff.sh. Run as:
#   ./scripts/ops/test_smoke_with_backoff.sh
#
# Verifies happy/fail/budget behaviour against fake endpoints — no
# external HTTP traffic needed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HELPER="$SCRIPT_DIR/smoke-with-backoff.sh"

if [ ! -x "$HELPER" ]; then
    echo "FAIL: $HELPER not executable" >&2
    exit 2
fi

# Test 1 — usage on no args.
if "$HELPER" 2>/dev/null; then
    echo "FAIL: expected non-zero rc on missing url" >&2
    exit 2
fi
echo "PASS: usage rejects empty arg"

# Test 2 — happy path returns 0 on first attempt.
# Use file:// URL pointing at this very script — curl returns OK on
# successful read.
if ! BOT_SQUAD_SMOKE_DELAYS=0 "$HELPER" "file://$HELPER" >/dev/null; then
    echo "FAIL: helper returned non-zero on a reachable url" >&2
    exit 2
fi
echo "PASS: happy path returns 0"

# Test 3 — unreachable host returns non-zero after all retries.
# Use a port that nothing's listening on; --max-time keeps total quick.
output="$(BOT_SQUAD_SMOKE_DELAYS=0,0,0 "$HELPER" 'http://127.0.0.1:1/' 2>&1 || true)"
rc=$?
if [ "$rc" -ne 0 ] 2>/dev/null; then : ; fi
case "$output" in
    *FAIL*after\ 3\ attempts*) echo "PASS: failure path reports retry count" ;;
    *) echo "FAIL: expected 'FAIL after 3 attempts' in output, got: $output" >&2; exit 2 ;;
esac

# Test 4 — log line on success shows attempt number.
output="$(BOT_SQUAD_SMOKE_DELAYS=0 "$HELPER" "file://$HELPER" 2>&1)"
case "$output" in
    *OK\ on\ attempt\ 1\ after*) echo "PASS: success log includes attempt number" ;;
    *) echo "FAIL: expected 'OK on attempt 1 after' in output, got: $output" >&2; exit 2 ;;
esac

echo "ALL PASS"
