#!/usr/bin/env bash
# Smoke test for rebuild_api_venv.sh. Run as:
#   ./scripts/ops/test_rebuild_api_venv.sh
#
# Builds a THROWAWAY venv (never the live install one) against the
# install's api/ source and asserts it lands byte-for-byte on
# api/requirements.lock — so the mechanics are exercised without ever
# touching /home/www/bot-squad/api/.venv. Needs network access (pip) and
# takes a couple of minutes; not meant for a tight inner loop.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HELPER="$SCRIPT_DIR/rebuild_api_venv.sh"
API_SRC="/home/www/bot-squad/api"
LOCK="$API_SRC/requirements.lock"

if [ ! -x "$HELPER" ]; then
    echo "FAIL: $HELPER not executable" >&2
    exit 2
fi
if [ ! -f "$LOCK" ]; then
    echo "SKIP: no $LOCK on this host — nothing to build against" >&2
    exit 0
fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
VENV="$SCRATCH/venv"

if ! "$HELPER" "$VENV" "$API_SRC" > "$SCRATCH/rebuild.log" 2>&1; then
    echo "FAIL: rebuild_api_venv.sh exited non-zero:" >&2
    tail -40 "$SCRATCH/rebuild.log" >&2
    exit 2
fi
echo "PASS: rebuild_api_venv.sh reported success"

if ! grep -q "matches .* exactly" "$SCRATCH/rebuild.log"; then
    echo "FAIL: success output did not confirm a lock match" >&2
    exit 2
fi
echo "PASS: script's own drift check confirmed a lock match"

# Independent re-check: freeze the built venv and diff it against the lock
# ourselves, rather than trusting only the script's self-report.
"$VENV/bin/pip" freeze --exclude-editable | grep -E '^[A-Za-z0-9_.-]+==' \
    | tr 'A-Z_' 'a-z-' | sort > "$SCRATCH/installed.txt"
grep -E '^[A-Za-z0-9_.-]+==' "$LOCK" \
    | tr 'A-Z_' 'a-z-' | sort > "$SCRATCH/locked.txt"

if ! diff -u "$SCRATCH/locked.txt" "$SCRATCH/installed.txt"; then
    echo "FAIL: independent freeze does not match the lock" >&2
    exit 2
fi
echo "PASS: independent freeze matches $LOCK exactly"

if ! "$VENV/bin/python" -c "import app; print(app.__file__)" | grep -q "^$API_SRC/app/"; then
    echo "FAIL: 'import app' did not resolve under $API_SRC" >&2
    exit 2
fi
echo "PASS: editable install resolves app to $API_SRC"

echo "ALL PASS"
