#!/usr/bin/env bash
# bot-squad installer — idempotent, claude-supervised after bootstrap.
#
# This file is served by the mothership at /i/<token>/install.sh.
# The mothership substitutes the four __PLACEHOLDER__ lines below at serve
# time. When read raw from the repo, the env-var defaults take over so the
# script is runnable for local smoke (set BOTSQUAD_SKIP_MOTHERSHIP=1).
#
# Re-run safe: a second invocation after a partial failure resumes at the
# first incomplete checkpoint; a third invocation on a complete install is
# a no-op. State lives at $BOTSQUAD_STATE_DIR/install.state (one completed
# checkpoint name per line).
#
# On any non-zero exit, the script prints a structured "what + how to fix"
# block keyed by the failing checkpoint. The bootstrap-claude session reads
# that block, helps the user resolve, and re-invokes this same script.

set -Eeuo pipefail

# ---- Cross-distro package abstraction (T-0030) ------------------------------
# Source pkg.sh so the install_* steps can use pkg_install/pkg_update/pkg_have
# instead of raw apt-get. Search order:
#   1. $BOTSQUAD_PKG_SH if set (tests + advanced overrides)
#   2. <dirname of this script>/pkg.sh
#   3. <CWD>/pkg.sh (for `bash install.sh` from the repo)
# detect_distro errors loudly if none of the candidates loads the helper,
# so this stays a soft load (no early exit on missing file).
__BS_INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo .)"
for __bs_pkg_candidate in \
    "${BOTSQUAD_PKG_SH:-}" \
    "$__BS_INSTALL_DIR/pkg.sh" \
    "$PWD/pkg.sh" \
    "./pkg.sh"; do
  if [[ -n "$__bs_pkg_candidate" ]] && [[ -r "$__bs_pkg_candidate" ]]; then
    # shellcheck source=./pkg.sh
    . "$__bs_pkg_candidate"
    break
  fi
done
unset __bs_pkg_candidate

# ---- Mothership substitution targets (replaced at serve-time) ----------------
BOTSQUAD_INSTALL_TOKEN="${BOTSQUAD_INSTALL_TOKEN:-__INSTALL_TOKEN__}"
BOTSQUAD_MOTHERSHIP_URL="${BOTSQUAD_MOTHERSHIP_URL:-__MOTHERSHIP_URL__}"
BOTSQUAD_CLONE_URL="${BOTSQUAD_CLONE_URL:-__CLONE_URL__}"
BOTSQUAD_REPO_REF="${BOTSQUAD_REPO_REF:-__REPO_REF__}"

# ---- Tunables ---------------------------------------------------------------
BOTSQUAD_INSTALL_DIR="${BOTSQUAD_INSTALL_DIR:-/home/www/bot-squad}"
BOTSQUAD_GROUP="${BOTSQUAD_GROUP:-www}"
BOTSQUAD_STATE_DIR="${BOTSQUAD_STATE_DIR:-${HOME}/.bot-squad}"
BOTSQUAD_OPERATOR_SESSION="${BOTSQUAD_OPERATOR_SESSION:-bot-squad-operator}"
# Cross-distro support (T-0030). When unset, detect_distro infers the
# family from /etc/os-release; when set, the explicit value wins and
# is persisted to the state file. Valid families: debian, fedora, arch.
BOTSQUAD_DISTRO_FAMILY="${BOTSQUAD_DISTRO_FAMILY:-}"
# Skip the mothership /connect handshake — for offline smoke only.
BOTSQUAD_SKIP_MOTHERSHIP="${BOTSQUAD_SKIP_MOTHERSHIP:-0}"
# Non-interactive mode (CI / smoke): refuse to prompt; fail with a clear
# checkpoint instead so claude can be told what env to set on rerun.
BOTSQUAD_NONINTERACTIVE="${BOTSQUAD_NONINTERACTIVE:-0}"
# HTTP/HTTPS proxy URL. Three-way semantics:
#   - unset       → interactive prompt at the proxy_url checkpoint
#   - empty ""    → skip the prompt, no proxy is configured
#   - non-empty   → use this URL (validated, then written to all sinks)
# Override target paths if needed (tests redirect these):
#   BOTSQUAD_PROXY_APT_CONF    — apt-conf sink (default /etc/apt/apt.conf.d/01proxy)
#   BOTSQUAD_CLAUDE_SETTINGS   — claude settings sink (default ~/.claude/settings.json)
#   BOTSQUAD_PROXY_PROBE_CMD   — validation command; default is curl --proxy <url> npmjs
#                                (the command receives the URL as $1; non-zero exit = fail,
#                                stderr is shown to the user as the failure reason)
BOTSQUAD_PROXY_APT_CONF="${BOTSQUAD_PROXY_APT_CONF:-/etc/apt/apt.conf.d/01proxy}"
BOTSQUAD_CLAUDE_SETTINGS="${BOTSQUAD_CLAUDE_SETTINGS:-${HOME}/.claude/settings.json}"
BOTSQUAD_PROXY_PROBE_CMD="${BOTSQUAD_PROXY_PROBE_CMD:-}"
# T-0194: per-installation Telegram egress proxy, distinct from the general
# install-time proxy above. The general proxy wires apt/npm/curl/claude; this
# one is the RUNTIME config the worker (tg.py + tg_listener) reads to route
# Telegram API calls — the durable, TG-only replacement for the blunt global
# HTTPS_PROXY on the worker unit (T-0192). Set via --tg-proxy-url=<url> (parsed
# into this env) or directly. Empty/unset → no TG proxy (direct egress).
# Validated as socks5(h)://, http://, or https://; written to the install's
# config/system_settings.toml [tg].proxy_url by the tg_proxy checkpoint.
#   BOTSQUAD_TG_PROXY_PY — python interpreter override (test seam); default
#                          prefers the worker venv python, then host python3.
BOTSQUAD_TG_PROXY_PY="${BOTSQUAD_TG_PROXY_PY:-}"
# install_docker checkpoint seams. Override target paths if needed (tests
# redirect these into a temp dir):
#   BOTSQUAD_DOCKER_GPG_KEYRING — keyring sink (default
#                                  /etc/apt/keyrings/docker.asc)
#   BOTSQUAD_DOCKER_APT_LIST    — sources.list.d snippet (default
#                                  /etc/apt/sources.list.d/docker.list)
#   BOTSQUAD_DOCKER_CODENAME    — override apt repo codename (default: detect
#                                  via /etc/os-release VERSION_CODENAME)
#   BOTSQUAD_DOCKER_ARCH        — override apt repo arch (default: dpkg --print-architecture)
#   BOTSQUAD_DOCKER_DISTRO      — override apt repo distro (ubuntu|debian; default
#                                  via /etc/os-release ID, fallback ubuntu)
#   BOTSQUAD_DOCKER_GPG_FETCH_CMD — command that writes the GPG key; receives
#                                    the destination path as $1. Default is
#                                    sudo curl from download.docker.com.
#   BOTSQUAD_DOCKER_INSTALL_CMD — command that installs the docker apt packages.
#                                  Default is `sudo apt-get update && sudo
#                                  apt-get install -y docker-ce ...`.
#   BOTSQUAD_DOCKER_USERMOD_CMD — command that adds $1 to the docker group.
#                                  Default is `sudo usermod -aG docker $1`.
#   BOTSQUAD_DOCKER_CHECK_CMD   — "compose works" probe (default
#                                  `docker compose version`). Idempotency
#                                  keys on the exit code of this command.
BOTSQUAD_DOCKER_GPG_KEYRING="${BOTSQUAD_DOCKER_GPG_KEYRING:-/etc/apt/keyrings/docker.asc}"
BOTSQUAD_DOCKER_APT_LIST="${BOTSQUAD_DOCKER_APT_LIST:-/etc/apt/sources.list.d/docker.list}"
BOTSQUAD_DOCKER_CODENAME="${BOTSQUAD_DOCKER_CODENAME:-}"
BOTSQUAD_DOCKER_ARCH="${BOTSQUAD_DOCKER_ARCH:-}"
BOTSQUAD_DOCKER_DISTRO="${BOTSQUAD_DOCKER_DISTRO:-}"
BOTSQUAD_DOCKER_GPG_FETCH_CMD="${BOTSQUAD_DOCKER_GPG_FETCH_CMD:-}"
BOTSQUAD_DOCKER_INSTALL_CMD="${BOTSQUAD_DOCKER_INSTALL_CMD:-}"
BOTSQUAD_DOCKER_USERMOD_CMD="${BOTSQUAD_DOCKER_USERMOD_CMD:-}"
BOTSQUAD_DOCKER_CHECK_CMD="${BOTSQUAD_DOCKER_CHECK_CMD:-docker compose version}"
# install_reverse_proxy checkpoint seams (T-0029). The default shape is the
# HTTP-only fallback (shape 2 in T-0029); the bundled-traefik shape was
# rejected — see vision/multi-server/reverse-proxy-decision.md.
#   BOTSQUAD_REVERSE_PROXY_MODE — auto|shared|fresh (default auto)
#                                  auto  → detect via the network probe below
#                                  shared → assume an external traefik is in
#                                           front (no-op; existing compose
#                                           labels are used as-is)
#                                  fresh  → force the fresh-host override
#                                           regardless of probe result
#   BOTSQUAD_HTTP_PORT          — host port to publish for the fresh-host
#                                  shape (default 8000)
#   BOTSQUAD_REVERSE_PROXY_NETWORK — name of the external traefik network
#                                    that the bundled compose file expects
#                                    (default avo_backend)
#   BOTSQUAD_DOCKER_NETWORK_PROBE_CMD — "shared reverse proxy is present"
#                                       probe. Receives the network name as
#                                       $1; non-zero exit = absent. Default is
#                                       `docker network inspect <name>`.
#   BOTSQUAD_FRESH_HOST_OVERRIDE — override compose-file path (default
#                                   $BOTSQUAD_INSTALL_DIR/docker-compose.fresh-host.yml)
BOTSQUAD_REVERSE_PROXY_MODE="${BOTSQUAD_REVERSE_PROXY_MODE:-auto}"
BOTSQUAD_HTTP_PORT="${BOTSQUAD_HTTP_PORT:-8000}"
BOTSQUAD_REVERSE_PROXY_NETWORK="${BOTSQUAD_REVERSE_PROXY_NETWORK:-avo_backend}"
BOTSQUAD_DOCKER_NETWORK_PROBE_CMD="${BOTSQUAD_DOCKER_NETWORK_PROBE_CMD:-}"
BOTSQUAD_FRESH_HOST_OVERRIDE="${BOTSQUAD_FRESH_HOST_OVERRIDE:-}"

STATE_FILE="${BOTSQUAD_STATE_DIR}/install.state"
# Side-car file holding the detected distro family + ID so a re-run on
# a different distro errors loudly instead of silently shelling out to
# the wrong package manager (T-0030).
DISTRO_STATE_FILE="${BOTSQUAD_STATE_DIR}/install.distro"
ENV_FILE="${BOTSQUAD_INSTALL_DIR}/.env"
FRESH_HOST_OVERRIDE_DEFAULT="${BOTSQUAD_INSTALL_DIR}/docker-compose.fresh-host.yml"

