#!/usr/bin/env bash
# Unit tests for the install_reverse_proxy checkpoint in install.sh (T-0029).
#
# Cases:
#   1. shared mode forced → no-op (no override file written) regardless
#      of probe.
#   2. auto + network present (probe returns 0) → no-op. This is the
#      load-bearing case for this server's own install path.
#   3. auto + network absent (probe returns non-zero) → override written
#      with the right shape: bot-squad-api gets a `ports:` block on
#      ${BOTSQUAD_HTTP_PORT}, and avo_backend is redefined as
#      external: false.
#   4. fresh mode forced → override written even if the probe would
#      have returned 0.
#   5. override is idempotent: re-running with the same port + network
#      doesn't rewrite the file (mtime preserved); changing the port
#      DOES rewrite it.
#   6. invalid mode → die_struct fires with helpful guidance.
#   7. static check: install_reverse_proxy is in STEPS, and lands before
#      docker_compose_up.
#   8. static check: step_docker_compose_up adds -f <override> iff the
#      override exists.
#
# All docker / sudo is funnelled through BOTSQUAD_DOCKER_NETWORK_PROBE_CMD
# + BOTSQUAD_FRESH_HOST_OVERRIDE so the test runs without docker / root.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# Source install.sh with main() suppressed (same trick as smoke_engine.sh).
TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"

# Redirect every side-effecting path into WORK so the test runs without
# touching anything outside $WORK.
export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_OPERATOR_SESSION="bs-reverse-proxy-$$"
export BOTSQUAD_FRESH_HOST_OVERRIDE="$WORK/install/docker-compose.fresh-host.yml"
mkdir -p "$WORK/install"

# shellcheck disable=SC1090
source "$TMP_SH"
ensure_state_dir
log()  { :; }
warn() { :; }
trap - ERR  # we drive step_install_reverse_proxy directly and check rc

reset_sinks() {
  rm -f "$BOTSQUAD_FRESH_HOST_OVERRIDE"
  unset BOTSQUAD_REVERSE_PROXY_MODE BOTSQUAD_HTTP_PORT \
        BOTSQUAD_REVERSE_PROXY_NETWORK BOTSQUAD_DOCKER_NETWORK_PROBE_CMD
  # Restore the defaults (the in-script defaults run only at source-time).
  export BOTSQUAD_REVERSE_PROXY_MODE="auto"
  export BOTSQUAD_HTTP_PORT="8000"
  export BOTSQUAD_REVERSE_PROXY_NETWORK="avo_backend"
}

# --- Case 1: forced shared mode → no-op -------------------------------------
case1() {
  reset_sinks
  export BOTSQUAD_REVERSE_PROXY_MODE="shared"
  # Make the probe explode if it gets called — it should NOT be called when
  # the mode is forced to shared.
  export BOTSQUAD_DOCKER_NETWORK_PROBE_CMD='echo "should not probe" >&2; exit 99'

  CURRENT_CHECKPOINT=install_reverse_proxy
  step_install_reverse_proxy \
    || fail "case1: step_install_reverse_proxy returned non-zero in forced-shared mode"
  [[ ! -f "$BOTSQUAD_FRESH_HOST_OVERRIDE" ]] \
    || fail "case1: override file written despite forced-shared mode"
  pass "case 1: BOTSQUAD_REVERSE_PROXY_MODE=shared ⇒ checkpoint is a no-op"
}

# --- Case 2: auto + shared traefik present ⇒ no-op (THIS SERVER's path) -----
case2() {
  reset_sinks
  # Probe returns 0 — "the avo_backend network exists on this docker daemon",
  # i.e. somebody else is already running a reverse proxy. Use `true` rather
  # than `exit 0` so the wrapper's "$1" pass-through doesn't trip
  # "exit: too many arguments".
  export BOTSQUAD_DOCKER_NETWORK_PROBE_CMD='true'

  CURRENT_CHECKPOINT=install_reverse_proxy
  step_install_reverse_proxy \
    || fail "case2: step_install_reverse_proxy failed when shared traefik is present"
  [[ ! -f "$BOTSQUAD_FRESH_HOST_OVERRIDE" ]] \
    || fail "case2: override file written despite avo_backend network being present"
  pass "case 2: auto + 'avo_backend' network present ⇒ no-op (preserves this server's install path)"
}

