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
# 2026-07-30 extends that rule into PHASE 1 as well: the `stt` block died under
# `set -e` on staging and took every gate with it, so a deploy that had shipped
# the app fine reported nothing about the app. Sidecar actions are now captured
# and judged in phase 2 — they still fail the deploy, they no longer silence it.
#
# ROLLBACK (T-0378) — read this before changing PHASE 1. Until 2026-08-11 this
# recipe force-removed the running container and then started its replacement,
# so ANY failure after that point left prod down with nothing to put back. The
# T-0201 hardening that fixed it was written into `data/watchrobot/deploy/
# prod.sh`, which `_recipe_path` never reads (it PREFERS this tracked copy), so
# it sat unexecuted for five weeks. What it is NOT safe to do is port that file
# back verbatim — see the block above PHASE 1 for the two ways it would have
# made things worse here.
#
# Exit codes:
#   2   the managed prod .env is missing (nothing was touched)
#   3   cwd is not the master clone / not on master (nothing was touched)
#   4   the master clone has diverged from origin/master
#   5   `docker compose up -d` failed — rollback attempted
#   20  a gate library is missing/not executable
#   22  the app never became ready INSIDE its container — rollback attempted
#   23  the `stt` sidecar never became healthy
#   24  the app was ready internally but never answered on its public URL
#       (i.e. traefik/LE, not the app) — rollback attempted
#   25  the `stt` container is not owned by this deploy's compose project
#   26  the `stt` sidecar's PHASE-1 actions (reclaim/build/up) failed
#   27  the deploy failed AND the rollback also failed — prod needs a human
set -euo pipefail

DEPLOY_START_S=$SECONDS
_loadavg() { cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo "load-unknown"; }

REPO="$(pwd)"

APP_CONTAINER="signal-tracker"
APP_INTERNAL_URL="http://localhost:8000/api/version"
APP_PUBLIC_URL="https://signal-tracker.dev.uzinvestapi.com/api/version"
STT_CONTAINER="stt"
STT_INTERNAL_URL="http://localhost:8003/health"

# PINNED compose project for the shared `stt` singleton — MUST match staging.sh
# exactly, or the two deploys go back to fighting over the container name. The
# full reasoning is in staging.sh and in lib/shared-container.sh; the short
# version is that compose derives its project from the cwd basename, this box
# has three clones (dev/deploy/master), `stt` is one container with a fixed
# name, and only its owning project can manage it. Prod running as project
# `master` would hit the identical `Conflict. The container name "/stt" is
# already in use` that killed the staging deploy at 20:07Z on 2026-07-30.
STT_PROJECT="watchrobot-stt"

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

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
GATE="$LIB/wait-ready.sh"
OWNER="$LIB/shared-container.sh"
for _f in "$GATE" "$OWNER"; do
    if [ ! -x "$_f" ]; then
        echo "[prod] FATAL: gate library $_f is missing or not executable." >&2
        echo "[prod] It ships alongside this recipe; a partial checkout of deploy-recipes/ is the likely cause." >&2
        # Refuse rather than skip: a missing guard must never read as a pass.
        exit 20
    fi
done

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

# ── managed prod .env (T-0201, ported here under T-0378) ────────────────────
# The master clone's `.env` is gitignored and lives OUTSIDE this clone's
# lifecycle, so a clone wipe/recreate (like the 2026-06-02 slug migration,
# which lost the original prod .env entirely) must not be able to take prod
# down again. The durable copy lives outside any git clone; this re-links it on
# every run so a fresh master clone self-heals instead of failing at
# `docker compose up` with "env file .env not found".
#
# WHY IT HAD TO COME BACK. `env_file:` is read when the container is CREATED,
# not when the image is built — i.e. at `up -d`, which is AFTER the old
# container is gone. That is precisely the 2026-06-05 outage, and it is the one
# failure mode that the build-runs-first ordering does NOT already cover.
# Measured 2026-08-11: the symlink is currently present and its target exists,
# so this is a latent hole rather than a live one — it arms itself the moment
# anyone recreates the master clone.
#
# Edit the secrets file to change prod config — never hand-edit the symlink
# target in this clone.
# Overridable ONLY so the missing-.env branch can be exercised by the selftest.
# A guard nobody has ever seen fire is the exact shape of the defect this whole
# file was audited for, so it gets a negative test like the gates do; the
# default is the real path and no deploy sets the override.
SECRETS_ENV="${WR_PROD_SECRETS_ENV:-/home/almdudleer/.bot-squad-secrets/watchrobot-prod.env}"
if [ ! -f "$SECRETS_ENV" ]; then
    echo "[prod] FATAL: managed prod .env not found at $SECRETS_ENV" >&2
    echo "[prod] This is the durable, out-of-clone store for prod secrets (T-0201)." >&2
    echo "[prod] See data/watchrobot/deploy/prod.env.template for the required keys." >&2
    # Refuse BEFORE the build, so the running container is never touched.
    exit 2