# ---- Plumbing ---------------------------------------------------------------
log()  { printf '\033[36m[bot-squad]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m[bot-squad]\033[0m %s\n' "$*" >&2; }
die_struct() {
  # die_struct <checkpoint> <what> <how>
  local cp="$1" what="$2" how="$3"
  printf '\n' >&2
  printf '============================================================\n' >&2
  printf 'INSTALL FAILED at checkpoint: %s\n' "$cp" >&2
  printf '------------------------------------------------------------\n' >&2
  printf 'What happened:\n%s\n' "$what" >&2
  printf '\nWhat to do:\n%s\n' "$how" >&2
  printf '\nThen re-run the same command. The installer picks up where it\n' >&2
  printf 'left off (state file: %s).\n' "$STATE_FILE" >&2
  printf '============================================================\n' >&2
  exit 1
}

on_err() {
  local exit_code=$? line="${BASH_LINENO[0]:-?}" cmd="${BASH_COMMAND:-?}"
  local cp="${CURRENT_CHECKPOINT:-<unknown>}"
  post_checkpoint_event "$cp" "failed" || true
  die_struct "$cp" \
    "Unhandled error (exit ${exit_code}) at line ${line}: ${cmd}" \
    "This is usually a permission issue or a missing dependency. Re-read the
last 20 lines above for the underlying command's output. Common fixes:
  - run with sudo if the message mentions Permission denied
  - install the missing package (apt install <name>)
  - on a fresh OS, run 'sudo apt-get update' once before re-running"
}
trap on_err ERR

post_checkpoint_event() {
  # Best-effort: forward a checkpoint event to the mothership for the
  # live install-wizard UI. Silently no-ops if the mothership isn't
  # configured or unreachable; we never fail the install on this.
  local cp="$1" status="$2"
  [[ "$BOTSQUAD_SKIP_MOTHERSHIP" = "1" ]] && return 0
  [[ "$BOTSQUAD_MOTHERSHIP_URL" = "__MOTHERSHIP_URL__" ]] && return 0
  command -v curl >/dev/null 2>&1 || return 0
  command -v jq   >/dev/null 2>&1 || return 0
  local auth_header bearer_file="${BOTSQUAD_STATE_DIR}/server.token"
  if [[ -r "$bearer_file" ]]; then
    auth_header="Authorization: Bearer $(cat "$bearer_file")"
  else
    auth_header="Authorization: Bearer ${BOTSQUAD_INSTALL_TOKEN}"
  fi
  local payload
  payload=$(jq -n \
    --arg cp "$cp" --arg status "$status" \
    --arg host "$(hostname -f 2>/dev/null || hostname)" \
    '{checkpoint: $cp, status: $status, hostname: $host, ts: (now | todate)}') || return 0
  # Endpoint locked in vision/architecture/mothership-seam.md; treated
  # as best-effort because UI display, not install correctness, depends
  # on it. Body: { checkpoint, status, hostname, ts }.
  curl -fsS --max-time 5 -X POST \
    -H "$auth_header" -H 'Content-Type: application/json' \
    -d "$payload" \
    "${BOTSQUAD_MOTHERSHIP_URL}/api/m/installer/checkpoint" >/dev/null 2>&1 || true
}

ensure_state_dir() {
  mkdir -p "$BOTSQUAD_STATE_DIR"
  touch "$STATE_FILE"
}

checkpoint_done() { grep -qxF "$1" "$STATE_FILE" 2>/dev/null; }
checkpoint_mark() { printf '%s\n' "$1" >> "$STATE_FILE"; }

run_checkpoint() {
  local name="$1"
  CURRENT_CHECKPOINT="$name"
  if checkpoint_done "$name"; then
    log "skip   $name (already done)"
    return 0
  fi
  log "begin  $name"
  post_checkpoint_event "$name" "begin"
  local rc=0
  "step_${name}" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    post_checkpoint_event "$name" "failed"
    return "$rc"
  fi
  checkpoint_mark "$name"
  log "done   $name"
  post_checkpoint_event "$name" "done"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die_struct "$CURRENT_CHECKPOINT" \
    "Required command '$1' is not on PATH." \
    "Install it via your package manager (e.g. 'sudo apt-get install -y $1')
and re-run the installer."
}

prompt_value() {
  # prompt_value <varname> <human-readable label> [<default>]
  local var="$1" label="$2" default="${3:-}"
  if [[ -n "${!var:-}" ]]; then return 0; fi
  if [[ "$BOTSQUAD_NONINTERACTIVE" = "1" ]]; then
    die_struct "$CURRENT_CHECKPOINT" \
      "Need value for $var ($label) but BOTSQUAD_NONINTERACTIVE=1." \
      "Re-run with: $var=<value> bash install.sh"
  fi
  local answer
  if [[ -n "$default" ]]; then
    read -r -p "[bot-squad] ${label} [${default}]: " answer </dev/tty || true
    answer="${answer:-$default}"
  else
    read -r -p "[bot-squad] ${label}: " answer </dev/tty || true
  fi
  if [[ -z "$answer" ]]; then
    die_struct "$CURRENT_CHECKPOINT" "Empty value for $var." \
      "Re-run and provide a value at the prompt, or pre-set the env var."
  fi
  printf -v "$var" '%s' "$answer"
  export "$var"
}

# ---- Checkpoints ------------------------------------------------------------

step_detect_distro() {
  case "$(uname -s)" in
    Linux) : ;;
    *) die_struct detect_distro "OS is $(uname -s); bot-squad install only supports Linux." \
         "Run on a supported Linux host (Debian/Ubuntu, Fedora/RHEL, or Arch)." ;;
  esac
  # Resolve family + ID via pkg.sh's detector. An empty result means the
  # host's /etc/os-release didn't match any family we have a package
  # map for. We bail loudly with a pointer at BOTSQUAD_DISTRO_FAMILY so
  # the user can force a guess on a close-enough derivative.
  if ! declare -F detect_distro_family >/dev/null 2>&1; then
    die_struct detect_distro \
      "pkg.sh helper not loaded (detect_distro_family unavailable)." \
      "The installer bundle is incomplete. Re-fetch install.sh + pkg.sh
from the mothership (or, if running from the repo, ensure scripts/install/pkg.sh
exists next to install.sh)."
  fi
  local family id=""
  family="$(detect_distro_family)"
  if [[ -r /etc/os-release ]]; then
    # shellcheck source=/dev/null
    id="$( . /etc/os-release && printf '%s' "${ID:-}" )"
  fi
  if [[ -z "$family" ]]; then
    die_struct detect_distro \
      "Could not detect a supported distro family from /etc/os-release (ID='${id:-?}')." \
      "Supported families: debian (Ubuntu 22.04+/Debian 12+), fedora
(Fedora 40+/RHEL 9+), arch (rolling), alpine (3.18+), nixos (24.05+;
declarative-only — install.sh emits a NixOS module and exits, the
admin runs nixos-rebuild switch). If your host is a derivative we
don't recognize, re-run with
BOTSQUAD_DISTRO_FAMILY=debian|fedora|arch|alpine|nixos to force a
family."
  fi
  export BOTSQUAD_DISTRO_FAMILY="$family"
  # Mismatch guard: if a previous run recorded a different family on
  # this state dir, the user almost certainly mounted the wrong state
  # dir (or migrated the host); refuse and explain.
  if [[ -r "$DISTRO_STATE_FILE" ]]; then
    local prev_family
    prev_family="$( . "$DISTRO_STATE_FILE" 2>/dev/null && printf '%s' "${BOTSQUAD_DISTRO_FAMILY:-}" )" || prev_family=""
    if [[ -n "$prev_family" ]] && [[ "$prev_family" != "$family" ]]; then
      die_struct detect_distro \
        "Detected distro family '$family' but state file at $DISTRO_STATE_FILE
records a previous run as '$prev_family'. The installer's state dir is
not transferable across families — package-manager assumptions baked
into earlier checkpoints would now be wrong." \
        "Use a fresh state dir for this host (BOTSQUAD_STATE_DIR=...),
or rm -rf $BOTSQUAD_STATE_DIR if you really intend to re-bootstrap on
the same host with a different family."
    fi
  fi
  # Persist for subsequent runs.
  umask 022
  cat > "$DISTRO_STATE_FILE" <<EOF
BOTSQUAD_DISTRO_FAMILY=$family
BOTSQUAD_DISTRO_ID=$id
EOF
  log "distro family: $family (ID=$id)"
}

step_require_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then return 0; fi
  if sudo -n true 2>/dev/null; then return 0; fi
  if [[ "$BOTSQUAD_NONINTERACTIVE" = "1" ]]; then
    die_struct require_sudo "This step needs sudo but no cached credential is available and NONINTERACTIVE=1." \
      "Run 'sudo -v' interactively once, then re-run the installer."
  fi
  log "you'll be prompted for your sudo password (needed for package install + group setup)"
  sudo -v || die_struct require_sudo "sudo authentication failed." \
    "Make sure your user is in /etc/sudoers (or the 'sudo' group), then re-run."
}

# --- proxy_url checkpoint helpers --------------------------------------------
#
# Three sinks for an accepted proxy URL:
#   1. script env  — export http_proxy/https_proxy so subsequent apt/curl/npm
#                    invocations inherit it (this checkpoint runs before
#                    pkg_index_update on purpose)
#   2. claude settings — ~/.claude/settings.json under .env.http_proxy /
#                        .env.https_proxy (the same {env: {...}} convention
#                        claude-code reads other vars from, e.g.
#                        CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS)
#   3. apt conf    — /etc/apt/apt.conf.d/01proxy with Acquire::http::Proxy +
#                    Acquire::https::Proxy (needs sudo, chmod 0644)
#
# Each sink writer is idempotent: re-running with the same URL is a no-op;
# changing the URL rewrites the sink.

# Run the validation probe for a candidate URL.
# Returns 0 on success; on failure prints a human-readable reason to stderr.
proxy_probe() {
  local url="$1"
  if [[ -n "$BOTSQUAD_PROXY_PROBE_CMD" ]]; then
    # Test/override hook: receives the URL as $1.
    bash -c "$BOTSQUAD_PROXY_PROBE_CMD \"\$1\"" _ "$url"
    return $?
  fi
  # Default probe: curl through the proxy to the npm registry.
  # -sS keeps it quiet but surfaces errors; --max-time 5 bounds the wait;
  # -o /dev/null discards the body; -w '%{http_code}' is consulted via the
  # exit code (curl returns non-zero on connection failure regardless).
  local err
  err="$(curl -sS --proxy "$url" --max-time 5 -o /dev/null \
    -w '%{http_code}' https://registry.npmjs.org/ 2>&1)" || {
    printf '%s\n' "$err" >&2
    return 1
  }
  # curl exited 0 → connection succeeded; check HTTP status.
  case "$err" in
    2*|3*) return 0 ;;
    *)
      printf 'proxy returned HTTP status %s from registry.npmjs.org\n' "$err" >&2
      return 1
      ;;
  esac
}

