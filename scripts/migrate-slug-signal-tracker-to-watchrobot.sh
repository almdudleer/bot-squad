#!/usr/bin/env bash
# migrate-slug-signal-tracker-to-watchrobot.sh — T-0189
#
# Renames the bot-squad project SLUG `signal-tracker` → `watchrobot` on the
# LIVE installation's data store. This handles only the on-disk DATA side;
# the config/code/doc changes (config/projects.toml slug, the /p redirect in
# App.tsx, AGENT_INSTRUCTIONS/roles) ship through the normal deploy.
#
# WHAT IT DOES (each step is idempotent — safe to re-run):
#   1. Absorb the pre-existing empty `data/watchrobot/` stub (2 known
#      zero-byte peer-bus files from a long-dead session) so the move target
#      is clear. Aborts if the stub holds ANY non-empty file (means a real
#      session wrote there — needs human eyes).
#   2. Rename the live tree `data/signal-tracker/` → `data/watchrobot/`
#      (single atomic mv — carries sessions, _chat, _counters, backlog,
#      teams, deploy, vision … intact; SIDs are slug-independent so nothing
#      inside needs rewriting).
#   3. Move + rewrite the slug-keyed state file that lives OUTSIDE the tree:
#      `data/_worker/autonomous/signal-tracker.json` → `watchrobot.json`
#      (also rewrites its embedded `"slug"` field).
#   4. Remove the orphaned `teams/signal-tracker.md` (the team reconciler
#      rebuilds `teams/watchrobot.md` keyed on the new slug on its next tick).
#
# WHAT IT DOES NOT DO:
#   - It does NOT touch git-tracked code/config (projects.toml, App.tsx, docs)
#     — those arrive via deploy.
#   - It does NOT restart the worker. Run AFTER the new projects.toml (slug =
#     watchrobot) is deployed, then `systemctl --user restart
#     bot-squad-worker.service` so the worker reloads the registry. The TL
#     owns that controlled window (active watchrobot sessions are quiesced).
#
# COORDINATED EXECUTION ORDER (TL window):
#   a. Push + deploy the config/code/doc commit (slug=watchrobot, redirect).
#   b. Run THIS script on the install (--dry-run first, then for real).
#   c. systemctl --user restart bot-squad-worker.service
#   d. Walk scenario Part B (data + TG).
#
# Usage:
#   migrate-slug-signal-tracker-to-watchrobot.sh [--dry-run]
#
# Env overrides (for testing on a copy):
#   BOT_SQUAD_DATA   default /home/www/bot-squad/data
set -euo pipefail

DATA="${BOT_SQUAD_DATA:-/home/www/bot-squad/data}"
OLD_SLUG=signal-tracker
NEW_SLUG=watchrobot

OLD_DIR="$DATA/$OLD_SLUG"
NEW_DIR="$DATA/$NEW_SLUG"
AUTON_DIR="$DATA/_worker/autonomous"
OLD_AUTON="$AUTON_DIR/$OLD_SLUG.json"
NEW_AUTON="$AUTON_DIR/$NEW_SLUG.json"

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

say()  { echo "[migrate-$OLD_SLUG→$NEW_SLUG] $*"; }
run()  { if [ "$DRY_RUN" -eq 1 ]; then echo "DRY: $*"; else eval "$*"; fi; }

# ---------------------------------------------------------------------------
# Step 0 — figure out where we are. Three legal states:
#   A. fresh:     OLD_DIR live, NEW_DIR absent-or-empty-stub  → do the move
#   B. done:      OLD_DIR absent, NEW_DIR populated           → verify-only
#   C. illegal:   anything else                               → abort loudly
# ---------------------------------------------------------------------------
old_exists=0; [ -d "$OLD_DIR" ] && old_exists=1
new_exists=0; [ -d "$NEW_DIR" ] && new_exists=1

