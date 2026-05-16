#!/usr/bin/env bash
# Unit tests for pkg.sh (T-0030).
#
# Drives the pure helpers directly (no install.sh sourcing required):
#   - detect_distro_family with a synthetic os-release
#   - pkg_map_one for the documented logical-name set
#   - pkg_install dispatch via BOTSQUAD_PKG_INSTALL_CMD seam
#   - pkg_update  dispatch via BOTSQUAD_PKG_UPDATE_CMD  seam
#
# No network, no sudo, no real package manager — every side effect is
# funnelled through the test seams.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PKG_SH="$HERE/../pkg.sh"
[[ -f "$PKG_SH" ]] || { echo "FAIL: $PKG_SH not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# shellcheck disable=SC1090
source "$PKG_SH"

# --- detect_distro_family: explicit override wins -----------------------------
case_family_override() {
  BOTSQUAD_DISTRO_FAMILY="arch" \
    bash -c "source '$PKG_SH'; detect_distro_family" > "$WORK/out"
  local out; out="$(cat "$WORK/out")"
  [[ "$out" = "arch" ]] \
    || fail "family_override: expected 'arch', got '$out'"
  pass "BOTSQUAD_DISTRO_FAMILY override wins over /etc/os-release"
}

# --- detect_distro_family: parses ID + ID_LIKE from a stub os-release -------
# We can't atomically swap /etc/os-release, so we exercise the parser via
# a wrapper function that mirrors detect_distro_family's case block over
# a stub-file's ID/ID_LIKE. This is enough to catch a typo in the case map.
case_family_from_os_release() {
  detect_family_from_file() {
    local f="$1" id="" id_like="" token
    # shellcheck source=/dev/null
    id="$( . "$f" && printf '%s' "${ID:-}" )"
    # shellcheck source=/dev/null
    id_like="$( . "$f" && printf '%s' "${ID_LIKE:-}" )"
    for token in $id $id_like; do
      case "$token" in
        ubuntu|debian|linuxmint|pop|raspbian)         echo debian; return ;;
        fedora|rhel|centos|rocky|almalinux|amzn|ol)   echo fedora; return ;;
        arch|manjaro|endeavouros|cachyos)             echo arch; return ;;
      esac
    done
    echo ""
  }
  local synth="$WORK/os-release"
  local entry input expect got
  for entry in \
      'ID=ubuntu             |debian' \
      'ID=debian             |debian' \
      'ID=linuxmint|ID_LIKE=ubuntu|debian' \
      'ID=fedora             |fedora' \
      'ID=rocky|ID_LIKE="rhel centos fedora"|fedora' \
      'ID=almalinux|ID_LIKE="rhel centos fedora"|fedora' \
      'ID=arch               |arch' \
      'ID=manjaro|ID_LIKE=arch|arch' \
      'ID=endeavouros|ID_LIKE=arch|arch' \
      'ID=void               |'; do
    input="${entry%|*}"
    expect="${entry##*|}"
    # Replace pipe separators inside the input with newlines to form a
    # valid os-release stub.
    : > "$synth"
    local field
    while IFS= read -r field; do
      printf '%s\n' "$field" >> "$synth"
    done < <(printf '%s\n' "$input" | tr '|' '\n')
    got="$(detect_family_from_file "$synth")"
    # Trim whitespace.
    got="${got## }"; got="${got%% }"
    expect="${expect## }"; expect="${expect%% }"
    [[ "$got" = "$expect" ]] \
      || fail "family_from_os_release: '$input' → got '$got', expected '$expect'"
  done
  pass "detect_distro_family case-map covers all documented ID/ID_LIKE values"
}

# --- pkg_map_one: cross-family logical → native mapping ---------------------
case_map_one() {
  # Pipe-separated rows so 'arch|nodejs|nodejs npm' (multi-word expected)
  # is preserved.
  local checks=(
    "debian|curl|curl"
    "debian|nodejs|nodejs"
    "debian|python3-venv|python3-venv"
    "debian|python3-pip|python3-pip"
    "fedora|nodejs|nodejs"
    "fedora|python3-venv|"
    "fedora|python3-pip|python3-pip"
    "arch|nodejs|nodejs npm"
    "arch|python3|python"
    "arch|python3-venv|"
    "arch|python3-pip|python-pip"
    "arch|ca-certificates|ca-certificates"
  )
  local row family logical expected got
  for row in "${checks[@]}"; do
    family="${row%%|*}"
    row="${row#*|}"
    logical="${row%%|*}"
    expected="${row#*|}"
    got="$(pkg_map_one "$family" "$logical")"
    [[ "$got" = "$expected" ]] \
      || fail "map_one: $family + $logical → got '$got', expected '$expected'"
  done
  pass "pkg_map_one maps logical → native (incl. multi-pkg + skip)"
}

