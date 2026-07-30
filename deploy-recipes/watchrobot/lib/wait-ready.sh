#!/usr/bin/env bash
# watchrobot deploy readiness gate (T-0427)
#
# WHY THIS FILE EXISTS, because the thing it replaces looked fine:
# staging.sh and prod.sh each carried their own copy of
#
#     for _ in $(seq 1 30); do curl -fsS <public-url>/api/version && break; sleep 3; done
#     [ "$smoke_ok" -ne 1 ] && echo FATAL && exit 22
#
# and that gate has now false-FATAL'd three times (2026-06-08, 2026-06-11,
# 2026-07-30). The third is the measured one: ten consecutive 502s, then
# `[staging] FATAL: /api/version never answered within ~90s of recreate`, exit
# 22 — while staging was in fact HEALTHY. Re-probed one minute later it answered
# 200 internally in 0.003s and 200 externally in 0.023s, with "Application
# startup complete" in its log. The deploy's wall clock was 37m19s against a
# ~246s cached-build estimate, at load average 67, with three concurrent docker
# builds from three different projects sharing one daemon.
#
# So the defect is NOT that 90 is too small a number. It is that the gate had
#   (a) no way to tell "not warm yet" from "dead", so it had to guess with a
#       timeout, and
#   (b) a positive signal — a public HTTPS URL — that folds the app's cold
#       start together with traefik label discovery and the LE challenge, three
#       independent latencies behind one constant measured on an idle box.
# Raising 90 to 300 would keep both and is the same defect with a bigger
# constant. This gate replaces the guess with two real signals:
#
#   POSITIVE: the app answering INSIDE its own container (uvicorn on :8000).
#     That removes traefik and LE from the readiness question entirely — they
#     get their own gate, so a routing failure is reported as a routing
#     failure instead of as the app.
#   NEGATIVE: the container's own state. A genuinely dead app — ImportError,
#     a bad migration, a crash on boot — does not sit at 502. It EXITS, and
#     docker records that as `State.Running=false` or as a rising
#     `RestartCount` under `restart: unless-stopped`. That case now FATALs
#     within one poll interval instead of waiting the whole window out.
#
# Only "container alive, not answering yet" consumes the timeout, and THAT is
# why the window can be generous: it is no longer what detects failure, it is
# only what bounds a hang. Negative-tested by lib/wait-ready-selftest.sh
# against a container that exits immediately, a container that stays up and
# never serves, and a container that does not exist — run it after any edit
# here. A gate loosened until it always passes is worse than no gate.
#
# Every progress line carries elapsed seconds AND the load average, so nobody
# sizes the next window off an idle-box figure again (T-0427 DoD). Those lines
# are also load-bearing for the deploy worker: it kills a run whose log has
# produced no bytes for BOT_SQUAD_DEPLOY_NO_PROGRESS_SECONDS (default 600s), so
# a gate that can now wait longer than that MUST keep talking. Do not silence
# them.
#
# Usage:
#   wait-ready.sh container <container> <url-inside-container> <timeout_s> <label>
#   wait-ready.sh public    <url> <timeout_s> <label>
#
# Exit codes (the caller maps these onto its own deploy exit codes):
#   0   ready
#   10  timed out: the target stayed alive but never answered within timeout_s
#   11  fast fail: the container is absent, stopped, or restarting (dead app)
#   64  usage error
set -euo pipefail

POLL_S="${WAIT_READY_POLL_SECONDS:-3}"
TICK_S="${WAIT_READY_TICK_SECONDS:-30}"

_load() { cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo "load-unknown"; }

# `[<label>] ...` on stdout so it interleaves with the recipe's own echoes.
_say() { echo "[$LABEL] $*"; }

# Probe a URL from INSIDE the container. Both images ship python; `stt` has no
# curl (that is why this is python and not curl), so one mechanism covers both.
_probe_container() {
    docker exec "$TARGET" python -c \
        'import sys,urllib.request; sys.exit(0 if urllib.request.urlopen(sys.argv[1], timeout=5).status == 200 else 1)' \
        "$URL" >/dev/null 2>&1
}

# Three fields in one call so the reading is atomic-ish: Running, RestartCount,
# ExitCode. A missing container makes `docker inspect` fail, which is itself the
# answer we want.
_container_state() {
    docker inspect -f '{{.State.Running}} {{.RestartCount}} {{.State.ExitCode}}' "$TARGET" 2>/dev/null
}

