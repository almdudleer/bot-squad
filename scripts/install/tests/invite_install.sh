#!/usr/bin/env bash
# Unit tests for the T-0026 invite-mode install.sh STEPS chain.
#
# Drives install.sh with a bsq_invite_* token + a mocked mothership and
# asserts the invite chain:
#   1. Prefix-detect picks INVITE_STEPS over INSTALL_STEPS.
#   2. Invite chain does NOT include install-dir / botsquad-group / clone /
#      docker-compose / systemd UNIT install (locks the DoD's
#      "no-group-create, no-compose-up" requirement).
#   3. step_mothership_join calls the mocked /installer/join, persists
#      the returned role + target_username + server_id to invite.target.
#   4. Linux-user-vs-target mismatch is rejected loudly.
#   5. Admin vs non-admin role both flow through end-to-end.
#
# The mothership is mocked via a temp-dir http server stand-in: we run
# `curl --unix-socket` against a python socketserver responding to
# /api/m/installer/join with a canned body. install.sh's curl gets the
# real HTTP shape it expects.
#
# Side-effecting paths (sudo, group ops, docker, npm) are stubbed via
# the existing BOTSQUAD_*_CMD seams plus a thin per-step shim.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# Spin up a minimal HTTP server on a unix socket that returns the canned
# /installer/join body. We use python's http.server because it's already in
# the base image used by the test runners (no extra dep). The mothership URL
# we hand install.sh points at this socket via curl's --resolve trick is
# overkill; install.sh uses raw curl with a URL, so we run on localhost.
START_MOTHERSHIP() {
  local role="$1" target="$2" server_id="$3" port="$4"
  python3 - "$port" "$role" "$target" "$server_id" <<'PYEOF' &
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer

port, role, target, server_id = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        if self.path == "/api/m/installer/join":
            body = json.dumps({"server_id": server_id, "target_username": target, "role": role}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/m/installer/checkpoint":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

HTTPServer(("127.0.0.1", port), H).serve_forever()
PYEOF
  MOTHERSHIP_PID=$!
  # Wait for it to bind.
  for _ in $(seq 1 50); do
    if curl -fsS --max-time 1 -o /dev/null -X POST "http://127.0.0.1:${port}/api/m/installer/join" -d '{}' 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  fail "mock mothership never came up on port $port"
}
STOP_MOTHERSHIP() {
  [[ -n "${MOTHERSHIP_PID:-}" ]] && kill "$MOTHERSHIP_PID" 2>/dev/null || true
  MOTHERSHIP_PID=""
}

# Pick a free port for the mock mothership.
PICK_PORT() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

# Source install.sh with main() suppressed so we can drive the engine.
TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"

# --- environment for all cases -----------------------------------------------
export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=0
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_PROXY_URL=""
export BOTSQUAD_DISTRO_FAMILY=debian
# enable-per-user-worker.sh is the helper called by step_per_user_worker_enable.
# Test override — we don't actually want to touch systemctl --user.
export BOTSQUAD_PER_USER_ENABLE_CMD='true'
mkdir -p "$BOTSQUAD_INSTALL_DIR/scripts/install"
touch "$BOTSQUAD_INSTALL_DIR/scripts/install/enable-per-user-worker.sh"

# shellcheck disable=SC1090
source "$TMP_SH"

# Silence install.sh's log/warn for cleaner test output. die_struct stays
# loud — we want failures to be visible.
log()  { :; }
warn() { :; }

# --- case 1: prefix-detect picks INVITE_STEPS, not INSTALL_STEPS -------------
# Drive the same logic main() does for STEPS selection. We can't call main()
# directly (it runs every step), but we can replicate the prefix check.
export BOTSQUAD_INSTALL_TOKEN="bsq_invite_aaaaaaaaaaaaaaaaaaaa"
if [[ "$BOTSQUAD_INSTALL_TOKEN" == bsq_invite_* ]]; then
  STEPS=("${INVITE_STEPS[@]}")
fi
# INVITE_STEPS must NOT contain the host-only operations.
forbidden=(install_dir botsquad_group clone_repo render_env systemd_unit docker_compose_up spawn_operator install_docker install_reverse_proxy python_venv per_user_worker_unit mothership_handshake)
for step in "${forbidden[@]}"; do
  for actual in "${STEPS[@]}"; do
    [[ "$actual" = "$step" ]] && fail "case1: invite chain includes forbidden step '$step'"
  done
done
# INVITE_STEPS must contain the new steps.
required=(mothership_join user_groups_join per_user_worker_enable print_join_attach)
for step in "${required[@]}"; do
  found=0
  for actual in "${STEPS[@]}"; do
    [[ "$actual" = "$step" ]] && found=1 && break
  done
  [[ "$found" -eq 1 ]] || fail "case1: invite chain missing required step '$step'"
done
pass "case1: invite-mode STEPS excludes host-only ops, includes invite-only ops"

# --- case 2: install-mode token gets INSTALL_STEPS ---------------------------
export BOTSQUAD_INSTALL_TOKEN="bsq_install_bbbbbbbbbbbbbbbbbb"
if [[ "$BOTSQUAD_INSTALL_TOKEN" == bsq_invite_* ]]; then
  STEPS=("${INVITE_STEPS[@]}")
else
  STEPS=("${INSTALL_STEPS[@]}")
fi
# INSTALL_STEPS must contain the host-only operations.
for step in install_dir botsquad_group docker_compose_up spawn_operator; do
  found=0
  for actual in "${STEPS[@]}"; do
    [[ "$actual" = "$step" ]] && found=1 && break
  done
  [[ "$found" -eq 1 ]] || fail "case2: install chain missing '$step'"
done
# And must NOT contain the invite-mode steps.
for step in mothership_join user_groups_join per_user_worker_enable; do
  for actual in "${STEPS[@]}"; do
    [[ "$actual" = "$step" ]] && fail "case2: install chain includes invite-only step '$step'"
  done
done
pass "case2: install-mode STEPS includes host-only ops, excludes invite-only ops"

# --- case 3: step_mothership_join happy path (non-admin) ---------------------
PORT="$(PICK_PORT)"
TARGET_USER="$(id -un)"
START_MOTHERSHIP "non-admin" "$TARGET_USER" "srv_test1" "$PORT"
export BOTSQUAD_MOTHERSHIP_URL="http://127.0.0.1:${PORT}"
export BOTSQUAD_INSTALL_TOKEN="bsq_invite_happy"
export BOTSQUAD_SKIP_MOTHERSHIP=0
rm -rf "$BOTSQUAD_STATE_DIR"
ensure_state_dir
step_mothership_join || fail "case3: step_mothership_join failed unexpectedly"
[[ -f "$BOTSQUAD_STATE_DIR/invite.target" ]] || fail "case3: invite.target not written"
grep -qxF "BOTSQUAD_INVITE_ROLE=non-admin" "$BOTSQUAD_STATE_DIR/invite.target" \
  || fail "case3: invite.target missing role"
grep -qxF "BOTSQUAD_INVITE_TARGET_USERNAME=$TARGET_USER" "$BOTSQUAD_STATE_DIR/invite.target" \
  || fail "case3: invite.target missing target_username"
grep -qxF "BOTSQUAD_INVITE_SERVER_ID=srv_test1" "$BOTSQUAD_STATE_DIR/invite.target" \
  || fail "case3: invite.target missing server_id"
[[ "$BOTSQUAD_INVITE_ROLE" = "non-admin" ]] || fail "case3: env not exported"
STOP_MOTHERSHIP
pass "case3: step_mothership_join records role + target + server_id"

# --- case 4: Linux-user mismatch is rejected ---------------------------------
PORT="$(PICK_PORT)"
START_MOTHERSHIP "admin" "someone-else-completely" "srv_test2" "$PORT"
export BOTSQUAD_MOTHERSHIP_URL="http://127.0.0.1:${PORT}"
export BOTSQUAD_INSTALL_TOKEN="bsq_invite_mismatch"
unset BOTSQUAD_INVITE_ROLE BOTSQUAD_INVITE_TARGET_USERNAME BOTSQUAD_INVITE_SERVER_ID
if ( step_mothership_join ) 2>/dev/null; then
  fail "case4: expected die on Linux-user mismatch"
fi
STOP_MOTHERSHIP
pass "case4: Linux-user vs invite-target mismatch is rejected"

# --- case 5: step_user_groups_join honors admin vs non-admin -----------------
# Stub usermod + getent so we can observe the calls without sudo.
USERMOD_LOG="$WORK/usermod.log"
sudo() {
  # Capture the args of every `sudo usermod -aG <group> <user>` call into
  # USERMOD_LOG so the assertion below can verify which groups were added.
  if [[ "${1:-}" = "usermod" ]]; then
    printf '%s\n' "$*" >> "$USERMOD_LOG"
    return 0
  fi
  if [[ "${1:-}" = "loginctl" ]]; then
    return 0
  fi
  # Any other sudo invocation in this step is a script bug — fail loudly.
  echo "unexpected sudo call: $*" >&2
  return 1
}
loginctl() { echo "no"; }
# Pretend both groups exist; pretend the current user isn't yet in them.
getent() {
  case "$1:$2" in
    group:www) echo "www:x:33:"; return 0 ;;
    group:bot-squad) echo "bot-squad:x:1001:"; return 0 ;;
    *) return 2 ;;
  esac
}
# Override `id -nG` to claim membership in no relevant groups.
id() {
  if [[ "${1:-}" = "-nG" ]]; then echo "users"
  elif [[ "${1:-}" = "-un" ]]; then echo "$TARGET_USER"
  elif [[ "${1:-}" = "-u" ]]; then echo "1000"
  else command id "$@"; fi
}

