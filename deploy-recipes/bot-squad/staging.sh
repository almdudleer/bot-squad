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

# T-0878 — detach the LIVE project registry from git, once, per install.
#
# config/projects.toml is the registry the install MUTATES: every project
# registered through the API writes a [projects.<slug>] block into it. While it
# was git-tracked, that guaranteed one of two bad outcomes on the very next
# deploy — the dirty guard below refusing (measured: 2026-08-06 08:27 and
# 2026-08-11 14:23, five days apart, both reaching nobody), or, without the
# guard, the ff-merge silently de-registering the project. It is git-ignored
# from this commit on; the repo ships config/projects.default.toml as the seed.
#
# This block runs BEFORE the guards on purpose: the live file is exactly the
# thing that makes the tree dirty, and reverting it (the first instinct on
# reading that guard's message) is what destroys the registration. So: keep the
# live content aside, restore the tracked copy so the tree is clean, let the
# ff-merge remove the now-untracked path, then put the live content back.
# Idempotent — `ls-files` is empty on every install that has already migrated.
# >>> T-0878-MIGRATION-STASH (extracted verbatim by
#     worker/tests/test_t0878_recipe_migration.py — keep the markers)
REGISTRY_STASH=""
if git -C "$INSTALL_DIR" ls-files --error-unmatch config/projects.toml >/dev/null 2>&1; then
    # Parked under data/ — the ops surface, git-ignored in full. Parking it in
    # config/ would surface as `?? config/...` and trip the very dirty guard
    # this migration exists to stop tripping.
    mkdir -p "$INSTALL_DIR/data/_worker"
    REGISTRY_STASH="$INSTALL_DIR/data/_worker/projects.toml.t0878-migration"
    echo "[bot-squad/staging] T-0878: config/projects.toml is still git-tracked here — migrating it to install-owned state"
    cp -p "$INSTALL_DIR/config/projects.toml" "$REGISTRY_STASH"
    # The window between this checkout and the restore below is the ONLY moment
    # the live registry is not at its live content. Every exit path out of that
    # window — the dirty guard (8), the diverged-install guard (5), a signal —
    # must put it back, or the migration itself becomes the de-registration this
    # ticket exists to prevent. The restore clears REGISTRY_STASH, so the trap
    # is a no-op on the happy path.
    trap 'if [ -n "${REGISTRY_STASH:-}" ] && [ -f "$REGISTRY_STASH" ]; then mv -f "$REGISTRY_STASH" "$INSTALL_DIR/config/projects.toml"; echo "[bot-squad/staging] T-0878: live registry restored after an early exit" >&2; fi' EXIT
    git -C "$INSTALL_DIR" checkout -- config/projects.toml
fi
# <<< T-0878-MIGRATION-STASH

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

# >>> T-0878-MIGRATION-RESTORE
# T-0878 (cont.) — the sync above is the last moment git could ever have
# touched the registry. Put the live content back, and seed a fresh install
# that has none. From here on the file is untracked + ignored, so no future
# deploy can block on it or delete it.
if [ -n "$REGISTRY_STASH" ] && [ -f "$REGISTRY_STASH" ]; then
    mv -f "$REGISTRY_STASH" "$INSTALL_DIR/config/projects.toml"
    REGISTRY_STASH=""
    trap - EXIT
    echo "[bot-squad/staging] T-0878: live registry restored, now install-owned (untracked)"
fi
if [ ! -f "$INSTALL_DIR/config/projects.toml" ] && [ -f "$INSTALL_DIR/config/projects.default.toml" ]; then
    cp -p "$INSTALL_DIR/config/projects.default.toml" "$INSTALL_DIR/config/projects.toml"
    echo "[bot-squad/staging] T-0878: seeded config/projects.toml from the tracked default"
fi
if [ -n "$(git -C "$INSTALL_DIR" status --porcelain -- config/projects.toml)" ]; then
    echo "[bot-squad/staging] FATAL: config/projects.toml is still visible to git after the T-0878 migration" >&2
    exit 10
fi
# <<< T-0878-MIGRATION-RESTORE

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

