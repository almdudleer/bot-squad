#!/usr/bin/env bash
# Multi-server install validation in Docker (T-0253, WS-3 Phase 1).
#
# Exercises the REAL mothership-served install path end to end — the gap that
# the unit tests (invite_install.sh / smoke_engine.sh) miss because they source
# the RAW repo install.sh with sentinels intact. This harness mints a real
# install_token, fetches the SUBSTITUTED bundle the mothership actually serves,
# and runs it in a clean container, asserting the handshake + checkpoint flow.
#
# It is the regression guard for:
#   - T-0263: bundle must be present in the API image (served, no mount).
#   - T-0279: substitution must not break the served install.sh guards.
#
# SAFE: containers only. No real servers, no host writes outside a temp dir +
# throwaway containers/network/image tag. Re-runnable; cleans up on exit.
#
# Usage:
#   scripts/install/tests/multiserver_install_docker.sh [--keep] [--image TAG]
#     --keep        leave containers/network/workdir up for inspection
#     --image TAG   API image tag to run as the mothership
#                   (default: build bot-squad-api:t0253-test from the repo)
#
# SOURCE/provenance: docs/roadmap/guidance-corpus.md §PART 7
#   (Multi-server/Mothership — installer Ch.I §§3-7 / WS-3-NEW).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"   # scripts/install/tests -> repo root
NET="bsq-msinstall-test-$$"
MS="t0253-ms-$$"
SRVC="t0253-srv-$$"
WORK="$(mktemp -d)"
KEEP=0
IMAGE=""
PASS=0; FAIL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP=1; shift ;;
    --image) IMAGE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { echo "  ok: $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $*" >&2; FAIL=$((FAIL+1)); }
note() { echo "== $*"; }

cleanup() {
  [[ "$KEEP" = "1" ]] && { echo "--keep: leaving $MS / $SRVC / net $NET / $WORK"; return; }
  docker rm -f "$MS" "$SRVC" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "docker required" >&2; exit 2; }

# --- 0. image -----------------------------------------------------------------
if [[ -z "$IMAGE" ]]; then
  IMAGE="bot-squad-api:t0253-test"
  note "building $IMAGE (api stage + scripts/install)"
  docker build -q -f "$REPO/api/Dockerfile" --build-arg VITE_MOTHERSHIP=0 -t "$IMAGE" "$REPO" >/dev/null \
    || { echo "image build failed" >&2; exit 1; }
fi
# T-0263: bundle must be in the image (not just the web-builder stage).
if docker run --rm --entrypoint sh "$IMAGE" -c 'test -f /app/scripts/install/install.sh'; then
  ok "T-0263: install bundle present in image at /app/scripts/install/install.sh"
else
  bad "T-0263: install bundle MISSING from image (bundle GET will 500)"
fi

# --- 1. mothership ------------------------------------------------------------
note "standing up mothership ($IMAGE, MOTHERSHIP=1)"
docker network create "$NET" >/dev/null 2>&1 || true
mkdir -p "$WORK/config" "$WORK/data"
HASH="$(docker run --rm --entrypoint python "$IMAGE" -c \
  "import bcrypt;print(bcrypt.hashpw(b'admin123',bcrypt.gensalt()).decode())")"
cat > "$WORK/config/auth.toml" <<EOF
[users]
admin = "$HASH"
[user_meta.admin]
linux_user = "tester"
server_role = "server_admin"
[session]
ttl = "1d"
EOF
echo "# empty" > "$WORK/config/projects.toml"
docker run -d --name "$MS" --network "$NET" \
  -e MOTHERSHIP=1 -e MOTHERSHIP_BASE_URL="http://$MS:8000" \
  -e JWT_SECRET=testsecret -e COOKIE_SECURE=0 -e BUILD_AT_IMPORT=1 \
  -e CONFIG_DIR=/config -e DATA_DIR=/data -e WORKER_SOCK=/data/_sock/worker.sock \
  -v "$WORK/config":/config:rw -v "$WORK/data":/data:rw \
  "$IMAGE" >/dev/null
MS_IP=""
for _ in $(seq 1 30); do
  MS_IP="$(docker inspect "$MS" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)"
  [[ -n "$MS_IP" ]] && curl -s -o /dev/null "http://$MS_IP:8000/api/health" && break
  sleep 1
