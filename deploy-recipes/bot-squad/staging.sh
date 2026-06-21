#!/usr/bin/env bash
# bot-squad → staging (this server, the mothership)
#
# Runs with cwd = the DEPLOY CLONE (T-0143): the worker sets cwd via
# Project.repo_for_target("staging") → repo_deploy, a disposable CI checkout
# it has already created (on first deploy) and force-synced to
# origin/bot_squad/dev before invoking this recipe. So $(pwd) is a clean tree
# at the latest PUSHED commit regardless of the shared dev clone's state — a
# dirty dev tree no longer blocks the deploy. (When repo_deploy is unset the
# worker falls back to running this in the dev clone, the legacy behaviour.)
# The fetch + ff-merge below are a no-op safety net against the synced clone.
# We then sync the install dir to the same branch and rebuild containers.
#
# Convention (update-delivery.md): "staging" = THIS server only.
# "prod" (future) = release artifact for OTHER attached servers.
set -euo pipefail

REPO="$(pwd)"
INSTALL_DIR="${BOT_SQUAD:-/home/www/bot-squad}"
DEPLOY_BRANCH="bot_squad/dev"

echo "[bot-squad/staging] dev clone:   $REPO"
echo "[bot-squad/staging] install dir: $INSTALL_DIR"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "$DEPLOY_BRANCH" ]; then
    echo "[bot-squad/staging] FATAL: expected dev clone on '$DEPLOY_BRANCH', got '$BRANCH'" >&2
    exit 3
fi

# 1. Pull dev locally for sanity.
git fetch origin
if ! git merge --ff-only "origin/$DEPLOY_BRANCH"; then
    echo "[bot-squad/staging] FATAL: dev clone diverged from origin/$DEPLOY_BRANCH — manual fixup required" >&2
    exit 4
fi

# 2. Sync the install dir to the dev branch. Idempotent: if it's already on
#    $DEPLOY_BRANCH, just ff-merge; if it's on master (legacy), switch.
INSTALL_BRANCH=$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)
git -C "$INSTALL_DIR" fetch origin

# T-0110 — direct-install-commit guard. The install is a deploy TARGET, not
# an editing surface. Any local-only commits on the install branch are about
# to be lost (we ff-merge from origin); list them loudly so the agent who
# made them can recover (cherry-pick into the dev clone) before re-deploying.
LOCAL_ONLY=$(git -C "$INSTALL_DIR" log --oneline "origin/$INSTALL_BRANCH..HEAD" 2>/dev/null || true)
if [ -n "$LOCAL_ONLY" ]; then
    n=$(echo "$LOCAL_ONLY" | wc -l)
    echo "[bot-squad/staging] FATAL: install dir has $n local-only commit(s) on $INSTALL_BRANCH that origin doesn't have:" >&2
    echo "$LOCAL_ONLY" | sed 's/^/    /' >&2
    echo "[bot-squad/staging] These commits would be wiped by this deploy (the install is a deploy target, not an editing surface)." >&2
    echo "[bot-squad/staging] Recover by cherry-picking into /home/almdudleer/bot-squad-mgmt (the dev clone) and pushing origin/$DEPLOY_BRANCH, then re-deploy." >&2
    exit 7
fi
DIRTY=$(git -C "$INSTALL_DIR" status --porcelain | grep -v '^?? data/' || true)
if [ -n "$DIRTY" ]; then
    echo "[bot-squad/staging] FATAL: install dir has uncommitted changes (excluding data/):" >&2
    echo "$DIRTY" | sed 's/^/    /' >&2
    echo "[bot-squad/staging] These edits would be overwritten by this deploy. Move them to the dev clone or revert." >&2
    exit 8
fi

if [ "$INSTALL_BRANCH" != "$DEPLOY_BRANCH" ]; then
    echo "[bot-squad/staging] install dir on '$INSTALL_BRANCH'; switching to '$DEPLOY_BRANCH'"
    # Create local tracking branch from origin if missing, then check out.
    git -C "$INSTALL_DIR" checkout -B "$DEPLOY_BRANCH" "origin/$DEPLOY_BRANCH"
