#!/usr/bin/env bash
# NEGATIVE TEST for the rollback path added to prod.sh under T-0378.
#
# Why this exists at all. T-0378 was filed because a hardened prod recipe sat in
# a file the deploy runner never reads, for five weeks, while green deploys read
# as confirmation. The fix for that is not another hand-reviewed recipe: it is a
# test that makes the recovery path RUN and asserts what it did. Every case here
# drives the real prod.sh with `docker` and the gate libraries shimmed, so it
# builds nothing, starts nothing, and touches no container.
#
#   A  happy path                  -> rc 0, pins a rollback tag, never rolls back
#   B  `compose up -d` fails       -> rc 5,  ROLLS BACK, prod serving again
#   C  up fails AND rollback fails -> rc 27, says prod needs a human
#   D  app readiness gate fails    -> rc 22, ROLLS BACK
#   E  only the stt gate fails     -> rc 23, does NOT roll the app back
#   F  public-route gate fails     -> rc 24, ROLLS BACK
#   G  managed .env missing        -> rc 2,  docker is NEVER invoked
#   H  from-empty deploy + failure -> rc 27, refuses to claim a rollback
#   I  the pinned tag is the CONTAINER's image, not the `:latest` the build moves
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE="${WR_RECIPE_UNDER_TEST:-$HERE/prod.sh}"

if [ ! -f "$RECIPE" ]; then
    echo "FATAL: recipe under test not found: $RECIPE" >&2
    exit 90
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
_ok()   { PASS=$((PASS+1)); echo "  ok    $1"; }
_bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }

# ── the fake master clone ───────────────────────────────────────────────────
# A REAL git repo on a real branch with a real origin: the recipe does `git
# fetch` / `merge --ff-only` / `rev-parse` / `rev-list --count` and shimming git
# too would leave those paths untested for the sake of the ones we care about.
build_clone() {
    local root="$1"
    rm -rf "$root"; mkdir -p "$root/src"
    git init -q --bare "$root/origin.git"
    git init -q -b master "$root/src"
    cd "$root/src"
    git config user.email t@t; git config user.name t
    mkdir -p web; echo x > web/f.ts; echo y > Dockerfile
    git add -A >/dev/null; git commit -qm init
    git remote add origin "$root/origin.git"
    git push -q origin master 2>/dev/null
    git branch -q --set-upstream-to=origin/master master 2>/dev/null
    cd - >/dev/null
}

# ── the shims ───────────────────────────────────────────────────────────────
# `docker` records every invocation to $TRACE and takes its behaviour from env,
# so a case can make exactly one verb fail and leave the rest honest.
write_shims() {
    local bin="$1" lib="$2"
    mkdir -p "$bin" "$lib"

    cat > "$bin/docker" <<'SHIM'
#!/usr/bin/env bash
echo "docker $*" >> "$TRACE"
case "$1 $2" in
  "compose config")
      # `compose config --images signal-tracker`
      echo "master-signal-tracker"; exit 0 ;;
esac
case "$1" in
  inspect)
      # `inspect <name>` (existence) or `inspect -f '{{.Image}}' <name>`
      if [ "${SHIM_NO_RUNNING_CONTAINER:-0}" = "1" ]; then exit 1; fi
      if [ "$2" = "-f" ]; then echo "sha256:PREVIMAGEID"; fi
      exit 0 ;;
  tag|rmi)  exit 0 ;;
  rm)       exit 0 ;;
  images)   exit 0 ;;
  compose)
      # find the verb after any -p <project> / --project-name
      shift; verb=""
      while [ $# -gt 0 ]; do
          case "$1" in
            -p|--project-name) shift 2; continue ;;
            build|up|config|run) verb="$1"; break ;;
            *) shift ;;
          esac
      done
      case "$verb" in
        build) exit 0 ;;
        up)
            # is this the app or the stt sidecar?
            if printf '%s\n' "$@" | grep -q 'signal-tracker'; then
                UP_N="$(cat "$TRACE.upcount" 2>/dev/null || echo 0)"
                UP_N=$((UP_N+1)); echo "$UP_N" > "$TRACE.upcount"
                # first `up` = the deploy; any later one = the rollback
                if [ "$UP_N" = "1" ] && [ "${SHIM_UP_FAIL:-0}" = "1" ]; then exit 1; fi
                if [ "$UP_N" -gt 1 ] && [ "${SHIM_ROLLBACK_UP_FAIL:-0}" = "1" ]; then exit 1; fi
            fi
            exit 0 ;;
        *) exit 0 ;;
      esac ;;
esac
exit 0
SHIM

    # gate library shim: `wait-ready.sh container <name> <url> <timeout> <label>`
    #                    `wait-ready.sh public <url> <timeout> <label>`
    cat > "$lib/wait-ready.sh" <<'SHIM'
#!/usr/bin/env bash
echo "gate $*" >> "$TRACE"
label="${!#}"
case "$label" in
  prod-app)   exit "${SHIM_APP_RC:-0}" ;;
  prod-stt)   exit "${SHIM_STT_RC:-0}" ;;
  prod-route) exit "${SHIM_ROUTE_RC:-0}" ;;
esac
exit 0
SHIM

    cat > "$lib/shared-container.sh" <<'SHIM'
#!/usr/bin/env bash
echo "owner $*" >> "$TRACE"
case "$1" in
  reclaim)      exit "${SHIM_RECLAIM_RC:-0}" ;;
  assert-owner) exit "${SHIM_OWN_RC:-0}" ;;
