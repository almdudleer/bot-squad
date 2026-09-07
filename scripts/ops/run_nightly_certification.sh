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
#
# T-1067: bash reads a script BY BYTE OFFSET as it executes, not into memory
# up front — a live edit of THIS file's own tracked path while a run is
# mid-flight (this run is ~30-45+ minutes) reaches the running interpreter
# silently, no error, no diagnostic. R-0010's routine `cmd` now invokes this
# script through `scripts/ops/run_from_copy.sh`, which snapshots it to a
# private path before it ever starts running, so THIS script's own bytes are
# frozen for the run's whole duration regardless of what happens afterward to
# the tracked path. Run it directly (`bash scripts/ops/run_nightly_certification.sh`)
# only for a quick manual check — a direct run reads the live tracked path
# and is exposed to the hazard for as long as it runs. See T-1067 for the
# positive control and for what this mitigation does NOT cover (anything
# reached by path AFTER this script starts — e.g. `$BSQ` below — is read live,
# not frozen; `scripts/cli/bsq` already has its own separate protection,
# T-0972).
set -uo pipefail

# T-1067: when run through run_from_copy.sh, ${BASH_SOURCE[0]} is a flat
# snapshot path (e.g. /tmp/tmp.XXXX/run_nightly_certification.sh) — this
# script's own bytes are deliberately relocated, but the REPO it needs to
# find (worker/, api/, $BSQ, web/) is not, and never should be (see the
# header comment on what the copy does and does not cover). Derive REPO from
# RUN_FROM_COPY_SRC (the original tracked path the wrapper copied FROM) when
# set, falling back to BASH_SOURCE for a direct, unwrapped run.
REPO="$(cd "$(dirname "${RUN_FROM_COPY_SRC:-${BASH_SOURCE[0]}}")/../.." && pwd)"
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

# Serialise this script against itself. PYTEST_TMP below is a FIXED path
# (see the byte-budget note there — it has no room left for a PID/random
# suffix), so two overlapping runs would corrupt each other's pytest scratch
# state the same way T-1004's hardcoded /tmp paths do. A daily routine
# overlapping with itself is unlikely, but this is what makes it impossible
# rather than merely unlikely, at zero byte cost to the path budget.
LOCK_FILE="/home/almdudleer/.t1046-cert.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  # Measured (T-1046, R-0010 handler): a silent skip here is THIS TICKET'S
  # OWN FAILURE MODE in miniature. The log redirection isn't armed until
  # further down, so without this, a lock-skip wrote no .log and no .rc and
  # exited 0 — the monitor's judge is nonzero_exit, so a skip reads as a
  # clean PASS, and R-0010's own Instruction tells the handler to "read the
  # newest .log", which after a silent skip is a STALE log from whatever run
  # actually happened last. A tier reporting healthy while running nothing
  # is the sentence T-1046's own title is made of. exit 0 stays — a skip
  # genuinely is not a breach — but it must be a VISIBLE, dated exit 0, not
  # an invisible one.
  echo "=== SKIPPED $RUN_ID ($(date -u -Iseconds)) — another run holds $LOCK_FILE ===" >"$LOG_DIR/$RUN_ID.log"
  echo 0 >"$LOG_DIR/$RUN_ID.rc"
  echo "run_nightly_certification: another run holds $LOCK_FILE — skipped, see $LOG_DIR/$RUN_ID.log" >&2
  exit 0
fi

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

# Measured (T-1046, two more bugs found by the ACTUAL routine, root-caused
# by the R-0010 handler — not by inspection): pointing pytest itself at the
# long TMPDIR above breaks worker tests two DIFFERENT ways, same root cause
# (TMPDIR above sits under /home/www/bot-squad, which is both long AND an
# ambient git repo):
#   (a) pytest's `tmp_path` fixture nests under
#       "$TMPDIR/pytest-of-<user>/pytest-<N>/<test-name><idx>/...", and
#       several worker tests (test_worker_census.py, test_jobs.py's
#       heartbeat test) bind a REAL AF_UNIX socket under
#       `tmp_path/data/_sock/[_sock/]worker.sock` — Linux caps a unix socket
#       path (`sun_path`) at 108 bytes; the long TMPDIR blew through it.
#   (b) test_t1019_provenance_gate.py's "no git reachable" tests build a
#       throwaway dir under tmp_path and assert the provenance gate's repo
#       walk finds NOTHING above it. Nested under the long TMPDIR, that walk
#       instead finds the ambient repo at /home/www/bot-squad — the gate
#       goes SILENT (a clean pass) rather than loud, on a test that exists
#       to prove it fires.
#
# IT IS `--basetemp` ITSELF THAT FIXES BOTH, NOT MERELY A SHORT BASE
# DIRECTORY (measured by the R-0010 handler, not assumed — a first pass at
# this comment credited "short base dir" and was wrong): the worst-case
# socket path is TMPDIR + 99 bytes with pytest's normal
# "pytest-of-<user>/pytest-<N>/" wrapper, and only 6 of that headroom was
# EVER available even under bare `/tmp` — the original setup was never
# robust, it was lucky. `--basetemp=<PYTEST_TMP>` removes that wrapper
# outright (saves ~30 bytes, not just "some"), which is the only reason any
# non-trivial TMPDIR fits at all: TMPDIR + 69 bytes, so PYTEST_TMP's own
# length still has to stay short — this is a tight budget, not a solved one.
#
# PYTEST_TMP="/home/almdudleer/.t1046-cert-tmp" (32 bytes; 101 of 108 used,
# 7 to spare on the worst known test) rather than a /tmp/* path: `/tmp` is
# read-only to this process (see above) and BOTH of the writable /tmp
# subdirectories in the worker's systemd ReadWritePaths turned out unsafe
# for different reasons the R-0010 handler verified against the unit file
# directly, not assumed from the list alone —
#   `/tmp/claude-1000` is an OPTIONAL entry (`-/tmp/claude-1000`, leading
#     dash): no /etc/tmpfiles.d rule creates it, the Claude Code harness
#     does, so after a host reboot before any session starts, systemd
#     silently SKIPS the entry and this path goes read-only again — the
#     exact original EROFS bug, invisible until a probe hits it.
#   `/tmp/tmux-1000` IS a hard, tmpfiles.d-guaranteed entry, but it is
#     tmux's own runtime directory (R-0009 monitors it, T-0645 was an
#     outage there) and not ours to write unrelated scratch files into.
# `/home/almdudleer` is a hard ReadWritePaths entry (no dash, always
# exists) with no git repo anywhere above it, so it fixes bug (b) too.
# Verified against all previously-failing tests directly under this exact
# setting (test_worker_census.py, the heartbeat test, and the full
# test_t1019_provenance_gate.py file), not inferred from a passing mktemp
# probe — a probe that doesn't invoke pytest cannot see any of this.
PYTEST_TMP="/home/almdudleer/.t1046-cert-tmp"
rm -rf "$PYTEST_TMP" 2>/dev/null || true
mkdir -p "$PYTEST_TMP"

