#!/usr/bin/env bash
# Unit tests for the tg_proxy checkpoint in install.sh (T-0194).
#
# The checkpoint seeds the per-installation Telegram egress proxy into the
# install's config/system_settings.toml under [tg].proxy_url — the same key the
# worker Config reads and the admin Settings UI writes.
#
# Cases:
#   1. unset/empty BOTSQUAD_TG_PROXY_URL → skip (no file written)
#   2. valid http URL → [tg].proxy_url written
#   3. socks5:// accepted; merge preserves other keys an admin already set
#   4. idempotent re-run with same URL → file mtime unchanged
#   5. invalid scheme → die_struct with reason; no write
#
# The python merge uses tomllib; we point BOTSQUAD_TG_PROXY_PY at the test
# host's python3 (>=3.11) so the test doesn't need the worker venv.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }

PY="${BOTSQUAD_TG_PROXY_PY:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: python3 required"; exit 1; }
"$PY" -c 'import tomllib' 2>/dev/null || { echo "FAIL: python >=3.11 (tomllib) required"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# Source install.sh with main() suppressed (same trick as proxy_url.sh).
TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"

export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_TG_PROXY_PY="$PY"
mkdir -p "$WORK/install/config"
SETTINGS="$WORK/install/config/system_settings.toml"

# shellcheck disable=SC1090
source "$TMP_SH"
ensure_state_dir
log() { :; }
warn() { :; }
trap - ERR  # we drive step_tg_proxy directly and check rc ourselves

# --- Case 1: unset → skip ----------------------------------------------------
case1() {
  rm -f "$SETTINGS"
  unset BOTSQUAD_TG_PROXY_URL
  CURRENT_CHECKPOINT=tg_proxy
  step_tg_proxy || fail "case1: step_tg_proxy returned non-zero with unset URL"
  [[ ! -f "$SETTINGS" ]] || fail "case1: settings written despite unset BOTSQUAD_TG_PROXY_URL"
  export BOTSQUAD_TG_PROXY_URL=""
  step_tg_proxy || fail "case1: step_tg_proxy returned non-zero with empty URL"
  [[ ! -f "$SETTINGS" ]] || fail "case1: settings written despite empty BOTSQUAD_TG_PROXY_URL"
  unset BOTSQUAD_TG_PROXY_URL
  pass "case 1: unset/empty BOTSQUAD_TG_PROXY_URL skips (no file)"
}

# --- Case 2: valid http URL → proxy_url written ------------------------------
case2() {
  rm -f "$SETTINGS"
  export BOTSQUAD_TG_PROXY_URL="http://153.80.195.83:8888"
  CURRENT_CHECKPOINT=tg_proxy
  step_tg_proxy || fail "case2: step_tg_proxy returned non-zero with valid URL"
  [[ -f "$SETTINGS" ]] || fail "case2: system_settings.toml not created"
  local got
  got="$("$PY" -c "import tomllib;print(tomllib.load(open('$SETTINGS','rb'))['tg']['proxy_url'])")"
  [[ "$got" = "$BOTSQUAD_TG_PROXY_URL" ]] \
    || fail "case2: [tg].proxy_url = '$got' (expected '$BOTSQUAD_TG_PROXY_URL')"
  unset BOTSQUAD_TG_PROXY_URL
  pass "case 2: valid http URL writes [tg].proxy_url"
}

# --- Case 3: socks5 accepted + merge preserves other keys --------------------
case3() {
  # Pre-seed a file an admin might have written via the Settings UI.
  printf '[tg]\ndefault_chat_id = "404580642"\nquiet_hours_start_utc = 18\n\n[session]\nttl = "7d"\n' \
    > "$SETTINGS"
  export BOTSQUAD_TG_PROXY_URL="socks5://10.0.0.1:1080"
  CURRENT_CHECKPOINT=tg_proxy
  step_tg_proxy || fail "case3: step_tg_proxy failed with socks5 URL"
  # All pre-existing keys must survive, plus the new proxy_url.
  "$PY" - "$SETTINGS" <<'PY' || fail "case3: merge dropped a pre-existing key or proxy_url"
import sys, tomllib
d = tomllib.load(open(sys.argv[1], "rb"))
assert d["tg"]["proxy_url"] == "socks5://10.0.0.1:1080", d
assert d["tg"]["default_chat_id"] == "404580642", d
assert d["tg"]["quiet_hours_start_utc"] == 18, d
assert d["session"]["ttl"] == "7d", d
PY
  unset BOTSQUAD_TG_PROXY_URL
  pass "case 3: socks5 accepted; merge preserves default_chat_id/quiet_hours/session"
}

# --- Case 4: idempotent re-run → mtime unchanged -----------------------------
case4() {
  export BOTSQUAD_TG_PROXY_URL="socks5://10.0.0.1:1080"  # same as case3 left
  CURRENT_CHECKPOINT=tg_proxy
  local m1 m2
  m1="$(stat -c '%Y' "$SETTINGS")"
  sleep 1
  step_tg_proxy || fail "case4: idempotent re-run failed"
  m2="$(stat -c '%Y' "$SETTINGS")"
  [[ "$m1" = "$m2" ]] || fail "case4: file rewritten on idempotent re-run (mtime $m1 → $m2)"
  unset BOTSQUAD_TG_PROXY_URL
  pass "case 4: idempotent re-run leaves file mtime unchanged"
}

# --- Case 5: invalid scheme → die_struct, no write ---------------------------
case5() {
  rm -f "$SETTINGS"
  export BOTSQUAD_TG_PROXY_URL="ftp://nope"
  CURRENT_CHECKPOINT=tg_proxy
  local err_log="$WORK/tg_proxy.err"
  if ( step_tg_proxy ) 2>"$err_log"; then
    fail "case5: step_tg_proxy unexpectedly succeeded with a bad scheme"
  fi
  grep -q "not valid" "$err_log" \
    || fail "case5: failure reason not surfaced; got: $(cat "$err_log")"
  grep -q "tg_proxy" "$err_log" || fail "case5: checkpoint name not in failure block"
  [[ ! -f "$SETTINGS" ]] || fail "case5: settings written despite invalid scheme"
  unset BOTSQUAD_TG_PROXY_URL
  pass "case 5: invalid scheme aborts with reason; no write"
}

# --- CLI flag parse: --tg-proxy-url=... maps to the env var ------------------
case_argparse() {
  unset BOTSQUAD_TG_PROXY_URL
  parse_cli_args --tg-proxy-url=http://proxy.example:8888
  [[ "${BOTSQUAD_TG_PROXY_URL:-}" = "http://proxy.example:8888" ]] \
    || fail "argparse: --tg-proxy-url=VALUE did not set the env (got '${BOTSQUAD_TG_PROXY_URL:-}')"
  unset BOTSQUAD_TG_PROXY_URL
  parse_cli_args --tg-proxy-url socks5://h:1080
  [[ "${BOTSQUAD_TG_PROXY_URL:-}" = "socks5://h:1080" ]] \
    || fail "argparse: --tg-proxy-url VALUE (space form) did not set the env"
  unset BOTSQUAD_TG_PROXY_URL
  pass "argparse: --tg-proxy-url (both = and space forms) set BOTSQUAD_TG_PROXY_URL"
}

case1
case2
case3
case4
case5
case_argparse

echo ""
echo "TG_PROXY: all assertions passed"