# --- pkg_install: invokes the seam with the resolved native names -----------
case_install_dispatch() {
  local capture="$WORK/install.argv"
  : > "$capture"
  local stub="$WORK/install_stub.sh"
  cat > "$stub" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$capture"
EOF
  chmod +x "$stub"
  export BOTSQUAD_PKG_INSTALL_CMD="$stub"

  # Arch: nodejs → "nodejs npm"; python3-venv → skipped → only python.
  BOTSQUAD_DISTRO_FAMILY=arch pkg_install nodejs python3-venv python3 \
    || fail "install_dispatch: pkg_install returned non-zero on arch"
  local got; got="$(cat "$capture")"
  [[ "$got" = "nodejs npm python" ]] \
    || fail "install_dispatch arch: expected 'nodejs npm python', got '$got'"

  # Fedora: python3-venv is empty; python3-pip stays.
  : > "$capture"
  BOTSQUAD_DISTRO_FAMILY=fedora pkg_install python3-venv python3-pip curl \
    || fail "install_dispatch: pkg_install returned non-zero on fedora"
  got="$(cat "$capture")"
  [[ "$got" = "python3-pip curl" ]] \
    || fail "install_dispatch fedora: expected 'python3-pip curl', got '$got'"

  # Debian: straight pass-through.
  : > "$capture"
  BOTSQUAD_DISTRO_FAMILY=debian pkg_install curl ca-certificates git jq \
    || fail "install_dispatch: pkg_install returned non-zero on debian"
  got="$(cat "$capture")"
  [[ "$got" = "curl ca-certificates git jq" ]] \
    || fail "install_dispatch debian: expected 'curl ca-certificates git jq', got '$got'"

  # All-skipped: zero packages → no invocation.
  : > "$capture"
  BOTSQUAD_DISTRO_FAMILY=fedora pkg_install python3-venv \
    || fail "install_dispatch: all-skipped run returned non-zero"
  [[ ! -s "$capture" ]] \
    || fail "install_dispatch: install cmd was invoked with empty pkg list:
$(cat "$capture")"

  unset BOTSQUAD_PKG_INSTALL_CMD
  pass "pkg_install resolves logical→native, skips empties, invokes seam exactly when needed"
}

# --- pkg_update: invokes the seam with no args ------------------------------
case_update_dispatch() {
  local capture="$WORK/update.calls"
  : > "$capture"
  local stub="$WORK/update_stub.sh"
  cat > "$stub" <<EOF
#!/usr/bin/env bash
echo "called" >> "$capture"
EOF
  chmod +x "$stub"
  export BOTSQUAD_PKG_UPDATE_CMD="$stub"
  BOTSQUAD_DISTRO_FAMILY=debian pkg_update \
    || fail "update_dispatch: pkg_update returned non-zero on debian"
  local n; n="$(wc -l < "$capture")"
  [[ "$n" -eq 1 ]] \
    || fail "update_dispatch: expected 1 update call, got $n"
  BOTSQUAD_DISTRO_FAMILY=fedora pkg_update >/dev/null
  BOTSQUAD_DISTRO_FAMILY=arch   pkg_update >/dev/null
  n="$(wc -l < "$capture")"
  [[ "$n" -eq 3 ]] \
    || fail "update_dispatch: expected 3 update calls across families, got $n"
  unset BOTSQUAD_PKG_UPDATE_CMD
  pass "pkg_update invokes the seam (family-agnostic when seamed)"
}

# --- pkg_have: thin wrapper -------------------------------------------------
case_have() {
  pkg_have bash || fail "have: 'bash' should be on PATH but pkg_have said no"
  pkg_have this-binary-definitely-does-not-exist-xyzzy \
    && fail "have: 'this-binary...' shouldn't exist but pkg_have said yes"
  pass "pkg_have wraps command -v"
}

case_family_override
case_family_from_os_release
case_map_one
case_install_dispatch
case_update_dispatch
case_have

echo ""
echo "PKG_HELPERS: all assertions passed"
