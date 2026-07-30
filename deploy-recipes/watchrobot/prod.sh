#!/usr/bin/env bash
# signal-tracker → prod
# Operates on the master clone (set by bot-squad as cwd via
# Project.repo_for_target("prod")). The TL is responsible for landing a
# release commit on master before queueing this deploy — either via
# squash-merge from bot_squad/dev or as a direct hotfix commit. This recipe
# just pulls, builds, swaps, and gates.
#
# STRUCTURE — load-bearing, and identical to staging.sh on purpose (the two
# recipes carrying independent copies of the same gate is how the same defect
# came to exist twice; the gate itself now lives once, in lib/wait-ready.sh):
#   PHASE 1  ACTIONS — everything that changes the target.
#   PHASE 2  GATES — everything that VERIFIES. Every gate runs, unconditionally;
#            one verdict at the end.
# The one time these were interleaved (T-0427 defect 2) the `stt` sidecar block
# sat AFTER the app's smoke gate, the smoke gate false-failed under load and
# exited 22, and the sidecar was never started — app healthy, Telegram voice
# path silently absent, which is precisely what the sidecar block exists to
# prevent. Reordering the pair only moves the trap. So no gate exits early.
#
# Exit codes:
#   3   cwd is not the master clone / not on master (nothing was touched)
#   4   the master clone has diverged from origin/master
#   20  the readiness gate library is missing/not executable
#   22  the app never became ready INSIDE its container
#   23  the `stt` sidecar never became healthy
#   24  the app was ready internally but never answered on its public URL
#       (i.e. traefik/LE, not the app)
set -euo pipefail

DEPLOY_START_S=$SECONDS
_loadavg() { cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo "load-unknown"; }

REPO="$(pwd)"

APP_CONTAINER="signal-tracker"
APP_INTERNAL_URL="http://localhost:8000/api/version"
APP_PUBLIC_URL="https://signal-tracker.dev.uzinvestapi.com/api/version"
STT_CONTAINER="stt"
STT_INTERNAL_URL="http://localhost:8003/health"

# Generous ON PURPOSE, and only safe because the gate has a real negative
# signal (lib/wait-ready.sh): a dead container FATALs within one poll, so a
# timeout no longer has to be short enough to detect failure — it only bounds a
# hang. See staging.sh for the worker-watchdog interaction that caps these.
APP_READY_TIMEOUT_S="${APP_READY_TIMEOUT_S:-600}"
ROUTE_TIMEOUT_S="${ROUTE_TIMEOUT_S:-180}"
STT_READY_TIMEOUT_S="${STT_READY_TIMEOUT_S:-300}"

# WHICH FILE AM I — see staging.sh. `grep 'recipe:' <run-log>` answers "which
# artifact executed" without inference. (T-0378, T-0427)
echo "[prod] recipe: ${BASH_SOURCE[0]} sha256=$(sha256sum "${BASH_SOURCE[0]}" | cut -c1-16)"
echo "[prod] deploy start $(date -u +%Y-%m-%dT%H:%M:%SZ); load $(_loadavg)"
echo "[prod] using cwd $REPO"

# Verify we're on master before doing anything destructive.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "master" ]; then
    echo "[prod] FATAL: expected master clone on branch 'master', got '$BRANCH'" >&2
    exit 3
fi

GATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/wait-ready.sh"
if [ ! -x "$GATE" ]; then
    echo "[prod] FATAL: readiness gate $GATE is missing or not executable." >&2
    echo "[prod] It ships alongside this recipe; a partial checkout of deploy-recipes/ is the likely cause." >&2
    exit 20
fi

git fetch origin
# Fast-forward if behind; bail out if diverged (would mean someone hand-
# committed locally without pushing — TL should resolve before re-queueing).
if ! git merge --ff-only origin/master; then
    echo "[prod] FATAL: master clone has diverged from origin/master — manual fixup required" >&2
    exit 4
fi

# Workaround for bot-squad worker running with a read-only /tmp: docker compose
# writes a transient metadata file at the end of `build` and crashes the recipe
# with `read-only file system`. Redirecting TMPDIR into the repo (which IS
# writable) lets the build exit cleanly so the recipe reaches `up -d`.
# Mirrors staging.sh; was latent here until watchrobot's first prod deploy
# (2026-06-05) hit it.
export TMPDIR="$REPO/.tmp-deploy"
mkdir -p "$TMPDIR"

# ══ PHASE 1: ACTIONS ════════════════════════════════════════════════════════
docker compose build signal-tracker
docker rm -f "$APP_CONTAINER" 2>/dev/null || true
docker compose up -d signal-tracker

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
docker compose build stt
docker compose up -d stt

echo "[prod] actions complete at $((SECONDS - DEPLOY_START_S))s; load $(_loadavg) — gates follow"

# ══ PHASE 2: GATES ══════════════════════════════════════════════════════════
# `|| rc=$?` keeps `set -e` from exiting here, which is the entire point: no
# gate may pre-empt another.
failed=""

app_rc=0
bash "$GATE" container "$APP_CONTAINER" "$APP_INTERNAL_URL" "$APP_READY_TIMEOUT_S" prod-app || app_rc=$?
[ "$app_rc" -eq 0 ] || failed="$failed app"

stt_rc=0
bash "$GATE" container "$STT_CONTAINER" "$STT_INTERNAL_URL" "$STT_READY_TIMEOUT_S" prod-stt || stt_rc=$?
[ "$stt_rc" -eq 0 ] || failed="$failed stt"

route_rc=0
if [ "$app_rc" -eq 0 ]; then
    bash "$GATE" public "$APP_PUBLIC_URL" "$ROUTE_TIMEOUT_S" prod-route || route_rc=$?
    [ "$route_rc" -eq 0 ] || failed="$failed public-route"
else
    echo "[prod-route] SKIPPED: the app is not ready inside its container, so a public failure would tell us nothing new."
fi

ELAPSED=$((SECONDS - DEPLOY_START_S))
# Wall clock ALWAYS with the load average — the window this gate replaced was
# sized off an idle-box figure. (T-0427 DoD)
echo "[prod] wall clock ${ELAPSED}s; load $(_loadavg) at finish"

if [ -n "$failed" ]; then
    echo "[prod] FATAL: deploy failed —$failed (app_rc=$app_rc stt_rc=$stt_rc route_rc=$route_rc)" >&2
    echo "[prod] every gate above was RUN; see its own lines for which signal it got." >&2
    [ "$app_rc" -eq 0 ] || exit 22
    [ "$stt_rc" -eq 0 ] || exit 23
    exit 24
fi

echo "[prod] release deployed: $(git rev-parse --short HEAD)"