# Write the proxy URL into ~/.claude/settings.json under .env.http_proxy and
# .env.https_proxy. Creates the file if missing.
proxy_write_claude_settings() {
  local url="$1" cfg="$BOTSQUAD_CLAUDE_SETTINGS"
  mkdir -p "$(dirname "$cfg")"
  if [[ ! -f "$cfg" ]]; then
    printf '%s\n' '{}' > "$cfg"
  fi
  local tmp; tmp="$(mktemp)"
  jq --arg url "$url" \
    '.env = ((.env // {}) + {http_proxy: $url, https_proxy: $url})' \
    "$cfg" > "$tmp" && mv "$tmp" "$cfg"
}

# Remove the proxy keys from ~/.claude/settings.json (used when re-running
# with a now-empty BOTSQUAD_PROXY_URL — but we only call this for transparency
# in tests; the main flow doesn't undo, it just no-ops on empty).
proxy_clear_claude_settings() {
  local cfg="$BOTSQUAD_CLAUDE_SETTINGS"
  [[ -f "$cfg" ]] || return 0
  local tmp; tmp="$(mktemp)"
  jq 'if .env then .env |= (del(.http_proxy) | del(.https_proxy))
        | (if (.env | length) == 0 then del(.env) else . end)
      else . end' \
    "$cfg" > "$tmp" && mv "$tmp" "$cfg"
}

# Write the apt conf snippet. Uses sudo unless the target path is already
# writable by the current user (the test suite redirects to a temp dir).
proxy_write_apt_conf() {
  local url="$1" path="$BOTSQUAD_PROXY_APT_CONF"
  local body
  body="$(printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$url" "$url")"
  # Idempotent: if the file already has the exact body, do nothing.
  if [[ -f "$path" ]] && [[ "$(cat "$path" 2>/dev/null)" = "$body" ]]; then
    return 0
  fi
  local parent; parent="$(dirname "$path")"
  if [[ -w "$parent" ]] || { [[ -f "$path" ]] && [[ -w "$path" ]]; }; then
    printf '%s' "$body" > "$path"
    chmod 0644 "$path"
  else
    printf '%s' "$body" | sudo tee "$path" >/dev/null
    sudo chmod 0644 "$path"
  fi
}

step_proxy_url() {
  # Decide the URL: explicit env (set, possibly empty) wins; otherwise prompt.
  local url
  if [[ "${BOTSQUAD_PROXY_URL+set}" = "set" ]]; then
    url="$BOTSQUAD_PROXY_URL"
    if [[ -z "$url" ]]; then
      log "BOTSQUAD_PROXY_URL is empty → no proxy configured (skipping)"
      return 0
    fi
    # Pre-set URL still gets validated; failure is fatal (no re-prompt in
    # non-interactive mode).
    if ! proxy_probe "$url" 2>/tmp/proxy_probe_err.$$; then
      local reason; reason="$(cat /tmp/proxy_probe_err.$$ 2>/dev/null || true)"
      rm -f /tmp/proxy_probe_err.$$
      die_struct proxy_url \
        "Proxy URL '$url' failed validation: ${reason:-unknown error}" \
        "Re-run with a working BOTSQUAD_PROXY_URL=... (or BOTSQUAD_PROXY_URL='' to skip)."
    fi
    rm -f /tmp/proxy_probe_err.$$
  else
    if [[ "$BOTSQUAD_NONINTERACTIVE" = "1" ]]; then
      log "BOTSQUAD_PROXY_URL unset + non-interactive → skipping proxy checkpoint"
      log "(set BOTSQUAD_PROXY_URL=http://... or BOTSQUAD_PROXY_URL='' to silence this)"
      return 0
    fi
    # Ask first whether a proxy is needed at all.
    local answer
    read -r -p "[bot-squad] Do you need an HTTP/HTTPS proxy for apt/npm/curl? [y/N]: " answer </dev/tty || answer=""
    case "$answer" in
      y|Y|yes|YES) : ;;
      *) log "no proxy configured"; return 0 ;;
    esac
    while :; do
      read -r -p "[bot-squad] Proxy URL (e.g. http://proxy.corp:3128): " url </dev/tty || url=""
      if [[ -z "$url" ]]; then
        warn "empty URL — re-enter, or Ctrl-C to abort"
        continue
      fi
      log "validating proxy by curl-ing https://registry.npmjs.org/ through it (5s timeout)..."
      if proxy_probe "$url" 2>/tmp/proxy_probe_err.$$; then
        rm -f /tmp/proxy_probe_err.$$
        break
      fi
      local reason; reason="$(cat /tmp/proxy_probe_err.$$ 2>/dev/null || true)"
      rm -f /tmp/proxy_probe_err.$$
      warn "proxy validation failed: ${reason:-unknown error}"
      warn "re-enter the URL (or Ctrl-C to abort)"
    done
  fi

  # Write all three sinks.
  export http_proxy="$url"
  export https_proxy="$url"
  export HTTP_PROXY="$url"
  export HTTPS_PROXY="$url"
  proxy_write_claude_settings "$url" || die_struct proxy_url \
    "Failed to write proxy URL into $BOTSQUAD_CLAUDE_SETTINGS." \
    "Check the file's permissions; ensure jq is installed."
  proxy_write_apt_conf "$url" || die_struct proxy_url \
    "Failed to write $BOTSQUAD_PROXY_APT_CONF." \
    "Check sudo permissions on $(dirname "$BOTSQUAD_PROXY_APT_CONF")."
  log "proxy configured: $url → env + claude settings + apt conf"
}

step_pkg_index_update() {
  # Replaces the v1 apt_update checkpoint. Dispatches via pkg_update()
  # to the family-appropriate refresh (apt-get update / dnf makecache
  # / pacman -Sy). Keeping it as its own checkpoint preserves the
  # "before proxy is wired" → "after proxy is wired" ordering that
  # the proxy_url checkpoint relies on for its first index pull.
  pkg_update || die_struct pkg_index_update \
    "Package index refresh failed (family=${BOTSQUAD_DISTRO_FAMILY:-?})." \
    "Check network connectivity and your package-manager sources
(/etc/apt/sources.list*, /etc/yum.repos.d/, or /etc/pacman.conf as
applicable). If you're behind a proxy, re-run with BOTSQUAD_PROXY_URL=
http://... — the proxy_url checkpoint will wire it through."
}

step_install_base_pkgs() {
  # curl + git + ca-certificates + jq (for parsing mothership responses).
  # Resolved per-family by pkg_install.
  pkg_install curl ca-certificates git jq || die_struct install_base_pkgs \
      "Install of base packages (curl/git/jq/ca-certificates) failed
(family=${BOTSQUAD_DISTRO_FAMILY:-?})." \
      "Inspect the package-manager output above. The most common cause is
a held package or a stale index — re-run the pkg_index_update checkpoint
manually (e.g. 'sudo apt-get update && sudo apt-get -f install' on
Debian/Ubuntu)."
}

step_install_tmux() {
  if pkg_have tmux; then return 0; fi
  pkg_install tmux || die_struct install_tmux \
    "Could not install tmux via the system package manager
(family=${BOTSQUAD_DISTRO_FAMILY:-?})." \
    "Try installing tmux manually (e.g. 'sudo apt-get install tmux',
'sudo dnf install tmux', or 'sudo pacman -S tmux') to see the exact error."
}

step_install_nodejs() {
  if command -v node >/dev/null 2>&1; then
    local v
    v="$(node -v 2>/dev/null | sed 's/^v//')"
    # claude-code needs node >= 18; be lenient on minor.
    if [[ -n "$v" ]] && [[ "${v%%.*}" -ge 18 ]]; then return 0; fi
  fi
  # NodeSource is the source-of-truth for current Node on Debian/Fedora;
  # Arch's `nodejs` package tracks current well enough on its own.
  case "${BOTSQUAD_DISTRO_FAMILY:-}" in
    debian)
      if ! curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null 2>&1; then
        die_struct install_nodejs "Failed to fetch the NodeSource setup script (deb)." \
          "Check network (curl https://deb.nodesource.com). If you're on a corporate
network, re-run with BOTSQUAD_PROXY_URL=http://... (the proxy_url checkpoint
will configure apt/curl/npm), then re-run the installer."
      fi
      pkg_install nodejs \
        || die_struct install_nodejs "apt-get install nodejs failed." \
           "Run 'sudo apt-get install nodejs' to see the exact apt error."
      ;;
    fedora)
      # NodeSource RPM repo (same source-of-truth as Debian, different URL).
      if ! curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo -E bash - >/dev/null 2>&1; then
        die_struct install_nodejs "Failed to fetch the NodeSource setup script (rpm)." \
          "Check network (curl https://rpm.nodesource.com). If you're on a corporate
network, re-run with BOTSQUAD_PROXY_URL=http://... and retry."
      fi
      pkg_install nodejs \
        || die_struct install_nodejs "dnf install nodejs failed." \
           "Run 'sudo dnf install nodejs' to see the exact dnf error."
      ;;
    arch)
      # Arch core/extra ships current node + npm; no third-party repo.
      pkg_install nodejs \
        || die_struct install_nodejs "pacman -S nodejs npm failed." \
           "Run 'sudo pacman -S nodejs npm' to see the exact pacman error."
      ;;
    alpine)
      # Alpine main repo ships nodejs + npm (separate packages, mapped
      # via pkg.sh). No NodeSource setup script — Alpine's nodejs tracks
      # the LTS that ships with the release.
      pkg_install nodejs \
        || die_struct install_nodejs "apk add nodejs npm failed." \
           "Run 'sudo apk add nodejs npm' to see the exact apk error.
If your Alpine release is older than 3.18, upgrade — earlier releases
ship nodejs < 18 which claude-code refuses to start under."
      ;;
    *)
      die_struct install_nodejs \
        "Unknown distro family '${BOTSQUAD_DISTRO_FAMILY:-?}'; cannot install nodejs." \
        "Re-run after the detect_distro checkpoint succeeds, or set
BOTSQUAD_DISTRO_FAMILY=debian|fedora|arch|alpine explicitly."
      ;;
  esac
}

step_install_claude_code() {
  if command -v claude >/dev/null 2>&1; then return 0; fi
  # Global install — claude-code publishes as @anthropic-ai/claude-code.
  if ! sudo npm install -g @anthropic-ai/claude-code >/dev/null 2>&1; then
    die_struct install_claude_code \
      "npm install -g @anthropic-ai/claude-code failed." \
      "Try the npm command directly to see the exact error:
  sudo npm install -g @anthropic-ai/claude-code
If you see EACCES, your global node prefix may need fixing
(npm config set prefix ~/.npm-global). If you see network errors,
re-run with BOTSQUAD_PROXY_URL=http://... and retry."
  fi
  command -v claude >/dev/null 2>&1 || die_struct install_claude_code \
    "npm install reported success but 'claude' is still not on PATH." \
    "Add npm's global bin dir to your PATH (npm bin -g) and re-run."
}

# --- install_docker checkpoint helpers ---------------------------------------
#
# Bootstrap Docker engine + compose plugin via Docker's official apt repo,
# using the signed-by gpg-key pattern (not pipe-curl-to-sudo-bash) per
# https://docs.docker.com/engine/install/ubuntu/.
#
# Idempotency is keyed on "compose works" (BOTSQUAD_DOCKER_CHECK_CMD), not
# "we ran apt": if `docker compose version` already returns 0, the entire
# checkpoint is a no-op. This keeps the checkpoint stable across docker
# minor-version bumps and across hosts where docker was installed by some
# other means.
#
# Every step that touches sudo / apt / network is funnelled through a
# BOTSQUAD_DOCKER_*_CMD env-var seam so the test suite can stub them.

