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
#   T   THIS script with an unwritable TMPDIR -> refuses at the mktemp with
#                                               rc 91, never reaches a case, so
#                                               nothing is written to / (T-0675)
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

# A failed `mktemp -d` is a REFUSAL here, not a continuation (T-0675; same form
# and same rc as the guard T-0655 put on prod-rollback-selftest.sh).
#
# `set -u` does not catch this and never could: the variable IS assigned — to
# the empty string. And this script is deliberately without `set -e`, so the
# failure carried straight on. `cat > "$TMP/compose.yml"` then becomes
# `cat > /compose.yml`: a write into the FILESYSTEM ROOT of whatever box this
# runs on, and `docker compose -f /compose.yml` reads it back from there.
#
# Measured before this guard existed, with `mktemp` shimmed to fail and the run
# confined to a bwrap sandbox whose / was a scratch dir: the script ran to
# completion (9 passed, 11 failed) and left /compose.yml at the sandbox root.
# Under uid 1000 on a real box that write is Permission denied and the suite
# merely reddens, which is exactly why it survived; run once as root and it
# lands in /.
TMP="$(mktemp -d -t wr-shared-container-selftest-XXXXXX)"
if [ -z "${TMP:-}" ] || [ ! -d "$TMP" ]; then
    echo "FATAL: mktemp -d produced no usable directory (TMP='${TMP:-}', TMPDIR='${TMPDIR:-<unset>}')." >&2
    echo "       Refusing to run. The compose file here is written as \"\$TMP/compose.yml\", so an" >&2
    echo "       empty TMP makes that path absolute (/compose.yml) and this selftest would write" >&2
    echo "       into the filesystem root." >&2
    echo "       A deploy namespace mounts / read-only, so /tmp is not writable there — set TMPDIR" >&2
    echo "       to a writable directory and re-run." >&2
    exit 91
fi

# The cleanup gets the same question asked of it. `rm -rf ""` is harmless today
# — and that is the problem: a silent no-op is indistinguishable from a cleanup
# that worked. It says which one happened instead, and it does not hand an
# absolute /compose.yml to `docker compose` on the way out either.
cleanup() {
    if [ -n "${TMP:-}" ] && [ -d "$TMP" ]; then
        docker compose -p "$OURS" -f "$TMP/compose.yml" down -t 1 >/dev/null 2>&1
        docker rm -f "$C" >/dev/null 2>&1
        rm -rf "$TMP"
    else
        docker rm -f "$C" >/dev/null 2>&1
        echo "WARN: no temp-dir cleanup performed — TMP was '${TMP:-}', which is not a directory." >&2
    fi
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


# ── T: an unwritable TMPDIR refuses instead of writing /compose.yml (T-0675) ─
# The guard at the top of this file is the only thing between a failed
# `mktemp -d` and a write into the filesystem root, and a guard that has never
# been made to fire is an untested claim. So: run THIS script again with TMPDIR
# unwritable. The child must refuse with the guard's OWN rc, name the reason,
# and never reach the docker preflight. It exits at the guard, so it cannot
# recurse; WR_SCS_NO_RECURSE is the backstop for the day the guard regresses.
#
# The sentinel is deliberately NOT the name the other two selftests use: one
# shared mute switch would silently skip this case in every file the day
# anything exports it.
#
# "any non-zero rc" would be VACUOUS here — a child that ran the whole suite and
# merely failed it also exits non-zero. It must be 91, the guard's own code.
if [ -z "${WR_SCS_NO_RECURSE:-}" ]; then
    T_OUT="$(env TMPDIR=/proc/nonexistent WR_SCS_NO_RECURSE=1 \
                 bash "${BASH_SOURCE[0]}" 2>&1)"
    T_RC=$?
    [ "$T_RC" = "91" ] \
        && ok "T unwritable TMPDIR: refused with the mktemp guard's own rc=91" \
        || { bad "T: expected rc 91 from the mktemp guard, got $T_RC"; sed 's/^/      | /' <<<"$T_OUT" | tail -10; }
    grep -qF 'mktemp -d produced no usable directory' <<<"$T_OUT" \
        && ok "T2 the refusal names the failed mktemp" \
        || bad "T2: refused without naming the reason"
    grep -qF '== shared-container.sh selftest ==' <<<"$T_OUT" \
        && bad "T3: the child started the case list anyway — the guard let it through" \
        || ok "T3 the child never reached a single case (nothing was written)"
fi

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
