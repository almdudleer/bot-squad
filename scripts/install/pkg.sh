#!/usr/bin/env bash
# Package-manager abstraction for the bot-squad installer (T-0030).
#
# Sourced by install.sh. Exposes a tiny logical-package API so the rest
# of the installer doesn't have to know whether the host runs apt, dnf
# or pacman:
#
#   pkg_install <logical>...   Install one or more logical packages.
#                              Logical names are mapped per-family; an
#                              empty mapping (e.g. python3-venv on a
#                              family where venv ships with python3)
#                              silently skips that name.
#   pkg_update                 Refresh the package index. No-op on
#                              managers that don't need a separate
#                              refresh step (pacman -Sy is run because
#                              the alternative is implicit -Syu which
#                              upgrades the whole world).
#   pkg_have <bin>             Wrapper for `command -v <bin>` — exists
#                              for symmetry with the rest of the API.
#
# The detected distro family ("debian" | "fedora" | "arch" | "alpine" |
# "nixos") is taken from $BOTSQUAD_DISTRO_FAMILY when set, or derived
# from /etc/os-release at source-time by detect_distro_family.
# install.sh's detect_distro checkpoint runs first and is responsible
# for persisting the family to the state file (so a re-run on a
# different distro errors loudly rather than silently picking the
# wrong manager). The "nixos" family is recognized but intentionally
# has no pkg_install / pkg_update branch (T-0057) — install.sh routes
# NixOS to a declarative emit-module step instead, and pkg_* calls on
# family=nixos die loudly with the default-case error.
#
# Test seams (all default to the family-appropriate sudo+manager
# invocation; the unit tests stub these to capture call args):
#
#   BOTSQUAD_PKG_INSTALL_CMD   command receiving the resolved native
#                              package names as $1..$N. Default: family
#                              install command (apt-get / dnf / pacman)
#                              with sudo + DEBIAN_FRONTEND=noninteractive
#                              on debian.
#   BOTSQUAD_PKG_UPDATE_CMD    command receiving no args. Default:
#                              family update command (or no-op).

# detect_distro_family — print the detected family name to stdout.
# Mapping rules (matches /etc/os-release ID and ID_LIKE):
#   ubuntu / debian / linuxmint / pop / raspbian       → debian
#   fedora / rhel / centos / rocky / almalinux / amzn  → fedora
#   arch / manjaro / endeavouros / cachyos             → arch
#   alpine                                             → alpine
#   nixos                                              → nixos (T-0057;
#       declarative-only — pkg_install/pkg_update have no nixos branch
#       and will die loudly if called)
# Anything else prints empty (caller must die_struct).
detect_distro_family() {
  if [[ -n "${BOTSQUAD_DISTRO_FAMILY:-}" ]]; then
    printf '%s' "$BOTSQUAD_DISTRO_FAMILY"
    return 0
  fi
  local id="" id_like=""
  if [[ -r /etc/os-release ]]; then
    # shellcheck source=/dev/null
    id="$( . /etc/os-release && printf '%s' "${ID:-}" )"
    # shellcheck source=/dev/null
    id_like="$( . /etc/os-release && printf '%s' "${ID_LIKE:-}" )"
  fi
  local token
  for token in $id $id_like; do
    case "$token" in
      ubuntu|debian|linuxmint|pop|raspbian)
        printf '%s' debian; return 0 ;;
      fedora|rhel|centos|rocky|almalinux|amzn|ol)
        printf '%s' fedora; return 0 ;;
      arch|manjaro|endeavouros|cachyos)
        printf '%s' arch; return 0 ;;
      alpine)
        printf '%s' alpine; return 0 ;;
      nixos)
        printf '%s' nixos; return 0 ;;
    esac
  done
  printf ''
}

# pkg_have <bin> — 0 iff $bin is on PATH.
pkg_have() {
  command -v "$1" >/dev/null 2>&1
}

