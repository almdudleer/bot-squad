#!/usr/bin/env bash
#
# peer-inbox-wait.sh — long-poll the peer inbox; returns when new mail
# arrives or the timeout elapses. Pairs with peer-inbox-read for the
# push-style "wake me when there's something" pattern: run with
# `run_in_background: true` from Claude Code so the harness fires its
# task-notification when this returns.
#
# Usage (via symlink at ops/bot-squad-bin/peer-inbox-wait):
#   peer-inbox-wait [--sid <SID>] [--timeout <seconds>]
#
# --timeout defaults to 1800s (worker hard-cap). Slug resolved from cwd.
# SID auto-detected from tmux when --sid is omitted.

set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
WORKER_SOCK="${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}"

sid=""
timeout="1800"
while [ $# -gt 0 ]; do
  case "$1" in
    --sid)     sid="$2";     shift 2 ;;
    --timeout) timeout="$2"; shift 2 ;;
    -h|--help)
      sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "peer-inbox-wait: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$sid" ]; then
  sid="$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null || true)"
fi
if [ -z "$sid" ]; then
  echo "peer-inbox-wait: SID not provided and not derivable from tmux. Pass --sid <SID>." >&2
  exit 2
fi

case "$timeout" in
  ''|*[!0-9]*) echo "peer-inbox-wait: --timeout must be an integer (got '$timeout')" >&2; exit 2 ;;
esac

slug=$(python3 - "$BOT_SQUAD/config/projects.toml" "$PWD" <<'PY'
import os, sys, tomllib
cfg_path, cwd = sys.argv[1], sys.argv[2]
real_cwd = os.path.realpath(cwd)
with open(cfg_path, "rb") as f: cfg = tomllib.load(f)
def _match(c,t):
    if not t: return False
    rt = os.path.realpath(t)
    return c==t or c.startswith(t.rstrip("/")+"/") or real_cwd==rt or real_cwd.startswith(rt.rstrip("/")+"/")
for s,p in cfg.get("projects",{}).items():
    for k in ("repo_path","repo_master"):
        if _match(cwd, p.get(k,"")):
            print(s); sys.exit(0)
PY
)

if [ -z "$slug" ]; then
  echo "peer-inbox-wait: CWD $PWD not in any registered project" >&2
  exit 2
fi

curl -sS --unix-socket "$WORKER_SOCK" \
  -X POST -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys; print(json.dumps({'slug':sys.argv[1],'sid':sys.argv[2],'timeout':int(sys.argv[3])}))" "$slug" "$sid" "$timeout")" \
  http://w/actions/peer_inbox_wait
echo
