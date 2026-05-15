#!/usr/bin/env bash
# Unit tests for the install_docker checkpoint in install.sh.
#
# Cases:
#   1. already-installed → "compose works" probe returns 0 → no-op
#      (no GPG key, no apt list, no install/usermod commands invoked).
#   2. fresh host → all four side-effects fire (gpg key written, apt list
#      written, install cmd invoked, usermod cmd invoked); final probe
#      now succeeds; idempotent re-run is a no-op.
#   3. apt-install failure → die_struct fires for install_docker with the
#      install-time guidance; no usermod is attempted; no state recorded.
#   4. post-install login required → install succeeds (docker on PATH)
#      but compose probe still fails → die_struct fires with the
#      "log out / newgrp docker" guidance (group membership fixed at
#      session start).
#   5. codename detection failure (BOTSQUAD_DOCKER_CODENAME empty + no
#      /etc/os-release VERSION_CODENAME exposed) → die_struct fires
#      pointing the user at BOTSQUAD_DOCKER_CODENAME or T-0030.
#   6. apt list idempotency → re-running with the same inputs does not
#      rewrite the file (mtime preserved).
#
# All network / sudo / apt / usermod is funnelled through env-var seams
# (BOTSQUAD_DOCKER_*_CMD); paths redirected into a temp WORK dir.

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

# Common env: redirect every side-effecting path into WORK.
export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_OPERATOR_SESSION="bs-install-docker-$$"
export BOTSQUAD_DOCKER_GPG_KEYRING="$WORK/keyrings/docker.asc"
export BOTSQUAD_DOCKER_APT_LIST="$WORK/sources.list.d/docker.list"
# Pre-create the sink parents so docker_write_apt_list / install_gpg_key
# take the user-writable branch (and so a test cleanup doesn't fight root).
mkdir -p "$WORK/keyrings" "$WORK/sources.list.d"
# Pin a known codename + arch so detection doesn't depend on the host.
export BOTSQUAD_DOCKER_CODENAME="noble"
export BOTSQUAD_DOCKER_ARCH="amd64"
export BOTSQUAD_DOCKER_DISTRO="ubuntu"

# shellcheck disable=SC1090
source "$TMP_SH"
ensure_state_dir
log()  { :; }
warn() { :; }
trap - ERR  # we drive step_install_docker directly and check rc

# Helper: reset all per-test sinks and trace files. Preserves the parent
# directories so docker_write_apt_list / docker_install_gpg_key stay in
# the user-writable branch (no sudo fallback).
reset_sinks() {
  rm -f "$BOTSQUAD_DOCKER_GPG_KEYRING" "$BOTSQUAD_DOCKER_APT_LIST"
  rm -f "$WORK/gpg_fetch.calls" "$WORK/install.calls" "$WORK/usermod.calls"
  touch "$WORK/gpg_fetch.calls" "$WORK/install.calls" "$WORK/usermod.calls"
}

# Helper: arm the standard stub set for a "fresh host" install run.
# Each side-effecting command writes a trace line into a calls log so the
# test can assert it was invoked exactly once with the expected args.
arm_fresh_stubs() {
  # GPG fetch: receives the destination path as $1; write a dummy key
  # contents and record the call.
  local gpg_stub="$WORK/gpg_fetch.sh"
  cat > "$gpg_stub" <<EOF
#!/usr/bin/env bash
echo "fetch \$1" >> "$WORK/gpg_fetch.calls"
mkdir -p "\$(dirname "\$1")"
printf 'fake-docker-gpg-key\n' > "\$1"
EOF
  chmod +x "$gpg_stub"
  export BOTSQUAD_DOCKER_GPG_FETCH_CMD="$gpg_stub"

  # Install command: just record the call.
  local install_stub="$WORK/install_cmd.sh"
  cat > "$install_stub" <<EOF
#!/usr/bin/env bash
echo "apt-install" >> "$WORK/install.calls"
EOF
  chmod +x "$install_stub"
  export BOTSQUAD_DOCKER_INSTALL_CMD="$install_stub"

  # Usermod: receives the username as \$1.
  local usermod_stub="$WORK/usermod_cmd.sh"
  cat > "$usermod_stub" <<EOF
#!/usr/bin/env bash
echo "usermod \$1" >> "$WORK/usermod.calls"
EOF
  chmod +x "$usermod_stub"
  export BOTSQUAD_DOCKER_USERMOD_CMD="$usermod_stub"
}

