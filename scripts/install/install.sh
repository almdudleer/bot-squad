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
# Skip the mothership /connect handshake — for offline smoke only.
BOTSQUAD_SKIP_MOTHERSHIP="${BOTSQUAD_SKIP_MOTHERSHIP:-0}"
# Non-interactive mode (CI / smoke): refuse to prompt; fail with a clear
# checkpoint instead so claude can be told what env to set on rerun.
BOTSQUAD_NONINTERACTIVE="${BOTSQUAD_NONINTERACTIVE:-0}"

STATE_FILE="${BOTSQUAD_STATE_DIR}/install.state"
ENV_FILE="${BOTSQUAD_INSTALL_DIR}/.env"

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
  # Endpoint owned by T-0024 — final path may change; currently treated
  # as best-effort. Body: { checkpoint, status, hostname, ts }.
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

step_require_linux() {
  case "$(uname -s)" in
    Linux) : ;;
    *) die_struct require_linux "OS is $(uname -s); bot-squad install only supports Linux." \
         "Run on an Ubuntu/Debian host." ;;
  esac
  if ! command -v apt-get >/dev/null 2>&1; then
    die_struct require_linux "apt-get not found; only Debian/Ubuntu are supported by this v1 installer." \
      "Install on Ubuntu 22.04+ or Debian 12+. Cross-distro support is tracked separately."
  fi
}

step_require_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then return 0; fi
  if sudo -n true 2>/dev/null; then return 0; fi
  if [[ "$BOTSQUAD_NONINTERACTIVE" = "1" ]]; then
    die_struct require_sudo "This step needs sudo but no cached credential is available and NONINTERACTIVE=1." \
      "Run 'sudo -v' interactively once, then re-run the installer."
  fi
  log "you'll be prompted for your sudo password (needed for apt + group setup)"
  sudo -v || die_struct require_sudo "sudo authentication failed." \
    "Make sure your user is in /etc/sudoers (or the 'sudo' group), then re-run."
}

step_apt_update() {
  sudo apt-get update -y >/dev/null || die_struct apt_update \
    "apt-get update failed." \
    "Check network connectivity and APT sources (/etc/apt/sources.list*).
If you're behind a proxy, configure /etc/apt/apt.conf.d/01proxy first."
}

step_install_base_pkgs() {
  # curl + git + ca-certificates + jq (for parsing mothership responses).
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl ca-certificates git jq >/dev/null || die_struct install_base_pkgs \
      "apt-get install of base packages (curl/git/jq/ca-certificates) failed." \
      "Inspect the apt-get output above. The most common cause is a held
package or a stale apt cache — try 'sudo apt-get update && sudo apt-get -f install'."
}

step_install_tmux() {
  if command -v tmux >/dev/null 2>&1; then return 0; fi
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tmux >/dev/null \
    || die_struct install_tmux "Could not install tmux via apt-get." \
       "Try 'sudo apt-get install tmux' manually to see the exact error."
}

step_install_nodejs() {
  if command -v node >/dev/null 2>&1; then
    local v
    v="$(node -v 2>/dev/null | sed 's/^v//')"
    # claude-code needs node >= 18; be lenient on minor.
    if [[ -n "$v" ]] && [[ "${v%%.*}" -ge 18 ]]; then return 0; fi
  fi
  # Ship NodeSource 20.x — minimum claude-code-compatible LTS.
  if ! curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null 2>&1; then
    die_struct install_nodejs "Failed to fetch the NodeSource setup script." \
      "Check network (curl https://deb.nodesource.com). If you're on a corporate
network, set HTTPS_PROXY before re-running. Then re-run the installer."
  fi
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs >/dev/null \
    || die_struct install_nodejs "apt-get install nodejs failed." \
       "Run 'sudo apt-get install nodejs' to see the exact apt error."
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
configure HTTPS_PROXY and retry."
  fi
  command -v claude >/dev/null 2>&1 || die_struct install_claude_code \
    "npm install reported success but 'claude' is still not on PATH." \
    "Add npm's global bin dir to your PATH (npm bin -g) and re-run."
}

step_require_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  die_struct require_docker \
    "Docker engine + compose plugin not detected. This v1 installer assumes
docker is already configured for the current user." \
    "Install docker engine + compose plugin per
https://docs.docker.com/engine/install/ubuntu/ — then add your user to the
'docker' group (sudo usermod -aG docker \$USER), log out + back in, and
re-run the installer. (Bootstrap-from-zero docker install is a follow-on
task.)"
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
  local resp http_code body bearer bearer_file
  bearer_file="${BOTSQUAD_STATE_DIR}/server.token"
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
  rm -f "$resp"
  if [[ -z "$bearer" || "$bearer" = "null" ]]; then
    die_struct mothership_handshake \
      "Mothership /connect returned 200 but no server_bearer in the body." \
      "This is a mothership bug; ping the bot-squad team."
  fi
  umask 077
  printf '%s\n' "$bearer" > "$bearer_file"
  log "server bearer stored at $bearer_file"
}

step_python_venv() {
  local venv="$BOTSQUAD_INSTALL_DIR/worker/.venv"
  if [[ -d "$venv" ]] && "$venv/bin/python" -c 'import bot_squad_worker' >/dev/null 2>&1; then
    log "worker venv already provisioned"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip >/dev/null \
      || die_struct python_venv "apt-get install python3 failed." \
         "Run 'sudo apt-get install python3 python3-venv python3-pip' manually."
  fi
  python3 -m venv "$venv" || die_struct python_venv \
    "python3 -m venv failed for $venv." \
    "Make sure python3-venv is installed (sudo apt-get install python3-venv)."
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

step_docker_compose_up() {
  ( cd "$BOTSQUAD_INSTALL_DIR" && docker compose up -d --build ) \
    || die_struct docker_compose_up "docker compose up failed." \
       "Run 'cd $BOTSQUAD_INSTALL_DIR && docker compose up -d --build'
manually to see the build error. If it complains about a missing
external network 'avo_backend', that prerequisite isn't bundled in v1 —
create it with 'docker network create avo_backend' (or whatever your
reverse-proxy network is) and re-run."
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

UI:       http://bot-squad.\$DOMAIN  (or however your reverse
          proxy routes to the bot-squad-api container)
Worker:   sudo systemctl status bot-squad-worker
State:    $STATE_FILE
============================================================
EOF
}

# ---- Step order -------------------------------------------------------------
STEPS=(
  require_linux
  require_sudo
  apt_update
  install_base_pkgs
  install_tmux
  install_nodejs
  install_claude_code
  require_docker
  botsquad_group
  install_dir
  clone_repo
  render_env
  mothership_handshake
  python_venv
  systemd_unit
  docker_compose_up
  agent_teams_flag
  spawn_operator
  print_attach
)

main() {
  ensure_state_dir
  log "bot-squad installer starting (state file: $STATE_FILE)"
  for step in "${STEPS[@]}"; do
    run_checkpoint "$step"
  done
  log "all checkpoints complete"
}

main "$@"
