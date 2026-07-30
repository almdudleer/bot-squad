#!/usr/bin/env bash
# watchrobot → staging
#
# Builds + restarts the staging container from the deploy clone's tree, which is
# force-synced to origin/bot_squad/dev by the worker. Staging only ever tracks
# bot_squad/dev, so there is NO separate `staging` branch and NO merge.
#
# T-0202 fix (2026-06-08): this recipe used to do
#     git checkout staging && git merge bot_squad/dev --no-ff
# IN cwd. That was wrong on two counts:
#   1. It SWITCHED the clone's branch. The dev clone is a fixed-branch editing
#      surface for the agent team — it must stay on bot_squad/dev and never be
#      checked out to another branch by the deploy queue. (Operator
#      requirement, 2026-06-08.)
#   2. The local `staging` branch had diverged from bot_squad/dev by 686
#      unpushed commits, so the merge conflicted on every deploy (rc=2) and
#      wedged the whole staging queue, while silently dragging that
#      staging-only divergence into staging builds.
# Since staging just mirrors bot_squad/dev, we drop the branch dance entirely
# and build cwd in place.
#
# STRUCTURE — and it is load-bearing, see the note on gates below. This recipe
# is two phases, in this order, and nothing may be interleaved:
#   PHASE 1  ACTIONS — everything that changes the target. Build and start the
#            app, build and start the sidecar.
#   PHASE 2  GATES — everything that VERIFIES. Every gate runs, unconditionally,
#            and their results are collected; the recipe exits non-zero at the
#            END if any of them failed.
# The reason is a defect that appeared the one time these were interleaved
# (T-0427 defect 2): the `stt` sidecar block was placed AFTER the app's smoke
# gate, the smoke gate false-failed under load and exited 22, and the sidecar
# was therefore never started at all — the app came up healthy and the Telegram
# voice path was silently absent, which is the exact failure the sidecar block
# was written to prevent, reintroduced one layer up purely by where it sat.
# Reordering the two would only move the same trap (an `stt` failure would then
# strand the app's gate and we would not learn whether the app was healthy). A
# gate that can be SKIPPED by another gate's failure is not a gate, so no gate
# is allowed to exit early: they all run and there is one verdict.
#
# 2026-07-30 extends that rule one layer down, because the same trap turned out
# to exist in PHASE 1. The `stt` block died at rc=1 under `set -e` (the compose
# name conflict, see $STT_PROJECT below) and killed the recipe before a single
# gate ran — so a deploy that had actually shipped the app fine reported nothing
# whatsoever about the app, and the log had to be read by hand to find that out.
# A SIDECAR's actions must not be able to strand the app's verdict either. So
# phase-1 sidecar actions are captured into an rc and judged in phase 2 with
# everything else. They still fail the deploy; they just no longer silence it.
#
# Exit codes:
#   3   cwd is not the expected branch (nothing was touched)
#   20  the readiness gate library is missing/not executable
#   22  the app never became ready INSIDE its container
#   23  the `stt` sidecar never became healthy
#   24  the app was ready internally but never answered on its public URL
#       (i.e. traefik/LE, not the app)
#   25  the `stt` container is not owned by this deploy's compose project — see
#       the ownership block below; a foreign container answering /health is the
#       failure this code exists to name
#   26  the `stt` sidecar's PHASE-1 actions (reclaim/build/up) failed
set -euo pipefail

DEPLOY_START_S=$SECONDS
_loadavg() { cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo "load-unknown"; }

REPO="$(pwd)"
DEPLOY_BRANCH="bot_squad/dev"

APP_CONTAINER="signal-tracker-staging"
APP_INTERNAL_URL="http://localhost:8000/api/version"
APP_PUBLIC_URL="https://signal-staging.dev.uzinvestapi.com/api/version"
STT_CONTAINER="stt"
STT_INTERNAL_URL="http://localhost:8003/health"

# THE COMPOSE PROJECT THAT OWNS THE SHARED `stt` SINGLETON, and it is pinned on
# purpose (T-0427). Compose derives its project name from the cwd basename, and
# this box holds three clones of this repo — dev, deploy, master — so the same
# docker-compose.yml is three different projects. `stt` is ONE shared container
# with a fixed `container_name`, so whichever project created it owns it and the
# other two cannot manage it: they try to create their own and the daemon
# refuses with `Conflict. The container name "/stt" is already in use`. That is
# what killed the 2026-07-30T20:07Z staging deploy at rc=1, with the live stt
# labelled `com.docker.compose.project=dev`.
#
# Pinning one project for this one service is the same fix docker-compose.yml
# already applies to the shared VOLUME (T-0082: pin the name so the cwd-derived
# prefix cannot split one shared resource into three per-clone ones). With it,
# `up -d` is idempotent from ANY clone: no-op when running, create when absent,
# recreate when the image changed, never a conflict.
#
# Do not change this string without changing prod.sh's to match — they must
# agree or the two deploys start fighting over the name again.
STT_PROJECT="watchrobot-stt"