docker_compose_works() {
  bash -c "$BOTSQUAD_DOCKER_CHECK_CMD" >/dev/null 2>&1
}

docker_detect_codename() {
  if [[ -n "$BOTSQUAD_DOCKER_CODENAME" ]]; then
    printf '%s' "$BOTSQUAD_DOCKER_CODENAME"; return
  fi
  local codename=""
  if [[ -r /etc/os-release ]]; then
    # shellcheck source=/dev/null
    codename="$( . /etc/os-release && printf '%s' "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}" )"
  fi
  if [[ -z "$codename" ]] && command -v lsb_release >/dev/null 2>&1; then
    codename="$(lsb_release -cs 2>/dev/null || true)"
  fi
  printf '%s' "$codename"
}

docker_detect_distro() {
  if [[ -n "$BOTSQUAD_DOCKER_DISTRO" ]]; then
    printf '%s' "$BOTSQUAD_DOCKER_DISTRO"; return
  fi
  if [[ -r /etc/os-release ]]; then
    ( . /etc/os-release && printf '%s' "${ID:-ubuntu}" )
  else
    printf '%s' ubuntu
  fi
}

docker_detect_arch() {
  if [[ -n "$BOTSQUAD_DOCKER_ARCH" ]]; then
    printf '%s' "$BOTSQUAD_DOCKER_ARCH"; return
  fi
  if command -v dpkg >/dev/null 2>&1; then
    dpkg --print-architecture 2>/dev/null || printf '%s' amd64
  else
    printf '%s' amd64
  fi
}

# Install Docker's official GPG key at $1 using the signed-by pattern.
# Returns non-zero on any failure (network / sudo / write).
docker_install_gpg_key() {
  local keyring="$1" parent
  parent="$(dirname "$keyring")"
  if [[ -n "$BOTSQUAD_DOCKER_GPG_FETCH_CMD" ]]; then
    mkdir -p "$parent" || return 1
    bash -c "$BOTSQUAD_DOCKER_GPG_FETCH_CMD \"\$1\"" _ "$keyring"
    return $?
  fi
  local distro; distro="$(docker_detect_distro)"
  if [[ -w "$parent" ]] || { [[ -f "$keyring" ]] && [[ -w "$keyring" ]]; }; then
    mkdir -p "$parent" || return 1
    curl -fsSL "https://download.docker.com/linux/${distro}/gpg" \
      -o "$keyring" || return 1
    chmod 0644 "$keyring" || return 1
  else
    sudo install -m 0755 -d "$parent" || return 1
    sudo curl -fsSL "https://download.docker.com/linux/${distro}/gpg" \
      -o "$keyring" || return 1
    sudo chmod 0644 "$keyring" || return 1
  fi
}

# Write the apt sources.list.d snippet for Docker's repo. Idempotent: if the
# file already has the exact body we'd write, leave its mtime alone.
docker_write_apt_list() {
  local list="$1" keyring="$2" distro codename arch
  distro="$(docker_detect_distro)"
  codename="$(docker_detect_codename)"
  arch="$(docker_detect_arch)"
  if [[ -z "$codename" ]]; then
    die_struct install_docker \
      "Could not detect distro codename for Docker's apt repo." \
      "Make sure /etc/os-release defines VERSION_CODENAME (or UBUNTU_CODENAME),
or install lsb-release. Cross-distro support is tracked under T-0030; for
now, override with BOTSQUAD_DOCKER_CODENAME=<codename>."
  fi
  local body
  body="$(printf 'deb [arch=%s signed-by=%s] https://download.docker.com/linux/%s %s stable\n' \
    "$arch" "$keyring" "$distro" "$codename")"
  if [[ -f "$list" ]] && [[ "$(cat "$list" 2>/dev/null)" = "$body" ]]; then
    return 0
  fi
  local parent; parent="$(dirname "$list")"
  if [[ -w "$parent" ]] || { [[ -f "$list" ]] && [[ -w "$list" ]]; }; then
    mkdir -p "$parent" || return 1
    printf '%s' "$body" > "$list"
    chmod 0644 "$list"
  else
    sudo install -m 0755 -d "$parent" || return 1
    printf '%s' "$body" | sudo tee "$list" >/dev/null
    sudo chmod 0644 "$list"
  fi
}

docker_run_install_cmd() {
  if [[ -n "$BOTSQUAD_DOCKER_INSTALL_CMD" ]]; then
    bash -c "$BOTSQUAD_DOCKER_INSTALL_CMD"
    return $?
  fi
  case "${BOTSQUAD_DISTRO_FAMILY:-debian}" in
    debian)
      sudo apt-get update -y >/dev/null || return 1
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin >/dev/null
      ;;
    fedora)
      # Docker's Fedora repo: written via dnf config-manager from the
      # upstream .repo file (no manual GPG key step — the .repo entry
      # carries the gpgkey URL, and dnf imports it on first install).
      sudo dnf -y install dnf-plugins-core >/dev/null || return 1
      # `dnf config-manager --add-repo <url>` is idempotent (writes the
      # same .repo each time).
      sudo dnf config-manager --add-repo \
        https://download.docker.com/linux/fedora/docker-ce.repo >/dev/null || return 1
      sudo dnf -y install \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin >/dev/null || return 1
      # On Fedora the docker daemon isn't started by the install, unlike
      # apt on Debian/Ubuntu. Enable + start so the post-install probe
      # has a socket to talk to.
      sudo systemctl enable --now docker >/dev/null 2>&1 || return 1
      ;;
    arch)
      # Arch ships docker + docker compose plugin in core/extra; no
      # third-party repo or GPG dance.
      sudo pacman -S --noconfirm --needed docker docker-compose >/dev/null || return 1
      sudo systemctl enable --now docker >/dev/null 2>&1 || return 1
      ;;
    alpine)
      # Alpine community ships docker + the compose-as-plugin (so
      # `docker compose version` works, matching the probe default).
      # Alpine is OpenRC, not systemd: enable at boot via rc-update,
      # start now via the service wrapper.
      sudo apk add --no-cache docker docker-cli-compose >/dev/null || return 1
      sudo rc-update add docker default >/dev/null 2>&1 || return 1
      sudo service docker start >/dev/null 2>&1 || return 1
      ;;
    *)
      return 1
      ;;
  esac
}

docker_run_usermod_cmd() {
  local user="$1"
  if [[ -n "$BOTSQUAD_DOCKER_USERMOD_CMD" ]]; then
    bash -c "$BOTSQUAD_DOCKER_USERMOD_CMD \"\$1\"" _ "$user"
    return $?
  fi
  sudo usermod -aG docker "$user"
}

step_install_docker() {
  # Idempotency is keyed on "compose works", not "we ran apt".
  if docker_compose_works; then
    log "docker compose already works (skipping)"
    return 0
  fi

  # The signed-by GPG keyring + apt sources.list snippet are Debian-family
  # specific. Fedora uses dnf config-manager --add-repo (handled inside
  # docker_run_install_cmd); Arch ships docker in core/extra (no
  # third-party repo). Default to the debian path so legacy tests that
  # never set BOTSQUAD_DISTRO_FAMILY keep working as before.
  case "${BOTSQUAD_DISTRO_FAMILY:-debian}" in
    debian)
      docker_install_gpg_key "$BOTSQUAD_DOCKER_GPG_KEYRING" \
        || die_struct install_docker \
          "Could not install Docker's GPG key to $BOTSQUAD_DOCKER_GPG_KEYRING." \
          "Check network connectivity to download.docker.com and that
$(dirname "$BOTSQUAD_DOCKER_GPG_KEYRING") is writable (with sudo). If
you're behind a proxy, re-run with BOTSQUAD_PROXY_URL=http://..."

      docker_write_apt_list "$BOTSQUAD_DOCKER_APT_LIST" "$BOTSQUAD_DOCKER_GPG_KEYRING" \
        || die_struct install_docker \
          "Could not write $BOTSQUAD_DOCKER_APT_LIST." \
          "Check sudo permissions on $(dirname "$BOTSQUAD_DOCKER_APT_LIST")."
      ;;
    fedora|arch|alpine)
      log "docker: using ${BOTSQUAD_DISTRO_FAMILY} package source (no apt keyring/list)"
      ;;
    *)
      die_struct install_docker \
        "Cannot install Docker on family '${BOTSQUAD_DISTRO_FAMILY:-?}'." \
        "Re-run after the detect_distro checkpoint succeeds, or set
BOTSQUAD_DISTRO_FAMILY=debian|fedora|arch|alpine explicitly."
      ;;
  esac

  docker_run_install_cmd \
    || die_struct install_docker \
      "apt-get install of docker-ce + plugins failed." \
      "Inspect the apt-get output above. Common causes: stale apt cache
(try 'sudo apt-get update'); held packages; or transient network. If
your distro codename isn't in Docker's upstream list, T-0030 tracks
cross-distro support."

  local user; user="$(id -un)"
  docker_run_usermod_cmd "$user" \
    || die_struct install_docker \
      "Could not add $user to the 'docker' group." \
      "Run 'sudo usermod -aG docker $user' manually to see the error."

  # Final probe: did install + group membership actually take effect for
  # this shell? Two failure modes are distinguished:
  #   (a) binaries missing — apt didn't really install (rare; would
  #       normally have been caught above).
  #   (b) binaries present but the current shell can't reach the docker
  #       socket — the user was just added to the 'docker' group, but
  #       Unix group membership is fixed at session start (login), so this
  #       shell inherited the pre-usermod group set. Re-running the
  #       installer in a new login (or after `newgrp docker`) will see
  #       `docker compose version` succeed and skip the whole checkpoint.
  if ! docker_compose_works; then
    if command -v docker >/dev/null 2>&1; then
      die_struct install_docker \
        "Docker is installed, but this shell can't talk to it yet.
Reason: you were just added to the 'docker' group, and Unix group
membership is fixed at session start — this shell inherited the
pre-usermod group set. This is NOT a script bug." \
        "Log out and back in (or, in this shell, run 'newgrp docker'),
then re-run the installer. The install_docker checkpoint will be a
no-op once 'docker compose version' returns 0 in the new shell."
    fi
    die_struct install_docker \
      "apt reported success but 'docker compose version' still fails and
'docker' isn't on PATH." \
      "Re-run 'sudo apt-get install -y docker-ce docker-ce-cli
containerd.io docker-buildx-plugin docker-compose-plugin' manually to
see the underlying error."
  fi
}

