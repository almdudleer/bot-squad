#!/usr/bin/env bash
#
# rebuild_api_venv.sh — (re)build a host python venv for the api suite as an
# EXACT replica of api/requirements.lock (T-1004), never a drifting
# `pip install` against pyproject.toml's `>=` floors.
#
# T-1025: /home/www/bot-squad/api/.venv was hand-built once (2026-05-10) and
# never touched again. Every one of its 30 packages had drifted from the
# deployed image by the time this was measured (fastapi 0.136.1 vs 0.141.1,
# pytest 9.0.3 vs 9.1.1, etc.), and it was outright missing 3 — cryptography,
# tomlkit, pyflakes — which are hard runtime/dev deps, so a host-venv api run
# gave 953 false reds (952x "No module named 'tomlkit'", 1x cryptography).
#
# Hand-topping-up just the missing packages would have "fixed" the loud
# failure into a silent one: a venv that imports fine but runs a DIFFERENT
# dependency set than production (operator ruling, T-1025). Since
# api/requirements.lock now exists and IS the deployed set (T-1004,
# pip-freeze'd from the running container), the venv can instead be a
# faithful, reproducible replica of it — same install-then-diff recipe as
# api/Dockerfile, so drift is a loud failure here too, not a silent one.
#
# Usage:
#   scripts/ops/rebuild_api_venv.sh [venv_path] [api_src_dir]
#
# venv_path defaults to /home/www/bot-squad/api/.venv (the install's host
# venv). api_src_dir defaults to /home/www/bot-squad/api (the install's own
# api/ — the code that venv's editable install must point at, since that is
# what a host-venv run against the INSTALL is meant to exercise; it is also
# where the venv's pre-existing editable finder already pointed). Pass both
# args to build/verify a throwaway copy instead — test_rebuild_api_venv.sh
# does exactly that, so this script is exercised without ever touching the
# live install.
#
# Safe to re-run any time the lock changes: recreates the venv from scratch
# (rm -rf + python3 -m venv) rather than layering installs on top of
# whatever was there, so there is no accumulated cruft to reason about.
#
# Exit codes: 0 on a venv whose installed set matches the lock exactly;
# non-zero (from `set -e` or the explicit drift check) on any failure.

set -euo pipefail

VENV="${1:-/home/www/bot-squad/api/.venv}"
API_DIR="${2:-/home/www/bot-squad/api}"
LOCK="$API_DIR/requirements.lock"

if [ ! -f "$LOCK" ]; then
    echo "rebuild_api_venv: no lock at $LOCK" >&2
    exit 2
fi

echo "rebuild_api_venv: target venv = $VENV"
echo "rebuild_api_venv: api src     = $API_DIR"
echo "rebuild_api_venv: lock        = $LOCK"

rm -rf "$VENV"
python3 -m venv "$VENV"

"$VENV/bin/pip" install --no-cache-dir --upgrade pip --quiet
"$VENV/bin/pip" install --no-cache-dir -r "$LOCK"
"$VENV/bin/pip" install --no-cache-dir --no-deps -e "$API_DIR[dev]"

# Same drift check as api/Dockerfile's T-1004 build step: names lowercased
# and underscores normalised, because pip freeze and PyPI disagree with the
# lock's own casing (pydantic_core vs pydantic-core, PyYAML vs pyyaml).
installed="$(mktemp)"
locked="$(mktemp)"
trap 'rm -f "$installed" "$locked"' EXIT

"$VENV/bin/pip" freeze --exclude-editable | grep -E '^[A-Za-z0-9_.-]+==' \
    | tr 'A-Z_' 'a-z-' | sort > "$installed"
grep -E '^[A-Za-z0-9_.-]+==' "$LOCK" \
    | tr 'A-Z_' 'a-z-' | sort > "$locked"

if ! diff -u "$locked" "$installed"; then
    echo "rebuild_api_venv: LOCK DRIFT — installed set does not match $LOCK. Refusing." >&2
    exit 1
fi

echo "rebuild_api_venv: OK — $VENV matches $LOCK exactly ($(wc -l < "$locked") packages)."