# Windows are generous ON PURPOSE and that is only safe because the gate has a
# real negative signal (see lib/wait-ready.sh): a dead container FATALs within
# one poll, so the timeout no longer has to be short enough to detect failure —
# it only has to bound a hang. Overridable for a manual run.
#
# The outer bound is the deploy worker's own watchdog: BOT_SQUAD_DEPLOY_TIMEOUT
# (default 1800s) hard-kills the run, and BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS
# (default 600s) kills a run whose log has gone quiet. The gate prints a
# progress line every ~30s specifically so a long wait cannot trip the second
# one. Keep the sum of these windows under the first, with build time on top.
APP_READY_TIMEOUT_S="${APP_READY_TIMEOUT_S:-600}"
ROUTE_TIMEOUT_S="${ROUTE_TIMEOUT_S:-180}"
STT_READY_TIMEOUT_S="${STT_READY_TIMEOUT_S:-300}"

# WHICH FILE AM I. This project has repeatedly verified the file it edited
# instead of the file that ran — a recipe change sitting in a management clone
# while the executor read a different copy, and a hand-edited `data/` copy
# losing to the tracked one. So the recipe states its own path and content hash
# on every run: `grep 'recipe:' <run-log>` answers "which artifact executed"
# without inference. (T-0378, T-0427)
echo "[staging] recipe: ${BASH_SOURCE[0]} sha256=$(sha256sum "${BASH_SOURCE[0]}" | cut -c1-16)"
echo "[staging] deploy start $(date -u +%Y-%m-%dT%H:%M:%SZ); load $(_loadavg)"

# Hard guard: never switch the clone's branch. If cwd is somehow not on
# bot_squad/dev, refuse rather than risk building/leaving the wrong branch.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "$DEPLOY_BRANCH" ]; then
    echo "[staging] FATAL: expected the clone on '$DEPLOY_BRANCH', got '$BRANCH'." >&2
    echo "[staging] This recipe never switches branches (T-0202); aborting so the clone is left untouched." >&2
    exit 3
fi
echo "[staging] building in place: $REPO @ $(git rev-parse --short HEAD) ($BRANCH)"

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
GATE="$LIB/wait-ready.sh"
OWNER="$LIB/shared-container.sh"
for _f in "$GATE" "$OWNER"; do
    if [ ! -x "$_f" ]; then
        echo "[staging] FATAL: gate library $_f is missing or not executable." >&2
        echo "[staging] It ships alongside this recipe; a partial checkout of deploy-recipes/ is the likely cause." >&2
        # Refusing here rather than skipping the gate: a missing guard must never
        # read as a passing one.
        exit 20
    fi
done

# Workaround for the bot-squad worker running with a read-only /tmp: docker
# compose writes a transient metadata file at the end of `build` and crashes
# the recipe with `read-only file system`. Redirect TMPDIR into the repo (which
# IS writable) so the build exits cleanly and the recipe reaches `up -d`.
export TMPDIR="$REPO/.tmp-deploy"
mkdir -p "$TMPDIR"

# ══ PHASE 1: ACTIONS ════════════════════════════════════════════════════════
docker compose build signal-tracker-staging
# Force-remove any existing container with this name (--force-recreate alone
# collides when container_name is fixed). Then recreate from the new image.
docker rm -f "$APP_CONTAINER" 2>/dev/null || true
docker compose up -d signal-tracker-staging

# ── T-0390 PREREQUISITE: the `stt` speech-to-text sidecar ────────────────────
# `docker compose up -d <named service>` does NOT start unrelated services, so
# until these lines existed NOTHING on a deploy target ever started `stt` — the
# service the end-user Telegram bot's voice path calls (D-0006 §1.2, T-0388).
# It has been alive on the dev box only because someone started it by hand and
# `restart: unless-stopped` kept it there indefinitely, and THAT is what hid
# the gap: the voice half works on the machine it was built on and is absent
# everywhere else, with the local success stopping anyone from noticing.
#
# Deliberately NO `docker rm -f stt` first, unlike the app container above.
# `stt` is a SINGLE SHARED instance for prod + staging (see the block comment
# in docker-compose.yml), the model costs ~36 s to load, and tearing it down on
# every deploy would take the other environment's voice path down with it.
# `up -d` is idempotent: a no-op when the service is already running from the
# current image, a start when it is absent, a recreate when the image changed.
#
# Building it here — after the app is already started — also means the app warms
# up in parallel with this build instead of being waited on first.
#
# T-0427: run under the PINNED project (see $STT_PROJECT above), and reclaim the
# name first if some other project holds it. `reclaim` removes ONLY on a label
# mismatch, so the everyday deploy — where the singleton is already ours — does
# not touch the running container and costs no voice-path gap.
#
# The whole block is captured rather than allowed to `set -e` the recipe dead.
# That is not leniency, it is the same rule as the gates: when this block died
# at rc=1 on 2026-07-30 it took EVERY gate with it, so a supervised deploy that
# had in fact shipped the app successfully reported nothing at all about the
# app. A sidecar's actions must not be able to strand the app's verdict. The
# failure is not swallowed — it is carried to phase 2 as $stt_action_rc and it
# fails the deploy there, with the rest of the picture alongside it.
#
# ORDER MATTERS: build BEFORE reclaim. `build` does not care who owns a
# container name, so doing it first means the only window in which the shared
# singleton is absent is reclaim→up, not build→up. On this box a docker build
# is never cheap (~246s even fully cached, most of it attestation export), so
# that reordering is the difference between a seconds-long voice gap and a
# minutes-long one — for PROD as well, since it shares this instance.
stt_action_rc=0
{
    docker compose -p "$STT_PROJECT" build stt &&
    bash "$OWNER" reclaim "$STT_CONTAINER" "$STT_PROJECT" &&
    docker compose -p "$STT_PROJECT" up -d stt
} || stt_action_rc=$?
[ "$stt_action_rc" -eq 0 ] || echo "[staging] stt actions FAILED (rc=$stt_action_rc); continuing to the gates so the app's verdict is not lost." >&2

