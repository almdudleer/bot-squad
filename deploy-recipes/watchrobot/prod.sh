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