# --- Case 3: auto + no shared traefik ⇒ override written with right shape ---
case3() {
  reset_sinks
  # Probe returns non-zero — no shared reverse proxy.
  export BOTSQUAD_DOCKER_NETWORK_PROBE_CMD='false'
  export BOTSQUAD_HTTP_PORT="8042"

  CURRENT_CHECKPOINT=install_reverse_proxy
  step_install_reverse_proxy \
    || fail "case3: step_install_reverse_proxy failed on fresh-host path"
  [[ -f "$BOTSQUAD_FRESH_HOST_OVERRIDE" ]] \
    || fail "case3: override file not written ($BOTSQUAD_FRESH_HOST_OVERRIDE)"

  # bot-squad-api service must get a ports: block on the chosen host port.
  grep -qE '^[[:space:]]*-[[:space:]]*"8042:8000"' "$BOTSQUAD_FRESH_HOST_OVERRIDE" \
    || fail "case3: override missing 'ports: [\"8042:8000\"]' line; got:
$(cat "$BOTSQUAD_FRESH_HOST_OVERRIDE")"
  grep -qE '^[[:space:]]*bot-squad-api:' "$BOTSQUAD_FRESH_HOST_OVERRIDE" \
    || fail "case3: override missing 'bot-squad-api:' service block"
  grep -qE '^[[:space:]]*ports:' "$BOTSQUAD_FRESH_HOST_OVERRIDE" \
    || fail "case3: override missing 'ports:' key"

  # The avo_backend network must be re-declared as external: false.
  grep -qE '^[[:space:]]*avo_backend:' "$BOTSQUAD_FRESH_HOST_OVERRIDE" \
    || fail "case3: override missing 'avo_backend:' network redefinition"
  grep -qE '^[[:space:]]*external:[[:space:]]*false' "$BOTSQUAD_FRESH_HOST_OVERRIDE" \
    || fail "case3: override missing 'external: false' for avo_backend"

  # Mode of the override file.
  local mode; mode="$(stat -c '%a' "$BOTSQUAD_FRESH_HOST_OVERRIDE")"
  [[ "$mode" = "644" ]] \
    || fail "case3: override mode = $mode (expected 644)"

  pass "case 3: fresh-host auto-detect writes override with ports + non-external avo_backend"
}

# --- Case 4: forced fresh mode → override written even with shared probe -----
case4() {
  reset_sinks
  export BOTSQUAD_REVERSE_PROXY_MODE="fresh"
  # Probe would say "shared is present", but mode=fresh overrides.
  export BOTSQUAD_DOCKER_NETWORK_PROBE_CMD='true'
  export BOTSQUAD_HTTP_PORT="9000"

  CURRENT_CHECKPOINT=install_reverse_proxy
  step_install_reverse_proxy \
    || fail "case4: step_install_reverse_proxy failed in forced-fresh mode"
  [[ -f "$BOTSQUAD_FRESH_HOST_OVERRIDE" ]] \
    || fail "case4: override file not written in forced-fresh mode"
  grep -qE '"9000:8000"' "$BOTSQUAD_FRESH_HOST_OVERRIDE" \
    || fail "case4: override missing 9000:8000 port mapping in forced-fresh mode"
  pass "case 4: BOTSQUAD_REVERSE_PROXY_MODE=fresh writes override regardless of probe result"
}

# --- Case 5: idempotency + change-port flow ---------------------------------
case5() {
  reset_sinks
  export BOTSQUAD_DOCKER_NETWORK_PROBE_CMD='false'
  export BOTSQUAD_HTTP_PORT="8000"

  CURRENT_CHECKPOINT=install_reverse_proxy
  step_install_reverse_proxy \
    || fail "case5: first call failed"
  [[ -f "$BOTSQUAD_FRESH_HOST_OVERRIDE" ]] \
    || fail "case5: override file not written on first call"

  local mt; mt="$(stat -c '%Y' "$BOTSQUAD_FRESH_HOST_OVERRIDE")"
  sleep 1
  # Re-run with identical inputs — file mtime must not change.
  step_install_reverse_proxy \
    || fail "case5: idempotent re-run returned non-zero"
  local mt2; mt2="$(stat -c '%Y' "$BOTSQUAD_FRESH_HOST_OVERRIDE")"
  [[ "$mt" = "$mt2" ]] \
    || fail "case5: override rewritten on idempotent re-run ($mt → $mt2)"

  # Change the port; the file MUST be rewritten with the new mapping.
  export BOTSQUAD_HTTP_PORT="9999"
  step_install_reverse_proxy \
    || fail "case5: change-port re-run failed"
  grep -qE '"9999:8000"' "$BOTSQUAD_FRESH_HOST_OVERRIDE" \
    || fail "case5: override not updated on port change; got:
$(cat "$BOTSQUAD_FRESH_HOST_OVERRIDE")"
  pass "case 5: override is idempotent on same inputs; rewrites on port change"
}

