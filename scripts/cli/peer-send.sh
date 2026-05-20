#!/usr/bin/env bash
#
# peer-send.sh — send a message to a peer session SID or a role keyword
# (teamlead / dev / all). Slug-scoped.
#
# Usage (via symlink at ops/bot-squad-bin/peer-send):
#   peer-send <to> "<text>" [--from <SID>]
#
# <to> is a SID (e.g. S-aqice-ai-chat-p99999) or one of the role keywords:
#   teamlead — every session with no task_id
#   dev      — every session with a real task_id
#   all      — every session registered for this slug
#
# --from auto-detects from tmux when omitted. Slug resolved from cwd.

set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
WORKER_SOCK="${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}"

to=""
text=""
from_sid=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from) from_sid="$2"; shift 2 ;;
    -h|--help)
      sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    --) shift; break ;;
    -*) echo "peer-send: unknown flag: $1" >&2; exit 2 ;;
    *)
      if   [ -z "$to" ];   then to="$1";   shift
      elif [ -z "$text" ]; then text="$1"; shift
      else echo "peer-send: extra positional arg: $1" >&2; exit 2
      fi ;;
  esac
done

if [ -z "$to" ] || [ -z "$text" ]; then
  echo 'usage: peer-send <to-sid-or-role> "<text>" [--from <SID>]' >&2
  exit 2
fi

if [ -z "$from_sid" ]; then
  from_sid="$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null || true)"
fi
if [ -z "$from_sid" ]; then
  echo "peer-send: --from SID not provided and not derivable from tmux." >&2
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
  echo "peer-send: CWD $PWD not in any registered project" >&2
  exit 2
fi

curl -sS --unix-socket "$WORKER_SOCK" \
  -X POST -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys; print(json.dumps({'slug':sys.argv[1],'from_sid':sys.argv[2],'to':sys.argv[3],'text':sys.argv[4]}))" "$slug" "$from_sid" "$to" "$text")" \
  http://w/actions/peer_send
echo
