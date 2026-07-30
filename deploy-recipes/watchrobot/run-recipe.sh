#!/usr/bin/env bash
# Run a watchrobot deploy recipe BY HAND and report its exit code honestly.
# (T-0427 defect 3.)
#
# WHAT THIS EXISTS TO PREVENT, in the exact words of the run that caused it:
#
#     [staging] FATAL: /api/version never answered within ~90s of recreate
#     === DEPLOY_RC=0 ===
#
# A deploy that had just failed printed a clean zero. The `$?` had been captured
# from a trailing `grep` in the invocation
#
#     bash <recipe> 2>&1 | grep -vE '^#[0-9]+ ' ; echo "=== DEPLOY_RC=$? ==="
#
# so it reported the exit status of `grep`, not of the recipe. That same
# afternoon the same shape of hand-rolled pipeline had also (a) produced a
# 0-byte log for a 35-minute run, because `tail` buffers until EOF, and (b)
# filtered away the traceback the run existed to produce. Three incidents in one
# day from one habit — a pattern, not three accidents.
#
# So: do not hand-roll the pipeline. Use this. Four things it does that the
# one-liner cannot:
#
#   1. The exit code comes from ${PIPESTATUS[0]} — the recipe — and never from
#      the tail of a pipeline.
#   2. The string `DEPLOY_RC=0` is only reachable on a branch that has already
#      tested rc -eq 0. It cannot be printed for a failed run even if the rc
#      capture itself were wrong.
#   3. An INDEPENDENT cross-check: if the log contains a FATAL line while rc is
#      0, that disagreement is itself reported as a failure rather than
#      resolved in favour of the comfortable answer. The false green above would
#      have been caught by this check alone.
#   4. Output is tee'd UNFILTERED to a log file. Nothing is grepped away, so the
#      evidence survives the run.
#
# Usage:
#   run-recipe.sh staging                # resolves + runs the staging recipe
#   run-recipe.sh prod                   # refuses without WR_PROD_CONFIRM=yes
#   run-recipe.sh /path/to/some-recipe.sh   # runs an explicit file in $PWD
#   run-recipe.sh staging --dry-run      # resolve + print, deploy NOTHING
#
#   WR_RUN_LOG=/path/to/log  run-recipe.sh staging     # default: a mktemp log
#
# Proven by run-recipe-selftest.sh, which feeds it recipes that fail on purpose
# — including one that prints FATAL and then exits 0. Run that after any edit
# here; an honest-reporting wrapper that has never been made to report a
# failure is just an untested claim.
#
# NOT set -e: this script must survive its own child's failure in order to
# report it.
set -uo pipefail

BOT_SQUAD_ROOT="${BOT_SQUAD_ROOT:-/home/www/bot-squad}"
SLUG=watchrobot

# cwd per target, mirroring config/projects.toml (the SSOT — if these ever
# disagree with it, projects.toml wins and this is the bug). The worker sets cwd
# via Project.repo_for_target(); a manual run in the wrong clone builds a
# different tree than the deploy would, which is worth more than a comment:
# the recipes themselves re-check the branch and refuse.
STAGING_CWD="${STAGING_CWD:-/home/almdudleer/watchrobot/deploy}"
PROD_CWD="${PROD_CWD:-/home/almdudleer/watchrobot/master}"

die() { echo "run-recipe: $*" >&2; exit 64; }

TARGET="${1:-}"
[ -n "$TARGET" ] || die "usage: run-recipe.sh <staging|prod|/path/to/recipe.sh> [--dry-run]"
DRY_RUN=0
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1

if [ -f "$TARGET" ]; then
    RECIPE="$(cd "$(dirname "$TARGET")" && pwd)/$(basename "$TARGET")"
    CWD="$PWD"
    LABEL="$(basename "$TARGET")"
