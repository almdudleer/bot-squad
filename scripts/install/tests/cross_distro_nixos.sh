#!/usr/bin/env bash
# Cross-distro smoke for the NixOS install path (T-0057).
#
# NixOS is declarative: install.sh cannot apt-get/dnf/pacman/apk docker
# on it. The expected behavior is:
#   1. detect_distro recognizes ID=nixos and persists family=nixos.
#   2. main() picks the NIXOS_STEPS chain (just detect_distro +
#      emit_nixos_module) — NOT INSTALL_STEPS — when family=nixos.
#   3. step_emit_nixos_module copies scripts/install/nixos/bot-squad.nix
#      into <install_dir>/nixos/bot-squad.nix and prints the admin's
#      nixos-rebuild instructions block.
#   4. pkg_install and pkg_update are NEVER invoked on family=nixos
#      (they'd hit pkg.sh's default-case die path with a loud error).
#
# All side effects are funnelled through seams (no real
# nixos-rebuild / docker invoked). No opt-in E2E lane here — there's
# no `nixos:latest` docker image we can drive end-to-end the way the
# fedora / arch / alpine tests do.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="$HERE/../install.sh"
NIXOS_MODULE_SRC="$HERE/../nixos/bot-squad.nix"
[[ -f "$INSTALL_SH" ]] || { echo "FAIL: $INSTALL_SH not found"; exit 1; }
[[ -f "$NIXOS_MODULE_SRC" ]] || { echo "FAIL: $NIXOS_MODULE_SRC not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# --- Unit lane: stub seams, source install.sh in-process --------------------
TMP_SH="$WORK/install.engine.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"
export BOTSQUAD_PKG_SH="$HERE/../pkg.sh"
# The test runs install.sh from a sed-copy in $WORK, which orphans the
# __BS_INSTALL_DIR-based source-of-truth lookup. Pin the source path
# explicitly so step_emit_nixos_module finds the canonical module.
export BOTSQUAD_NIXOS_MODULE_SRC="$NIXOS_MODULE_SRC"

export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_INSTALL_DIR="$WORK/install"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
export BOTSQUAD_OPERATOR_SESSION="bs-cd-nixos-$$"
export BOTSQUAD_DISTRO_FAMILY="nixos"
mkdir -p "$WORK/install"

# shellcheck disable=SC1090
source "$TMP_SH"
ensure_state_dir
log()  { :; }
warn() { :; }
trap - ERR

# --- Case 1: detect_distro records family=nixos -----------------------------
case1() {
  rm -f "$DISTRO_STATE_FILE"
  CURRENT_CHECKPOINT=detect_distro
  step_detect_distro \
    || fail "case1: step_detect_distro returned non-zero with family=nixos override"
  grep -qx "BOTSQUAD_DISTRO_FAMILY=nixos" "$DISTRO_STATE_FILE" \
    || fail "case1: state file missing BOTSQUAD_DISTRO_FAMILY=nixos; got:
$(cat "$DISTRO_STATE_FILE")"
  pass "case 1: detect_distro persists family=nixos to $DISTRO_STATE_FILE"
}

# --- Case 2: detect_distro_family resolves ID=nixos from /etc/os-release ----
case2() {
  # Stand up a sandbox bash that points detect_distro_family at a fake
  # os-release file. We can't override /etc/os-release on the host;
  # instead we re-source pkg.sh after monkey-patching the open path via
  # a wrapper function. Done in a subshell so our environment isn't
  # polluted with the patched function.
  local fake="$WORK/os-release.nixos"
  cat > "$fake" <<'EOF'
ID=nixos
NAME="NixOS"
VERSION="24.05 (Uakari)"
EOF
  local got
  got="$(BOTSQUAD_DISTRO_FAMILY='' bash <<EOF
. "$BOTSQUAD_PKG_SH"
# Patch detect_distro_family to read from the fake file instead of
# /etc/os-release. The case-statement body must match pkg.sh — we
# verify nixos resolves to "nixos" specifically.
detect_distro_family() {
  local id id_like
  id="\$( . "$fake" && printf '%s' "\${ID:-}" )"
  id_like="\$( . "$fake" && printf '%s' "\${ID_LIKE:-}" )"
  local token
  for token in \$id \$id_like; do
    case "\$token" in
      ubuntu|debian|linuxmint|pop|raspbian) printf '%s' debian; return 0 ;;
      fedora|rhel|centos|rocky|almalinux|amzn|ol) printf '%s' fedora; return 0 ;;
      arch|manjaro|endeavouros|cachyos) printf '%s' arch; return 0 ;;
      alpine) printf '%s' alpine; return 0 ;;
      nixos) printf '%s' nixos; return 0 ;;
    esac
  done
  printf ''
}
detect_distro_family
EOF
)"
  [[ "$got" = "nixos" ]] \
    || fail "case2: detect_distro_family didn't resolve ID=nixos → 'nixos' (got '$got')"
  # And confirm the real pkg.sh actually contains the nixos branch — so
  # the patched-function test above isn't passing while pkg.sh diverges.
  grep -qE '^[[:space:]]*nixos\)' "$BOTSQUAD_PKG_SH" \
    || fail "case2: pkg.sh's detect_distro_family is missing the 'nixos)' case"
  pass "case 2: detect_distro_family resolves ID=nixos (and pkg.sh has the nixos case)"
}