fi
ln -sf "$SECRETS_ENV" "$REPO/.env"

# ── build identity (T-0553) ─────────────────────────────────────────────────
# MIRRORS staging.sh — and prod needs it for the same reason, not as a copy:
# both compose services build the SAME Dockerfile, whose lib-builder stage
# REFUSES to run without these. Fixing only staging would leave prod
# undeployable — including an emergency deploy under the freeze — and the
# breakage would appear only at the moment someone needed it most.
# Computed in the checkout being built (deploys are cherry-picks; only the
# builder's HEAD is true). VCS_REF also cures the image's `revision=unknown`,
# which is why the deployed sha had to be guessed from content at all.
#
# T-0587 — bare assignment, then export. `export x=$(false)` exits 0 under
# `set -e` (measured) and would set an empty string on a git that could not
# answer; a plain assignment exits 1. Same three lines as staging.sh, same
# reason, and they must stay identical.
WR_LIB_COMMIT="$(git rev-parse HEAD)"
WR_LIB_COUNT="$(git rev-list --count HEAD)"
WR_LIB_VERSION="0.0.${WR_LIB_COUNT}+g${WR_LIB_COMMIT:0:12}"
# `.dirty` as web/vite.lib.config.ts derives it in-checkout, scoped to web/.
# A version must not name a commit that does not contain what was built.
[ -z "$(git status --porcelain -- web/)" ] || WR_LIB_VERSION="${WR_LIB_VERSION}.dirty"
export WR_LIB_COMMIT WR_LIB_VERSION
export VCS_REF="$WR_LIB_COMMIT"
echo "[prod] build identity: $WR_LIB_VERSION (VCS_REF=$VCS_REF)"

# ── ROLLBACK TARGET (T-0378) ────────────────────────────────────────────────
# The failure this closes: `docker rm -f` below destroys the running container
# before its replacement exists, so anything that goes wrong afterwards left
# prod down with NOTHING to put back. Detection was never the gap — the gates
# in phase 2 are loud and correct. RECOVERY was.
#
# WHY AN IMAGE TAG AND NOT THE RENAME-ASIDE FROM `data/watchrobot/deploy/
# prod.sh`. That copy keeps the old CONTAINER as the rollback target, and it is
# tempting to port because it is written, reviewed, and older than this note.
# It was never executed, and it has two defects that only appear once it is:
#
#   1. `docker compose up -d` selects containers by its own LABELS
#      (com.docker.compose.project/service), NOT by container name. A renamed
#      container keeps those labels, so compose still sees it as this service's
#      container and recreates it — deleting the very thing being kept as the
#      rollback target. The rename buys nothing and hides that it bought
#      nothing.
#   2. It leaves the old container RUNNING with its traefik labels intact, so
#      traefik load-balances across old and new during the overlap. The
#      public-route gate in phase 2 can then be answered by the OLD container
#      and pass — a false green, after which the recipe deletes the rollback
#      target believing the new release serves. Strictly worse than no rollback,
#      because it converts "prod is down and we know" into "prod is broken and
#      the deploy said OK".
#
# An image tag has neither problem: it is inert, compose cannot garbage-collect
# it, nothing serves traffic from it until we ask, and it stays valid even after
# the container is gone. The cost is that a rollback is a container start rather
# than a rename — seconds, on a path that is already an outage.
APP_IMAGE="$(docker compose config --images signal-tracker)"
ROLLBACK_IMAGE=""
if docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
    # The container's OWN image id — not the `:latest` tag, which the build
    # below is about to move off it. Resolving by tag here would pin a rollback
    # target that becomes the new image the moment the build finishes, i.e. a
    # rollback to exactly what failed.
    PREV_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$APP_CONTAINER")"
    ROLLBACK_IMAGE="${APP_IMAGE}:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
    docker tag "$PREV_IMAGE_ID" "$ROLLBACK_IMAGE"
    echo "[prod] rollback target pinned: $ROLLBACK_IMAGE -> ${PREV_IMAGE_ID#sha256:}"
else
    echo "[prod] no running $APP_CONTAINER: from-empty deploy, so there is no rollback target and a failure below cannot be undone." >&2
fi

# Returns 0 only if prod is serving the PREVIOUS image again.
rollback() {
    if [ -z "$ROLLBACK_IMAGE" ]; then
        echo "[prod] ROLLBACK UNAVAILABLE: nothing was running before this deploy." >&2
        return 1
    fi
    echo "[prod] ROLLING BACK to $ROLLBACK_IMAGE" >&2
    docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
    # Move the tag compose resolves back onto the known-good image, then let
    # compose recreate from it — so the restored container keeps this file's
    # env/labels/networks rather than whatever a hand-run `docker run` implies.
    docker tag "$ROLLBACK_IMAGE" "${APP_IMAGE}:latest"
    if docker compose up -d signal-tracker; then
        echo "[prod] rollback complete: previous image is running again." >&2
        echo "[prod] The tree still contains the release that failed — do NOT re-queue this deploy until it is fixed." >&2
        return 0
    fi
    echo "[prod] FATAL: THE ROLLBACK ITSELF FAILED. Prod is down and needs a human now." >&2
    return 1
}

