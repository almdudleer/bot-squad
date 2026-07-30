#!/usr/bin/env bash
# NEGATIVE TEST for lib/shared-container.sh (T-0427).
#
# The defect this guards against is one that PASSED review and passed a live
# deploy, because the check that was supposed to catch it (an /health probe)
# was satisfied by a container belonging to somebody else's compose project.
# So a guard here that has only ever been watched succeed would be worth
# nothing. Every case below is driven by a REAL container in the REAL docker
# daemon, and the headline case (E) reproduces the verbatim daemon error from
# the failed deploy rather than simulating it:
#
#     Error response from daemon: Conflict. The container name "/..." is
#     already in use by container "..."
#
#   R1  reclaim, no such container            -> rc 0, no-op
#   R2  reclaim, foreign compose project      -> rc 0 AND the container is gone
#   R3  reclaim, correct compose project      -> rc 0 AND the SAME container id
#                                               is still running (the "a normal
#                                               deploy costs no voice gap" claim,
#                                               asserted by id, not by existence)
#   R4  reclaim, no compose label at all      -> rc 0, removed, and the log names
#                                               it <not compose-managed> rather
#                                               than pretending it is project ''
#   O1  assert-owner, no such container       -> rc 14
#   O2  assert-owner, foreign project, RUNNING and answering
#                                             -> rc 13   <-- THE defect. A
#                                               healthy, responsive container
#                                               must still fail this gate.
#   O3  assert-owner, correct project         -> rc 0
#   O4  assert-owner, correct project, exited -> rc 15
#   O5  usage error                           -> rc 64
#   E   END-TO-END on the daemon: a foreign-owned container of the target name
#       makes a real `docker compose -p <ours> up -d` fail with the verbatim
#       Conflict; reclaim then makes the same command succeed; assert-owner then
#       passes. This is the whole bug and the whole fix, in one case.
#
# Uses scratch names only (wr-t0427-sc-$$), never the real `stt` container, and
# `alpine` which is already present on this box — it builds nothing, pulls
# nothing, and deploys nothing. It is nonetheless NOT fast: every case is a real
# container create/remove against a daemon this box shares between several
# projects, so it runs in ~1-4 min depending on load. Do not "fix" that by
# replacing the containers with mocks; driving the real daemon is the entire
# reason this file has any authority.
#
# Sabotage-tested 2026-07-30 — the suite was watched FAILING, not just passing,
# against three broken copies of the lib:
#   ownership check disabled in assert-owner  -> O2, O2b red (the regression that
#                                                turns the gate back into a
#                                                liveness check)
#   reclaim always removes                    -> R3b, E5 red (it would blink the
#                                                shared singleton every deploy)
#   reclaim never removes                     -> R2b, R4b, E3, E4 red
# Re-run that exercise on a copy in a scratch dir after any edit here; never
# sabotage the tracked file in place, this is a shared clone.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SC="$HERE/shared-container.sh"

C="wr-t0427-sc-$$"          # scratch container name under test
OURS="wr-t0427-ours-$$"     # the pinned ("our") compose project
THEIRS="wr-t0427-theirs-$$" # a foreign compose project
TMP="$(mktemp -d -t wr-shared-container-selftest-XXXXXX)"

cleanup() {
    docker compose -p "$OURS" -f "$TMP/compose.yml" down -t 1 >/dev/null 2>&1
    docker rm -f "$C" >/dev/null 2>&1
    rm -rf "$TMP"
}
trap cleanup EXIT

pass=0
fail=0

ok()   { echo "ok   $1"; pass=$((pass + 1)); }
bad()  { echo "FAIL $1"; fail=$((fail + 1)); }

# Create the scratch container with an explicit compose-project label.
# `--label` with no value at all reproduces a bare `docker run` (no label key).
mkcontainer() { # mkcontainer <project-or-empty>
    docker rm -f "$C" >/dev/null 2>&1
    if [ -z "$1" ]; then
        docker run -d --name "$C" alpine sleep 600 >/dev/null
    else
        docker run -d --name "$C" \
            --label "com.docker.compose.project=$1" \
            --label "com.docker.compose.service=scratch" \
            alpine sleep 600 >/dev/null
    fi
}

# run <expected_rc> <name> <args...>  — asserts rc and stashes output in $OUT
OUT=""
run() {
    local want="$1" name="$2"; shift 2
    OUT=$(bash "$SC" "$@" 2>&1)
    local rc=$?
    if [ "$rc" -eq "$want" ]; then
        ok "$name (rc=$rc)"
        return 0
    fi
    bad "$name: expected rc=$want, got rc=$rc"
    sed 's/^/      | /' <<<"$OUT" | tail -10
    return 1
}

echo "== shared-container.sh selftest =="
echo "lib: $SC"
[ -x "$SC" ] || { echo "FAIL: $SC is not executable"; exit 1; }
docker info >/dev/null 2>&1 || { echo "FAIL: no usable docker daemon"; exit 1; }
# Refuse to run if the scratch name is somehow taken by something real.
if [ "$C" = "stt" ]; then echo "FAIL: scratch name collided with the real singleton"; exit 1; fi

# ── R1: nothing to reclaim ──────────────────────────────────────────────────
docker rm -f "$C" >/dev/null 2>&1
run 0 "R1 reclaim of an absent container is a no-op" reclaim "$C" "$OURS" \
    && { grep -q 'absent' <<<"$OUT" || bad "R1b: did not say the container was absent"; }

