#!/usr/bin/env bash
# Stop hook — fire a Telegram ping if the session stopped after being idle >60s.
#
# Fires when Claude Code exits (Stop event). If the last user prompt was >60s
# ago it means the agent ran a long sequence with no new user input — worth
# notifying so the stakeholder can review and continue.
#
# Design notes:
#   - Never exits non-zero: a broken hook must not block Claude Code sessions.
#   - Silent exit 0 on: not in tmux, no timestamp file, age ≤ 60s, unknown project.
#   - set -uo pipefail (no -e) so a failed subcommand doesn't kill the script.
set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
WORKER_SOCK="${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}"

# Must be in tmux to have a meaningful SID.
sid=$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null) || exit 0
[ -n "$sid" ] || exit 0

# Check timestamp file (written by user_prompt_submit.sh).
stamp=".claude/last_user_prompt_ts"
[ -f "$stamp" ] || exit 0

mtime=$(stat -c %Y "$stamp" 2>/dev/null || echo 0)
now=$(date +%s)
age=$((now - mtime))
[ "$age" -gt 60 ] || exit 0

# Resolve slug from CWD against projects.toml.
slug=$(python3 - <<'PY'
import os, sys, tomllib
cfg_path = os.environ.get("BOT_SQUAD", "/home/www/bot-squad") + "/config/projects.toml"
try:
    cfg = tomllib.loads(open(cfg_path).read())
except Exception:
    sys.exit(0)
cwd = os.getcwd()
for slug, p in cfg.get("projects", {}).items():
    repo = p.get("repo_path", "")
    if repo and (cwd == repo or cwd.startswith(repo.rstrip("/") + "/")):
        print(slug)
        sys.exit(0)
sys.exit(0)
PY
2>/dev/null) || exit 0
[ -n "$slug" ] || exit 0

# Build the JSON payload and POST to the worker.
username=$(id -un 2>/dev/null || echo unknown)
payload=$(python3 -c '
import json, sys
print(json.dumps({
    "slug":    sys.argv[1],
    "sid":     sys.argv[2],
    "user":    sys.argv[3],
    "message": "needs your input",
}))
' "$slug" "$sid" "$username" 2>/dev/null) || exit 0

curl -sS --unix-socket "$WORKER_SOCK" \
    -X POST -H "Content-Type: application/json" \
    -d "$payload" \
    http://w/actions/tg_notify >/dev/null 2>&1 || true

exit 0