# --- Case 3: main() picks NIXOS_STEPS when family=nixos --------------------
case3() {
  # NIXOS_STEPS must be declared and short — detect_distro +
  # emit_nixos_module, nothing else. The full INSTALL_STEPS chain
  # would (incorrectly) invoke apt/dnf/pacman/apk via pkg_install,
  # which is a configuration anti-pattern on NixOS.
  declare -p NIXOS_STEPS >/dev/null 2>&1 \
    || fail "case3: NIXOS_STEPS array not declared in install.sh"
  local n="${#NIXOS_STEPS[@]}"
  [[ "$n" -eq 2 ]] \
    || fail "case3: NIXOS_STEPS has $n entries (expected 2: detect_distro + emit_nixos_module)"
  [[ "${NIXOS_STEPS[0]}" = "detect_distro" ]] \
    || fail "case3: NIXOS_STEPS[0]='${NIXOS_STEPS[0]}' (expected 'detect_distro')"
  [[ "${NIXOS_STEPS[1]}" = "emit_nixos_module" ]] \
    || fail "case3: NIXOS_STEPS[1]='${NIXOS_STEPS[1]}' (expected 'emit_nixos_module')"
  # NIXOS_STEPS must NOT contain any of the imperative pkg steps.
  local forbidden
  for forbidden in install_base_pkgs install_tmux install_nodejs \
                   install_claude_code install_docker pkg_index_update \
                   docker_compose_up clone_repo; do
    if printf '%s\n' "${NIXOS_STEPS[@]}" | grep -qxF "$forbidden"; then
      fail "case3: NIXOS_STEPS contains '$forbidden' — declarative path must skip it"
    fi
  done
  pass "case 3: NIXOS_STEPS is (detect_distro, emit_nixos_module) — no pkg/clone/compose steps"
}

# --- Case 4: emit_nixos_module writes the module + prints instructions -----
case4() {
  local dst="$WORK/install/nixos/bot-squad.nix"
  rm -rf "$WORK/install/nixos"
  CURRENT_CHECKPOINT=emit_nixos_module
  local out
  out="$(step_emit_nixos_module 2>/dev/null)" \
    || fail "case4: step_emit_nixos_module failed"
  [[ -f "$dst" ]] \
    || fail "case4: $dst not written"
  # The dst file must be byte-identical to the canonical source.
  cmp -s "$NIXOS_MODULE_SRC" "$dst" \
    || fail "case4: $dst does not match canonical $NIXOS_MODULE_SRC"
  # The module must declare the DoD-required surface: docker, nodejs,
  # python3, tmux, ca-certificates, a bot-squad systemd unit.
  grep -q 'virtualisation.docker.enable' "$dst" \
    || fail "case4: module missing 'virtualisation.docker.enable'"
  grep -q 'nodejs_20\|nodejs' "$dst" \
    || fail "case4: module missing nodejs declaration"
  grep -q 'python3' "$dst" \
    || fail "case4: module missing python3 declaration"
  grep -q 'tmux' "$dst" \
    || fail "case4: module missing tmux"
  grep -q 'cacert\|ca-certificates' "$dst" \
    || fail "case4: module missing ca-certificates/cacert"
  grep -q 'systemd.services.bot-squad-worker' "$dst" \
    || fail "case4: module missing bot-squad-worker systemd unit"
  # The printed instructions block must point the admin at
  # nixos-rebuild switch and the dst path.
  printf '%s' "$out" | grep -q 'nixos-rebuild switch' \
    || fail "case4: instructions block does not mention 'nixos-rebuild switch':
--- begin block ---
$out
--- end block ---"
  printf '%s' "$out" | grep -qF "$dst" \
    || fail "case4: instructions block does not mention $dst"
  pass "case 4: emit_nixos_module writes $dst (byte-identical to canonical) + prints rebuild instructions"
}

# --- Case 5: emit_nixos_module is idempotent (re-run rewrites cleanly) -----
case5() {
  local dst="$WORK/install/nixos/bot-squad.nix"
  # First call wrote dst (case4). Mutate it, re-run, confirm it's
  # restored to the canonical contents.
  printf '%s\n' "# tampered" >> "$dst"
  CURRENT_CHECKPOINT=emit_nixos_module
  step_emit_nixos_module >/dev/null \
    || fail "case5: idempotent re-run of step_emit_nixos_module failed"
  cmp -s "$NIXOS_MODULE_SRC" "$dst" \
    || fail "case5: re-run did not restore $dst to canonical source"
  pass "case 5: emit_nixos_module re-run restores canonical module bytes"
}

# --- Case 6: pkg_install + pkg_update die loudly on family=nixos -----------
case6() {
  # Direct calls (NOT routed via NIXOS_STEPS, which doesn't include
  # them) must hit pkg.sh's default-case error rather than silently
  # picking a wrong manager.
  local rc out
  set +e
  out="$(pkg_install curl 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] \
    || fail "case6: pkg_install on family=nixos returned 0 (must die loudly):
$out"
  printf '%s' "$out" | grep -qF "no install command for family 'nixos'" \
    || fail "case6: pkg_install error doesn't mention family='nixos':
$out"
  set +e
  out="$(pkg_update 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] \
    || fail "case6: pkg_update on family=nixos returned 0 (must die loudly):
$out"
  printf '%s' "$out" | grep -qF "no update command for family 'nixos'" \
    || fail "case6: pkg_update error doesn't mention family='nixos':
$out"
  pass "case 6: pkg_install + pkg_update die loudly on family=nixos (no silent fallback)"
}

case1
case2
case3
case4
case5
case6

echo ""
echo "CROSS_DISTRO_NIXOS: all assertions passed"