# ── R2: the migration case — a foreign owner is removed ─────────────────────
mkcontainer "$THEIRS"
run 0 "R2 reclaim removes a foreign-owned container" reclaim "$C" "$OURS"
if docker container inspect "$C" >/dev/null 2>&1; then
    bad "R2b: the foreign container SURVIVED reclaim — the name is still held"
else
    ok "R2b foreign container is gone"
fi

# ── R3: the everyday case — ours is left strictly alone ─────────────────────
# Asserted by container id, because "a container of that name exists" would
# also be true if reclaim had destroyed and something else had recreated it.
mkcontainer "$OURS"
before_id=$(docker inspect -f '{{.Id}}' "$C")
run 0 "R3 reclaim leaves a correctly-owned container alone" reclaim "$C" "$OURS"
after_id=$(docker inspect -f '{{.Id}}' "$C" 2>/dev/null || echo MISSING)
if [ "$before_id" = "$after_id" ]; then
    ok "R3b same container id survived (no needless restart, no voice gap)"
else
    bad "R3b: container id changed ($before_id -> $after_id) — a normal deploy would blink the shared singleton"
fi

# ── R4: started outside compose entirely ────────────────────────────────────
mkcontainer ""
run 0 "R4 reclaim removes a container with no compose label" reclaim "$C" "$OURS"
if docker container inspect "$C" >/dev/null 2>&1; then
    bad "R4b: an unlabelled container survived reclaim"
else
    ok "R4b unlabelled container is gone"
fi
if grep -q 'not compose-managed' <<<"$OUT"; then
    ok "R4c log named it <not compose-managed> rather than project ''"
else
    bad "R4c: log did not distinguish 'no label' from 'empty project name'"
    sed 's/^/      | /' <<<"$OUT"
fi

# ── O1: absent ──────────────────────────────────────────────────────────────
docker rm -f "$C" >/dev/null 2>&1
run 14 "O1 assert-owner on an absent container fails 14" assert-owner "$C" "$OURS"

# ── O2: THE DEFECT. Running, healthy, answering — and still not ours ────────
# The container is deliberately alive and responsive here: this is the exact
# state that let the real defect pass review for ten hours. If this case ever
# returns 0, the gate has been reduced to a health check and is useless.
mkcontainer "$THEIRS"
if docker exec "$C" true >/dev/null 2>&1; then
    ok "O2a the foreign container is genuinely alive and responsive"
else
    bad "O2a: the fixture container is not responsive — O2 would prove nothing"
fi
run 13 "O2 assert-owner FAILS 13 on a healthy container owned by another project" \
    assert-owner "$C" "$OURS"
if grep -qi "proves nothing\|expected '$OURS'" <<<"$OUT"; then
    ok "O2b the failure names the wrong owner"
else
    bad "O2b: failure output did not identify the ownership problem"
fi

# ── O3: ours and running ────────────────────────────────────────────────────
mkcontainer "$OURS"
run 0 "O3 assert-owner passes for our own running container" assert-owner "$C" "$OURS"

# ── O4: ours, but stopped ───────────────────────────────────────────────────
docker stop -t 1 "$C" >/dev/null 2>&1
run 15 "O4 assert-owner fails 15 when our container is not running" assert-owner "$C" "$OURS"

# ── O5: usage ───────────────────────────────────────────────────────────────
run 64 "O5 missing arguments is a usage error" assert-owner "$C"

# ── E: the real daemon conflict, end to end ─────────────────────────────────
# Everything above drives the lib directly. This case drives `docker compose`
# itself, so the thing being fixed is the actual observed failure and not our
# model of it.
docker rm -f "$C" >/dev/null 2>&1
cat > "$TMP/compose.yml" <<YAML
services:
  scratch:
    image: alpine
    container_name: $C
    command: sleep 600
YAML

mkcontainer "$THEIRS"   # a foreign project holds the name, as project `dev` did

E_OUT=$(docker compose -p "$OURS" -f "$TMP/compose.yml" up -d 2>&1)
E_RC=$?
if [ "$E_RC" -ne 0 ] && grep -qi 'is already in use by container' <<<"$E_OUT"; then
    ok "E1 reproduced the deploy's verbatim daemon error (rc=$E_RC, 'already in use by container')"
else
    bad "E1: expected compose to fail with a name Conflict, got rc=$E_RC"
    sed 's/^/      | /' <<<"$E_OUT" | tail -10
fi

run 0 "E2 reclaim clears the conflict" reclaim "$C" "$OURS"

E_OUT=$(docker compose -p "$OURS" -f "$TMP/compose.yml" up -d 2>&1)
E_RC=$?
if [ "$E_RC" -eq 0 ]; then
    ok "E3 the same compose up -d now succeeds"
else
    bad "E3: compose up -d still failed after reclaim (rc=$E_RC)"
    sed 's/^/      | /' <<<"$E_OUT" | tail -10
fi

run 0 "E4 assert-owner now passes against the real compose-created container" \
    assert-owner "$C" "$OURS"

# And the idempotence claim the recipe leans on: a SECOND run changes nothing.
before_id=$(docker inspect -f '{{.Id}}' "$C" 2>/dev/null || echo MISSING)
bash "$SC" reclaim "$C" "$OURS" >/dev/null 2>&1
docker compose -p "$OURS" -f "$TMP/compose.yml" up -d >/dev/null 2>&1
after_id=$(docker inspect -f '{{.Id}}' "$C" 2>/dev/null || echo MISSING)
if [ "$before_id" = "$after_id" ] && [ "$after_id" != "MISSING" ]; then
    ok "E5 a repeat run is a true no-op (same container id)"
else
    bad "E5: a repeat run recreated the container ($before_id -> $after_id)"
fi

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
