#!/usr/bin/env bash
#
# register-session.sh — manual session registration for environments that
# can't run the SessionStart hook.
#
# Usage (via symlink at ops/bot-squad-bin/register-session):
#   register-session <window> [--task T-NNNN] [--initiative <name>]
#                             [--user <linux-user>] [--pane <id>]
#                             [--claude-uuid <uuid>] [--owner <user>]
#
# Bot-squad's session discovery is tmux-based: scripts/hooks/session_start.sh
# fires when Claude Code (CLI) starts a session, computes the SID from the
# enclosing tmux pane, and writes data/<slug>/sessions/<sid>.md so the
# registry sees it. Sessions launched outside that path — Claude Desktop on
# a remote laptop SSHing into the repo, IDE Claude integrations, anything
# without a Claude process running in a tmux pane on this host — never get
# discovered, so they can't be addressed by peer_send and don't show up in
# to=teamlead / to=dev / to=all broadcasts.
#
# This shim does the same registry write by hand. SID format mirrors the
# tmux-based one (S-<user>-<window>-p<pane>) so existing parsers work
# without changes; pane defaults to "99999" as a sentinel meaning "not a
# real tmux pane id". The assigned SID is printed on stdout — save it,
# you'll use it for peer_send / peer_inbox_read / peer_inbox_wait.
#
# Re-running with the same <window>+<user>+<pane> is idempotent: updates
# fields in place and preserves started_at. To deregister, just delete the
# md file printed in the stderr hint.

set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
CFG="$BOT_SQUAD/config/projects.toml"

# --- arg parsing ---
window=""
task_id=""
initiative=""
linux_user=""
pane="99999"
claude_uuid=""
owner=""

while [ $# -gt 0 ]; do
  case "$1" in
    --task)         task_id="$2";     shift 2 ;;
    --initiative)   initiative="$2";  shift 2 ;;
    --user)         linux_user="$2";  shift 2 ;;
    --pane)         pane="$2";        shift 2 ;;
    --claude-uuid)  claude_uuid="$2"; shift 2 ;;
    --owner)        owner="$2";       shift 2 ;;
    --)             shift; break ;;
    -h|--help)
      sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*)
      echo "register-session: unknown flag: $1" >&2; exit 2 ;;
    *)
      if [ -z "$window" ]; then
        window="$1"; shift
      else
        echo "register-session: extra positional arg: $1" >&2; exit 2
      fi ;;
  esac
done

if [ -z "$window" ]; then
  cat >&2 <<USAGE
usage: register-session <window> [--task T-NNNN] [--initiative <name>]
                                  [--user <linux-user>] [--pane <id>]
                                  [--claude-uuid <uuid>] [--owner <user>]

Registers an externally-running Claude session (e.g. Claude Desktop over
SSH) into the bot-squad registry for the project at the current directory.

Prints the assigned SID on stdout.
USAGE
  exit 2
fi

[ -z "$linux_user" ] && linux_user="$(id -un)"

# Pane must match the tmux format (digits only) so existing SID parsers work.
case "$pane" in
  ''|*[!0-9]*) echo "register-session: --pane must be numeric (got '$pane')" >&2; exit 2 ;;
esac

# --- resolve slug from CWD (same logic as session_start.sh / deploy.sh) ---
slug=$(python3 - "$CFG" "$PWD" <<'PY'
import os, sys, tomllib
cfg_path, cwd = sys.argv[1], sys.argv[2]
real_cwd = os.path.realpath(cwd)
with open(cfg_path, "rb") as f:
    cfg = tomllib.load(f)
def _match(candidate, target):
    if not target:
        return False
    real_target = os.path.realpath(target)
    return (
        candidate == target
        or candidate.startswith(target.rstrip("/") + "/")
        or real_cwd == real_target
        or real_cwd.startswith(real_target.rstrip("/") + "/")
    )
for slug, p in cfg.get("projects", {}).items():
    for key in ("repo_path", "repo_master"):
        if _match(cwd, p.get(key, "")):
            print(slug); sys.exit(0)
PY
)

if [ -z "$slug" ]; then
  echo "register-session: CWD $PWD doesn't belong to any registered bot-squad project" >&2
  exit 2
fi

sid="S-${linux_user}-${window}-p${pane}"
data="$BOT_SQUAD/data/$slug"
mkdir -p "$data/sessions"
md_path="$data/sessions/${sid}.md"

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Preserve started_at + extras across re-registrations.
existing_started_at=""
existing_extra_task_ids="[]"
existing_extra_initiatives="[]"
if [ -f "$md_path" ]; then
  existing_started_at=$(grep -E '^started_at:' "$md_path" | head -1 | awk '{print $2}')
  v=$(grep -E '^extra_task_ids:' "$md_path" | head -1 | sed 's/^extra_task_ids: //')
  [ -n "$v" ] && existing_extra_task_ids="$v"
  v=$(grep -E '^extra_initiatives:' "$md_path" | head -1 | sed 's/^extra_initiatives: //')
  [ -n "$v" ] && existing_extra_initiatives="$v"
fi
[ -z "$existing_started_at" ] && existing_started_at="$now"

cat > "$md_path" <<MD
---
sid: $sid
status: active
window: $window
cwd: $PWD
claude_uuid: ${claude_uuid:-~}
task_id: ${task_id:-~}
initiative: ${initiative:-~}
extra_task_ids: $existing_extra_task_ids
extra_initiatives: $existing_extra_initiatives
started_at: $existing_started_at
owner: ${owner:-~}
---
MD

echo "$sid"
cat >&2 <<EOF
[register-session] registered $sid for project $slug
[register-session] md: $md_path

Peer bus is now available for this SID. Examples:

  # drain inbox (manual):
  curl -sS --unix-socket $BOT_SQUAD/data/_sock/worker.sock \\
    -X POST -H 'Content-Type: application/json' \\
    -d '{"slug":"$slug","sid":"$sid"}' \\
    http://w/actions/peer_inbox_read

  # long-poll for new mail (block up to 1800s):
  curl -sS --unix-socket $BOT_SQUAD/data/_sock/worker.sock \\
    -X POST -H 'Content-Type: application/json' \\
    -d '{"slug":"$slug","sid":"$sid","timeout":1800}' \\
    http://w/actions/peer_inbox_wait

  # send to a peer / role:
  curl -sS --unix-socket $BOT_SQUAD/data/_sock/worker.sock \\
    -X POST -H 'Content-Type: application/json' \\
    -d '{"slug":"$slug","from_sid":"$sid","to":"teamlead","text":"…"}' \\
    http://w/actions/peer_send

Re-run this command with the same <window>+--user+--pane to refresh the
registration (e.g. update --task or --claude-uuid). Delete $md_path to
deregister.
EOF