# --- install_reverse_proxy checkpoint helpers (T-0029) -----------------------
#
# Two shapes, distinguished by whether a shared reverse proxy is already in
# front of this docker daemon:
#
#   shared (no-op): the host already runs a reverse proxy (e.g. traefik on
#     the `avo_backend` external network — the bot-squad-api compose service
#     has matching `traefik.http.routers.bot-squad.rule=Host(...)` labels).
#     The detection sentinel is `docker network inspect avo_backend` (or
#     whatever name BOTSQUAD_REVERSE_PROXY_NETWORK is set to). When the
#     probe returns 0, this checkpoint is a no-op — the existing labels
#     do the work. This is the path the bot-squad mothership server itself
#     takes and MUST stay no-op for it.
#
#   fresh: the host has no reverse proxy. We write a docker-compose override
#     (BOTSQUAD_FRESH_HOST_OVERRIDE, default docker-compose.fresh-host.yml)
#     that (a) redefines `avo_backend` as a stack-local network so compose
#     creates it, and (b) publishes bot-squad-api:8000 on the host at
#     BOTSQUAD_HTTP_PORT (default 8000). step_docker_compose_up picks the
#     override file up automatically when it's present.
#
# TLS bootstrap is deliberately deferred: shape 2 only serves HTTP on the
# published port; users layer their own caddy / nginx / traefik in front
# (the gitea/plausible/forgejo playbook).
#
# Rationale: vision/multi-server/reverse-proxy-decision.md

reverse_proxy_override_path() {
  if [[ -n "$BOTSQUAD_FRESH_HOST_OVERRIDE" ]]; then
    printf '%s' "$BOTSQUAD_FRESH_HOST_OVERRIDE"
  else
    printf '%s' "${BOTSQUAD_INSTALL_DIR}/docker-compose.fresh-host.yml"
  fi
}

# Return 0 if the shared reverse-proxy network is present (i.e. somebody
# else is already running a reverse proxy on this docker daemon), non-zero
# otherwise. Honors the BOTSQUAD_DOCKER_NETWORK_PROBE_CMD test seam: when
# set, the command is invoked with the network name as $1.
reverse_proxy_shared_present() {
  local network="$BOTSQUAD_REVERSE_PROXY_NETWORK"
  if [[ -n "$BOTSQUAD_DOCKER_NETWORK_PROBE_CMD" ]]; then
    bash -c "$BOTSQUAD_DOCKER_NETWORK_PROBE_CMD \"\$1\"" _ "$network" >/dev/null 2>&1
    return $?
  fi
  # Default probe: `docker network inspect <name>` returns 0 iff the
  # network exists on the local daemon. Silent on both branches.
  command -v docker >/dev/null 2>&1 || return 1
  docker network inspect "$network" >/dev/null 2>&1
}

# Write the fresh-host docker-compose override. Idempotent: if the file
# already has the exact body, leave its mtime alone.
reverse_proxy_write_override() {
  local path="$1" port="$2" network="$3"
  local body
  # The override targets the bot-squad-api service from the parent
  # compose. We redefine `avo_backend` as stack-local (external: false)
  # so compose creates it, and add a ports: block to publish the API.
  # Comments explain the intent for anyone who opens the file later.
  body="$(cat <<EOF
# Generated by bot-squad installer (T-0029, install_reverse_proxy step).
# Layered on top of docker-compose.yml when this host has no shared
# reverse proxy (no '${network}' external network). Publishes the API
# on a host port so the user can curl/browse it directly, or layer
# their own caddy/nginx/traefik in front. To remove (e.g. you set up a
# shared traefik later), just delete this file and re-run the installer.
services:
  bot-squad-api:
    ports:
      - "${port}:8000"

networks:
  ${network}:
    external: false
EOF
)"
  if [[ -f "$path" ]] && [[ "$(cat "$path" 2>/dev/null)" = "$body" ]]; then
    return 0
  fi
  local parent; parent="$(dirname "$path")"
  if [[ ! -d "$parent" ]]; then
    mkdir -p "$parent" 2>/dev/null || sudo mkdir -p "$parent" || return 1
  fi
  if [[ -w "$parent" ]] || { [[ -f "$path" ]] && [[ -w "$path" ]]; }; then
    printf '%s\n' "$body" > "$path"
    chmod 0644 "$path"
  else
    printf '%s\n' "$body" | sudo tee "$path" >/dev/null
    sudo chmod 0644 "$path"
  fi
}

step_install_reverse_proxy() {
  local override; override="$(reverse_proxy_override_path)"
  case "$BOTSQUAD_REVERSE_PROXY_MODE" in
    shared)
      log "reverse-proxy mode forced to shared → no-op (existing traefik labels apply)"
      return 0
      ;;
    fresh)
      log "reverse-proxy mode forced to fresh → writing override $override"
      ;;
    auto|"")
      if reverse_proxy_shared_present; then
        log "shared reverse proxy detected (network '$BOTSQUAD_REVERSE_PROXY_NETWORK' exists) → no-op"
        return 0
      fi
      log "no shared reverse proxy detected → writing fresh-host override $override"
      ;;
    *)
      die_struct install_reverse_proxy \
        "Unknown BOTSQUAD_REVERSE_PROXY_MODE='$BOTSQUAD_REVERSE_PROXY_MODE'." \
        "Set BOTSQUAD_REVERSE_PROXY_MODE to one of: auto, shared, fresh."
      ;;
  esac
  reverse_proxy_write_override "$override" \
      "$BOTSQUAD_HTTP_PORT" "$BOTSQUAD_REVERSE_PROXY_NETWORK" \
    || die_struct install_reverse_proxy \
      "Could not write fresh-host compose override at $override." \
      "Check permissions on $(dirname "$override"). If you're behind a
read-only mount, set BOTSQUAD_FRESH_HOST_OVERRIDE to a writable path."
  log "fresh-host override ready: bot-squad-api → host:${BOTSQUAD_HTTP_PORT}"
}

step_botsquad_group() {
  if getent group "$BOTSQUAD_GROUP" >/dev/null 2>&1; then
    log "group $BOTSQUAD_GROUP already exists"
  else
    sudo groupadd "$BOTSQUAD_GROUP" || die_struct botsquad_group \
      "Could not create group '$BOTSQUAD_GROUP'." \
      "Run 'sudo groupadd $BOTSQUAD_GROUP' manually to see the error."
  fi
  if id -nG "$(id -un)" | tr ' ' '\n' | grep -qxF "$BOTSQUAD_GROUP"; then
    log "user $(id -un) already in $BOTSQUAD_GROUP"
  else
    sudo usermod -aG "$BOTSQUAD_GROUP" "$(id -un)" || die_struct botsquad_group \
      "Could not add user $(id -un) to group '$BOTSQUAD_GROUP'." \
      "Run 'sudo usermod -aG $BOTSQUAD_GROUP $(id -un)' manually."
    warn "you were just added to the '$BOTSQUAD_GROUP' group; you may need to
log out and back in (or run 'newgrp $BOTSQUAD_GROUP') for write access
to $BOTSQUAD_INSTALL_DIR to take effect."
  fi
}

step_install_dir() {
  if [[ ! -d "$BOTSQUAD_INSTALL_DIR" ]]; then
    sudo mkdir -p "$BOTSQUAD_INSTALL_DIR" || die_struct install_dir \
      "Could not mkdir $BOTSQUAD_INSTALL_DIR." \
      "Check parent permissions and try 'sudo mkdir -p $BOTSQUAD_INSTALL_DIR'."
  fi
  sudo chgrp -R "$BOTSQUAD_GROUP" "$BOTSQUAD_INSTALL_DIR"
  sudo chmod -R g+rwX "$BOTSQUAD_INSTALL_DIR"
  sudo chmod g+s "$BOTSQUAD_INSTALL_DIR"
}

step_clone_repo() {
  if [[ -d "$BOTSQUAD_INSTALL_DIR/.git" ]]; then
    log "repo already present at $BOTSQUAD_INSTALL_DIR (skipping clone)"
    return 0
  fi
  if [[ "$BOTSQUAD_CLONE_URL" = "__CLONE_URL__" ]]; then
    die_struct clone_repo \
      "BOTSQUAD_CLONE_URL was not substituted by the mothership and no
override was provided in the environment." \
      "Re-fetch your install command from the mothership UI — the link
embeds the clone URL. If you're running install.sh locally for testing,
set BOTSQUAD_CLONE_URL=<git url or local path> before re-running."
  fi
  # newgrp is interactive; the chgrp+chmod above made the dir writable to
  # any current member of $BOTSQUAD_GROUP. Effective gid may not have
  # picked up the new group yet — use sudo -g to force it for the clone.
  if id -nG "$(id -un)" | tr ' ' '\n' | grep -qxF "$BOTSQUAD_GROUP"; then
    sg "$BOTSQUAD_GROUP" -c "git clone --branch '$BOTSQUAD_REPO_REF' '$BOTSQUAD_CLONE_URL' '$BOTSQUAD_INSTALL_DIR'" \
      || die_struct clone_repo \
        "git clone of $BOTSQUAD_CLONE_URL (ref $BOTSQUAD_REPO_REF) failed." \
        "Common causes: expired install token (24h TTL) -- re-issue from
the mothership UI; or no network access. Re-read the git output above."
  else
    sudo -u "$(id -un)" git clone --branch "$BOTSQUAD_REPO_REF" "$BOTSQUAD_CLONE_URL" "$BOTSQUAD_INSTALL_DIR" \
      || die_struct clone_repo \
        "git clone of $BOTSQUAD_CLONE_URL (ref $BOTSQUAD_REPO_REF) failed." \
        "Common causes: expired install token (24h TTL) -- re-issue from
the mothership UI; or no network access. Re-read the git output above."
  fi
}

step_render_env() {
  if [[ -f "$ENV_FILE" ]]; then
    log ".env already exists at $ENV_FILE (skipping render; edit manually if needed)"
    return 0
  fi
  prompt_value BOTSQUAD_DOMAIN \
    "Domain root for traefik (host will be bot-squad.<this>)" \
    "$(hostname -f 2>/dev/null || hostname)"
  if [[ -z "${BOTSQUAD_JWT_SECRET:-}" ]]; then
    BOTSQUAD_JWT_SECRET="$(openssl rand -hex 32 2>/dev/null || tr -dc 'a-f0-9' </dev/urandom | head -c 64)"
    log "generated JWT_SECRET (64 hex chars)"
  fi
  prompt_value BOTSQUAD_TG_BOT_TOKEN \
    "Telegram bot token for login + notifications (leave blank to use mothership's @bot_squad_bot proxy)" \
    "MOTHERSHIP_PROXY"
  local uid gid
  uid="$(id -u)"
  gid="$(id -g)"
  cat > "$ENV_FILE" <<EOF
# Generated by bot-squad installer on $(date -u +%FT%TZ)
DOMAIN=${BOTSQUAD_DOMAIN}
JWT_SECRET=${BOTSQUAD_JWT_SECRET}
TG_BOT_TOKEN=${BOTSQUAD_TG_BOT_TOKEN}
UID=${uid}
GID=${gid}
EOF
  chmod 0640 "$ENV_FILE"
}

# --- tg_proxy checkpoint (T-0194) --------------------------------------------
#
# Seeds the per-installation Telegram egress proxy into the install's
# config/system_settings.toml under [tg].proxy_url — the same key the worker's
# Config.load() reads and the admin Settings UI writes. Merges (preserves any
# other keys an admin already set via the UI) rather than clobbering; idempotent
# (re-run with the same URL leaves the file's mtime alone). Empty/unset env →
# no-op (no proxy configured).

# Pick a python with tomllib (3.11+). Test seam wins; else worker venv; else host.
tg_proxy_python() {
  if [[ -n "$BOTSQUAD_TG_PROXY_PY" ]]; then
    printf '%s' "$BOTSQUAD_TG_PROXY_PY"; return 0
  fi
  local venv_py="$BOTSQUAD_INSTALL_DIR/worker/.venv/bin/python"
  if [[ -x "$venv_py" ]]; then printf '%s' "$venv_py"; return 0; fi
  if command -v python3 >/dev/null 2>&1; then printf '%s' python3; return 0; fi
  return 1
}

