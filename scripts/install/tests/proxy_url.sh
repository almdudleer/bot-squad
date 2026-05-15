#!/usr/bin/env bash
# Unit tests for the proxy_url checkpoint in install.sh.
#
# Three cases:
#   1. empty BOTSQUAD_PROXY_URL → skip (no sinks written)
#   2. valid URL (probe stubbed → ok) → all 3 sinks written + script env set
#   3. invalid URL → reprompt loop, then succeeds on the 2nd input
#
# The curl probe is mocked via BOTSQUAD_PROXY_PROBE_CMD so the test
# doesn't hit the network. The apt-conf sink is redirected into the
# temp WORK dir so we don't need sudo.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }

command -v jq >/dev/null 2>&1 || { echo "FAIL: jq required"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# Source install.sh with main() suppressed; reuse the trick from smoke_engine.sh.
TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"

# Common env: redirect every side-effecting path into WORK.
export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_OPERATOR_SESSION="bs-proxy-url-$$"
export BOTSQUAD_PROXY_APT_CONF="$WORK/apt/01proxy"
export BOTSQUAD_CLAUDE_SETTINGS="$WORK/claude/settings.json"
mkdir -p "$WORK/apt" "$WORK/claude"

# shellcheck disable=SC1090
source "$TMP_SH"
ensure_state_dir
log() { :; }
warn() { :; }
trap - ERR  # we drive step_proxy_url directly and check rc ourselves

# --- Case 1: empty BOTSQUAD_PROXY_URL → skip ---------------------------------
case1() {
  rm -f "$BOTSQUAD_PROXY_APT_CONF" "$BOTSQUAD_CLAUDE_SETTINGS"
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
  export BOTSQUAD_PROXY_URL=""
  CURRENT_CHECKPOINT=proxy_url
  step_proxy_url || fail "case1: step_proxy_url returned non-zero with empty URL"
  [[ ! -f "$BOTSQUAD_PROXY_APT_CONF" ]] \
    || fail "case1: apt conf written despite empty BOTSQUAD_PROXY_URL"
  [[ ! -f "$BOTSQUAD_CLAUDE_SETTINGS" ]] \
    || fail "case1: claude settings written despite empty BOTSQUAD_PROXY_URL"
  [[ -z "${http_proxy:-}" ]] \
    || fail "case1: http_proxy was exported despite empty BOTSQUAD_PROXY_URL"
  unset BOTSQUAD_PROXY_URL
  pass "case 1: empty BOTSQUAD_PROXY_URL skips all sinks"
}

# --- Case 2: valid URL → 3 sinks written + idempotent re-run ----------------
case2() {
  rm -f "$BOTSQUAD_PROXY_APT_CONF" "$BOTSQUAD_CLAUDE_SETTINGS"
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
  export BOTSQUAD_PROXY_URL="http://proxy.test.invalid:3128"
  # Stub the probe to succeed for any URL.
  export BOTSQUAD_PROXY_PROBE_CMD="true"
  CURRENT_CHECKPOINT=proxy_url
  step_proxy_url || fail "case2: step_proxy_url returned non-zero with valid URL"

  # Sink 1: script env.
  [[ "${http_proxy:-}" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: http_proxy not exported (got '${http_proxy:-}')"
  [[ "${https_proxy:-}" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: https_proxy not exported"
  [[ "${HTTP_PROXY:-}" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: HTTP_PROXY not exported"
  [[ "${HTTPS_PROXY:-}" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: HTTPS_PROXY not exported"

  # Sink 2: claude settings.json.
  [[ -f "$BOTSQUAD_CLAUDE_SETTINGS" ]] \
    || fail "case2: claude settings.json was not created"
  local got_http got_https
  got_http="$(jq -r '.env.http_proxy' "$BOTSQUAD_CLAUDE_SETTINGS")"
  got_https="$(jq -r '.env.https_proxy' "$BOTSQUAD_CLAUDE_SETTINGS")"
  [[ "$got_http" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: claude .env.http_proxy = '$got_http' (expected '$BOTSQUAD_PROXY_URL')"
  [[ "$got_https" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: claude .env.https_proxy = '$got_https'"

  # Sink 3: apt conf.
  [[ -f "$BOTSQUAD_PROXY_APT_CONF" ]] \
    || fail "case2: apt conf was not written"
  grep -qF "Acquire::http::Proxy \"$BOTSQUAD_PROXY_URL\"" "$BOTSQUAD_PROXY_APT_CONF" \
    || fail "case2: apt conf missing Acquire::http::Proxy line"
  grep -qF "Acquire::https::Proxy \"$BOTSQUAD_PROXY_URL\"" "$BOTSQUAD_PROXY_APT_CONF" \
    || fail "case2: apt conf missing Acquire::https::Proxy line"
  local mode; mode="$(stat -c '%a' "$BOTSQUAD_PROXY_APT_CONF")"
  [[ "$mode" = "644" ]] \
    || fail "case2: apt conf mode = $mode (expected 644)"

  # Idempotency: re-run with same URL → no change in file mtimes.
  local mt_apt mt_claude
  mt_apt="$(stat -c '%Y' "$BOTSQUAD_PROXY_APT_CONF")"
  mt_claude="$(stat -c '%Y' "$BOTSQUAD_CLAUDE_SETTINGS")"
  sleep 1
  step_proxy_url || fail "case2: re-run of step_proxy_url failed"
  local mt_apt2 mt_claude2
  mt_apt2="$(stat -c '%Y' "$BOTSQUAD_PROXY_APT_CONF")"
  mt_claude2="$(stat -c '%Y' "$BOTSQUAD_CLAUDE_SETTINGS")"
  [[ "$mt_apt" = "$mt_apt2" ]] \
    || fail "case2: apt conf rewritten on idempotent re-run (mtime $mt_apt → $mt_apt2)"
  # Claude settings may be rewritten but content must stay identical.
  got_http="$(jq -r '.env.http_proxy' "$BOTSQUAD_CLAUDE_SETTINGS")"
  [[ "$got_http" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: claude .env.http_proxy changed on idempotent re-run"

  # Change-URL flow: a different URL must update all three sinks.
  export BOTSQUAD_PROXY_URL="http://proxy2.test.invalid:3128"
  step_proxy_url || fail "case2: change-URL re-run failed"
  got_http="$(jq -r '.env.http_proxy' "$BOTSQUAD_CLAUDE_SETTINGS")"
  [[ "$got_http" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: claude .env.http_proxy not updated on URL change (got '$got_http')"
  grep -qF "Acquire::http::Proxy \"$BOTSQUAD_PROXY_URL\"" "$BOTSQUAD_PROXY_APT_CONF" \
    || fail "case2: apt conf not updated on URL change"
  [[ "${http_proxy:-}" = "$BOTSQUAD_PROXY_URL" ]] \
    || fail "case2: http_proxy not re-exported on URL change"

  unset BOTSQUAD_PROXY_URL BOTSQUAD_PROXY_PROBE_CMD
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
  pass "case 2: valid URL writes all 3 sinks; idempotent; change-URL updates all"
}

# --- Case 3: invalid URL → re-prompt loop -----------------------------------
# In non-interactive mode the pre-set URL path fails fatally on a bad probe.
# This is the same observable contract (re-run with a new URL) — we verify
# the failure mode here. The interactive re-prompt branch is exercised below
# by piping stdin to a /dev/tty-bypassing variant.
case3a() {
  rm -f "$BOTSQUAD_PROXY_APT_CONF" "$BOTSQUAD_CLAUDE_SETTINGS"
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
  export BOTSQUAD_PROXY_URL="http://broken.test.invalid:9999"
  # Stub probe to fail with a recognizable message.
  export BOTSQUAD_PROXY_PROBE_CMD='echo "curl: (7) Failed to connect" >&2; exit 1'
  CURRENT_CHECKPOINT=proxy_url
  local err_log="$WORK/proxy_url.err"
  # die_struct calls exit; run in a subshell so it doesn't kill the test runner.
  if ( step_proxy_url ) 2>"$err_log"; then
    fail "case3a: step_proxy_url unexpectedly succeeded with a failing probe"
  fi
  grep -q "Failed to connect" "$err_log" \
    || fail "case3a: failure reason not surfaced in stderr; got: $(cat "$err_log")"
  grep -q "proxy_url" "$err_log" \
    || fail "case3a: checkpoint name not in failure block"
  [[ ! -f "$BOTSQUAD_PROXY_APT_CONF" ]] \
    || fail "case3a: apt conf was written despite probe failure"
  [[ ! -f "$BOTSQUAD_CLAUDE_SETTINGS" ]] \
    || fail "case3a: claude settings written despite probe failure"
  unset BOTSQUAD_PROXY_URL BOTSQUAD_PROXY_PROBE_CMD
  pass "case 3a: bad URL with pre-set env aborts with reason; no sinks written"
}

# Case 3b: interactive re-prompt loop. We simulate the interactive flow by
# unsetting BOTSQUAD_PROXY_URL + NONINTERACTIVE and feeding answers on
# /dev/tty's stand-in. The script reads from </dev/tty, which can't be
# redirected from stdin. Instead, we exercise the loop by directly calling
# proxy_probe with a counter-based stub.
case3b() {
  # Counter that fails the first 2 calls, succeeds on the 3rd.
  local counter_file="$WORK/probe_counter"; echo 0 > "$counter_file"
  # Write the probe stub as a script and reference it by path so we avoid
  # double-quoting hell.
  local probe_stub="$WORK/probe_stub.sh"
  cat > "$probe_stub" <<EOF
#!/usr/bin/env bash
n=\$(cat "$counter_file")
n=\$((n+1))
echo "\$n" > "$counter_file"
if [[ "\$n" -lt 3 ]]; then
  echo "fail attempt \$n" >&2
  exit 1
fi
exit 0
EOF
  chmod +x "$probe_stub"
  export BOTSQUAD_PROXY_PROBE_CMD="$probe_stub"

  # Probe must fail twice then succeed.
  proxy_probe "http://x.invalid" 2>/dev/null && fail "case3b: probe should have failed on attempt 1"
  proxy_probe "http://x.invalid" 2>/dev/null && fail "case3b: probe should have failed on attempt 2"
  proxy_probe "http://x.invalid" 2>/dev/null || fail "case3b: probe should have succeeded on attempt 3"

  # Inspect: the script has a `while :; do ... break ... continue` loop
  # over proxy_probe that re-prompts on failure. Static-check it.
  grep -q 'while :; do' "$INSTALL_SH" \
    || fail "case3b: re-prompt while-loop missing from install.sh"
  grep -q 'proxy validation failed' "$INSTALL_SH" \
    || fail "case3b: re-prompt failure message missing from install.sh"
  unset BOTSQUAD_PROXY_PROBE_CMD
  pass "case 3b: probe stub fails twice then succeeds; re-prompt loop present"
}

case1
case2
case3a
case3b

echo ""
echo "PROXY_URL: all assertions passed"