else
    case "$TARGET" in
        staging) CWD="$STAGING_CWD" ;;
        prod)
            CWD="$PROD_CWD"
            # Prod is hotfix-only under the deploy freeze (AGENT_INSTRUCTIONS
            # "PROD IS CURRENTLY HOTFIX-ONLY"). A convenience name that deploys
            # prod on one word is not a convenience. --dry-run is exempt: it
            # resolves and prints, and cannot deploy.
            [ "$DRY_RUN" -eq 1 ] || [ "${WR_PROD_CONFIRM:-}" = "yes" ] || die \
                "prod is hotfix-only under the current freeze; re-run with WR_PROD_CONFIRM=yes if that is what you mean"
            ;;
        *) die "unknown target '$TARGET' (expected staging, prod, or a path to a recipe file)" ;;
    esac
    # Resolve exactly as bot_squad_worker.deploy._recipe_path does: the
    # version-controlled copy wins, the legacy data/ copy is the fallback. If
    # this ever diverges from that function, THIS is the bug — a wrapper that
    # runs a different file than the deploy does is worse than no wrapper.
    TRACKED="$BOT_SQUAD_ROOT/deploy-recipes/$SLUG/$TARGET.sh"
    LEGACY="$BOT_SQUAD_ROOT/data/$SLUG/deploy/$TARGET.sh"
    if [ -f "$TRACKED" ]; then
        RECIPE="$TRACKED"
    elif [ -f "$LEGACY" ]; then
        RECIPE="$LEGACY"
        echo "run-recipe: WARNING — falling back to the legacy data/ copy; the tracked recipe is missing." >&2
    else
        die "no recipe for '$TARGET' at $TRACKED or $LEGACY"
    fi
    LABEL="$TARGET"
fi

[ -d "$CWD" ] || die "cwd '$CWD' does not exist"

if [ "$DRY_RUN" -eq 1 ]; then
    LOG="(none — dry run)"
else
    LOG="${WR_RUN_LOG:-$(mktemp -t "wr-deploy-$LABEL-XXXXXX.log")}"
fi

# State WHICH file is about to run, with its hash. Four times in one morning
# this project verified the file it had edited instead of the file that ran; the
# hash is what makes "was my change in this run?" answerable afterwards.
echo "=== run-recipe: $LABEL ==="
echo "    recipe : $RECIPE"
echo "    sha256 : $(sha256sum "$RECIPE" | cut -d' ' -f1)"
echo "    cwd    : $CWD ($(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'not a git repo') @ $(git -C "$CWD" rev-parse --short HEAD 2>/dev/null || echo '?'))"
echo "    log    : $LOG"
echo "    started: $(date -u +%Y-%m-%dT%H:%M:%SZ)  load $(cut -d' ' -f1-3 /proc/loadavg)"

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    echo "=== DRY RUN — nothing was deployed. The recipe above is what would run. ==="
    exit 0
fi
echo

START_S=$SECONDS

# The whole point. `tee` cannot fail in a way that matters and `${PIPESTATUS[0]}`
# is the recipe's own status. NOTHING is filtered out of this pipeline — a
# `grep -v` here is what destroyed the evidence on the run that motivated this
# script.
( cd "$CWD" && bash "$RECIPE" ) 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}

ELAPSED=$((SECONDS - START_S))

# Independent cross-check on the rc, deliberately not derived from it. This is
# the check that would have caught the original false green on its own.
fatal_lines=$(grep -c 'FATAL' "$LOG" 2>/dev/null || true)
: "${fatal_lines:=0}"

echo
echo "    finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)  wall clock ${ELAPSED}s  load $(cut -d' ' -f1-3 /proc/loadavg)"

if [ "$rc" -ne 0 ]; then
    echo "=== DEPLOY_RC=$rc — DEPLOY FAILED ($LABEL) ==="
    echo "    full log: $LOG"
    exit "$rc"
fi

if [ "$fatal_lines" -gt 0 ]; then
    # Deliberately does NOT print the token `DEPLOY_RC=0`, even though the
    # recipe's own status was 0. `DEPLOY_RC=<n>` is this wrapper's VERDICT, not
    # a passthrough of `$?`, and a reader who greps for `DEPLOY_RC=0` — or just
    # skims — must not find it anywhere in a failing run. The selftest asserts
    # exactly that, and caught this line saying it.
    echo "=== DEPLOY_RC=1 — DEPLOY FAILED ($LABEL) ===" >&2
    echo "    The recipe's own exit status was $rc, but its log contains $fatal_lines FATAL line(s):" >&2
    grep -n 'FATAL' "$LOG" | sed 's/^/      /' >&2
    echo "    A zero exit alongside a FATAL is the exact false green this wrapper exists to catch," >&2
    echo "    so the disagreement is resolved AGAINST the comfortable answer." >&2
    echo "    Either the recipe swallowed a failure or it logs FATAL for something non-fatal; both are bugs." >&2
    echo "    full log: $LOG" >&2
    exit 1
fi

echo "=== DEPLOY_RC=0 — deploy OK ($LABEL, ${ELAPSED}s) ==="
echo "    full log: $LOG"