_wait_container() {
    local baseline state running restarts exitcode
    if ! state=$(_container_state); then
        _say "FATAL: container '$TARGET' does not exist — nothing to wait for."
        return 11
    fi
    baseline=$(awk '{print $2}' <<<"$state")
    _say "waiting for $TARGET to answer $URL (up to ${TIMEOUT_S}s); load $(_load), RestartCount baseline $baseline"

    local start now elapsed last_tick=0
    start=$SECONDS
    while :; do
        if _probe_container; then
            elapsed=$((SECONDS - start))
            _say "READY after ${elapsed}s — 200 from $URL inside $TARGET; load $(_load)"
            return 0
        fi

        # Negative signal BEFORE the timeout check: a dead app must not be able
        # to spend the whole window looking like a slow one.
        if ! state=$(_container_state); then
            _say "FATAL: container '$TARGET' disappeared while waiting (removed mid-deploy?)"
            return 11
        fi
        running=$(awk '{print $1}' <<<"$state")
        restarts=$(awk '{print $2}' <<<"$state")
        exitcode=$(awk '{print $3}' <<<"$state")
        if [ "$running" != "true" ]; then
            _say "FATAL: container '$TARGET' is NOT running (exit code $exitcode) — it died on boot, it is not warming up."
            _say "       last 20 log lines follow:"
            docker logs --tail 20 "$TARGET" 2>&1 | sed "s/^/[$LABEL]        /" || true
            return 11
        fi
        if [ "$restarts" -gt "$baseline" ]; then
            _say "FATAL: container '$TARGET' RestartCount rose $baseline -> $restarts — it is crash-looping, not warming up."
            _say "       last 20 log lines follow:"
            docker logs --tail 20 "$TARGET" 2>&1 | sed "s/^/[$LABEL]        /" || true
            return 11
        fi

        now=$((SECONDS - start))
        if [ "$now" -ge "$TIMEOUT_S" ]; then
            _say "FATAL: $TARGET stayed alive but never answered $URL within ${now}s; load $(_load)"
            _say "       (alive-but-silent is a hang, not a cold start — the container never exited.)"
            _say "       last 20 log lines follow:"
            docker logs --tail 20 "$TARGET" 2>&1 | sed "s/^/[$LABEL]        /" || true
            return 10
        fi
        if [ $((now - last_tick)) -ge "$TICK_S" ]; then
            last_tick=$now
            _say "still warming: ${now}s/${TIMEOUT_S}s, container up, restarts $restarts, load $(_load)"
        fi
        sleep "$POLL_S"
    done
}

_wait_public() {
    _say "waiting for $URL to answer publicly (up to ${TIMEOUT_S}s); load $(_load)"
    local start now last_tick=0 elapsed
    start=$SECONDS
    while :; do
        if curl -fsS -o /dev/null --max-time 10 "$URL"; then
            elapsed=$((SECONDS - start))
            _say "READY after ${elapsed}s — public $URL answered; load $(_load)"
            return 0
        fi
        now=$((SECONDS - start))
        if [ "$now" -ge "$TIMEOUT_S" ]; then
            _say "FATAL: $URL did not answer publicly within ${now}s; load $(_load)"
            _say "       the app itself was already proven ready inside its container, so this is"
            _say "       the EDGE — traefik label discovery, the router, or the LE cert — not the app."
            return 10
        fi
        if [ $((now - last_tick)) -ge "$TICK_S" ]; then
            last_tick=$now
            _say "still unrouted: ${now}s/${TIMEOUT_S}s, load $(_load)"
        fi
        sleep "$POLL_S"
    done
}

case "${1:-}" in
    container)
        [ "$#" -eq 5 ] || { echo "usage: wait-ready.sh container <container> <url> <timeout_s> <label>" >&2; exit 64; }
        TARGET="$2"; URL="$3"; TIMEOUT_S="$4"; LABEL="$5"
        _wait_container
        ;;
    public)
        [ "$#" -eq 4 ] || { echo "usage: wait-ready.sh public <url> <timeout_s> <label>" >&2; exit 64; }
        URL="$2"; TIMEOUT_S="$3"; LABEL="$4"
        _wait_public
        ;;
    *)
        cat >&2 <<'EOF'
usage:
  wait-ready.sh container <container> <url-inside-container> <timeout_s> <label>
  wait-ready.sh public    <url> <timeout_s> <label>
exit codes: 0 ready · 10 timed out (alive but silent) · 11 dead container · 64 usage
EOF
        exit 64
        ;;
esac