# T-1001 — KEEP THE IMAGE THE MEASUREMENTS THAT GATED THIS DEPLOY WERE TAKEN IN.
#
# `docker compose build` below moves `bot-squad-api:latest` onto a new image.
# Every api certification is taken in that tag, so the deploy those
# certifications GATED is the event that makes them uncheckable — not by
# accident, but every single time we do this correctly. Measured 2026-09-06:
# `docker image inspect` on 926bf32fcb08 and 8ceaa2fcd999, the two images that
# carried a whole afternoon's api chain, both returned rc 1. Not untagged:
# ABSENT.
#
# Naming the digest in the report was already the rule and was followed all
# day; it did not help, because an anchor makes a claim CHECKABLE, not
# REPRODUCIBLE. And rebuilding from the ref is not the answer either:
# api/pyproject.toml pins nothing but floors and there is no lockfile, so a
# rebuild resolves whatever PyPI serves that day — a new environment, not a
# reproduction. What is left is to keep the artefact, under a name this rebuild
# cannot move.
#
# Bounded, because the disk is a live constraint on this box (it collapsed
# twice under IO on 2026-09-06) — and the layers are shared with :latest, so
# the marginal cost is a fraction of the image size. Non-fatal by construction:
# neither retaining nor reclaiming may redden a healthy deploy.
# >>> T-1001-IMAGE-RETAIN (extracted verbatim by
#     worker/tests/test_t1001_image_retain.py — keep the markers)
RETAIN_KEEP=3
RETAIN_OLD_ID="$(docker image inspect bot-squad-api:latest --format '{{.Id}}' 2>/dev/null || true)"
if [ -z "$RETAIN_OLD_ID" ]; then
    echo "[bot-squad/staging] T-1001: no outgoing bot-squad-api:latest to retain (first build on this host)"
else
    # The tag carries BOTH coordinates a later reader needs: WHEN the image was
    # built (so the set sorts chronologically with no extra bookkeeping) and
    # WHICH REF it was built from (so a certification naming a sha can be
    # matched to an image without inspecting every candidate). Derived from the
    # image itself, never from the clock, so re-running this on an unchanged
    # image is idempotent rather than a second tag for the same bytes.
    RETAIN_CREATED="$(docker image inspect bot-squad-api:latest --format '{{.Created}}' 2>/dev/null || true)"
    # NO `head -1` HERE, and it is not style. The recipe runs under `set -euo
    # pipefail`: a consumer that exits early SIGPIPEs `sed`, pipefail promotes
    # that to a non-zero pipeline status, and a failing command substitution
    # under `set -e` kills the deploy — on an image whose env happens to have
    # more lines. `sed` reads to EOF; the first line is taken in the shell.
    RETAIN_REF="$(docker image inspect bot-squad-api:latest \
                    --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
                  | sed -n 's/^BOT_SQUAD_GIT_SHA=//p' || true)"
    RETAIN_REF="${RETAIN_REF%%
*}"
    RETAIN_STAMP="$(date -u -d "$RETAIN_CREATED" +%Y%m%dT%H%M%SZ 2>/dev/null || true)"
    [ -n "$RETAIN_STAMP" ] || RETAIN_STAMP="unknown"
    case "$RETAIN_REF" in
        ""|unknown) RETAIN_REF="$(printf '%s' "${RETAIN_OLD_ID#sha256:}" | cut -c1-12)" ;;
        *)          RETAIN_REF="$(printf '%s' "$RETAIN_REF" | cut -c1-12)" ;;
    esac
    RETAIN_TAG="bot-squad-api:cert-${RETAIN_STAMP}-${RETAIN_REF}"
    if docker tag "$RETAIN_OLD_ID" "$RETAIN_TAG" 2>/dev/null; then
        echo "[bot-squad/staging] T-1001: RETAINED the outgoing api image as $RETAIN_TAG ($RETAIN_OLD_ID)"
        echo "[bot-squad/staging] T-1001: certifications taken in bot-squad-api:latest before this deploy are re-runnable as $RETAIN_TAG"
    else
        echo "[bot-squad/staging] T-1001: WARN could not retain $RETAIN_OLD_ID — certifications taken in it become UNCHECKABLE after this build" >&2
    fi

    # Reclaim beyond the window — LOUDLY. Silent reclamation is the whole
    # defect: nobody noticed the afternoon's images going because nothing said
    # so. Never touch the image :latest currently points at.
    docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 'bot-squad-api:cert-*' 2>/dev/null \
        | sort -r | tail -n +$((RETAIN_KEEP + 1)) \
        | while read -r _old _oldid; do
              case "$RETAIN_OLD_ID" in
                  *"$_oldid"*) echo "[bot-squad/staging] T-1001: keeping $_old — it is the outgoing image"; continue ;;
              esac
              echo "[bot-squad/staging] T-1001: RECLAIMING $_old — beyond the $RETAIN_KEEP-deploy retention window; measurements naming it are no longer re-runnable"
              docker rmi "$_old" >/dev/null 2>&1 || true
          done || true
fi
# <<< T-1001-IMAGE-RETAIN

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