# Non-admin path: expect ONLY 'www', not bot-squad.
: > "$USERMOD_LOG"
export BOTSQUAD_INVITE_ROLE="non-admin"
step_user_groups_join || fail "case5a: step_user_groups_join failed for non-admin"
grep -qE "usermod -aG www( |$)" "$USERMOD_LOG" || fail "case5a: 'www' usermod missing"
if grep -qE "usermod -aG bot-squad( |$)" "$USERMOD_LOG"; then
  fail "case5a: non-admin was added to 'bot-squad' admin group (DoD violation)"
fi
pass "case5a: non-admin gets 'www' only, NOT 'bot-squad'"

# Admin path: expect BOTH groups.
: > "$USERMOD_LOG"
export BOTSQUAD_INVITE_ROLE="admin"
step_user_groups_join || fail "case5b: step_user_groups_join failed for admin"
grep -qE "usermod -aG www( |$)" "$USERMOD_LOG" || fail "case5b: 'www' usermod missing"
grep -qE "usermod -aG bot-squad( |$)" "$USERMOD_LOG" || fail "case5b: admin 'bot-squad' usermod missing"
pass "case5b: admin gets both 'www' and 'bot-squad'"

# --- case 6: step_per_user_worker_enable runs the helper (stubbed) -----------
# Already overridden via BOTSQUAD_PER_USER_ENABLE_CMD=true above; just make
# sure the step doesn't blow up.
step_per_user_worker_enable || fail "case6: step_per_user_worker_enable failed"
pass "case6: step_per_user_worker_enable runs the user-helper command"

echo ""
echo "INVITE INSTALL: all assertions passed"
