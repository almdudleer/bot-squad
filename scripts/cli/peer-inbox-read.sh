#!/usr/bin/env bash
#
# peer-inbox-read.sh — drain new peer-bus messages addressed to this SID.
#
# Usage (via symlink at ops/bot-squad-bin/peer-inbox-read):
#   peer-inbox-read [--sid <SID>]
#
# Slug is resolved from cwd (same logic as deploy / pause-deploys /
# register-session / safe-commit). SID is auto-detected from tmux when
# --sid is omitted; for non-tmux sessions (Claude Desktop, IDE), pass it
# explicitly (use the value `register-session` printed).
#
# Output: raw JSON from the worker action — {"ok":true,"messages":[…],"count":N}.

set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
WORKER_SOCK="${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}"

sid=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sid) sid="$2"; shift 2 ;;
    -h|--help)
      sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "peer-inbox-read: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Auto-detect SID from tmux if not given.
if [ -z "$sid" ]; then
  sid="$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null || true)"
fi
if [ -z "$sid" ]; then
  echo "peer-inbox-read: SID not provided and not derivable from tmux. Pass --sid <SID>." >&2
  exit 2
fi

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
  echo "peer-inbox-read: CWD $PWD not in any registered project" >&2
  exit 2
fi

curl -sS --unix-socket "$WORKER_SOCK" \
  -X POST -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys; print(json.dumps({'slug':sys.argv[1],'sid':sys.argv[2]}))" "$slug" "$sid")" \
  http://w/actions/peer_inbox_read
echo
