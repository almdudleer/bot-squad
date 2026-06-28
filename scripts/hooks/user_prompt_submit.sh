#!/usr/bin/env bash
# UserPromptSubmit hook — record that the user sent a prompt.
# Creates/updates .claude/last_user_prompt_ts in the current working directory.
# Claude Code runs hooks with cwd = project root, so this lands in the right place.
set -uo pipefail
mkdir -p .claude
touch .claude/last_user_prompt_ts

# T-0470 (M1/F1.7): a prompt was submitted → a turn is in progress, so this
# session is NOT idle. Stamp the per-SID `.active` lifecycle marker so the
# lifecycle engine's hook-driven stall clock reads 0 while the turn runs (a
# `.active` newer than the Stop marker = busy). Per-SID (per-pane), mirroring
# the Stop hook + lifecycle_events.marker_path. Best-effort; never fail the hook.
{
    _bsq="${BOT_SQUAD:-/home/www/bot-squad}"
    _sid=$("$_bsq/scripts/hooks/hook_my_sid.sh" 2>/dev/null) || _sid=""
    if [ -n "$_sid" ]; then
        mkdir -p .claude/bsq_lifecycle 2>/dev/null \
            && : > ".claude/bsq_lifecycle/${_sid}.active" 2>/dev/null || true
    fi
} >/dev/null 2>&1 || true

# T-0155: a prompt was submitted into this pane — whoever was blocked here just
# got input (typically the stakeholder replying in tmux). Cancel any pending TG
# stall escalation for this session. Best-effort: never fail the hook.
BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
WORKER_SOCK="${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}"
{
    sid=$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null) || sid=""
    if [ -n "$sid" ] && [ -S "$WORKER_SOCK" ]; then
        slug=$(python3 - <<'PY' 2>/dev/null || true
import os, sys, tomllib
cfg_path = os.environ.get("BOT_SQUAD", "/home/www/bot-squad") + "/config/projects.toml"
try:
    cfg = tomllib.loads(open(cfg_path).read())
except Exception:
    sys.exit(0)
cwd = os.getcwd()
real_cwd = os.path.realpath(cwd)
def _match(t):
    if not t:
        return False
    rt = os.path.realpath(t)
    return cwd == t or cwd.startswith(t.rstrip("/") + "/") or real_cwd == rt or real_cwd.startswith(rt.rstrip("/") + "/")
for slug, p in cfg.get("projects", {}).items():
    if _match(p.get("repo_path", "")) or _match(p.get("repo_master", "")) or _match(p.get("repo_workspace", "")):
        print(slug); sys.exit(0)
PY
)
        if [ -n "$slug" ]; then
            payload=$(python3 -c 'import json,sys; print(json.dumps({"slug":sys.argv[1],"sid":sys.argv[2]}))' "$slug" "$sid" 2>/dev/null) || payload=""
            [ -n "$payload" ] && curl -sS --unix-socket "$WORKER_SOCK" \
                -X POST -H "Content-Type: application/json" \
                -d "$payload" http://w/actions/tg_stall_clear >/dev/null 2>&1 || true
        fi
    fi
} >/dev/null 2>&1 || true

exit 0
