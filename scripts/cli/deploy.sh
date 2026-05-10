#!/usr/bin/env bash
# deploy.sh — agent-facing shim that queues a deploy via the worker socket.
#
# Usage (via symlink at ops/bot-squad-bin/deploy):
#   deploy <target> "<reason>"
#
# Targets: staging, dev (whatever projects.toml::deploy_targets allows).
# The worker validates the target; this shim just forwards the request.
#
# SID format: S-<user>-<window>-p<pane_id>  (unique even when pane names clash)
set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
WORKER_SOCK="${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}"

# --- args ---
target="${1:-}"
reason="${2:-}"
if [ -z "$target" ] || [ -z "$reason" ]; then
    echo "usage: deploy <target> \"<reason>\"" >&2
    exit 2
fi

# --- resolve slug from CWD ---
slug=$(python3 - <<'PY'
import os, sys, tomllib
cfg_path = os.environ.get("BOT_SQUAD", "/home/www/bot-squad") + "/config/projects.toml"
try:
    cfg = tomllib.loads(open(cfg_path).read())
except Exception as e:
    print(f"error: cannot read projects.toml: {e}", file=sys.stderr)
    sys.exit(1)
cwd = os.getcwd()
for slug, p in cfg.get("projects", {}).items():
    repo = p.get("repo_path", "")
    if repo and (cwd == repo or cwd.startswith(repo.rstrip("/") + "/")):
        print(slug)
        sys.exit(0)
print(f"error: CWD {cwd!r} not in any registered project", file=sys.stderr)
sys.exit(1)
PY
) || exit 1

# --- compute SID (inline, same logic as hook_my_sid.sh) ---
sid=""
if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
    user=$(id -un 2>/dev/null || echo u)
    user=${user//[^A-Za-z0-9_]/_}
    _target_flag=""
    [ -n "${TMUX_PANE:-}" ] && _target_flag="-t $TMUX_PANE"
    # shellcheck disable=SC2086
    window=$(tmux display-message -p ${_target_flag} -F '#W' 2>/dev/null) || window=""
    window=${window//[^A-Za-z0-9_-]/_}
    # shellcheck disable=SC2086
    pane_raw=$(tmux display-message -p ${_target_flag} -F '#{pane_id}' 2>/dev/null) || pane_raw=""
    pane=${pane_raw#%}
    if [ -n "$window" ] && [ -n "$pane" ]; then
        sid="S-${user}-${window}-p${pane}"
    fi
fi

# --- build JSON payload ---
requested_by=$(id -un 2>/dev/null || echo unknown)
payload=$(python3 -c '
import json, sys
d = {
    "slug": sys.argv[1],
    "target": sys.argv[2],
    "reason": sys.argv[3],
    "requested_by": sys.argv[4],
}
print(json.dumps(d))
' "$slug" "$target" "$reason" "$requested_by")

# --- POST to worker ---
echo "Queuing deploy: slug=$slug target=$target"
response=$(curl -sS --unix-socket "$WORKER_SOCK" \
    -X POST -H "Content-Type: application/json" \
    -d "$payload" \
    http://w/actions/deploy 2>&1) || {
    echo "error: worker unreachable (socket: $WORKER_SOCK)" >&2
    exit 1
}

# --- parse response ---
ok=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d.get("ok","?"))' "$response" 2>/dev/null || echo "?")
if [ "$ok" = "True" ] || [ "$ok" = "true" ]; then
    queue_id=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d.get("queue_id","?"))' "$response" 2>/dev/null || echo "?")
    echo "Queued: $queue_id"
    [ -n "$sid" ] && echo "SID: $sid"
    echo "Deploy monitor will process within ~60s when tree is clean."
else
    error=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d.get("detail", d.get("error","unknown")))' "$response" 2>/dev/null || echo "$response")
    echo "error: worker rejected deploy: $error" >&2
    exit 1
fi
