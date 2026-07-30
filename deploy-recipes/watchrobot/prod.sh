#!/usr/bin/env bash
# signal-tracker → prod
# Operates on the master clone (set by bot-squad as cwd via
# Project.repo_for_target("prod")). The TL is responsible for landing a
# release commit on master before queueing this deploy — either via
# squash-merge from bot_squad/dev or as a direct hotfix commit. This recipe
# just pulls, builds, swaps, and smokes.
set -euo pipefail

REPO="$(pwd)"
echo "[prod] using cwd $REPO"

# Verify we're on master before doing anything destructive.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "master" ]; then
    echo "[prod] FATAL: expected master clone on branch 'master', got '$BRANCH'" >&2
    exit 3
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

docker compose build signal-tracker
docker rm -f signal-tracker 2>/dev/null || true
docker compose up -d signal-tracker

# Smoke with retry. After a container recreate, traefik label discovery + LE
# challenge + app cold-start on this loaded host can take >60s before
# /api/version answers; the old single `sleep 5; curl` false-failed with a 502
# mid-warmup (curl rc=22) on EVERY prod deploy though build+swap succeeded
# (5 false .fail.22 on 2026-06-11 alone). Poll ~90s before declaring failure,
# matching staging.sh. (T-0203)
# ORDERING IS LOAD-BEARING (T-0390, fixed 2026-07-30): this block sits BEFORE
# the app's /api/version smoke gate, not after it. It was after it for one
# deploy, and that deploy proved why it cannot be: the smoke gate false-failed
# on a loaded box (load avg ~50, three concurrent docker builds), exited 22,
# and SKIPPED the sidecar entirely — the app came up fine and voice intake was
# silently absent, which is the exact failure this block exists to prevent,
# reintroduced one layer up by where the block was placed. The sidecar is
# independent of the app, so nothing about the app's readiness should be able
# to strand it. The app is already recreated above and warms up while this runs.
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
docker compose build stt
docker compose up -d stt

# Prove it actually came up, rather than trusting that the two lines above ran.
# /health returns 503 until the model is resident (~13-36 s on this host), so
# this polls for a real 200. The container has no curl; it does have python.
stt_ok=0
for _ in $(seq 1 40); do
    if docker exec stt python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8003/health', timeout=3).status == 200 else 1)" >/dev/null 2>&1; then
        stt_ok=1
        echo "[prod] stt sidecar healthy (model resident)"
        break
    fi
    sleep 3
done
if [ "$stt_ok" -ne 1 ]; then
    echo "[prod] FATAL: stt sidecar never became healthy within ~120s — the Telegram bot's voice path would be dead on this target" >&2
    exit 23
fi

smoke_ok=0
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null https://signal-tracker.dev.uzinvestapi.com/api/version; then
        smoke_ok=1
        echo "[prod] smoke /api/version OK"
        break
    fi
    sleep 3
done
if [ "$smoke_ok" -ne 1 ]; then
    echo "[prod] FATAL: /api/version never answered within ~90s of recreate" >&2
    exit 22
fi


echo "[prod] release deployed: $(git rev-parse --short HEAD)"