exec > >(tee -a "$LOG") 2>&1
echo "=== T-1046 host-side nightly certification — run $RUN_ID ($(date -u -Iseconds)) ==="

# T-1067: record which bytes this run actually executed, in THIS run's own
# dated log (not just run_from_copy.sh's own stdout, which nothing else
# reads). RUN_FROM_COPY_* is exported by scripts/ops/run_from_copy.sh before
# it exec's into the snapshot — set only when invoked that way.
if [ -n "${RUN_FROM_COPY_PATH:-}" ]; then
  echo "T-1067: running from a snapshot copy — src=${RUN_FROM_COPY_SRC:-?} copy=$RUN_FROM_COPY_PATH sha256=${RUN_FROM_COPY_SHA256:-?}"
else
  echo "T-1067: NOT running from a copy — reading the live tracked path directly ($0). Exposed to the mid-run-rewrite hazard for this run's full duration. Prefer: scripts/ops/run_from_copy.sh $0"
fi

# `--with-git` is LOAD-BEARING beyond the T-1010 reason (scripts-cli needs a
# real .git): TMPDIR above sits under /home/www/bot-squad, which is itself
# inside an ambient git repo. `--with-git` takes the "materialise as a real
# clone, the tree IS its own repo" branch, so verify-isolated's own ambient-
# repo guard does not fire on it. Drop `--with-git` and it WILL refuse with
# "AMBIENT GIT REPO" (confirmed by the R-0010 handler, 2026-09-07) — do not
# remove this flag as a supposed simplification.
MATERIALIZE_LOG="$(mktemp)"
# python3 "$BSQ", not a direct exec: measured (T-1046) that scripts/cli/bsq
# lost its execute bit mid-run while another session had it open with 300+
# uncommitted lines — a real, if transient, hazard on a file this heavily
# co-edited. bsq has a `#!/usr/bin/env python3` shebang and is fully
# readable at 644, so invoking the interpreter directly sidesteps the x-bit
# dependency entirely rather than waiting out someone else's save.
python3 "$BSQ" verify-isolated --with-git --ref bot_squad/dev --keep -- true >"$MATERIALIZE_LOG" 2>&1
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
#
# `timeout 300`: measured (T-1046, R-0010 handler, host load 254 spike):
# under extreme concurrent host load, a bare `docker run` can hang for many
# minutes waiting for a container slot, and this call's own output was
# already discarded (`>/dev/null`) — a hang here was indistinguishable from
# progress until the routine's OUTER 5400s ceiling finally killed the whole
# script with no arm having actually failed. A 300s bound on just this step
# turns "silently eats the whole 90-minute budget" into "this one step timed
# out," which is a diagnosable, bounded failure instead of an opaque one.
timeout 300 docker run --rm -v "$DEST":/repo --entrypoint chown \
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

# shell-tests — T-1039's six scripts plus T-1067's run_from_copy.sh test, no
# installs, plain bash from the isolated checkout.
run_arm shell-tests bash -c "
  cd '$DEST' &&
  git config user.email ci@bot-squad.local &&
  git config user.name 'bot-squad nightly host cert' &&
  bash .githooks/test_commit_policy.sh &&
  bash .githooks/test_duplicate_def_gate.sh &&
  bash .githooks/test_push_policy.sh &&
  bash scripts/hooks/test_derive_role.sh &&
  bash scripts/hooks/test_worktree_guard.sh &&
  bash scripts/ops/test_smoke_with_backoff.sh &&
  bash scripts/ops/test_run_from_copy.sh
"

echo "=== SUMMARY ($(date -u -Iseconds)) ==="
for name in worker api web scripts-cli shell-tests; do
  echo "$name: ${RESULT[$name]:-DID_NOT_RUN}"
done
echo "overall: $([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL)"

echo "$FAIL" >"$LOG_DIR/$RUN_ID.rc"
exit "$FAIL"
