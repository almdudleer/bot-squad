#!/usr/bin/env bash
# Cross-distro smoke for the Arch install path (T-0030).
#
# Mirrors cross_distro_fedora.sh in structure, with these family-specific
# asserts:
#   1. detect_distro persists BOTSQUAD_DISTRO_FAMILY=arch.
#   2. install_base_pkgs uses arch-mapped names (curl/ca-certificates/git/jq
#      pass through unchanged on this family too).
#   3. install_docker on family=arch skips the Debian apt keyring AND the
#      Fedora dnf-config-manager dance — both are handled inside the
#      install seam by simply running `pacman -S docker docker-compose`.
#   4. nodejs install routes to `pacman -S nodejs npm` (logical "nodejs"
#      resolves to two native names on arch).
#   5. pkg_index_update routes through pkg_update.
#
# All side effects are funnelled through seams; no pacman / sudo invoked.
# Real archlinux:latest container E2E is gated behind BOTSQUAD_E2E=1.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# --- Optional E2E lane: real archlinux:latest container --------------------
if [[ "${BOTSQUAD_E2E:-0}" = "1" ]]; then
  command -v docker >/dev/null 2>&1 \
    || { echo "SKIP: BOTSQUAD_E2E=1 but docker not on PATH"; exit 0; }
  echo "running archlinux:latest container E2E (this is slow)..."
  docker run --rm -v "$HERE/..":/install:ro archlinux:latest bash -c '
    set -e
    pacman -Sy --noconfirm bash sudo curl >/dev/null 2>&1
    cd /tmp
    cp /install/install.sh /install/pkg.sh .
    export BOTSQUAD_STATE_DIR=/tmp/state BOTSQUAD_INSTALL_DIR=/tmp/install
    export BOTSQUAD_SKIP_MOTHERSHIP=1 BOTSQUAD_NONINTERACTIVE=1
    export BOTSQUAD_OPERATOR_SESSION=bs-arch-e2e-$$
    sed "s/^main \"\$@\"$/# sourced/" install.sh > install.engine.sh
    source install.engine.sh
    ensure_state_dir
    log() { :; }; warn() { :; }
    trap - ERR
    CURRENT_CHECKPOINT=detect_distro
    step_detect_distro
    fam="$(grep ^BOTSQUAD_DISTRO_FAMILY= /tmp/state/install.distro | cut -d= -f2)"
    [[ "$fam" = "arch" ]] || { echo "FAIL: e2e detect_distro = $fam"; exit 1; }
    echo "e2e: detect_distro = arch"
  ' || fail "arch E2E container run failed"
  pass "arch E2E (BOTSQUAD_E2E=1) succeeded"
  echo ""
  echo "CROSS_DISTRO_ARCH: E2E pass"
  exit 0
fi

# --- Unit lane: stub seams, source install.sh in-process --------------------
TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"
export BOTSQUAD_PKG_SH="$HERE/../pkg.sh"

export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_OPERATOR_SESSION="bs-cd-arch-$$"
export BOTSQUAD_DISTRO_FAMILY="arch"
export BOTSQUAD_DOCKER_GPG_KEYRING="$WORK/keyrings/docker.asc"
export BOTSQUAD_DOCKER_APT_LIST="$WORK/sources.list.d/docker.list"
mkdir -p "$WORK/keyrings" "$WORK/sources.list.d" "$WORK/install"

# shellcheck disable=SC1090
source "$TMP_SH"
ensure_state_dir
log()  { :; }
warn() { :; }
trap - ERR

# --- Case 1: detect_distro records family=arch ------------------------------
case1() {
  rm -f "$DISTRO_STATE_FILE"
  CURRENT_CHECKPOINT=detect_distro
  step_detect_distro \
    || fail "case1: step_detect_distro returned non-zero with family=arch override"
  grep -qx "BOTSQUAD_DISTRO_FAMILY=arch" "$DISTRO_STATE_FILE" \
    || fail "case1: state file missing BOTSQUAD_DISTRO_FAMILY=arch; got:
$(cat "$DISTRO_STATE_FILE")"
  pass "case 1: detect_distro persists family=arch to $DISTRO_STATE_FILE"
}

# --- Case 2: install_base_pkgs uses arch-mapped names -----------------------
case2() {
  local capture="$WORK/install_base.calls"
  : > "$capture"
  local stub="$WORK/install_base_stub.sh"
  cat > "$stub" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$capture"
EOF
  chmod +x "$stub"
  export BOTSQUAD_PKG_INSTALL_CMD="$stub"
  CURRENT_CHECKPOINT=install_base_pkgs
  step_install_base_pkgs \
    || fail "case2: step_install_base_pkgs failed on arch"
  local got; got="$(cat "$capture")"
  [[ "$got" = "curl ca-certificates git jq" ]] \
    || fail "case2: install seam invoked with '$got' (expected 'curl ca-certificates git jq')"
  unset BOTSQUAD_PKG_INSTALL_CMD
  pass "case 2: install_base_pkgs → arch native names via seam"
}