step_tg_proxy() {
  if [[ "${BOTSQUAD_TG_PROXY_URL:-}" = "" ]]; then
    log "no TG egress proxy configured (BOTSQUAD_TG_PROXY_URL unset/empty)"
    return 0
  fi
  local url="$BOTSQUAD_TG_PROXY_URL"
  # Validate scheme — mirror api routes_settings._PROXY_RE and the UI's PROXY_RE.
  if [[ ! "$url" =~ ^(socks5h?|https?):// ]]; then
    die_struct tg_proxy \
      "TG proxy URL '$url' is not valid." \
      "Use socks5://, http://, or https:// — e.g.
  --tg-proxy-url=http://153.80.195.83:8888
Unset it (or pass an empty value) to configure no TG proxy."
  fi
  local cfg_dir="$BOTSQUAD_INSTALL_DIR/config"
  local settings="$cfg_dir/system_settings.toml"
  if [[ ! -d "$cfg_dir" ]]; then
    mkdir -p "$cfg_dir" 2>/dev/null || sudo mkdir -p "$cfg_dir" \
      || die_struct tg_proxy "Could not create $cfg_dir." \
         "Check permissions on $BOTSQUAD_INSTALL_DIR (the clone_repo +
install_dir checkpoints should have created it group-writable)."
  fi
  local py
  py="$(tg_proxy_python)" || die_struct tg_proxy \
    "No python3 with tomllib available to merge $settings." \
    "Ensure the python_venv checkpoint succeeded (it provisions
$BOTSQUAD_INSTALL_DIR/worker/.venv), or install a system python3 (>=3.11)."
  # Merge proxy_url into [tg], preserving other keys; idempotent write.
  BOTSQUAD_TG_PROXY_URL="$url" "$py" - "$settings" <<'PY' || die_struct tg_proxy \
    "Failed to write proxy_url into the system settings file." \
    "Inspect the python error above. Check that $BOTSQUAD_INSTALL_DIR/config
is writable by the installing user."
import os, sys, tomllib

path = sys.argv[1]
url = os.environ["BOTSQUAD_TG_PROXY_URL"]

try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
except FileNotFoundError:
    data = {}

data.setdefault("tg", {})["proxy_url"] = url

def emit(d):
    # One-level tables of scalars — matches the system_settings schema.
    out = ["# bot-squad system settings. Managed by /api/system-settings + installer (T-0194)."]
    for table, body in d.items():
        if not isinstance(body, dict):
            continue
        out.append("")
        out.append(f"[{table}]")
        for k, v in body.items():
            if isinstance(v, bool):
                out.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                out.append(f"{k} = {v}")
            else:
                s = str(v).replace("\\", "\\\\").replace('"', '\\"')
                out.append(f'{k} = "{s}"')
    out.append("")
    return "\n".join(out)

new = emit(data)
old = None
try:
    with open(path) as f:
        old = f.read()
except FileNotFoundError:
    pass
if new != old:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(new)
    os.replace(tmp, path)
PY
  log "TG egress proxy written to $settings ([tg].proxy_url=$url)"
}

step_mothership_handshake() {
  if [[ "$BOTSQUAD_SKIP_MOTHERSHIP" = "1" ]]; then
    log "skipping mothership handshake (BOTSQUAD_SKIP_MOTHERSHIP=1)"
    return 0
  fi
  if [[ "$BOTSQUAD_INSTALL_TOKEN" = "__INSTALL_TOKEN__" ]] || [[ "$BOTSQUAD_MOTHERSHIP_URL" = "__MOTHERSHIP_URL__" ]]; then
    die_struct mothership_handshake \
      "Install token / mothership URL were not substituted." \
      "This usually means install.sh was run from the repo directly, not
from the mothership. To smoke locally, re-run with
BOTSQUAD_SKIP_MOTHERSHIP=1. Otherwise re-fetch your install command from
the mothership UI."
  fi
  local resp http_code body bearer bearer_file server_id install_id_file
  bearer_file="${BOTSQUAD_STATE_DIR}/server.token"
  # T-0088: persist the mothership-issued server_id so the worker's
  # autoupdate poller can stamp it on every telemetry POST. Lives under
  # the bot-squad install's data dir (not $BOTSQUAD_STATE_DIR, which is
  # $HOME-scoped) so the systemd worker — which may run as a different
  # user — can read it without crossing user-home boundaries.
  install_id_file="${BOTSQUAD_INSTALL_DIR}/data/_worker/install.id"
  # Body shape per mothership-seam.md: { token, server_meta }.
  local payload
  payload=$(jq -n \
    --arg token "$BOTSQUAD_INSTALL_TOKEN" \
    --arg host "$(hostname -f 2>/dev/null || hostname)" \
    --arg install_dir "$BOTSQUAD_INSTALL_DIR" \
    --arg coordinator_user "$(id -un)" \
    '{token: $token, server_meta: {hostname: $host, install_dir: $install_dir, coordinator_user: $coordinator_user}}')
  resp="$(mktemp)"
  http_code="$(curl -sS -o "$resp" -w '%{http_code}' \
    -X POST "${BOTSQUAD_MOTHERSHIP_URL}/api/m/installer/connect" \
    -H 'Content-Type: application/json' \
    -d "$payload" || echo 000)"
  if [[ "$http_code" != "200" ]]; then
    body="$(cat "$resp")"; rm -f "$resp"
    die_struct mothership_handshake \
      "Mothership /connect returned HTTP $http_code: $body" \
      "If 410 Gone: install token expired (24h TTL); re-issue from the
mothership UI and re-run with the new link. If 5xx: the mothership is
unreachable; check network/DNS and retry. If 404: confirm
BOTSQUAD_MOTHERSHIP_URL is correct."
  fi
  bearer="$(jq -r '.server_bearer' "$resp")"
  server_id="$(jq -r '.server_id' "$resp")"
  rm -f "$resp"
  if [[ -z "$bearer" || "$bearer" = "null" ]]; then
    die_struct mothership_handshake \
      "Mothership /connect returned 200 but no server_bearer in the body." \
      "This is a mothership bug; ping the bot-squad team."
  fi
  if [[ -z "$server_id" || "$server_id" = "null" ]]; then
    die_struct mothership_handshake \
      "Mothership /connect returned 200 but no server_id in the body." \
      "This is a mothership bug; ping the bot-squad team."
  fi
  umask 077
  printf '%s\n' "$bearer" > "$bearer_file"
  log "server bearer stored at $bearer_file"
  # install.id is world-readable inside the install tree (mode 0644);
  # it's just a server identifier, not a credential, and the worker may
  # run as a different user than the installer.
  umask 022
  mkdir -p "$(dirname "$install_id_file")"
  printf '%s\n' "$server_id" > "$install_id_file"
  log "install id stored at $install_id_file"
}

step_python_venv() {
  local venv="$BOTSQUAD_INSTALL_DIR/worker/.venv"
  if [[ -d "$venv" ]] && "$venv/bin/python" -c 'import bot_squad_worker' >/dev/null 2>&1; then
    log "worker venv already provisioned"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    pkg_install python3 python3-venv python3-pip \
      || die_struct python_venv "Install of python3+venv+pip failed (family=${BOTSQUAD_DISTRO_FAMILY:-?})." \
         "Run the install manually for your distro (e.g.
'sudo apt-get install python3 python3-venv python3-pip' on Debian/Ubuntu,
'sudo dnf install python3 python3-pip' on Fedora,
'sudo pacman -S python python-pip' on Arch)."
  fi
  python3 -m venv "$venv" || die_struct python_venv \
    "python3 -m venv failed for $venv." \
    "On Debian/Ubuntu the venv module is a separate package
(sudo apt-get install python3-venv). On Fedora/Arch it ships with python3."
  "$venv/bin/pip" install --upgrade pip >/dev/null
  "$venv/bin/pip" install -e "$BOTSQUAD_INSTALL_DIR/worker" >/dev/null \
    || die_struct python_venv "pip install -e worker failed." \
       "Run '$venv/bin/pip install -e $BOTSQUAD_INSTALL_DIR/worker' manually
to see the exact pip error."
}

step_systemd_unit() {
  local src="$BOTSQUAD_INSTALL_DIR/systemd/bot-squad-worker.service"
  local dest="/etc/systemd/system/bot-squad-worker.service"
  [[ -f "$src" ]] || die_struct systemd_unit \
    "Source unit file missing: $src" \
    "The repo clone may be incomplete. Try 'cd $BOTSQUAD_INSTALL_DIR && git status'."
  sudo install -m 0644 "$src" "$dest" || die_struct systemd_unit \
    "Could not install $dest." \
    "Run 'sudo install -m 0644 $src $dest' manually to see the error."
  sudo systemctl daemon-reload
  sudo systemctl enable --now bot-squad-worker.service || die_struct systemd_unit \
    "systemctl enable --now bot-squad-worker.service failed." \
    "Run 'sudo systemctl status bot-squad-worker.service' and 'journalctl
-u bot-squad-worker.service -n 50' to see why."
}

step_per_user_worker_unit() {
  # T-0067: drop the per-user systemd-user unit into /etc/systemd/user/ so
  # any attached non-coordinator Linux user can `systemctl --user enable
  # --now bot-squad-user-worker.service` themselves (one-shot via
  # scripts/install/enable-per-user-worker.sh) without a fresh sudo cycle
  # to land the unit file. The coordinator (this install owner) keeps the
  # system-level bot-squad-worker.service from step_systemd_unit — they
  # are NOT mutually exclusive.
  #
  # No `systemctl daemon-reload` here: /etc/systemd/user/ is read by each
  # user's own systemctl --user; the user's enable script does its own
  # daemon-reload when it runs. A system-level reload would not propagate.
  local src="$BOTSQUAD_INSTALL_DIR/systemd/bot-squad-user-worker.service"
  # BOTSQUAD_USER_UNIT_DEST seam: tests redirect this into a temp dir.
  local dest="${BOTSQUAD_USER_UNIT_DEST:-/etc/systemd/user/bot-squad-user-worker.service}"
  [[ -f "$src" ]] || die_struct per_user_worker_unit \
    "Source unit file missing: $src" \
    "The repo clone may be incomplete or out of date. Try
'cd $BOTSQUAD_INSTALL_DIR && git status' and re-pull if needed."
  local dest_dir; dest_dir="$(dirname "$dest")"
  # Idempotency: if the dest exists and is byte-identical, no-op.
  if [[ -f "$dest" ]] && cmp -s "$src" "$dest"; then
    log "per-user systemd-user unit already up to date at $dest"
    return 0
  fi
  if [[ -w "$dest_dir" ]] || { [[ -f "$dest" ]] && [[ -w "$dest" ]]; }; then
    mkdir -p "$dest_dir" || true
    install -m 0644 "$src" "$dest" || die_struct per_user_worker_unit \
      "Could not install $dest." \
      "Run 'install -m 0644 $src $dest' manually to see the error."
  else
    sudo install -d -m 0755 "$dest_dir" || die_struct per_user_worker_unit \
      "Could not create $dest_dir." \
      "Run 'sudo mkdir -p $dest_dir' manually to see the error."
    sudo install -m 0644 "$src" "$dest" || die_struct per_user_worker_unit \
      "Could not install $dest." \
      "Run 'sudo install -m 0644 $src $dest' manually to see the error."
  fi
  log "per-user systemd-user unit installed at $dest"
}