done
[[ "$(curl -s -o /dev/null -w '%{http_code}' "http://$MS_IP:8000/api/health")" = "200" ]] \
  && ok "mothership healthy at $MS_IP" || { bad "mothership did not come up"; exit 1; }

J="$WORK/cookies.txt"

# --- 2. create server entry + mint install_token ------------------------------
note "login + POST /api/m/servers"
curl -s -c "$J" -X POST "http://$MS_IP:8000/api/auth/login" \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin123"}' >/dev/null
RESP="$(curl -s -b "$J" -X POST "http://$MS_IP:8000/api/m/servers" \
  -H 'Content-Type: application/json' -d "{\"display_name\":\"2nd\",\"base_url\":\"http://$SRVC:8000\"}")"
TOKEN="$(echo "$RESP" | grep -o 'bsq_install_[A-Za-z0-9_-]*' | head -1)"
SRV="$(echo "$RESP"  | grep -o 'srv_[0-9a-f]*' | head -1)"
[[ -n "$TOKEN" && -n "$SRV" ]] && ok "minted install_token for $SRV" || { bad "token mint failed: $RESP"; exit 1; }

# --- 3. serve the bundle (T-0263: no mount) -----------------------------------
note "GET /i/<token>/install.sh"
CODE="$(curl -s -o "$WORK/served.sh" -w '%{http_code}' "http://$MS_IP:8000/i/$TOKEN/install.sh")"
[[ "$CODE" = "200" ]] && ok "T-0263: bundle served HTTP 200 (no mount)" || bad "T-0263: bundle HTTP $CODE (expected 200)"
LEFT="$(grep -c '__INSTALL_TOKEN__\|__MOTHERSHIP_URL__\|__CLONE_URL__\|__REPO_REF__' "$WORK/served.sh")"
[[ "$LEFT" = "0" ]] && ok "all placeholders substituted" || bad "$LEFT placeholders left unsubstituted"

# --- 4. run the SERVED script in a clean container; assert handshake ----------
note "run served install.sh in clean debian (handshake should COMPLETE)"
# Seed state so only mothership_handshake executes (avoids pkg/docker/compose
# steps that need dind — out of scope for this SAFE harness).
printf '%s\n' detect_distro require_sudo proxy_url pkg_index_update install_base_pkgs \
  install_tmux install_nodejs install_claude_code install_docker botsquad_group \
  install_dir clone_repo render_env python_venv tg_proxy systemd_unit \
  per_user_worker_unit install_reverse_proxy docker_compose_up agent_teams_flag \
  spawn_operator print_attach > "$WORK/install.state.seed"

# SSE listener (durable jsonl is the primary assertion; SSE is a bonus signal).
( curl -sN -b "$J" --max-time 45 "http://$MS_IP:8000/api/m/servers/$SRV/checkpoints" > "$WORK/sse.log" 2>&1 ) &
SSE_PID=$!
sleep 2

docker rm -f "$SRVC" >/dev/null 2>&1
RUN_OUT="$(docker run --rm --name "$SRVC" --network "$NET" \
  -v "$WORK/served.sh":/install.sh:ro \
  -v "$WORK/install.state.seed":/seed/install.state:ro \
  debian:bookworm-slim bash -c '
    set -e
    apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq curl jq ca-certificates >/dev/null 2>&1
    mkdir -p /state /install/data/_worker; cp /seed/install.state /state/install.state
    BOTSQUAD_NONINTERACTIVE=1 BOTSQUAD_STATE_DIR=/state BOTSQUAD_INSTALL_DIR=/install bash /install.sh >/tmp/o 2>&1
    rc=$?
    echo "RC=$rc"
    echo "TOKENMODE=$(stat -c %a /state/server.token 2>/dev/null)"
    echo "BEARERPFX=$(cut -c1-11 /state/server.token 2>/dev/null)"
    echo "IDMODE=$(stat -c %a /install/data/_worker/install.id 2>/dev/null)"
    echo "IDVAL=$(cat /install/data/_worker/install.id 2>/dev/null)"
    # idempotent rerun: handshake must skip, bearer unchanged
    B1=$(cat /state/server.token 2>/dev/null)
    BOTSQUAD_NONINTERACTIVE=1 BOTSQUAD_STATE_DIR=/state BOTSQUAD_INSTALL_DIR=/install bash /install.sh >/tmp/o2 2>&1
    grep -q "skip   mothership_handshake" /tmp/o2 && echo "RERUN=skipped" || echo "RERUN=reran"
    [ "$B1" = "$(cat /state/server.token)" ] && echo "BEARERSTABLE=yes" || echo "BEARERSTABLE=no"
  ')"
wait "$SSE_PID" 2>/dev/null
echo "$RUN_OUT" | sed 's/^/    /'

get() { echo "$RUN_OUT" | grep "^$1=" | head -1 | cut -d= -f2-; }
[[ "$(get RC)" = "0" ]] && ok "T-0279: served install.sh handshake completed (RC=0)" || bad "T-0279: handshake failed (RC=$(get RC)) — served path broken"
[[ "$(get TOKENMODE)" = "600" ]] && ok "server.token mode 0600" || bad "server.token mode=$(get TOKENMODE) (expected 600)"
[[ "$(get BEARERPFX)" = "bsq_server_" ]] && ok "bearer prefix bsq_server_" || bad "bearer prefix=$(get BEARERPFX)"
[[ "$(get IDMODE)" = "644" ]] && ok "install.id mode 0644" || bad "install.id mode=$(get IDMODE)"
[[ "$(get IDVAL)" = "$SRV" ]] && ok "install.id == server_id ($SRV)" || bad "install.id=$(get IDVAL) != $SRV"
[[ "$(get RERUN)" = "skipped" ]] && ok "idempotent: rerun skips handshake" || bad "rerun did not skip handshake"
[[ "$(get BEARERSTABLE)" = "yes" ]] && ok "idempotent: bearer unchanged on rerun" || bad "bearer changed on rerun"

# --- 5. mothership-side state + checkpoint log --------------------------------
note "mothership-side verification"
STATE="$(python3 -c "import json;d=json.load(open('$WORK/data/_mothership/servers.json'));print(next((s['install_state']+'|'+str(bool(s['install_token_hash']))+'|'+str(bool(s['server_bearer_hash'])) for s in d['servers'] if s['id']=='$SRV'),'missing'))")"
[[ "$STATE" = "connected|False|True" ]] && ok "server connected, token burned, bearer set" || bad "server state=$STATE (expected connected|False|True)"
CPL="$WORK/data/_mothership/checkpoints/$SRV.jsonl"
if [[ -f "$CPL" ]] && grep -q '"status": "begin"' "$CPL" && grep -q '"status": "done"' "$CPL"; then
  ok "T-0279: checkpoint log has handshake begin+done (posting works)"
else
  bad "T-0279: checkpoint log missing begin/done (post_checkpoint_event broken)"
fi
[[ "$(stat -c %a "$WORK/data/_mothership/bearers/$SRV" 2>/dev/null)" = "600" ]] \
  && ok "bearer sidecar mode 0600" || bad "bearer sidecar mode wrong"
grep -q '^data:' "$WORK/sse.log" 2>/dev/null \
  && ok "SSE delivered checkpoint events as data: frames" \
  || echo "  (note) SSE live-capture empty this run — durable jsonl is the authoritative check"

# --- 6. negative: burned token re-/connect -> 410 -----------------------------
note "negative: re-/connect with burned token"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://$MS_IP:8000/api/m/installer/connect" \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\",\"server_meta\":{\"hostname\":\"x\",\"install_dir\":\"/i\",\"coordinator_user\":\"r\"}}")"
[[ "$CODE" = "410" ]] && ok "burned token re-/connect -> 410 (single-burn enforced)" || bad "burned token -> $CODE (expected 410)"

# --- 7. standalone path (no mothership) ---------------------------------------
note "standalone: BOTSQUAD_SKIP_MOTHERSHIP=1 must skip handshake cleanly"
SOUT="$(docker run --rm --network "$NET" \
  -v "$REPO/scripts/install":/repo:ro \
  -v "$WORK/install.state.seed":/seed/install.state:ro \
  debian:bookworm-slim bash -c '
    set -e
    apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq curl jq ca-certificates >/dev/null 2>&1
    mkdir -p /state /install/data/_worker; cp /seed/install.state /state/install.state
    BOTSQUAD_NONINTERACTIVE=1 BOTSQUAD_STATE_DIR=/state BOTSQUAD_INSTALL_DIR=/install \
      BOTSQUAD_SKIP_MOTHERSHIP=1 bash /repo/install.sh >/tmp/o 2>&1; echo "RC=$?"
    grep -q "skipping mothership handshake" /tmp/o && echo "SKIPPED=yes" || echo "SKIPPED=no"
    [ -f /state/server.token ] && echo "TOKEN=present" || echo "TOKEN=absent"
  ')"
echo "$SOUT" | grep -q "RC=0" && echo "$SOUT" | grep -q "SKIPPED=yes" && echo "$SOUT" | grep -q "TOKEN=absent" \
  && ok "standalone skips handshake, no bearer minted" || bad "standalone path: $SOUT"

# --- summary ------------------------------------------------------------------
echo ""
echo "================ RESULT: $PASS passed, $FAIL failed ================"
[[ "$FAIL" -eq 0 ]] && echo "MULTI-SERVER INSTALL: GREEN" || echo "MULTI-SERVER INSTALL: RED"
exit "$FAIL"