# --- Case 1: already-installed → no-op --------------------------------------
case1() {
  reset_sinks
  # Probe succeeds immediately → entire checkpoint is a no-op.
  export BOTSQUAD_DOCKER_CHECK_CMD="true"
  # Arm stubs that would explode if called — they should NOT be called.
  export BOTSQUAD_DOCKER_GPG_FETCH_CMD='echo "should not fetch" >&2; exit 99'
  export BOTSQUAD_DOCKER_INSTALL_CMD='echo "should not install" >&2; exit 99'
  export BOTSQUAD_DOCKER_USERMOD_CMD='echo "should not usermod" >&2; exit 99'

  CURRENT_CHECKPOINT=install_docker
  step_install_docker \
    || fail "case1: step_install_docker returned non-zero when compose already works"

  [[ ! -f "$BOTSQUAD_DOCKER_GPG_KEYRING" ]] \
    || fail "case1: GPG keyring written despite 'compose works'"
  [[ ! -f "$BOTSQUAD_DOCKER_APT_LIST" ]] \
    || fail "case1: apt list written despite 'compose works'"
  pass "case 1: 'compose works' probe ⇒ entire checkpoint no-ops"

  unset BOTSQUAD_DOCKER_GPG_FETCH_CMD BOTSQUAD_DOCKER_INSTALL_CMD \
        BOTSQUAD_DOCKER_USERMOD_CMD
}

# --- Case 2: fresh host → all sinks + commands fire; idempotent re-run -----
case2() {
  reset_sinks
  arm_fresh_stubs

  # Probe: fail on first call (pre-install), succeed thereafter (post-install).
  local probe_counter="$WORK/probe_counter"; echo 0 > "$probe_counter"
  local probe_stub="$WORK/probe.sh"
  cat > "$probe_stub" <<EOF
#!/usr/bin/env bash
n=\$(cat "$probe_counter"); n=\$((n+1)); echo "\$n" > "$probe_counter"
# First call (idempotency gate, pre-install) → fail; rest succeed.
[[ "\$n" -ge 2 ]]
EOF
  chmod +x "$probe_stub"
  export BOTSQUAD_DOCKER_CHECK_CMD="$probe_stub"

  CURRENT_CHECKPOINT=install_docker
  step_install_docker \
    || fail "case2: step_install_docker failed on fresh-host happy path"

  [[ -f "$BOTSQUAD_DOCKER_GPG_KEYRING" ]] \
    || fail "case2: GPG keyring file not written ($BOTSQUAD_DOCKER_GPG_KEYRING)"
  [[ -f "$BOTSQUAD_DOCKER_APT_LIST" ]] \
    || fail "case2: apt list file not written ($BOTSQUAD_DOCKER_APT_LIST)"

  # apt list must be the signed-by pattern, not pipe-curl-to-bash.
  grep -qF "signed-by=$BOTSQUAD_DOCKER_GPG_KEYRING" "$BOTSQUAD_DOCKER_APT_LIST" \
    || fail "case2: apt list missing signed-by=$BOTSQUAD_DOCKER_GPG_KEYRING"
  grep -qF "https://download.docker.com/linux/ubuntu" "$BOTSQUAD_DOCKER_APT_LIST" \
    || fail "case2: apt list missing docker.com URL"
  grep -qF "noble stable" "$BOTSQUAD_DOCKER_APT_LIST" \
    || fail "case2: apt list missing 'noble stable' suite/component"
  grep -qF "arch=amd64" "$BOTSQUAD_DOCKER_APT_LIST" \
    || fail "case2: apt list missing arch=amd64"

  local apt_mode; apt_mode="$(stat -c '%a' "$BOTSQUAD_DOCKER_APT_LIST")"
  [[ "$apt_mode" = "644" ]] \
    || fail "case2: apt list mode = $apt_mode (expected 644)"

  # Each side-effect must have fired exactly once.
  local n
  n="$(wc -l < "$WORK/gpg_fetch.calls")"; [[ "$n" -eq 1 ]] \
    || fail "case2: expected 1 gpg fetch call, got $n"
  n="$(wc -l < "$WORK/install.calls")"; [[ "$n" -eq 1 ]] \
    || fail "case2: expected 1 install call, got $n"
  n="$(wc -l < "$WORK/usermod.calls")"; [[ "$n" -eq 1 ]] \
    || fail "case2: expected 1 usermod call, got $n"
  # usermod must have been called with the current user.
  grep -qF "usermod $(id -un)" "$WORK/usermod.calls" \
    || fail "case2: usermod not called with current user; got: $(cat "$WORK/usermod.calls")"

  # --- Idempotency: a second run, with "compose works" now true, is a
  # full no-op.  No new gpg fetch, no new install, no new usermod, no
  # rewrite of the apt list.
  local mt_list mt_key
  mt_list="$(stat -c '%Y' "$BOTSQUAD_DOCKER_APT_LIST")"
  mt_key="$(stat -c '%Y' "$BOTSQUAD_DOCKER_GPG_KEYRING")"
  sleep 1
  export BOTSQUAD_DOCKER_CHECK_CMD="true"
  step_install_docker \
    || fail "case2: idempotent re-run returned non-zero"
  local mt_list2 mt_key2
  mt_list2="$(stat -c '%Y' "$BOTSQUAD_DOCKER_APT_LIST")"
  mt_key2="$(stat -c '%Y' "$BOTSQUAD_DOCKER_GPG_KEYRING")"
  [[ "$mt_list" = "$mt_list2" ]] \
    || fail "case2: apt list rewritten on idempotent re-run ($mt_list → $mt_list2)"
  [[ "$mt_key" = "$mt_key2" ]] \
    || fail "case2: gpg keyring rewritten on idempotent re-run"
  n="$(wc -l < "$WORK/install.calls")"; [[ "$n" -eq 1 ]] \
    || fail "case2: install cmd invoked again on idempotent re-run (count=$n)"
  n="$(wc -l < "$WORK/usermod.calls")"; [[ "$n" -eq 1 ]] \
    || fail "case2: usermod invoked again on idempotent re-run (count=$n)"

  pass "case 2: fresh-host install writes signed-by apt list, fires all commands, idempotent re-run is a no-op"

  unset BOTSQUAD_DOCKER_CHECK_CMD BOTSQUAD_DOCKER_GPG_FETCH_CMD \
        BOTSQUAD_DOCKER_INSTALL_CMD BOTSQUAD_DOCKER_USERMOD_CMD
}