# --- Case 6: invalid mode → die_struct --------------------------------------
case6() {
  reset_sinks
  export BOTSQUAD_REVERSE_PROXY_MODE="not-a-mode"
  CURRENT_CHECKPOINT=install_reverse_proxy
  local err_log="$WORK/reverse_proxy.err"
  if ( step_install_reverse_proxy ) 2>"$err_log"; then
    fail "case6: step_install_reverse_proxy unexpectedly succeeded with invalid mode"
  fi
  grep -q "install_reverse_proxy" "$err_log" \
    || fail "case6: checkpoint name missing from failure block; got: $(cat "$err_log")"
  grep -q "BOTSQUAD_REVERSE_PROXY_MODE" "$err_log" \
    || fail "case6: failure block should mention BOTSQUAD_REVERSE_PROXY_MODE"
  grep -q "not-a-mode" "$err_log" \
    || fail "case6: failure block should echo the bad mode value"
  pass "case 6: invalid mode triggers structured error pointing at the env var"
}

# --- Case 7: STEPS contains install_reverse_proxy before docker_compose_up --
case7() {
  local steps_str=" ${STEPS[*]} "
  [[ "$steps_str" = *" install_reverse_proxy "* ]] \
    || fail "case7: install_reverse_proxy missing from STEPS array"
  local rp_idx=-1 dcu_idx=-1 i
  for i in "${!STEPS[@]}"; do
    case "${STEPS[$i]}" in
      install_reverse_proxy) rp_idx="$i" ;;
      docker_compose_up)     dcu_idx="$i" ;;
    esac
  done
  [[ "$rp_idx" -ge 0 ]] || fail "case7: install_reverse_proxy not located"
  [[ "$dcu_idx" -ge 0 ]] || fail "case7: docker_compose_up not located"
  [[ "$rp_idx" -lt "$dcu_idx" ]] \
    || fail "case7: install_reverse_proxy (idx $rp_idx) must come before docker_compose_up (idx $dcu_idx)"
  pass "case 7: install_reverse_proxy is in STEPS before docker_compose_up"
}

# --- Case 8: docker_compose_up wires -f <override> in only when present -----
case8() {
  # Static check on the source: step_docker_compose_up references the
  # override path. Behavioral check: invoking it with no override file
  # present invokes plain `docker compose -f docker-compose.yml ...`;
  # with the override present, it gains `-f <override>`.
  grep -q 'reverse_proxy_override_path' "$INSTALL_SH" \
    || fail "case8: step_docker_compose_up doesn't reference reverse_proxy_override_path"

  # Behavioral: stub `docker` on PATH to capture argv, then drive the
  # step. We rely on the install dir being writable here.
  reset_sinks
  rm -f "$BOTSQUAD_FRESH_HOST_OVERRIDE"
  local fakebin="$WORK/fakebin"
  mkdir -p "$fakebin"
  local capture="$WORK/docker.argv"
  cat > "$fakebin/docker" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$capture"
EOF
  chmod +x "$fakebin/docker"
  local saved_path="$PATH"
  export PATH="$fakebin:$PATH"
  # Need docker-compose.yml in the install dir or compose -f will fail.
  # We stub docker entirely so it doesn't actually look at the file.
  touch "$BOTSQUAD_INSTALL_DIR/docker-compose.yml"
  : > "$capture"

  CURRENT_CHECKPOINT=docker_compose_up
  step_docker_compose_up \
    || fail "case8: step_docker_compose_up failed without override"
  grep -q -- '-f docker-compose.yml up -d --build' "$capture" \
    || fail "case8: docker_compose_up didn't invoke compose with base -f docker-compose.yml; capture:
$(cat "$capture")"
  grep -q "docker-compose.fresh-host.yml" "$capture" \
    && fail "case8: docker_compose_up included override flag despite no override file"

  # Now write an override, re-run, expect a second -f flag.
  : > "$capture"
  printf 'services:\n  bot-squad-api:\n    ports:\n      - "8000:8000"\n' \
    > "$BOTSQUAD_FRESH_HOST_OVERRIDE"
  step_docker_compose_up \
    || fail "case8: step_docker_compose_up failed with override"
  grep -q "$BOTSQUAD_FRESH_HOST_OVERRIDE" "$capture" \
    || fail "case8: docker_compose_up didn't add -f <override> when file exists; capture:
$(cat "$capture")"

  export PATH="$saved_path"
  pass "case 8: docker_compose_up layers -f <override> iff the override file exists"
}

case1
case2
case3
case4
case5
case6
case7
case8

echo ""
echo "INSTALL_REVERSE_PROXY: all assertions passed"