# --- Case 3: install_docker on arch is a single-shot pacman dispatch -------
case3() {
  rm -f "$BOTSQUAD_DOCKER_GPG_KEYRING" "$BOTSQUAD_DOCKER_APT_LIST"
  local capture="$WORK/docker_install.calls"
  : > "$capture"
  local stub="$WORK/docker_install_stub.sh"
  cat > "$stub" <<EOF
#!/usr/bin/env bash
echo "arch-pacman-install" >> "$capture"
EOF
  chmod +x "$stub"
  export BOTSQUAD_DOCKER_INSTALL_CMD="$stub"
  local usermod_stub="$WORK/usermod_stub.sh"
  cat > "$usermod_stub" <<EOF
#!/usr/bin/env bash
echo "usermod \$1" >> "$WORK/usermod.calls"
EOF
  chmod +x "$usermod_stub"
  export BOTSQUAD_DOCKER_USERMOD_CMD="$usermod_stub"
  : > "$WORK/usermod.calls"
  echo 0 > "$WORK/probe_counter"
  local probe_stub="$WORK/probe.sh"
  cat > "$probe_stub" <<EOF
#!/usr/bin/env bash
n=\$(cat "$WORK/probe_counter"); n=\$((n+1)); echo "\$n" > "$WORK/probe_counter"
[[ "\$n" -ge 2 ]]
EOF
  chmod +x "$probe_stub"
  export BOTSQUAD_DOCKER_CHECK_CMD="$probe_stub"
  # GPG fetch and apt-list writes should NOT happen on arch.
  export BOTSQUAD_DOCKER_GPG_FETCH_CMD='echo "should not gpg-fetch on arch" >&2; exit 99'

  CURRENT_CHECKPOINT=install_docker
  step_install_docker \
    || fail "case3: step_install_docker failed on family=arch"
  [[ ! -f "$BOTSQUAD_DOCKER_GPG_KEYRING" ]] \
    || fail "case3: GPG keyring written on arch (apt-only)"
  [[ ! -f "$BOTSQUAD_DOCKER_APT_LIST" ]] \
    || fail "case3: apt list written on arch (apt-only)"
  local n; n="$(wc -l < "$capture")"
  [[ "$n" -eq 1 ]] \
    || fail "case3: expected 1 install-cmd call on arch, got $n"
  unset BOTSQUAD_DOCKER_INSTALL_CMD BOTSQUAD_DOCKER_USERMOD_CMD \
        BOTSQUAD_DOCKER_CHECK_CMD BOTSQUAD_DOCKER_GPG_FETCH_CMD
  pass "case 3: install_docker on arch skips apt key/list, fires single pacman install"
}

# --- Case 4: install_nodejs maps to 'nodejs npm' on arch -------------------
case4() {
  local capture="$WORK/install_node.calls"
  : > "$capture"
  local stub="$WORK/install_node_stub.sh"
  cat > "$stub" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$capture"
EOF
  chmod +x "$stub"
  export BOTSQUAD_PKG_INSTALL_CMD="$stub"
  # Force the install branch by hiding any host `node`.
  local emptybin="$WORK/emptybin"; mkdir -p "$emptybin"
  local saved_path="$PATH"
  export PATH="$emptybin"
  # But we still need basic shell utilities — re-add /usr/bin and /bin.
  export PATH="$PATH:/usr/bin:/bin"
  CURRENT_CHECKPOINT=install_nodejs
  step_install_nodejs \
    || fail "case4: step_install_nodejs failed on arch"
  local got; got="$(cat "$capture")"
  [[ "$got" = "nodejs npm" ]] \
    || fail "case4: pkg_install seam called with '$got' (expected 'nodejs npm')"
  export PATH="$saved_path"
  unset BOTSQUAD_PKG_INSTALL_CMD
  pass "case 4: install_nodejs on arch routes 'nodejs' logical → 'nodejs npm' native"
}

# --- Case 5: pkg_index_update routes through pkg_update on arch ------------
case5() {
  local capture="$WORK/idx.calls"
  : > "$capture"
  local stub="$WORK/idx_stub.sh"
  cat > "$stub" <<EOF
#!/usr/bin/env bash
echo "called" >> "$capture"
EOF
  chmod +x "$stub"
  export BOTSQUAD_PKG_UPDATE_CMD="$stub"
  CURRENT_CHECKPOINT=pkg_index_update
  step_pkg_index_update \
    || fail "case5: step_pkg_index_update failed on arch"
  [[ -s "$capture" ]] \
    || fail "case5: pkg_index_update did not invoke the update seam"
  unset BOTSQUAD_PKG_UPDATE_CMD
  pass "case 5: pkg_index_update routes through pkg_update seam on arch"
}

case1
case2
case3
case4
case5

echo ""
echo "CROSS_DISTRO_ARCH: all assertions passed"
