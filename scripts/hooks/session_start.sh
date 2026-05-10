#!/usr/bin/env bash
# session_start.sh — bot-squad SessionStart hook.
#
# Resolves the project from CWD against config/projects.toml, then prints
# layered vision + open task headlines + active sessions to stdout. Claude
# Code appends this output to the session's context.
#
# Referenced from each project's committed .claude/settings.json. Single
# source of truth for ALL projects.
set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
CFG="$BOT_SQUAD/config/projects.toml"

# Resolve current project by matching CWD against repo_path entries.
slug=$(python3 - "$CFG" "$PWD" <<'PY'
import sys, tomllib
cfg_path, cwd = sys.argv[1], sys.argv[2]
with open(cfg_path, "rb") as f:
    cfg = tomllib.load(f)
for slug, p in cfg.get("projects", {}).items():
    repo = p.get("repo_path", "")
    if cwd == repo or cwd.startswith(repo.rstrip("/") + "/"):
        print(slug)
        break
PY
)

if [ -z "$slug" ]; then
    # Not a registered bot-squad project — silent exit.
    exit 0
fi

DATA="$BOT_SQUAD/data/$slug"

print_section() {
    echo
    echo "=== $1 ==="
}

# 1. AGENT_INSTRUCTIONS.md
if [ -f "$DATA/AGENT_INSTRUCTIONS.md" ]; then
    print_section "AGENT_INSTRUCTIONS"
    cat "$DATA/AGENT_INSTRUCTIONS.md"
fi

# 2. Vision layers
if [ -f "$DATA/vision/north-star.md" ]; then
    print_section "VISION: NORTH STAR"
    awk '/^# /{p=1} p; /^$/ && p>=1 && NR>1 {p++} p>=3 {exit}' "$DATA/vision/north-star.md"
fi
for layer in strategy tactical; do
    f="$DATA/vision/$layer.md"
    if [ -f "$f" ]; then
        print_section "VISION: $(echo $layer | tr a-z A-Z)"
        cat "$f"
    fi
done

# 3. Open task headlines (max 30 lines)
if [ -d "$DATA/backlog" ]; then
    print_section "OPEN BACKLOG"
    python3 - "$DATA/backlog" <<'PY'
import os, re, sys
try:
    import yaml
except ImportError:
    print("(open backlog scan unavailable: pyyaml not installed)")
    sys.exit(0)
backlog = sys.argv[1]
shown = 0
for fn in sorted(os.listdir(backlog)):
    if not fn.endswith(".md"): continue
    p = os.path.join(backlog, fn)
    with open(p) as f: t = f.read()
    m = re.match(r"\A---\n(.*?)\n---\n", t, re.DOTALL)
    if not m: continue
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError: continue
    if meta.get("status") not in ("open", "reopened"): continue
    print(f"- [{meta.get('id','?')}] {meta.get('title','?')}")
    shown += 1
    if shown >= 30:
        print("  ... (truncated; see ops/bot-squad/backlog/ for the rest)")
        break
PY
fi

# 4. Active sessions
if [ -d "$DATA/sessions" ]; then
    active=$(grep -l 'status: active' "$DATA/sessions"/*.md 2>/dev/null | wc -l)
    if [ "$active" -gt 0 ]; then
        print_section "ACTIVE SESSIONS"
        for f in "$DATA/sessions"/*.md; do
            grep -q 'status: active' "$f" 2>/dev/null && echo "- $(basename "$f" .md)"
        done
    fi
fi

exit 0
