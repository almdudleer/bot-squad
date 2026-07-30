#!/usr/bin/env bash
# watchrobot shared-singleton container ownership (T-0427)
#
# WHY THIS FILE EXISTS, because the thing it replaces looked like it worked for
# ten hours:
#
# `stt` is a deliberate SINGLETON — one instance serves prod AND staging (see
# the block comment on the `stt` service in docker-compose.yml: ~36 s model
# load, ~1 GB resident, duplicating it buys no isolation at v1 volume). It gets
# that singleton-ness from `container_name: stt`, a name that is global to the
# docker daemon.
#
# But the compose PROJECT it belongs to is not global: compose derives it from
# the cwd basename, and this box has three clones of the same repo —
# /home/almdudleer/watchrobot/{dev,deploy,master} — so the same
# docker-compose.yml is three different projects called `dev`, `deploy` and
# `master`. A container may only be managed by the project that owns it.
#
# That is the whole defect. `docker compose up -d stt` from the deploy clone is
# project `deploy` asking for a container named `stt`; the daemon already has
# one owned by project `dev`; the daemon refuses:
#
#     Container stt  Creating
#     Error response from daemon: Conflict. The container name "/stt" is
#     already in use by container "e077a318a4a9"
#
# and the recipe dies at rc=1 before any gate runs. Measured 2026-07-30T20:07Z
# on the first supervised staging deploy that genuinely had to create it.
#
# WHY IT HID FOR SO LONG, and this is the part worth internalising: the block
# was reviewed, and the reviewer checked that `stt` ANSWERED from inside the
# newly built app container. It did. But the thing answering was the
# `dev`-project container replying over the shared `avo_backend` network. The
# health check passed for a reason unrelated to what it claimed to test, and it
# would have passed identically if the deploy's own stt block had never existed.
# A health probe cannot see ownership. Hence `assert-owner` below, which is the
# gate the reviewer needed and did not have.
#
# THE FIX is the one this repo already applied one field over. docker-compose.yml
# (T-0082) pins the shared VOLUME's name explicitly, precisely because "the
# default project-name prefix (derived from the cwd basename) would otherwise
# split this into unrelated volumes per clone". The singleton container has the
# same shape and gets the same treatment at the project level: every recipe runs
# stt's actions under one explicit `-p <project>`, from whichever clone it
# happens to execute in. All three clones then address the same compose project,
# and `up -d` is idempotent from anywhere — no-op when it is already running,
# create when absent, recreate when the image changed, never a name conflict.
#
# Rejected: making each recipe `docker rm -f stt` first. That does remove the
# conflict, by taking the OTHER environment's voice path down on every single
# deploy. Rejected: treating stt as external and never creating it — that is
# exactly the state T-0390 existed to end (it survived only because a human
# started it by hand and `restart: unless-stopped` kept it there, so the voice
# half worked on the machine it was built on and was absent everywhere else).
#
# Usage:
#   shared-container.sh reclaim      <container> <expected-project>
#   shared-container.sh assert-owner <container> <expected-project>
#
# `reclaim` is a PHASE-1 ACTION: if a container of that name exists and is NOT
# owned by <expected-project>, remove it so the pinned project can create its
# own. Deliberately narrow — it removes ONLY on a label mismatch, so a normal
# deploy (label already correct) never touches the running singleton and never
# costs a voice-path gap. It is the migration path for the container that is
# mislabelled today AND the self-heal for any future stray hand-start.
#
# `assert-owner` is a PHASE-2 GATE and it is the point of the file. It answers
# the question a 200 from /health cannot: is the container serving this name the
# one THIS deploy's compose project owns and can manage?
#
# Exit codes (the caller maps these onto its own deploy exit codes):
#   0   reclaim: nothing needed, or the foreign container was removed
#       assert-owner: the container exists, is running, and is ours
#   12  reclaim: a foreign container exists and could NOT be removed
#   13  assert-owner: the container exists but belongs to another compose
#       project — this is the defect above, and the only signal that catches it
#   14  assert-owner: no container of that name exists at all
#   15  assert-owner: the container is ours but is not running
#   64  usage error
#
# Negative-tested by lib/shared-container-selftest.sh, which builds the real
# conflict (a scratch container of the target name labelled with a foreign
# compose project) and asserts each code. Run it after any edit here. A guard
# nobody has watched fail is not a guard.
set -uo pipefail