step_docker_compose_up() {
  # T-0029: if the fresh-host override exists (written by
  # install_reverse_proxy on hosts with no shared traefik), layer it on
  # top of the base compose file via -f. On hosts with a shared traefik,
  # the override is absent and `docker compose up` uses just the base.
  local override; override="$(reverse_proxy_override_path)"
  local -a compose_args=(compose -f docker-compose.yml)
  if [[ -f "$override" ]]; then
    compose_args+=(-f "$override")
    log "docker compose: layering fresh-host override $override"
  fi
  compose_args+=(up -d --build)
  ( cd "$BOTSQUAD_INSTALL_DIR" && docker "${compose_args[@]}" ) \
    || die_struct docker_compose_up "docker compose up failed." \
       "Run 'cd $BOTSQUAD_INSTALL_DIR && docker compose up -d --build'
manually to see the build error. If it complains about a missing
external network 'avo_backend', the install_reverse_proxy checkpoint
should have written $override — re-run the installer (it'll resume at
install_reverse_proxy)."
}

step_agent_teams_flag() {
  # claude-code experimental flag for spawning agent teams.
  local cfg="${HOME}/.claude/settings.json"
  mkdir -p "$(dirname "$cfg")"
  if [[ -f "$cfg" ]] && grep -q '"enableAgentTeams": *true' "$cfg" 2>/dev/null; then
    log "agent-teams flag already set in $cfg"
    return 0
  fi
  if [[ ! -f "$cfg" ]]; then
    printf '%s\n' '{"enableAgentTeams": true}' > "$cfg"
    return 0
  fi
  # Append-or-update via jq.
  local tmp; tmp="$(mktemp)"
  jq '. + {enableAgentTeams: true}' "$cfg" > "$tmp" && mv "$tmp" "$cfg"
}

step_spawn_operator() {
  if tmux has-session -t "$BOTSQUAD_OPERATOR_SESSION" 2>/dev/null; then
    log "operator session '$BOTSQUAD_OPERATOR_SESSION' already running"
    return 0
  fi
  # The operator session runs claude with a brief that points it at the
  # bot-squad install. claude-code reads CLAUDE_PROJECT_DIR and any local
  # AGENTS.md / CLAUDE.md once it's running.
  local op_cwd="$BOTSQUAD_INSTALL_DIR/data/bot-squad"
  if [[ ! -d "$op_cwd" ]]; then
    # First-run install: the data dir is created by the worker on first
    # boot; spawn the operator from the install root instead.
    op_cwd="$BOTSQUAD_INSTALL_DIR"
  fi
  tmux new-session -d -s "$BOTSQUAD_OPERATOR_SESSION" -c "$op_cwd" \
    "claude --dangerously-skip-permissions" \
    || die_struct spawn_operator "tmux new-session for operator failed." \
       "Run 'tmux new-session -d -s $BOTSQUAD_OPERATOR_SESSION -c $op_cwd claude'
manually to see the error."
  # Brief the operator session.
  local brief="You are the bot-squad operator for this install. The
installer just finished bringing up FastAPI + worker + UI. Your job is
to manage projects, sessions, and high-level orchestration on this
server. Read AGENTS.md if present. Wait for the user."
  tmux send-keys -t "$BOTSQUAD_OPERATOR_SESSION" "$brief" Enter
}

step_print_attach() {
  cat <<EOF

============================================================
bot-squad is installed.
------------------------------------------------------------
Operator session is running in tmux. From any ssh session on
this host, attach with:

    tmux a -t $BOTSQUAD_OPERATOR_SESSION

When you're attached and have spoken to the operator, you can
exit the bootstrap claude session that drove this install —
that one's job is done.

UI:       http://bot-squad.\$DOMAIN/welcome  (or however your
          reverse proxy routes to the bot-squad-api container)
          — lands on the "you're all set" handoff screen with a
          copyable tmux-attach command; click Next to enter the
          server view.
Worker:   sudo systemctl status bot-squad-worker
State:    $STATE_FILE
============================================================
EOF
}

# ---- NixOS-mode checkpoint (T-0057) -----------------------------------------
# NixOS is declarative: packages and systemd units come from
# /etc/nixos/configuration.nix, not from `apk add` / `nix-env -i` invoked
# at runtime. install.sh therefore CANNOT install docker (etc.) on a
# NixOS host the way it does on debian/fedora/arch/alpine — that would
# bypass the configuration.nix source-of-truth and leave the host in
# an unmanaged state.
#
# Instead, when /etc/os-release announces ID=nixos (or
# BOTSQUAD_DISTRO_FAMILY=nixos is forced), main() picks the NIXOS_STEPS
# chain (just detect_distro + emit_nixos_module). emit_nixos_module
# copies scripts/install/nixos/bot-squad.nix into <install_dir>/nixos/,
# prints the imports/instructions block, and exits. The admin reviews
# the module, adds it to their configuration.nix imports list, and
# runs `nixos-rebuild switch`. The post-rebuild repo clone +
# docker-compose-up are deferred (see backlog T-0057 notes).
#
# Seams:
#   BOTSQUAD_NIXOS_MODULE_DEST — override destination path (default
#                                $BOTSQUAD_INSTALL_DIR/nixos/bot-squad.nix)
#   BOTSQUAD_NIXOS_MODULE_SRC  — override source-of-truth path
#                                (default $__BS_INSTALL_DIR/nixos/bot-squad.nix,
#                                 i.e. alongside install.sh in a repo
#                                 checkout)

step_emit_nixos_module() {
  local dst="${BOTSQUAD_NIXOS_MODULE_DEST:-${BOTSQUAD_INSTALL_DIR}/nixos/bot-squad.nix}"
  local src="${BOTSQUAD_NIXOS_MODULE_SRC:-${__BS_INSTALL_DIR}/nixos/bot-squad.nix}"
  [[ -r "$src" ]] || die_struct emit_nixos_module \
    "Source NixOS module not found at $src." \
    "The installer bundle is incomplete. Re-fetch install.sh AND its
nixos/ subdir from the mothership (or, if running from a repo checkout,
ensure scripts/install/nixos/bot-squad.nix is present alongside
install.sh). You can also point BOTSQUAD_NIXOS_MODULE_SRC at a custom
path if you've staged the module elsewhere on this host."
  local dst_dir; dst_dir="$(dirname "$dst")"
  if [[ ! -d "$dst_dir" ]]; then
    if ! mkdir -p "$dst_dir" 2>/dev/null; then
      sudo mkdir -p "$dst_dir" || die_struct emit_nixos_module \
        "Could not mkdir $dst_dir for the bot-squad.nix module." \
        "Check parent permissions and try 'sudo mkdir -p $dst_dir'."
    fi
  fi
  if [[ -w "$dst_dir" ]] || { [[ -f "$dst" ]] && [[ -w "$dst" ]]; }; then
    install -m 0644 "$src" "$dst" \
      || die_struct emit_nixos_module \
        "Could not write $dst." \
        "Check write permissions on $dst_dir."
  else
    sudo install -m 0644 "$src" "$dst" \
      || die_struct emit_nixos_module \
        "Could not write $dst (with sudo)." \
        "Check sudo + write permissions on $dst_dir."
  fi
  log "bot-squad NixOS module written to $dst"
  cat <<EOF

============================================================
bot-squad on NixOS — declarative integration required.
------------------------------------------------------------
NixOS configures system packages declaratively, so the
installer cannot apt-get/dnf/pacman/apk docker on this host.
It emitted a NixOS module that declares bot-squad's OS-level
dependencies (docker, nodejs, python3, tmux, jq, curl,
ca-certificates) + the bot-squad-worker systemd unit.

Module file:
    $dst

To finish:

  1. Review the module:
         less $dst

  2. Add it to /etc/nixos/configuration.nix (imports list):
         imports = [
           /etc/nixos/hardware-configuration.nix
           $dst
         ];
     And enable it elsewhere in the same file:
         services.bot-squad.enable = true;
         services.bot-squad.user   = "$(id -un)";

  3. Rebuild your NixOS system:
         sudo nixos-rebuild switch

  4. After the rebuild succeeds, the docker socket + tmux +
     nodejs are available system-wide. Manual follow-up:
     clone the bot-squad repo into the install dir
     ($BOTSQUAD_INSTALL_DIR by default), provision the Python
     venv under worker/.venv, and run
     'docker compose up -d --build' to bring the API + worker
     stack online. Auto-clone + compose-up from install.sh on
     NixOS is deferred (see backlog T-0057 notes).

State: $STATE_FILE
============================================================
EOF
}

# ---- Invite-mode checkpoints (T-0026) ---------------------------------------
# When BOTSQUAD_INSTALL_TOKEN carries an invite prefix (bsq_invite_*), the
# script runs the join-existing-install chain instead of fresh-install. The
# steps below are siblings of the fresh-install ones but DO NOT create the
# install dir, the www group, the systemd UNIT install, or run docker
# compose — that's the existing coordinator's territory.

