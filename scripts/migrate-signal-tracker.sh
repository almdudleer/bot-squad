#!/usr/bin/env bash
# migrate-signal-tracker.sh — one-shot orchestrator.
#
# Migrates /home/almdudleer/signal_tracker_mgmt from the old multi-role flow
# into bot-squad as the first registered project. NEVER COMMITS — leaves
# everything staged for Alexey to review and commit.
#
# Pre-flight requires a clean working tree. Run with --dry-run first to
# review what would change.
set -euo pipefail

REPO=${REPO:-/home/almdudleer/signal_tracker_mgmt}
OLD_CHECKOUT=${OLD_CHECKOUT:-/home/almdudleer/signal_tracker}
BOT_SQUAD=${BOT_SQUAD:-/home/www/bot-squad}
SLUG=signal-tracker
DATA="$BOT_SQUAD/data/$SLUG"
ARCHIVE="$BOT_SQUAD/archived/${SLUG}-old"

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

run() { if [ "$DRY_RUN" -eq 1 ]; then echo "DRY: $*"; else eval "$*"; fi; }

# 1. Pre-flight — clean tree.
cd "$REPO"
if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty in $REPO. Commit or stash first." >&2
    exit 1
fi

# 2. bot-squad project init (idempotent).
run "$BOT_SQUAD/scripts/cli/bot-squad project init $SLUG --repo $REPO"

# 3. Decompose BACKLOG.md.
run "$BOT_SQUAD/scripts/cli/migrate_backlog.py $REPO/BACKLOG.md --out $DATA"

# 4. Seed vision layers.
run "$BOT_SQUAD/scripts/cli/seed_vision.py \
    --constitution $REPO/CONSTITUTION.md \
    --agents $REPO/AGENTS.md \
    --backlog $REPO/BACKLOG.md \
    --feature-v06 $REPO/ops/feature_v06.md \
    --out $DATA"

# 5. Archive old multi-role flow files.
run "mkdir -p $ARCHIVE"
ARCHIVED_FILES=(
    AGENTS.md
    BACKLOG.md
    CONSTITUTION.md
    MR.md
    crontab.txt
    crontab-timpo.txt
    ops/dev.md
    ops/pm.md
    ops/tl.md
    ops/retro.md
    ops/MONITOR.md
    ops/feature_v06.md
    ops/sessions_schedule.md
    ops/dispatch.sh
    ops/run_session.sh
    ops/refresh_oauth.py
    ops/update_schedule.py
)
for f in "${ARCHIVED_FILES[@]}"; do
    if [ -f "$REPO/$f" ]; then
        run "cp -a $REPO/$f $ARCHIVE/$(echo $f | tr / __)"
        run "git -C $REPO rm $f"
    fi
done

# 6. Write the new slim AGENTS.md (template + vision injection).
# NOTE: Not using run() for this block — heredoc with backtick content and
# variable interpolation is done directly with an explicit dry-run guard.
NEW_AGENTS="$REPO/AGENTS.md"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY: would write $NEW_AGENTS"
else
    NORTH_STAR_HEAD=$(head -50 "$DATA/vision/north-star.md")
    STRATEGY_HEAD=$(head -30 "$DATA/vision/strategy.md")
    TACTICAL_HEAD=$(head -30 "$DATA/vision/tactical.md")

    cat > "$NEW_AGENTS" <<NEW_AGENTS_EOF
# Signal Tracker — agent quick reference

Always-cached, every-turn. If you find yourself re-reading larger context
files repeatedly, the right answer probably belongs here.

## North star — do not lose sight of

$NORTH_STAR_HEAD

