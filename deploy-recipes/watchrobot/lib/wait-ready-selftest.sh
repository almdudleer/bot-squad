#!/usr/bin/env bash
# NEGATIVE TEST for lib/wait-ready.sh (T-0427 DoD).
#
# The gate this replaces was loosened twice already (a ~31s window in 2026-06-08
# became ~90s in 2026-06-11) and false-failed anyway. The risk in loosening a
# gate a third time is the opposite failure: a gate that has been relaxed until
# it always passes, which is worse than no gate — it converts every future
# broken deploy into a green one. So the loosened gate is only trustworthy with
# a test that makes it FAIL on purpose, and the interesting cases are the
# negative ones:
#
#   A  healthy container, answers            -> 0
#   B  container that EXITS immediately      -> 11, and FAST (this is the whole
#                                              point: a dead app must not be
#                                              able to spend the window looking
#                                              like a slow one)
#   C  container up but nothing listening    -> 10 at the timeout (a hang IS
#                                              only detectable by waiting)
#   D  container that does not exist         -> 11 immediately
#   E  crash-looping container               -> 11 via RestartCount
#   F  public URL that cannot resolve        -> 10 at the timeout
#   G  a real 404 (reachable but not 200)    -> 10, i.e. "answers" means 200
#
# Every container is created with an explicit unique name and removed by name in
# the trap — nothing here matches a pattern, because other sessions share this
# docker daemon.
#
# Run it after ANY edit to wait-ready.sh:
#     bash deploy-recipes/watchrobot/lib/wait-ready-selftest.sh
# It needs ~90s and it builds nothing.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/wait-ready.sh"
# The staging app image: local, has python, and is the same image family the
# gate runs against for real. Commands are overridden and NO env/network is
# passed, so none of these containers can reach a database.
IMG="${SELFTEST_IMAGE:-deploy-signal-tracker-staging:latest}"
PFX="wr-waitready-selftest-$$"

pass=0
fail=0

cleanup() {
    for n in "$PFX-healthy" "$PFX-exits" "$PFX-silent" "$PFX-crashloop"; do
        docker rm -f "$n" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT

# assert <case> <expected_rc> <max_elapsed_s|-> <cmd...>
assert() {
    local name="$1" want="$2" max="$3"; shift 3
    local t0 rc elapsed
    t0=$SECONDS
    "$@" >/tmp/.wrsel.$$ 2>&1
    rc=$?
    elapsed=$((SECONDS - t0))
    if [ "$rc" -ne "$want" ]; then
        echo "FAIL $name: expected rc=$want, got rc=$rc after ${elapsed}s"
        sed 's/^/      | /' /tmp/.wrsel.$$
        fail=$((fail + 1))
        return
    fi
    if [ "$max" != "-" ] && [ "$elapsed" -gt "$max" ]; then
        echo "FAIL $name: rc=$rc correct but took ${elapsed}s, expected <=${max}s"
        echo "      a dead target that consumes the whole window is the defect this gate exists to fix"
        sed 's/^/      | /' /tmp/.wrsel.$$
        fail=$((fail + 1))
        return
    fi
    echo "ok   $name (rc=$rc, ${elapsed}s)"
    pass=$((pass + 1))
}

echo "== wait-ready.sh selftest =="
echo "gate:  $GATE"
echo "image: $IMG"
echo "load:  $(cut -d' ' -f1-3 /proc/loadavg)"
[ -x "$GATE" ] || { echo "FAIL: $GATE is not executable"; exit 1; }

# ── A. healthy ───────────────────────────────────────────────────────────────
docker rm -f "$PFX-healthy" >/dev/null 2>&1 || true
docker run -d --name "$PFX-healthy" --entrypoint sh "$IMG" \
    -c 'cd /tmp && python -m http.server 8099' >/dev/null
assert "A healthy container answers" 0 60 \
    bash "$GATE" container "$PFX-healthy" http://localhost:8099 60 selftest-A

# ── B. exits immediately — MUST fail fast, not wait out the window ───────────
# No restart policy: the container reaches Running=false and stays there, which
# is the ImportError / bad-migration shape.
docker rm -f "$PFX-exits" >/dev/null 2>&1 || true
docker run -d --name "$PFX-exits" --entrypoint sh "$IMG" -c 'exit 1' >/dev/null
sleep 2
assert "B dead container FATALs fast (window 120s)" 11 20 \
    bash "$GATE" container "$PFX-exits" http://localhost:8099 120 selftest-B

# ── C. alive but nothing listening — the timeout still has a job ─────────────
docker rm -f "$PFX-silent" >/dev/null 2>&1 || true
docker run -d --name "$PFX-silent" --entrypoint sh "$IMG" -c 'sleep 600' >/dev/null
assert "C alive-but-silent times out" 10 30 \
    bash "$GATE" container "$PFX-silent" http://localhost:8099 10 selftest-C

# ── D. no such container ─────────────────────────────────────────────────────
assert "D absent container FATALs at once" 11 10 \
    bash "$GATE" container "$PFX-does-not-exist" http://localhost:8099 120 selftest-D

# ── E. crash loop: Running is true most of the time, RestartCount is the tell ─
docker rm -f "$PFX-crashloop" >/dev/null 2>&1 || true
docker run -d --name "$PFX-crashloop" --restart=always --entrypoint sh "$IMG" \
    -c 'sleep 3; exit 1' >/dev/null
assert "E crash-looping container FATALs (window 120s)" 11 60 \
    bash "$GATE" container "$PFX-crashloop" http://localhost:8099 120 selftest-E
docker update --restart=no "$PFX-crashloop" >/dev/null 2>&1 || true

# ── F. public leg: unreachable host ──────────────────────────────────────────
assert "F unreachable public URL times out" 10 30 \
    bash "$GATE" public "https://wr-selftest.invalid/api/version" 8 selftest-F

# ── G. reachable but not 200 — "answered" must mean 200, not "connected" ─────
# Note the /api/ prefix. A non-/api path is served by the SPA catch-all and
# returns 200 even when the backend is dead (measured: `/__wr_selftest_404__`
# -> 200, `/api/__wr_selftest_404__` -> 404), which is exactly why the public
# gate probes /api/version and not /.
# T-1079 (2026-09-08): signal-staging.dev.uzinvestapi.com is retired — any live
# reachable host works here (the assertion is about the 404, not the hostname),
# so this now uses the kept staging alias to stay reachable once traefik drops
# the old name (a different ticket step, not yet done as of this edit).
assert "G public 404 is not READY" 10 30 \
    bash "$GATE" public "https://watchrobot-dev.nolim.finance/api/__wr_selftest_404__" 8 selftest-G

# ── usage errors ─────────────────────────────────────────────────────────────
assert "H no args is a usage error" 64 10 bash "$GATE"
assert "I wrong arity is a usage error" 64 10 bash "$GATE" container onlyone

rm -f /tmp/.wrsel.$$
echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
