#!/usr/bin/env bash
# watchrobot → staging
#
# Builds + restarts the staging container from the dev clone's tree, which is
# pinned to bot_squad/dev. Staging only ever tracks bot_squad/dev, so there is
# NO separate `staging` branch and NO merge.
#
# T-0202 fix (2026-06-08): this recipe used to do
#     git checkout staging && git merge bot_squad/dev --no-ff
# IN cwd (the dev clone, project.repo_path = /home/almdudleer/watchrobot/dev).
# That was wrong on two counts:
#   1. It SWITCHED the dev clone's branch. That clone is a fixed-branch
#      editing surface for the agent team — it must stay on bot_squad/dev and
#      never be checked out to another branch by the deploy queue. (Operator
#      requirement, 2026-06-08.)
#   2. The local `staging` branch in this clone had diverged from bot_squad/dev
#      by 686 unpushed commits (chat_tools.py +63, portfolio-radar, …), so the
#      merge conflicted on every deploy (rc=2) and wedged the whole staging
#      queue. The merge also silently dragged that staging-only divergence into
#      staging builds.
# Since staging just mirrors bot_squad/dev, we drop the branch dance entirely
# and build cwd in place. The worker's pre-run guards already ensure cwd is a
# CLEAN checkout (deploy._is_clean) at the latest PUSHED commit
# (deploy._local_only_commits refuses unpushed work) before this runs.
#
# Durable fix (filed as user feedback to bot-squad): give watchrobot a
# `repo_deploy` entry in config/projects.toml so the worker runs this recipe in
# a disposable, origin-synced deploy clone (T-0143) — that also removes the
# dev-tree-dirtiness gate. Until that lands + a worker restart, this in-place
# recipe is the safe interim that, critically, never mutates the dev clone's
# branch.
set -euo pipefail

REPO="$(pwd)"
DEPLOY_BRANCH="bot_squad/dev"

# Hard guard: never switch the dev clone's branch. If cwd is somehow not on
# bot_squad/dev, refuse rather than risk building/leaving the wrong branch.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "$DEPLOY_BRANCH" ]; then
    echo "[staging] FATAL: expected the dev clone on '$DEPLOY_BRANCH', got '$BRANCH'." >&2
    echo "[staging] This recipe never switches branches (T-0202); aborting so the clone is left untouched." >&2
    exit 3
fi
echo "[staging] building in place: $REPO @ $(git rev-parse --short HEAD) ($BRANCH)"

# Workaround for the bot-squad worker running with a read-only /tmp: docker
# compose writes a transient metadata file at the end of `build` and crashes
# the recipe with `read-only file system`. Redirect TMPDIR into the repo (which
# IS writable) so the build exits cleanly and the recipe reaches `up -d`.
export TMPDIR="$REPO/.tmp-deploy"
mkdir -p "$TMPDIR"

docker compose build signal-tracker-staging
# Force-remove any existing container with this name (--force-recreate alone
# collides when container_name is fixed). Then recreate from the new image.
docker rm -f signal-tracker-staging 2>/dev/null || true
docker compose up -d signal-tracker-staging

# Smoke with retry. After a container recreate, traefik label discovery + LE
# challenge + app cold-start on this loaded host can take >60s before
# /api/version answers — a fixed `sleep 5; curl` false-fails with a 502 during
# warmup (observed 2026-06-08, curl rc=22) even though the deploy succeeded, and
# the original ~31s backoff window still false-failed on busy days (2026-06-11).
# Poll ~90s before declaring failure, matching prod.sh. (T-0203)
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
        echo "[staging] stt sidecar healthy (model resident)"
        break
    fi
    sleep 3
done
if [ "$stt_ok" -ne 1 ]; then
    echo "[staging] FATAL: stt sidecar never became healthy within ~120s — the Telegram bot's voice path would be dead on this target" >&2
    exit 23
fi

smoke_ok=0
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null https://signal-staging.dev.uzinvestapi.com/api/version; then
        smoke_ok=1
        echo "[staging] smoke /api/version OK"
        break
    fi
    sleep 3
done
if [ "$smoke_ok" -ne 1 ]; then
    echo "[staging] FATAL: /api/version never answered within ~90s of recreate" >&2
    exit 22
fi

echo "[staging] release deployed: $(git rev-parse --short HEAD)"