else
    if ! git -C "$INSTALL_DIR" merge --ff-only "origin/$DEPLOY_BRANCH"; then
        echo "[bot-squad/staging] FATAL: install dir diverged from origin/$DEPLOY_BRANCH" >&2
        exit 5
    fi
fi

# 3. Rebuild + restart containers. Picks up new docker-compose.yml + new
#    Dockerfile + new web/dist baked into the api image.
#
#    Quirk: under the worker user, /tmp is read-only (some systemd layout),
#    so `docker compose build`'s post-build metadata-file write fails with
#    rc=1 even though the image was produced cleanly. We point compose at
#    a writable TMPDIR to sidestep it. The image-equality case (label-only
#    deploys where the build is fully cached) is also normal — `up -d`
#    still recreates containers when compose detects label/env diffs.
cd "$INSTALL_DIR"

export TMPDIR="${INSTALL_DIR}/_tmp"
mkdir -p "$TMPDIR"

# T-0379: stamp the deployed git sha into the image (ARG GIT_SHA → ENV
# BOT_SQUAD_GIT_SHA, surfaced at /api/health). The install dir is already
# ff-merged to origin/$DEPLOY_BRANCH above, so its HEAD IS the deployed commit.
export GIT_SHA="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
echo "[bot-squad/staging] building image stamped GIT_SHA=$GIT_SHA"

docker compose build || {
    rc=$?
    echo "[bot-squad/staging] WARN: docker compose build rc=$rc; checking whether image was still produced..." >&2
    if ! docker image inspect bot-squad-api:latest >/dev/null 2>&1; then
        echo "[bot-squad/staging] FATAL: no bot-squad-api:latest image after build attempt" >&2
        exit 6
    fi
    echo "[bot-squad/staging] image present despite rc=$rc; proceeding with up -d"
}

docker compose up -d

# 3b. T-0379: assert the RUNNING container is actually the commit we deployed.
#     Closes the stale-image gap (a deploy 'succeeded' but shipped an older HEAD
#     because the build context / timing didn't match the intended commit). The
#     image bakes BOT_SQUAD_GIT_SHA at build; if the running env doesn't match
#     $GIT_SHA the recipe FAILS loudly instead of reporting a false success.
running_sha="$(docker exec bot-squad-api printenv BOT_SQUAD_GIT_SHA 2>/dev/null || true)"
if [ "$running_sha" != "$GIT_SHA" ]; then
    echo "[bot-squad/staging] FATAL: running container sha '$running_sha' != deployed sha '$GIT_SHA'" >&2
    echo "[bot-squad/staging] the image did not pick up the deployed commit — NOT a successful deploy." >&2
    exit 9
fi
echo "[bot-squad/staging] verified running container sha == deployed sha ($GIT_SHA)"

# 4. Worker restart is NOT done here in the recipe. T-0181 moved it into
#    deploy.run_next, which (on a clean success, when the deploy request set
#    restart_worker=true) launches a DETACHED `systemctl --user restart
#    bot-squad-worker` + smoke in its own systemd scope AFTER this recipe and
#    the queue bookkeeping finish. Doing it in-recipe would either (a) kill the
#    recipe mid-run [pre-T-0213] or (b) orphan the queue file [post-T-0213, the
#    worker dies before run_next records the result]. So: enqueue with
#    restart_worker=true when the diff touches worker-loaded code; otherwise the
#    worker keeps running the synced-but-not-reloaded code until a manual
#    `systemctl --user restart bot-squad-worker`. (T-0213 separately wraps THIS
#    recipe in its own scope so a *concurrent* worker restart can't SIGTERM the
#    in-flight build.)

# 5. Smoke the running API. T-0079: retry-with-backoff instead of a fixed
#    5s sleep. After a label-flip recreate, traefik label discovery + LE
#    challenge can take 5-20s before /api/health answers; the helper polls
#    on the schedule 1s/2s/4s/8s/16s (~31s total budget) and prints which
#    attempt won so audit logs show whether the budget needs tuning.
"$REPO/scripts/ops/smoke-with-backoff.sh" https://staging.botsquad.dev/api/health

echo "[bot-squad/staging] release deployed: $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
