#!/usr/bin/env bash
# Unit tests for the per_user_worker_unit checkpoint in install.sh (T-0067).
#
# Cases:
#   1. missing source unit → die_struct fires with the per_user_worker_unit
#      checkpoint and a "repo clone incomplete" hint.
#   2. fresh install → unit is dropped at $BOTSQUAD_USER_UNIT_DEST with
#      mode 0644 and byte-identical to the source.
#   3. idempotent re-run on byte-identical dest → mtime preserved (no rewrite).
#   4. content drift → re-run overwrites a stale dest.
#
# Side-effecting paths are all redirected via $BOTSQUAD_USER_UNIT_DEST so
# the test never touches /etc/systemd/user/.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# Source install.sh with main() suppressed.
TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"

export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_USER_UNIT_DEST="$WORK/etc/systemd/user/bot-squad-user-worker.service"
mkdir -p "$BOTSQUAD_INSTALL_DIR/systemd" "$(dirname "$BOTSQUAD_USER_UNIT_DEST")"

# shellcheck disable=SC1090
source "$TMP_SH"
ensure_state_dir
log()  { :; }
warn() { :; }

# --- case 1: missing source unit fails loudly --------------------------------
rm -f "$BOTSQUAD_INSTALL_DIR/systemd/bot-squad-user-worker.service"
if ( step_per_user_worker_unit ) 2>/dev/null; then
  fail "case1: expected step to die on missing source unit"
fi
pass "case1: missing source unit → step fails"

# --- case 2: fresh install drops the unit ------------------------------------
SRC="$BOTSQUAD_INSTALL_DIR/systemd/bot-squad-user-worker.service"
SRC_BODY='[Unit]
Description=bot-squad per-user worker (tmux ops)
[Service]
ExecStart=/usr/bin/true
[Install]
WantedBy=default.target
'
printf '%s' "$SRC_BODY" > "$SRC"

# Remove the dest so we exercise the install path, not idempotency.
rm -f "$BOTSQUAD_USER_UNIT_DEST"
step_per_user_worker_unit || fail "case2: step failed unexpectedly"
[[ -f "$BOTSQUAD_USER_UNIT_DEST" ]] || fail "case2: dest unit not created"
diff <(cat "$SRC") <(cat "$BOTSQUAD_USER_UNIT_DEST") >/dev/null || \
  fail "case2: dest content differs from source"

# Mode check — must be 0644 (POSIX install -m 0644).
mode="$(stat -c '%a' "$BOTSQUAD_USER_UNIT_DEST" 2>/dev/null || stat -f '%Lp' "$BOTSQUAD_USER_UNIT_DEST")"
[[ "$mode" = "644" ]] || fail "case2: expected mode 644, got $mode"
pass "case2: fresh install drops byte-identical 0644 unit"

# --- case 3: idempotent re-run preserves mtime -------------------------------
# Force an old mtime so the comparison is meaningful even if filesystem
# granularity is whole-second.
touch -d "2020-01-01" "$BOTSQUAD_USER_UNIT_DEST"
before_mtime="$(stat -c '%Y' "$BOTSQUAD_USER_UNIT_DEST" 2>/dev/null || stat -f '%m' "$BOTSQUAD_USER_UNIT_DEST")"
step_per_user_worker_unit || fail "case3: step failed unexpectedly"
after_mtime="$(stat -c '%Y' "$BOTSQUAD_USER_UNIT_DEST" 2>/dev/null || stat -f '%m' "$BOTSQUAD_USER_UNIT_DEST")"
[[ "$before_mtime" = "$after_mtime" ]] \
  || fail "case3: mtime changed ($before_mtime → $after_mtime); idempotency broken"
pass "case3: byte-identical re-run preserves mtime"

# --- case 4: content drift triggers rewrite ----------------------------------
printf 'STALE\n' > "$BOTSQUAD_USER_UNIT_DEST"
step_per_user_worker_unit || fail "case4: step failed unexpectedly"
diff <(cat "$SRC") <(cat "$BOTSQUAD_USER_UNIT_DEST") >/dev/null \
  || fail "case4: stale dest was not overwritten"
pass "case4: stale dest is overwritten with fresh source"

echo "all per_user_worker_unit cases passed"