# pkg_map_one <family> <logical> — print the native package name(s) for
# this logical on this family, space-separated, or empty if no install
# is needed on this family (e.g. python3-venv on fedora/arch, where
# venv ships with python3). Unknown logicals print themselves
# unchanged (treated as a pass-through to the native manager).
pkg_map_one() {
  local family="$1" logical="$2"
  case "$family:$logical" in
    debian:curl)            printf 'curl' ;;
    debian:git)             printf 'git' ;;
    debian:jq)              printf 'jq' ;;
    debian:tmux)            printf 'tmux' ;;
    debian:ca-certificates) printf 'ca-certificates' ;;
    debian:nodejs)          printf 'nodejs' ;;
    debian:python3)         printf 'python3' ;;
    debian:python3-venv)    printf 'python3-venv' ;;
    debian:python3-pip)     printf 'python3-pip' ;;

    fedora:curl)            printf 'curl' ;;
    fedora:git)             printf 'git' ;;
    fedora:jq)              printf 'jq' ;;
    fedora:tmux)            printf 'tmux' ;;
    fedora:ca-certificates) printf 'ca-certificates' ;;
    # NodeSource RPM provides 'nodejs' that bundles npm; the plain
    # fedora package does too. Either works for our purposes.
    fedora:nodejs)          printf 'nodejs' ;;
    fedora:python3)         printf 'python3' ;;
    # Fedora's python3 ships venv built-in; no separate package.
    fedora:python3-venv)    printf '' ;;
    fedora:python3-pip)     printf 'python3-pip' ;;

    arch:curl)              printf 'curl' ;;
    arch:git)               printf 'git' ;;
    arch:jq)                printf 'jq' ;;
    arch:tmux)              printf 'tmux' ;;
    arch:ca-certificates)   printf 'ca-certificates' ;;
    # nodejs + npm are separate packages on Arch and npm is required
    # for the install_claude_code step that follows.
    arch:nodejs)            printf 'nodejs npm' ;;
    arch:python3)           printf 'python' ;;
    # Arch's python ships venv built-in.
    arch:python3-venv)      printf '' ;;
    arch:python3-pip)       printf 'python-pip' ;;

    alpine:curl)            printf 'curl' ;;
    alpine:git)             printf 'git' ;;
    alpine:jq)              printf 'jq' ;;
    alpine:tmux)            printf 'tmux' ;;
    alpine:ca-certificates) printf 'ca-certificates' ;;
    # Alpine's main repo ships nodejs + npm as separate packages, same
    # shape as Arch (npm is required for install_claude_code).
    alpine:nodejs)          printf 'nodejs npm' ;;
    alpine:python3)         printf 'python3' ;;
    # Alpine's python3 ships venv built-in (since 3.12); no separate package.
    alpine:python3-venv)    printf '' ;;
    # Alpine uses py3-* prefix for python3 modules from the system index.
    alpine:python3-pip)     printf 'py3-pip' ;;

    *)
      # Unknown family or unknown logical → pass-through (last-resort
      # so a caller can ask for a literal native package by name).
      printf '%s' "$logical"
      ;;
  esac
}

# pkg_install <logical>... — resolve every argument to its native
# package name(s) for the current family, then invoke the family's
# install command with the deduped list. Empty mappings are skipped.
pkg_install() {
  local family; family="$(detect_distro_family)"
  if [[ -z "$family" ]]; then
    echo "[pkg.sh] pkg_install called without a detected distro family" >&2
    return 1
  fi
  local logical resolved
  local -a pkgs=()
  for logical in "$@"; do
    resolved="$(pkg_map_one "$family" "$logical")"
    [[ -z "$resolved" ]] && continue
    # resolved may be space-separated.
    local p
    for p in $resolved; do
      pkgs+=("$p")
    done
  done
  if [[ "${#pkgs[@]}" -eq 0 ]]; then
    return 0
  fi
  if [[ -n "${BOTSQUAD_PKG_INSTALL_CMD:-}" ]]; then
    bash -c "$BOTSQUAD_PKG_INSTALL_CMD \"\$@\"" _ "${pkgs[@]}"
    return $?
  fi
  case "$family" in
    debian)
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}" >/dev/null
      ;;
    fedora)
      sudo dnf install -y "${pkgs[@]}" >/dev/null
      ;;
    arch)
      sudo pacman -S --noconfirm --needed "${pkgs[@]}" >/dev/null
      ;;
    alpine)
      # --no-cache keeps /var/cache/apk/ empty (Alpine convention) and
      # implicitly refreshes the index so a separate `apk update` isn't
      # required before this call.
      sudo apk add --no-cache "${pkgs[@]}" >/dev/null
      ;;
    *)
      echo "[pkg.sh] no install command for family '$family'" >&2
      return 1
      ;;
  esac
}

# pkg_update — refresh the local package index.
#   - debian: apt-get update -y
#   - fedora: dnf makecache (cheap; check-update would exit-code 100
#             on "updates available" which we'd have to special-case)
#   - arch:   pacman -Sy   (sync only; no -u, we don't want to upgrade
#             the world from inside an installer)
#   - alpine: apk update   (sync only; --no-cache on pkg_install is the
#             usual idiom, but a standalone refresh is still cheap)
pkg_update() {
  if [[ -n "${BOTSQUAD_PKG_UPDATE_CMD:-}" ]]; then
    bash -c "$BOTSQUAD_PKG_UPDATE_CMD"
    return $?
  fi
  local family; family="$(detect_distro_family)"
  case "$family" in
    debian) sudo apt-get update -y >/dev/null ;;
    fedora) sudo dnf -y makecache >/dev/null ;;
    arch)   sudo pacman -Sy --noconfirm >/dev/null ;;
    alpine) sudo apk update >/dev/null ;;
    *)
      echo "[pkg.sh] no update command for family '$family'" >&2
      return 1
      ;;
  esac
}
