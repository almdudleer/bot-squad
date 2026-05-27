#!/usr/bin/env bash
#
# smoke-with-backoff.sh — poll a URL until it returns 2xx or the budget
# elapses. Used by deploy recipes after `docker compose up` so the smoke
# check tolerates the seconds-of-warmup window where traefik picks up
# new labels, Let's Encrypt finishes its challenge, or the container's
# own readiness gate hasn't flipped yet.
#
# Usage:
#   smoke-with-backoff.sh <url>
#
# Schedule: pre-poll wait, then GET on attempts at t=1s, 2s, 4s, 8s, 16s
# from script start (5 attempts spread over ~31s). Happy path (already-
# healthy container) returns 200 on attempt #1 and exits in ~1s — no
# material slowdown vs. the legacy `sleep 5 + curl` pattern. Recreated
# container with traefik label discovery / LE challenge gets the full
# window.
#
# Exit codes: 0 on the first 2xx response; the last curl's rc otherwise.
# On success, prints which retry won to stdout so the deploy log carries
# the timing forensics — if 30s ever isn't enough we'll see it in the
# audit, not in a silent .fail.N.
#
# T-0079: replaces the fixed `sleep 5` smoke pattern that produced false
# `.fail.60` deploy-queue results after label-flip recreates.

set -uo pipefail

url="${1:-}"
if [ -z "$url" ]; then
    echo "usage: smoke-with-backoff.sh <url>" >&2
    exit 2
fi

# Backoff schedule (seconds between attempts). Tunable via env for tests.
# Default sums to ~31s wall time including curl overhead.
delays_csv="${BOT_SQUAD_SMOKE_DELAYS:-1,2,4,8,16}"
IFS=',' read -r -a delays <<< "$delays_csv"

# curl: -f makes HTTP 4xx/5xx exit non-zero, -s silences progress, -S
# shows the error on stderr if -s suppressed it, --max-time bounds a
# single attempt so a hung connection can't blow the whole budget.
curl_args=(-fsS --max-time 10)

last_rc=0
attempt=0
t_start="$(date +%s)"
for delay in "${delays[@]}"; do
    attempt=$((attempt + 1))
    sleep "$delay"
    curl "${curl_args[@]}" "$url" >/dev/null 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        elapsed=$(( $(date +%s) - t_start ))
        echo "[smoke] OK on attempt $attempt after ${elapsed}s ($url)"
        exit 0
    fi
    last_rc="$rc"
done

elapsed=$(( $(date +%s) - t_start ))
echo "[smoke] FAIL after $attempt attempts over ${elapsed}s ($url) — last curl rc=$last_rc" >&2
exit "$last_rc"