LBL="com.docker.compose.project"

usage() {
    echo "usage: shared-container.sh {reclaim|assert-owner} <container> <expected-project>" >&2
    exit 64
}

# Prints the container's compose-project label, or nothing.
# Empty output is ambiguous on its own (absent container vs. a container started
# by bare `docker run`, which has no such label), so callers check existence
# separately rather than reading meaning into an empty string.
_project_of() {
    docker inspect -f "{{index .Config.Labels \"$LBL\"}}" "$1" 2>/dev/null
}

_exists() { docker container inspect "$1" >/dev/null 2>&1; }

_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

# Describe an owner for humans. A container with no compose label at all is not
# "owned by ''" — it was started outside compose, and saying so is the
# difference between a diagnosable log line and a confusing one.
_owner_str() {
    local p="$1"
    if [ -z "$p" ]; then echo "<not compose-managed>"; else echo "$p"; fi
}

CMD="${1:-}"
CONTAINER="${2:-}"
WANT="${3:-}"
[ -n "$CMD" ] && [ -n "$CONTAINER" ] && [ -n "$WANT" ] || usage

case "$CMD" in

reclaim)
    if ! _exists "$CONTAINER"; then
        echo "[reclaim:$CONTAINER] absent — nothing to reclaim; compose project '$WANT' will create it."
        exit 0
    fi
    have="$(_project_of "$CONTAINER")"
    if [ "$have" = "$WANT" ]; then
        echo "[reclaim:$CONTAINER] already owned by compose project '$WANT' — left running, untouched."
        exit 0
    fi
    # The only branch that removes anything. Say WHY out loud, with both
    # project names, because this line is the one that will be read later when
    # someone asks why the voice path blinked.
    echo "[reclaim:$CONTAINER] OWNERSHIP CONFLICT: name '$CONTAINER' is held by compose project '$(_owner_str "$have")', but this deploy runs it as '$WANT'."
    echo "[reclaim:$CONTAINER] compose cannot manage a container it does not own, so 'up -d' would fail with 'Conflict. The container name /$CONTAINER is already in use'."
    echo "[reclaim:$CONTAINER] removing the foreign container so project '$WANT' can create and own it. This is a one-time gap for a shared singleton."
    if ! docker rm -f "$CONTAINER"; then
        echo "[reclaim:$CONTAINER] FATAL: could not remove the foreign container; the name is still held by '$(_owner_str "$have")'." >&2
        exit 12
    fi
    echo "[reclaim:$CONTAINER] removed; '$WANT' now has the name."
    exit 0
    ;;

assert-owner)
    if ! _exists "$CONTAINER"; then
        echo "[owner:$CONTAINER] FATAL: no container named '$CONTAINER' exists. The sidecar was never created." >&2
        exit 14
    fi
    have="$(_project_of "$CONTAINER")"
    if [ "$have" != "$WANT" ]; then
        echo "[owner:$CONTAINER] FATAL: owned by compose project '$(_owner_str "$have")', expected '$WANT'." >&2
        echo "[owner:$CONTAINER] This deploy did NOT create this container and cannot manage it. Anything reaching it over the shared network is talking to somebody else's instance — which is precisely why a 200 from its /health proves nothing here." >&2
        exit 13
    fi
    if ! _running "$CONTAINER"; then
        echo "[owner:$CONTAINER] FATAL: owned by '$WANT' but not running (state: $(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null))." >&2
        exit 15
    fi
    echo "[owner:$CONTAINER] OK: owned by compose project '$WANT' (id $(docker inspect -f '{{.Id}}' "$CONTAINER" 2>/dev/null | cut -c1-12)), running."
    exit 0
    ;;

*)
    usage
    ;;
esac