esac
exit 0
SHIM
    chmod +x "$bin/docker" "$lib/wait-ready.sh" "$lib/shared-container.sh"
}

# Runs the recipe under test in an isolated clone. Echoes nothing; sets RC/LOG.
run_case() {
    local name="$1"; shift
    local root="$WORK/$name"
    build_clone "$root"
    local stage="$root/stage"
    mkdir -p "$stage"
    cp "$RECIPE" "$stage/prod.sh"
    write_shims "$root/bin" "$stage/lib"

    TRACE="$root/trace"; : > "$TRACE"
    LOG="$root/log"
    (
        cd "$root/src"
        export TRACE PATH="$root/bin:$PATH"
        # every case runs with the REAL secrets path unless it overrides it
        env "$@" bash "$stage/prod.sh"
    ) > "$LOG" 2>&1
    RC=$?
}

_has()    { grep -qF "$2" "$1"; }
_hasnt()  { ! grep -qF "$2" "$1"; }

echo "== prod.sh rollback selftest =="
echo "recipe under test: $RECIPE"
echo

# ── A: happy path ───────────────────────────────────────────────────────────
run_case A
[ "$RC" = "0" ] && _ok "A rc=0 on a clean deploy" || _bad "A expected rc 0, got $RC"
_has "$LOG" "release deployed"        && _ok "A reports the release"        || _bad "A no release line"
_has "$LOG" "rollback target pinned"  && _ok "A pins a rollback target"     || _bad "A pinned nothing"
_hasnt "$LOG" "ROLLING BACK"          && _ok "A never rolls back"           || _bad "A rolled back on success"

# ── I: the pinned tag is the container's own image ──────────────────────────
# The subtle one. Resolving the rollback target through the `:latest` tag would
# pin whatever the build is about to produce — a "rollback" to the thing that
# just failed. Assert the tag is cut from the CONTAINER's image id.
grep -q "docker tag sha256:PREVIMAGEID master-signal-tracker:rollback-" "$WORK/A/trace" \
    && _ok "I tags the container's own image id, not :latest" \
    || _bad "I rollback tag was not cut from the running container's image"

# ── B: `compose up -d` fails ────────────────────────────────────────────────
run_case B SHIM_UP_FAIL=1
[ "$RC" = "5" ] && _ok "B rc=5 when up fails" || _bad "B expected rc 5, got $RC"
_has "$LOG" "ROLLING BACK"      && _ok "B rolls back"                || _bad "B did not roll back"
_has "$LOG" "rollback complete" && _ok "B restores the previous image" || _bad "B rollback did not complete"
grep -q "docker tag master-signal-tracker:rollback-.* master-signal-tracker:latest" "$WORK/B/trace" \
    && _ok "B moves :latest back onto the known-good image" \
    || _bad "B never retagged :latest"

# ── C: the rollback itself fails ────────────────────────────────────────────
run_case C SHIM_UP_FAIL=1 SHIM_ROLLBACK_UP_FAIL=1
[ "$RC" = "27" ] && _ok "C rc=27 when the rollback fails too" || _bad "C expected rc 27, got $RC"
_has "$LOG" "ROLLBACK ITSELF FAILED" && _ok "C says prod needs a human" || _bad "C did not escalate"

# ── D: the app never becomes ready ──────────────────────────────────────────
run_case D SHIM_APP_RC=1
[ "$RC" = "22" ] && _ok "D rc=22 when the app gate fails" || _bad "D expected rc 22, got $RC"
_has "$LOG" "ROLLING BACK" && _ok "D rolls back an unhealthy app" || _bad "D left prod down"

# ── E: only the sidecar fails ───────────────────────────────────────────────
run_case E SHIM_STT_RC=1
[ "$RC" = "23" ] && _ok "E rc=23 when only stt fails" || _bad "E expected rc 23, got $RC"
_hasnt "$LOG" "ROLLING BACK" && _ok "E does NOT roll back a serving app" || _bad "E rolled back a healthy app"
_has "$LOG" "not rolling back" && _ok "E says why it declined" || _bad "E declined silently"

# ── F: healthy inside, absent on the public URL ─────────────────────────────
run_case F SHIM_ROUTE_RC=1
[ "$RC" = "24" ] && _ok "F rc=24 when the public route fails" || _bad "F expected rc 24, got $RC"
_has "$LOG" "ROLLING BACK" && _ok "F rolls back an unreachable release" || _bad "F did not roll back"

# ── G: the managed .env is gone ─────────────────────────────────────────────
run_case G WR_PROD_SECRETS_ENV="$WORK/definitely-not-here.env"
[ "$RC" = "2" ] && _ok "G rc=2 when the managed .env is missing" || _bad "G expected rc 2, got $RC"
_has "$LOG" "managed prod .env not found" && _ok "G names the missing file" || _bad "G did not name it"
# The whole point of failing here is that it happens BEFORE anything is touched.
[ ! -s "$WORK/G/trace" ] && _ok "G touched no container: docker never invoked" \
    || _bad "G ran docker before failing: $(head -1 "$WORK/G/trace")"

# ── H: from-empty deploy, then a failure ────────────────────────────────────
run_case H SHIM_NO_RUNNING_CONTAINER=1 SHIM_UP_FAIL=1
[ "$RC" = "27" ] && _ok "H rc=27 when there was nothing to roll back to" || _bad "H expected rc 27, got $RC"
_has "$LOG" "ROLLBACK UNAVAILABLE" && _ok "H refuses to claim a rollback it cannot do" || _bad "H claimed a rollback"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