echo "[staging] actions complete at $((SECONDS - DEPLOY_START_S))s; load $(_loadavg) — gates follow"

# ══ PHASE 2: GATES ══════════════════════════════════════════════════════════
# Each gate records its own verdict. `|| rc=$?` keeps `set -e` from exiting
# here, which is the entire point: no gate may pre-empt another.
failed=""

app_rc=0
bash "$GATE" container "$APP_CONTAINER" "$APP_INTERNAL_URL" "$APP_READY_TIMEOUT_S" staging-app || app_rc=$?
[ "$app_rc" -eq 0 ] || failed="$failed app"

stt_rc=0
bash "$GATE" container "$STT_CONTAINER" "$STT_INTERNAL_URL" "$STT_READY_TIMEOUT_S" staging-stt || stt_rc=$?
[ "$stt_rc" -eq 0 ] || failed="$failed stt"

# OWNERSHIP GATE (T-0427) — the one the previous review needed and did not have.
# The gate above asks "does something answer at http://stt:8003/health". For ten
# hours the answer was yes, from a container belonging to compose project `dev`,
# reachable over the shared avo_backend network — so the health check passed and
# would have passed identically if this recipe's stt block had never existed.
# A health probe cannot see ownership; only the label can. This gate fails
# exactly in the case that stayed invisible, and no container answering 200 can
# satisfy it.
stt_own_rc=0
bash "$OWNER" assert-owner "$STT_CONTAINER" "$STT_PROJECT" || stt_own_rc=$?
[ "$stt_own_rc" -eq 0 ] || failed="$failed stt-ownership"

# Phase-1's stt actions get their verdict recorded HERE, in phase 2, with the
# others — so a sidecar action failure is one line in one verdict rather than a
# dead recipe.
[ "$stt_action_rc" -eq 0 ] || failed="$failed stt-actions"

# Only meaningful once the app itself is known ready — otherwise a public
# failure is just the app's failure reported at the wrong layer. Skipped (not
# failed) when the app gate is red, and the app gate's own non-zero exit is
# what fails the deploy in that case.
route_rc=0
if [ "$app_rc" -eq 0 ]; then
    bash "$GATE" public "$APP_PUBLIC_URL" "$ROUTE_TIMEOUT_S" staging-route || route_rc=$?
    [ "$route_rc" -eq 0 ] || failed="$failed public-route"
else
    echo "[staging-route] SKIPPED: the app is not ready inside its container, so a public failure would tell us nothing new."
fi

ELAPSED=$((SECONDS - DEPLOY_START_S))
# Always record wall clock WITH the load average. The ~90s window this gate
# replaced was sized off an idle-box figure; the deploy that exposed it took
# 37m19s at load average 67 against a ~246s cached-build estimate. Without both
# numbers on the same line the next person sizes the next window off the wrong
# one again. (T-0427 DoD)
echo "[staging] wall clock ${ELAPSED}s; load $(_loadavg) at finish"

if [ -n "$failed" ]; then
    echo "[staging] FATAL: deploy failed —$failed (app_rc=$app_rc stt_rc=$stt_rc stt_own_rc=$stt_own_rc stt_action_rc=$stt_action_rc route_rc=$route_rc)" >&2
    echo "[staging] every gate above was RUN; see its own lines for which signal it got." >&2
    # App down is the most severe, then the voice path, then ownership, then
    # routing. Every branch is explicit: an unlisted failure must not fall
    # through to whichever exit code happens to be last.
    [ "$app_rc" -eq 0 ]        || exit 22
    [ "$stt_rc" -eq 0 ]        || exit 23
    [ "$stt_own_rc" -eq 0 ]    || exit 25
    [ "$stt_action_rc" -eq 0 ] || exit 26
    exit 24
fi

echo "[staging] release deployed: $(git rev-parse --short HEAD)"