# ══ PHASE 1: ACTIONS ════════════════════════════════════════════════════════
docker compose build signal-tracker
docker rm -f "$APP_CONTAINER" 2>/dev/null || true
if ! docker compose up -d signal-tracker; then
    echo "[prod] FATAL: docker compose up failed — the app container does not exist." >&2
    if rollback; then exit 5; else exit 27; fi
fi

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
# T-0427: pinned project + reclaim-on-mismatch. `reclaim` removes ONLY when
# another compose project holds the name, so a normal prod deploy never touches
# the running singleton and staging's voice path never blinks because of it.
# Captured, not fatal, for the reason in the STRUCTURE note at the top.
#
# Build BEFORE reclaim — see staging.sh. A build does not care who owns a
# container name, so building first shrinks the window in which the shared
# singleton is absent from build→up down to reclaim→up.
stt_action_rc=0
{
    docker compose -p "$STT_PROJECT" build stt &&
    bash "$OWNER" reclaim "$STT_CONTAINER" "$STT_PROJECT" &&
    docker compose -p "$STT_PROJECT" up -d stt
} || stt_action_rc=$?
[ "$stt_action_rc" -eq 0 ] || echo "[prod] stt actions FAILED (rc=$stt_action_rc); continuing to the gates so the app's verdict is not lost." >&2

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

# OWNERSHIP GATE (T-0427). The gate above only asks whether SOMETHING answers on
# http://stt:8003/health — and on this box something always does, over the
# shared avo_backend network, regardless of which project owns it. That is how
# a wiring check passed for ten hours while this recipe's stt block had never
# once created a container. Only the compose-project label can tell the two
# apart, so that is what this asserts.
stt_own_rc=0
bash "$OWNER" assert-owner "$STT_CONTAINER" "$STT_PROJECT" || stt_own_rc=$?
[ "$stt_own_rc" -eq 0 ] || failed="$failed stt-ownership"

# Phase-1's sidecar actions get their verdict here, with the rest.
[ "$stt_action_rc" -eq 0 ] || failed="$failed stt-actions"

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
    echo "[prod] FATAL: deploy failed —$failed (app_rc=$app_rc stt_rc=$stt_rc stt_own_rc=$stt_own_rc stt_action_rc=$stt_action_rc route_rc=$route_rc)" >&2
    echo "[prod] every gate above was RUN; see its own lines for which signal it got." >&2

    # ── ROLL BACK, BUT ONLY WHAT A ROLLBACK ACTUALLY FIXES (T-0378) ─────────
    # The trigger is "users are not being served", which is exactly app_rc
    # (unhealthy inside its own container) and route_rc (healthy inside, absent
    # on the public URL — a release that broke its traefik labels lands here).
    # A route failure whose real cause is traefik/LE is not made worse by this:
    # the image we restore is the one that WAS answering on that URL.
    #
    # An `stt`-only failure deliberately does NOT roll the app back. The app is
    # serving; reverting it would trade a working release for an outage and
    # would not start the sidecar either. It still fails the deploy — see the
    # exit codes below — it just does not take prod down to say so.
    if [ "$app_rc" -ne 0 ] || [ "$route_rc" -ne 0 ]; then
        rollback || exit 27
    else
        echo "[prod] not rolling back: the app is serving. Failure is —$failed, which a rollback of the app cannot fix." >&2
    fi

    [ "$app_rc" -eq 0 ]        || exit 22
    [ "$stt_rc" -eq 0 ]        || exit 23
    [ "$stt_own_rc" -eq 0 ]    || exit 25
    [ "$stt_action_rc" -eq 0 ] || exit 26
    exit 24
fi

# Deploy is good. KEEP this run's rollback tag — it is the pre-deploy state, and
# the next failure may be noticed hours later — but bound the set, because these
# are full images on a box that already builds three projects. Non-fatal by
# construction: a prune that fails must never redden a healthy deploy.
if [ -n "$ROLLBACK_IMAGE" ]; then
    docker images --format '{{.Repository}}:{{.Tag}}' "${APP_IMAGE}:rollback-*" 2>/dev/null \
        | sort -r | tail -n +4 \
        | while read -r _old; do
              echo "[prod] pruning superseded rollback tag $_old"
              docker rmi "$_old" >/dev/null 2>&1 || true
          done || true
fi

echo "[prod] release deployed: $(git rev-parse --short HEAD)"