(Full at \`ops/bot-squad/vision/north-star.md\`.)

## How we decide what to build

1. Identify the user problem first (Jobs-to-be-Done framing).
2. Find evidence in user feedback (\`ops/bot-squad/feedback/\`).
3. Filter user-suggested SOLUTIONS — implement the underlying problem.
4. Score against the north-star: does this raise user catch-rate?
5. If unclear, raise to stakeholders rather than guessing.

## Current strategy

$STRATEGY_HEAD

(Full at \`ops/bot-squad/vision/strategy.md\`.)

## Current tactical priorities

$TACTICAL_HEAD

(Full at \`ops/bot-squad/vision/tactical.md\`.)

## Hard rules

- Never push, never merge, never amend. Those are user actions.
- Never edit \`ops/bot-squad/vision/constitution.md\` — agent-immutable.
- Never run destructive commands (\`rm -rf\`, \`git reset --hard\`,
  \`git push --force\`, dropping DB tables) without explicit user ask.
- Don't read or copy from \`/home/www/bot-squad/archived/signal-tracker-old/\`
  — that's the deprecated multi-role flow. Patterns there do not apply.

## Stack & paths

FastAPI + asyncpg, React 19 + Vite + TS + Tailwind, PostgreSQL, Docker + traefik.
Repo: this directory. Vision/backlog/feedback: \`ops/bot-squad/\`
(gitignored symlink to /home/www/bot-squad/data/signal-tracker/).

## Branching & commits

- Working branch: \`agent_team/dev\` (created in spec #3; until then, master).
- Commit prefix: \`[backend]\`, \`[web]\`, \`[ops]\`, \`[docs]\`.
- Imperative summary, ≤70 chars. Co-Authored-By trailer is auto-added.
- One commit per discrete change.

## Deploy (when wired up by spec #3)

\`ops/bot-squad/deploy <target> "<reason>"\`
Targets: \`staging\`, \`dev\`. Worker queues, gates on clean tree, builds, TG-pings.
You don't manage the loop. Commit and walk away (or call \`deploy\` if your
work needs a deploy NOW).

## Test commands

- Backend: \`docker exec signal-tracker python test_api.py\`
- Frontend: \`npm run test:e2e\` (from \`web/\`)
- Type-check: \`npm run typecheck\`
- Lint: \`npm run lint\`

## When you need more

- \`ops/bot-squad/vision/strategy.md\` — full current bets + rationale
- \`ops/bot-squad/vision/tactical.md\` — full current cycle priorities
- \`ops/bot-squad/vision/initiatives/\` — discrete strategic bets
- \`ops/bot-squad/backlog/\` — all open work
- \`ops/bot-squad/feedback/\` — raw user feedback for evidence
- \`ops/bot-squad/AGENT_INSTRUCTIONS.md\` — recipes, gotchas, longer reference

## Telegram

TG bot token at \`8036906248:...\`. Ping rarely (hard blockers, prod errors,
finished long-running work) via worker action \`tg_notify\` (spec #3).

NEW_AGENTS_EOF
fi

# 7. Write MIGRATION_LOG.md.
# NOTE: Not using run() — same heredoc quoting reasons as above.
if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY: would write $ARCHIVE/MIGRATION_LOG.md"
else
    cat > "$ARCHIVE/MIGRATION_LOG.md" <<MIGRATION_LOG_EOF
# signal-tracker → bot-squad migration

Migrated $(date -u '+%Y-%m-%dT%H:%M:%SZ') from $REPO into $BOT_SQUAD.

## Files archived (verbatim copy in this dir)

$(printf '  - %s\n' "${ARCHIVED_FILES[@]}")

## Files generated under $DATA

  - vision/constitution.md (verbatim from $REPO/CONSTITUTION.md)
  - vision/north-star.md (seeded; hand-edit)
  - vision/strategy.md (seeded; hand-edit)
  - vision/tactical.md (seeded; hand-edit)
  - vision/initiatives/v0.6-topic-monitoring.md
  - backlog/T-*.md (decomposed from BACKLOG.md)
  - feedback/F-*.md (extracted from BACKLOG.md User Feedback)

## Reversal

Reversal isn't automated; restore individual files with:
  cp -a archived/signal-tracker-old/<flat_name> /home/almdudleer/signal_tracker_mgmt/<orig_path>
MIGRATION_LOG_EOF
fi

# 8. Stage in repo (NOT commit).
run "git -C $REPO add AGENTS.md .claude/settings.json .gitignore"

# 9. Drop second checkout (with confirmation).
if [ -d "$OLD_CHECKOUT" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY: would rm -rf $OLD_CHECKOUT"
    else
        echo
        echo "About to remove second checkout: $OLD_CHECKOUT"
        read -r -p "Type YES to confirm: " ans
        if [ "$ans" = "YES" ]; then
            rm -rf "$OLD_CHECKOUT"
            echo "removed."
        else
            echo "skipped (left $OLD_CHECKOUT in place)."
        fi
    fi
fi

# 10. Set CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 in user settings.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    if ! grep -q '"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"' "$SETTINGS"; then
        run "python3 -c 'import json,pathlib,os
p=pathlib.Path(os.environ[\"S\"])
d=json.loads(p.read_text())
d.setdefault(\"env\",{})[\"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS\"]=\"1\"
p.write_text(json.dumps(d, indent=2)+\"\n\")' S=$SETTINGS"
    fi
fi

cat <<EOF

Migration ${DRY_RUN:+(dry-run)} complete.

Next steps:
  1. cd $REPO
  2. git status                              # review staged changes
  3. git diff --staged                       # eyeball the new AGENTS.md
  4. cat ops/bot-squad/MIGRATION_REPORT.md   # vision seed report
  5. cat ops/bot-squad/vision/*.md           # eyeball vision layers
  6. (Hand-edit vision layers and AGENTS.md if needed.)
  7. git add -A && git commit -m "ops: migrate to bot-squad framework"
EOF