# Is NEW_DIR the known empty stub? (only zero-byte files, nothing else.)
new_is_stub=0
if [ "$new_exists" -eq 1 ]; then
    nonempty="$(find "$NEW_DIR" -type f ! -size 0 -print -quit 2>/dev/null || true)"
    if [ -z "$nonempty" ]; then
        new_is_stub=1
    fi
fi
# Is NEW_DIR populated (a real, already-migrated tree)?
new_populated=0
if [ "$new_exists" -eq 1 ] && [ "$new_is_stub" -eq 0 ]; then
    new_populated=1
fi

if [ "$old_exists" -eq 0 ] && [ "$new_populated" -eq 1 ]; then
    say "already migrated (no $OLD_DIR, $NEW_DIR populated) — verifying tail steps only."
elif [ "$old_exists" -eq 1 ] && { [ "$new_exists" -eq 0 ] || [ "$new_is_stub" -eq 1 ]; }; then
    say "fresh migration: $OLD_DIR is live, target is ${new_exists:+stub/}absent."
else
    echo "ABORT: ambiguous state — OLD_DIR exists=$old_exists, NEW_DIR exists=$new_exists, stub=$new_is_stub, populated=$new_populated." >&2
    echo "       Refusing to guess. Inspect $OLD_DIR and $NEW_DIR by hand." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 1 — absorb the empty stub, then Step 2 — move the tree.
# ---------------------------------------------------------------------------
if [ "$old_exists" -eq 1 ]; then
    if [ "$new_is_stub" -eq 1 ]; then
        say "absorbing empty stub at $NEW_DIR (zero-byte peer-bus leftovers)."
        # Belt-and-suspenders: only ever rm zero-byte files; the state guard
        # above already proved there are no non-empty files under NEW_DIR.
        while IFS= read -r f; do
            [ -n "$f" ] && run "rm -f -- '$f'"
        done < <(find "$NEW_DIR" -type f -size 0 2>/dev/null)
        # Remove now-empty dirs (deepest first) so the rename target is clear.
        while IFS= read -r d; do
            [ -n "$d" ] && run "rmdir -- '$d' 2>/dev/null || true"
        done < <(find "$NEW_DIR" -depth -type d 2>/dev/null)
    fi
    say "renaming tree $OLD_DIR → $NEW_DIR"
    run "mv -- '$OLD_DIR' '$NEW_DIR'"
else
    say "tree already at $NEW_DIR — skipping move."
fi

# ---------------------------------------------------------------------------
# Step 3 — slug-keyed autonomous state file (lives outside the tree).
# ---------------------------------------------------------------------------
if [ -f "$OLD_AUTON" ]; then
    say "moving autonomous state $OLD_AUTON → $NEW_AUTON (+ rewriting embedded slug)"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY: rewrite slug in $OLD_AUTON and mv → $NEW_AUTON"
    else
        python3 - "$OLD_AUTON" "$NEW_AUTON" "$NEW_SLUG" <<'PY'
import json, os, sys
src, dst, new_slug = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src) as fh:
    data = json.load(fh)
data["slug"] = new_slug
tmp = dst + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(tmp, dst)
os.remove(src)
PY
    fi
elif [ -f "$NEW_AUTON" ]; then
    say "autonomous state already at $NEW_AUTON — skipping."
else
    say "no autonomous state file for either slug — skipping."
fi

# ---------------------------------------------------------------------------
# Step 4 — drop the orphaned old-slug team md (reconciler rebuilds the new one).
# ---------------------------------------------------------------------------
ORPHAN_TEAM="$NEW_DIR/teams/$OLD_SLUG.md"
if [ -f "$ORPHAN_TEAM" ]; then
    say "removing orphaned team file $ORPHAN_TEAM (reconciler rebuilds teams/$NEW_SLUG.md)"
    run "rm -f -- '$ORPHAN_TEAM'"
else
    say "no orphaned team file — skipping."
fi

if [ "$DRY_RUN" -eq 1 ]; then
    say "done. (dry-run — no changes written)"
else
    say "done."
    echo
    say "NEXT (TL): systemctl --user restart bot-squad-worker.service, then walk scenario Part B."
fi
