#!/usr/bin/env bash
#
# pause-deploys.sh — write a PAUSED.json marker so deploy_monitor defers
# every queued + new deploy for the project at CWD until resume-deploys.
#
# Usage (via symlink at ops/bot-squad-bin/pause-deploys):
#   pause-deploys "<reason>"
#
# Slug is resolved from the current directory the same way deploy.sh does
# (projects.toml::repo_path / repo_master).
#
# One TG ping fires at pause time (and one at resume time) so the operator
# knows the queue state flipped. No per-tick reminder spam.

set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
WORKER_SOCK="${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}"

reason="${1:-}"
if [ -z "$reason" ]; then
    echo "usage: pause-deploys \"<reason>\"" >&2
    exit 2
fi

# Resolve slug from CWD (same logic as deploy.sh / session_start.sh)
slug=$(python3 - "$BOT_SQUAD/config/projects.toml" "$PWD" <<'PY'
import os, sys, tomllib
cfg_path, cwd = sys.argv[1], sys.argv[2]
real_cwd = os.path.realpath(cwd)
with open(cfg_path, "rb") as f:
    cfg = tomllib.load(f)
def _match(candidate, target):
    if not target: return False
    real_target = os.path.realpath(target)
    return (candidate == target
            or candidate.startswith(target.rstrip("/") + "/")
            or real_cwd == real_target
            or real_cwd.startswith(real_target.rstrip("/") + "/"))
for slug, p in cfg.get("projects", {}).items():
    for key in ("repo_path", "repo_master"):
        if _match(cwd, p.get(key, "")):
            print(slug); sys.exit(0)
PY
)

if [ -z "$slug" ]; then
    echo "pause-deploys: CWD $PWD not in any registered project" >&2
    exit 2
fi

user="${USER:-$(id -un)}"

curl -sS --unix-socket "$WORKER_SOCK" \
    -X POST -H 'Content-Type: application/json' \
    -d "$(python3 -c "import json,sys; print(json.dumps({'slug':sys.argv[1],'reason':sys.argv[2],'requested_by':sys.argv[3]}))" "$slug" "$reason" "$user")" \
    http://w/actions/pause_deploys
echo
