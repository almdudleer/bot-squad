#!/usr/bin/env bash
# T-1046: nightly-suites.yml's `schedule:`/`workflow_dispatch:` triggers can
# never fire on GitHub Actions — GitHub only runs those from the copy of the
# workflow on the repo's DEFAULT branch (`master`), and `master` carries no
# `.github/` directory at all (measured: `git ls-tree origin/master
# .github/workflows/` is empty). Syncing master or moving the default branch
# is a push-authority decision (dev workers do not push); until that happens
# this script is the host-side stand-in for the same five arms.
#
# Runs against an ISOLATED `bsq verify-isolated --with-git` checkout of
# bot_squad/dev HEAD, never the live shared tree — the shared tree can be
# mid-edit under a peer session at any moment, and a nightly cert run must
# not manufacture a false red (or a false green) out of somebody else's
# in-progress hunk.
#
# Deliberately NOT a byte-identical port of nightly-suites.yml: the worker
# arm's reverse-file-order re-run (T-0774, a specific process-global-leak
# regression test) is omitted to keep this host job's runtime bounded. GitHub
# Actions remains the target for full fidelity once T-1046's DoD item 1 is
# revisited with push authority.
#
# Meant to be driven by a `bsq routine` (monitor, shell probe, ~24h
# interval) — see T-1046 Context for the declared routine id. Exit 0 only if
# every arm passed; a nonzero exit is the routine's breach signal, and the
# routine's `on_breach: spawn` is what gets a dev looking at a real failure,
# same job a red GitHub check would have done.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BSQ="$REPO/scripts/cli/bsq"

# Mount-root sentinel (AGENT_INSTRUCTIONS.md): a wrong REPO fails silently
# otherwise, and every result below would be worthless.
if [ ! -f "$REPO/scripts/lint/undefined_names.py" ] || [ ! -d "$REPO/api/tests" ]; then
  echo "run_nightly_certification: WRONG ROOT ($REPO) — refusing to run" >&2
  exit 1
fi

LOG_DIR="/home/www/bot-squad/data/bot-squad/_jobs/nightly-certification/runs"
mkdir -p "$LOG_DIR"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
LOG="$LOG_DIR/$RUN_ID.log"

# Measured (T-1046): the bsq routine's monitor probe runs this in the WORKER
# PROCESS's own environment, where /tmp is READ-ONLY (`mktemp: failed to
# create file via template ... Read-only file system`) — unlike an
# interactive session's shell, where /tmp is writable and this went
# undetected until the routine's own first probe fired. `mktemp` below and
# `bsq verify-isolated`'s internal `tempfile.mkdtemp()` (Python's tempfile
# module honours $TMPDIR) both need a scratch dir this process can actually
# write to; this data path already proved writable by LOG_DIR above.
TMPDIR="/home/www/bot-squad/data/bot-squad/_jobs/nightly-certification/scratch"
mkdir -p "$TMPDIR"
export TMPDIR

# Measured (T-1046, second bug found by the ACTUAL routine): pointing pytest
# itself at the long TMPDIR above breaks the worker suite's own socket-based
# tests. pytest's `tmp_path` fixture nests under
# "$TMPDIR/pytest-of-<user>/pytest-<N>/<test-name><idx>/...", and several
# worker tests (test_worker_census.py, test_jobs.py's heartbeat test) bind a
# REAL AF_UNIX socket under `tmp_path/data/_sock/worker.sock` — Linux caps a
# unix socket path (`sun_path`) at 108 bytes, and the long TMPDIR above blew
# through it, one 8-test batch of them failed with `OSError: AF_UNIX path
# too long`, none of them touched by this ticket's own change.
# `--basetemp=<PYTEST_TMP>` skips pytest's "pytest-of-<user>/pytest-<N>/"
# wrapper (saves ~35 bytes) and PYTEST_TMP itself is kept short and
# PID-unique — short because a long base is the whole problem, unique
# because a SHARED short path across concurrent runs would just trade this
# bug for T-1004's exact hazard (a race on a fixed path). Verified short
# enough: with PYTEST_TMP this length, the worst offending test's full
# socket path measured 75 bytes, 33 under the 108 cap.
PYTEST_TMP="/tmp/tmux-1000/t1046-$$"
mkdir -p "$PYTEST_TMP"

exec > >(tee -a "$LOG") 2>&1
echo "=== T-1046 host-side nightly certification — run $RUN_ID ($(date -u -Iseconds)) ==="

# `--with-git` is LOAD-BEARING beyond the T-1010 reason (scripts-cli needs a
# real .git): TMPDIR above sits under /home/www/bot-squad, which is itself
# inside an ambient git repo. `--with-git` takes the "materialise as a real
# clone, the tree IS its own repo" branch, so verify-isolated's own ambient-
# repo guard does not fire on it. Drop `--with-git` and it WILL refuse with
# "AMBIENT GIT REPO" (confirmed by the R-0010 handler, 2026-09-07) — do not
# remove this flag as a supposed simplification.
MATERIALIZE_LOG="$(mktemp)"
"$BSQ" verify-isolated --with-git --ref bot_squad/dev --keep -- true >"$MATERIALIZE_LOG" 2>&1
cat "$MATERIALIZE_LOG"
DEST="$(grep -oE ' to (/\S+)$' "$MATERIALIZE_LOG" | awk '{print $NF}' | tail -1)"
rm -f "$MATERIALIZE_LOG"