# --- Case 3: apt install failure → fatal, no usermod attempted ---------------
case3() {
  reset_sinks
  arm_fresh_stubs
  # Replace install stub with a failing one.
  export BOTSQUAD_DOCKER_INSTALL_CMD='echo "E: Unable to locate package docker-ce" >&2; exit 100'
  export BOTSQUAD_DOCKER_CHECK_CMD="false"

  CURRENT_CHECKPOINT=install_docker
  local err_log="$WORK/install_docker.err"
  if ( step_install_docker ) 2>"$err_log"; then
    fail "case3: step_install_docker unexpectedly succeeded with failing apt install"
  fi
  grep -q "install_docker" "$err_log" \
    || fail "case3: checkpoint name not in failure block; got: $(cat "$err_log")"
  grep -q "apt-get install" "$err_log" \
    || fail "case3: install failure block missing 'apt-get install' wording"

  # usermod must NOT have run (apt install failed first).
  local n; n="$(wc -l < "$WORK/usermod.calls")"
  [[ "$n" -eq 0 ]] \
    || fail "case3: usermod was attempted after install failure (count=$n)"
  pass "case 3: apt install failure raises structured error; no usermod"

  unset BOTSQUAD_DOCKER_INSTALL_CMD BOTSQUAD_DOCKER_USERMOD_CMD \
        BOTSQUAD_DOCKER_GPG_FETCH_CMD BOTSQUAD_DOCKER_CHECK_CMD
}