step_mothership_join() {
  # Sibling of step_mothership_handshake but for invite tokens. Calls
  # POST /api/m/installer/join with the invite plaintext, gets back the
  # target_username + role the mothership recorded at mint time, then
  # refuses if the Linux user running install.sh isn't the target.
  if [[ "$BOTSQUAD_SKIP_MOTHERSHIP" = "1" ]]; then
    die_struct mothership_join \
      "BOTSQUAD_SKIP_MOTHERSHIP=1 is incompatible with invite-mode install." \
      "Invite-mode requires the mothership to authoritatively name the target
Linux user + role — there is no offline override. Unset
BOTSQUAD_SKIP_MOTHERSHIP and re-run."
  fi
  if [[ "$BOTSQUAD_INSTALL_TOKEN" = "__INSTALL_TOKEN__" ]] || [[ "$BOTSQUAD_MOTHERSHIP_URL" = "__MOTHERSHIP_URL__" ]]; then
    die_struct mothership_join \
      "Invite token / mothership URL were not substituted." \
      "Invite-mode install must be invoked from the mothership-served
install.sh (curl https://<mothership>/i/<invite>/install.sh). Re-fetch the
install URL from the invite link."
  fi
  local resp http_code body target_username role server_id state_file
  state_file="${BOTSQUAD_STATE_DIR}/invite.target"
  local payload
  payload=$(jq -n \
    --arg token "$BOTSQUAD_INSTALL_TOKEN" \
    '{token: $token}')
  resp="$(mktemp)"
  http_code="$(curl -sS -o "$resp" -w '%{http_code}' \
    -X POST "${BOTSQUAD_MOTHERSHIP_URL}/api/m/installer/join" \
    -H 'Content-Type: application/json' \
    -d "$payload" || echo 000)"
  if [[ "$http_code" != "200" ]]; then
    body="$(cat "$resp")"; rm -f "$resp"
    die_struct mothership_join \
      "Mothership /installer/join returned HTTP $http_code: $body" \
      "If 410 Gone: invite token expired (24h TTL) or already used; ask
the inviter to issue a new link. If 400: the token doesn't look like an
invite — make sure you're using the invite URL, not a fresh-install URL.
If 5xx: check network/DNS and retry."
  fi
  target_username="$(jq -r '.target_username' "$resp")"
  role="$(jq -r '.role' "$resp")"
  server_id="$(jq -r '.server_id' "$resp")"
  rm -f "$resp"
  if [[ -z "$target_username" || "$target_username" = "null" ]]; then
    die_struct mothership_join \
      "Mothership /installer/join returned 200 but no target_username." \
      "This is a mothership bug; ping the bot-squad team."
  fi
  if [[ "$role" != "admin" && "$role" != "non-admin" ]]; then
    die_struct mothership_join \
      "Mothership returned unrecognized role '$role'." \
      "Expected 'admin' or 'non-admin'. Ping the bot-squad team."
  fi
  local current_user; current_user="$(id -un)"
  if [[ "$current_user" != "$target_username" ]]; then
    die_struct mothership_join \
      "Invite is for Linux user '$target_username' but install.sh is
running as '$current_user'." \
      "Switch to the target user (su - $target_username) on this host and
re-run the same install command. Invites are bound to a specific Linux
username at mint time and cannot be transferred."
  fi
  # Persist for downstream steps. Mode 0644 — the values are not secret,
  # the invite plaintext is already burned at this point.
  umask 022
  cat > "$state_file" <<EOF
BOTSQUAD_INVITE_ROLE=$role
BOTSQUAD_INVITE_TARGET_USERNAME=$target_username
BOTSQUAD_INVITE_SERVER_ID=$server_id
EOF
  export BOTSQUAD_INVITE_ROLE="$role"
  export BOTSQUAD_INVITE_TARGET_USERNAME="$target_username"
  export BOTSQUAD_INVITE_SERVER_ID="$server_id"
  log "joining server $server_id as $target_username (role: $role)"
}

step_user_groups_join() {
  # Two group memberships, gated by invite role:
  #   - 'www' (BOTSQUAD_SHARED_GROUP, default "www") — EVERY invited user
  #     needs this for read access to the per-user socket dir at
  #     $BOTSQUAD_INSTALL_DIR/data/_sock. Per docs/multi-user-setup.md
  #     step 1, this is unconditional for any non-coordinator user.
  #   - 'bot-squad' (BOTSQUAD_ADMIN_GROUP, default "bot-squad") — admin
  #     role only, per the T-0026 DoD: "Admin gets added to bot-squad
  #     group; non-admin doesn't (usermod -aG bot-squad only for admin)."
  #     If the group is absent on this host (older fresh-install layout
  #     doesn't create it), we warn but DON'T fail — the invite-mode
  #     install shouldn't refuse over a missing group it didn't create.
  local user shared_group admin_group
  user="$(id -un)"
  shared_group="${BOTSQUAD_SHARED_GROUP:-www}"
  admin_group="${BOTSQUAD_ADMIN_GROUP:-bot-squad}"
  if ! getent group "$shared_group" >/dev/null 2>&1; then
    die_struct user_groups_join \
      "Group '$shared_group' does not exist on this host." \
      "The existing bot-squad install creates this group on a fresh
host. Either the host wasn't installed via the bot-squad installer, or
the group was removed. Ask the existing install's admin to (re)create
it: 'sudo groupadd $shared_group'."
  fi
  if id -nG "$user" | tr ' ' '\n' | grep -qxF "$shared_group"; then
    log "user $user already in '$shared_group' group"
  else
    sudo usermod -aG "$shared_group" "$user" || die_struct user_groups_join \
      "Could not add user $user to group '$shared_group'." \
      "Run 'sudo usermod -aG $shared_group $user' manually to see the error."
    warn "added $user to '$shared_group' group — log out and back in (or run
'newgrp $shared_group') for the membership to take effect in this shell."
  fi
  if [[ "${BOTSQUAD_INVITE_ROLE:-}" = "admin" ]]; then
    if ! getent group "$admin_group" >/dev/null 2>&1; then
      warn "admin group '$admin_group' is absent on this host — skipping
admin group membership. The install admin can create it later
('sudo groupadd $admin_group && sudo usermod -aG $admin_group $user')."
    elif id -nG "$user" | tr ' ' '\n' | grep -qxF "$admin_group"; then
      log "user $user already in '$admin_group' group (admin)"
    else
      sudo usermod -aG "$admin_group" "$user" || die_struct user_groups_join \
        "Could not add user $user to admin group '$admin_group'." \
        "Run 'sudo usermod -aG $admin_group $user' manually."
      warn "added $user to '$admin_group' (admin) — re-log for it to apply."
    fi
  else
    log "non-admin invite — skipping '$admin_group' group membership"
  fi
  # loginctl enable-linger so the user-worker survives logout (per
  # docs/multi-user-setup.md step 1). Idempotent.
  if command -v loginctl >/dev/null 2>&1; then
    if [[ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
      sudo loginctl enable-linger "$user" || warn "loginctl enable-linger $user failed; the user-worker won't survive logout"
    else
      log "linger already enabled for $user"
    fi
  fi
}

step_per_user_worker_enable() {
  # Call the existing enable-per-user-worker.sh helper. Skipped if the
  # system-wide unit isn't dropped yet — the user's coordinator must
  # have run the fresh-install path first.
  local helper="${BOTSQUAD_INSTALL_DIR}/scripts/install/enable-per-user-worker.sh"
  if [[ ! -x "$helper" ]] && [[ ! -f "$helper" ]]; then
    die_struct per_user_worker_enable \
      "Expected helper $helper missing." \
      "The install at $BOTSQUAD_INSTALL_DIR may be out of date or
incomplete. Ask the existing install's admin to verify the bot-squad
checkout includes scripts/install/enable-per-user-worker.sh."
  fi
  # Honor a test-override so the shell test can substitute a stub.
  local cmd="${BOTSQUAD_PER_USER_ENABLE_CMD:-bash \"$helper\"}"
  bash -c "$cmd" || die_struct per_user_worker_enable \
    "Per-user worker enablement failed." \
    "Re-run '$helper' manually to see the underlying error. Common
causes: systemd-user not running for this account, or the system-wide
unit /etc/systemd/user/bot-squad-user-worker.service hasn't been
installed yet — ask the install admin to re-run the host installer."
}

step_print_join_attach() {
  cat <<EOF

============================================================
You're joined to bot-squad as $(id -un) (${BOTSQUAD_INVITE_ROLE:-?}).
------------------------------------------------------------
Your per-user worker is running. From any ssh session on this
host, you can now spawn sessions in the UI and they'll land in
YOUR tmux server.

UI:       ask the install admin for the bot-squad URL; sign in
          with the credentials they minted for you.
Worker:   systemctl --user status bot-squad-user-worker
Install:  $BOTSQUAD_INSTALL_DIR  (shared with other users)
State:    $STATE_FILE

If this shell can't reach the bot-squad data dir, log out and
back in — you were just added to the 'www' group and Unix
group membership is fixed at session start.
============================================================
EOF
}

# ---- Step order -------------------------------------------------------------
# Three chains, dispatched at main() startup time:
#   - INSTALL_STEPS: fresh install on a never-installed host. Owns the
#     install dir, creates the www group, drops systemd units, runs
#     docker compose up.
#   - INVITE_STEPS  (T-0026): the host ALREADY runs bot-squad; an
#     additional Linux user is joining via a per-user worker. NO group
#     create, NO install-dir create, NO systemd UNIT install at
#     install-level, NO docker compose up. The existing coordinator
#     keeps the install; this script just provisions the invitee.
#   - NIXOS_STEPS  (T-0057): declarative-distro path. install.sh CANNOT
#     install OS packages via configuration.nix from a shell script
#     without breaking NixOS's source-of-truth contract. The chain
#     short-circuits to detect_distro + emit_nixos_module — the latter
#     copies scripts/install/nixos/bot-squad.nix into <install_dir>/
#     and prints the admin's nixos-rebuild instructions block.
INSTALL_STEPS=(
  detect_distro
  require_sudo
  proxy_url
  pkg_index_update
  install_base_pkgs
  install_tmux
  install_nodejs
  install_claude_code
  install_docker
  botsquad_group
  install_dir
  clone_repo
  render_env
  mothership_handshake
  python_venv
  tg_proxy
  systemd_unit
  per_user_worker_unit
  install_reverse_proxy
  docker_compose_up
  agent_teams_flag
  spawn_operator
  print_attach
)
INVITE_STEPS=(
  detect_distro
  require_sudo
  proxy_url
  pkg_index_update
  install_base_pkgs
  install_tmux
  install_nodejs
  install_claude_code
  mothership_join
  user_groups_join
  per_user_worker_enable
  agent_teams_flag
  print_join_attach
)
NIXOS_STEPS=(
  detect_distro
  emit_nixos_module
)
# STEPS is selected at runtime in main(). Default to INSTALL_STEPS so a
# sourced-without-main test (smoke_engine.sh) sees the historical array.
STEPS=("${INSTALL_STEPS[@]}")

# Parse the handful of CLI flags the installer accepts. Everything else is
# env-driven; flags are sugar that set the matching BOTSQUAD_* env var so the
# checkpoints read them uniformly. Unknown args are ignored (forward-compatible
# with mothership-appended flags). T-0194: --tg-proxy-url.
parse_cli_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tg-proxy-url=*) export BOTSQUAD_TG_PROXY_URL="${1#*=}"; shift ;;
      --tg-proxy-url)   export BOTSQUAD_TG_PROXY_URL="${2:-}"; shift 2 ;;
      *) shift ;;
    esac
  done
}

main() {
  parse_cli_args "$@"
  ensure_state_dir
  # Pick the chain. Three signals, evaluated in order:
  #   1. invite-token prefix (bsq_invite_*) → INVITE_STEPS
  #   2. /etc/os-release ID=nixos (or BOTSQUAD_DISTRO_FAMILY=nixos) →
  #      NIXOS_STEPS — declarative, can't run apt/dnf/pacman/apk
  #   3. else → INSTALL_STEPS (debian/fedora/arch/alpine)
  # We resolve BEFORE checkpoint dispatch so the state file is
  # consistent across reruns (a half-finished INSTALL_STEPS state +
  # a NixOS rerun would otherwise silently skip half the steps).
  # The prefix detect matches install_tokens.py:INVITE_PREFIX.
  if [[ "$BOTSQUAD_INSTALL_TOKEN" == bsq_invite_* ]]; then
    STEPS=("${INVITE_STEPS[@]}")
    log "bot-squad installer starting in INVITE mode (state file: $STATE_FILE)"
  elif [[ "$(detect_distro_family 2>/dev/null || true)" = "nixos" ]]; then
    STEPS=("${NIXOS_STEPS[@]}")
    log "bot-squad installer starting in NIXOS mode (declarative; emit module + exit; state file: $STATE_FILE)"
  else
    STEPS=("${INSTALL_STEPS[@]}")
    log "bot-squad installer starting (state file: $STATE_FILE)"
  fi
  for step in "${STEPS[@]}"; do
    run_checkpoint "$step"
  done
  log "all checkpoints complete"
}

main "$@"