if [ -z "${DEST:-}" ] || [ ! -d "$DEST" ]; then
  echo "FATAL: could not materialise an isolated checkout of bot_squad/dev — see above" >&2
  echo 1 >"$LOG_DIR/$RUN_ID.rc"
  exit 1
fi
# `|| true`: this MUST NOT be allowed to fail. A trap's own exit status
# overrides an explicit `exit "$FAIL"` below it when the trap's last command
# fails — measured here (T-1046): the api arm's `docker run` (as root)
# leaves root-owned __pycache__/.pytest_cache files under $DEST, `rm -rf`
# then exits nonzero as this user, and the script reported exit 1 on a run
# where every arm had actually passed. A cleanup step that can silently
# invert a green result is worse than no cleanup step.
trap 'rm -rf "$DEST" "$PYTEST_TMP" 2>/dev/null || true' EXIT

FAIL=0
declare -A RESULT

run_arm() {
  local name="$1"; shift
  echo "--- $name ($(date -u -Iseconds)) ---"
  if "$@"; then
    RESULT["$name"]="PASS"
  else
    RESULT["$name"]="FAIL"
    FAIL=1
  fi
}

# worker — the shared worker/.venv already carries pytest + pyyaml
# (worker/pyproject.toml declares pyyaml; scripts/cli also rides this venv
# below rather than a second install).
rm -rf "${PYTEST_TMP:?}"/* 2>/dev/null || true
run_arm worker bash -c "cd '$DEST' && TMPDIR='$PYTEST_TMP' '$REPO/worker/.venv/bin/python' -m pytest --basetemp='$PYTEST_TMP' worker/tests -q"

# api — inside the EXISTING bot-squad-api:latest image. Never rebuilt here:
# a build is an exclusive, announced event (T-0994) and out of scope for an
# unattended nightly job.
run_arm api bash -c "docker run --rm -e BUILD_AT_IMPORT=0 -v '$DEST':/repo -w /repo/api --entrypoint python bot-squad-api:latest -m pytest -q"

# The api container runs as root, so anything it writes under the bind mount
# ($DEST) is root-owned — chown it back before this user has to touch $DEST
# again (the web arm below, and the EXIT trap's cleanup). Best-effort: a
# failure here must not affect FAIL, so it is deliberately outside run_arm.
docker run --rm -v "$DEST":/repo --entrypoint chown \
  bot-squad-api:latest -R "$(id -u):$(id -g)" /repo >/dev/null 2>&1 || true

# web — reuse the shared web/node_modules PACKAGES (symlinked individually,
# not the directory itself) so `node_modules/.vite/` is a real, writable dir
# under $DEST rather than a path that resolves back into the shared tree.
# Measured (T-1046): symlinking the whole node_modules directory makes
# vitest's result-cache write land in the SHARED node_modules/.vite/vitest/
# — exactly the shared-tree leak this isolated run exists to avoid — and it
# failed there with EACCES, reporting the whole arm FAIL even though all 4
# tests it managed to run had already passed. A fresh `npm ci` every night
# is still unneeded network/IO cost for a job that runs unattended.
mkdir -p "$DEST/web/node_modules"
find "$REPO/web/node_modules" -mindepth 1 -maxdepth 1 -not -name '.vite' \
  -exec ln -s {} "$DEST/web/node_modules/" \;
mkdir -p "$DEST/web/node_modules/.vite"
# Measured (T-1046): `npm test -- run` looked reasonable (vitest's own CLI is
# `vitest run`) but web/package.json's "test" script is ALREADY `vitest run`
# — the extra "run" then lands as a vitest CLI positional, which FILTERS by
# test name/path instead of no-op-ing. That silently dropped 719 tests to 4
# (only files matching /run/i) while still exiting 0 — the exact "false
# green from filtering" failure this ticket exists to catch, caught here by
# reading the actual test COUNT in the tail output, not just the exit code.
run_arm web bash -c "cd '$DEST/web' && npm test"

# scripts-cli — same venv as worker; T-0991 measured pytest+pyyaml as the
# whole dependency set, no editable install of worker/ or api/ needed.
rm -rf "${PYTEST_TMP:?}"/* 2>/dev/null || true
run_arm scripts-cli bash -c "cd '$DEST' && TMPDIR='$PYTEST_TMP' '$REPO/worker/.venv/bin/python' -m pytest --basetemp='$PYTEST_TMP' -q scripts/cli"

# shell-tests — T-1039's six scripts, no installs, plain bash from the
# isolated checkout.
run_arm shell-tests bash -c "
  cd '$DEST' &&
  git config user.email ci@bot-squad.local &&
  git config user.name 'bot-squad nightly host cert' &&
  bash .githooks/test_commit_policy.sh &&
  bash .githooks/test_duplicate_def_gate.sh &&
  bash .githooks/test_push_policy.sh &&
  bash scripts/hooks/test_derive_role.sh &&
  bash scripts/hooks/test_worktree_guard.sh &&
  bash scripts/ops/test_smoke_with_backoff.sh
"

echo "=== SUMMARY ($(date -u -Iseconds)) ==="
for name in worker api web scripts-cli shell-tests; do
  echo "$name: ${RESULT[$name]:-DID_NOT_RUN}"
done
echo "overall: $([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL)"

echo "$FAIL" >"$LOG_DIR/$RUN_ID.rc"
exit "$FAIL"