# --- Case 4: post-install needs re-login → structured "newgrp docker" -------
case4() {
  reset_sinks
  arm_fresh_stubs
  # Probe ALWAYS fails (compose can't reach the socket because we haven't
  # logged out + back in yet) — but the `docker` binary IS on PATH (we
  # stub command-v-docker via a fake docker on PATH).
  local fake_path="$WORK/fakebin"
  mkdir -p "$fake_path"
  cat > "$fake_path/docker" <<'EOF'
#!/usr/bin/env bash
# Real docker would print version + exit 0; for this test we only need
# the binary to exist on PATH (command -v docker succeeds).
exit 0
EOF
  chmod +x "$fake_path/docker"
  local saved_path="$PATH"
  export PATH="$fake_path:$PATH"
  # Probe fails: 'docker compose version' returns non-zero (e.g.
  # permission denied on /var/run/docker.sock).
  export BOTSQUAD_DOCKER_CHECK_CMD="false"

  CURRENT_CHECKPOINT=install_docker
  local err_log="$WORK/install_docker.err"
  if ( step_install_docker ) 2>"$err_log"; then
    fail "case4: step_install_docker succeeded despite probe failing"
  fi
  grep -q "install_docker" "$err_log" \
    || fail "case4: checkpoint name not in failure block"
  grep -q "newgrp docker" "$err_log" \
    || fail "case4: 'newgrp docker' guidance missing; got: $(cat "$err_log")"
  grep -qE "(log out|fixed at session start|group membership)" "$err_log" \
    || fail "case4: re-login rationale not surfaced in failure block"
  grep -q "NOT a script bug" "$err_log" \
    || fail "case4: failure block should clarify this is not a script bug"

  export PATH="$saved_path"
  pass "case 4: post-install login-required raises structured 'newgrp docker' error"

  unset BOTSQUAD_DOCKER_CHECK_CMD BOTSQUAD_DOCKER_GPG_FETCH_CMD \
        BOTSQUAD_DOCKER_INSTALL_CMD BOTSQUAD_DOCKER_USERMOD_CMD
}

# --- Case 5: codename detection failure → structured guidance ---------------
case5() {
  reset_sinks
  # Force codename detection to come up empty.
  local saved_codename="$BOTSQUAD_DOCKER_CODENAME"
  export BOTSQUAD_DOCKER_CODENAME=""
  # Build a sealed env: no /etc/os-release readable AND no lsb_release on PATH.
  # We can't unmount /etc/os-release in a test, so instead we invoke
  # docker_write_apt_list directly with a wrapper that nukes the detection
  # signals.
  local probe_stub='exit 1'
  export BOTSQUAD_DOCKER_CHECK_CMD="$probe_stub"

  # Use a subshell so the empty PATH and source-overrides don't leak.
  local err_log="$WORK/codename.err"
  set +e
  (
    # Override docker_detect_codename to return empty, simulating a host
    # whose /etc/os-release has no VERSION_CODENAME.
    docker_detect_codename() { printf ''; }
    CURRENT_CHECKPOINT=install_docker
    docker_write_apt_list "$BOTSQUAD_DOCKER_APT_LIST" "$BOTSQUAD_DOCKER_GPG_KEYRING"
  ) 2>"$err_log"
  local rc=$?
  set -e
  [[ "$rc" -ne 0 ]] \
    || fail "case5: docker_write_apt_list should have died on empty codename"
  grep -q "install_docker" "$err_log" \
    || fail "case5: checkpoint name not in failure block"
  grep -q "BOTSQUAD_DOCKER_CODENAME" "$err_log" \
    || fail "case5: failure block should mention the BOTSQUAD_DOCKER_CODENAME override"
  grep -q "T-0030" "$err_log" \
    || fail "case5: failure block should point at the cross-distro follow-on (T-0030)"

  export BOTSQUAD_DOCKER_CODENAME="$saved_codename"
  pass "case 5: empty codename ⇒ structured error pointing at BOTSQUAD_DOCKER_CODENAME / T-0030"
}

# --- Case 6: pipe-curl-to-bash NOT used (style + DoD note) ------------------
case6() {
  # Static check: install.sh must not pipe curl directly to bash/sh in
  # the docker-install path. The signed-by-gpg-key pattern requires
  # writing the key to a file under /etc/apt/keyrings, not executing it.
  if grep -nE 'curl[^|]*\|[[:space:]]*(sudo[[:space:]]+)?(ba)?sh' "$INSTALL_SH" \
       | grep -vi 'nodesource' >/dev/null; then
    fail "case6: install.sh appears to pipe curl into a shell outside the NodeSource lane"
  fi
  # Sanity: the docker repo URL is referenced under download.docker.com.
  grep -q "download.docker.com/linux" "$INSTALL_SH" \
    || fail "case6: install.sh missing the download.docker.com repo URL"
  # Sanity: the five expected apt packages are installed together.
  grep -q "docker-ce docker-ce-cli containerd.io" "$INSTALL_SH" \
    || fail "case6: docker-ce / docker-ce-cli / containerd.io triple missing"
  grep -q "docker-buildx-plugin docker-compose-plugin" "$INSTALL_SH" \
    || fail "case6: docker-buildx-plugin + docker-compose-plugin missing"
  pass "case 6: install path uses signed-by pattern (no pipe-curl-to-bash) and pins the 5 docker apt packages"
}

case1
case2
case3
case4
case5
case6

echo ""
echo "INSTALL_DOCKER: all assertions passed"
