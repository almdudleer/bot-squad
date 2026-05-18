#!/usr/bin/env bash
#
# safe-commit.sh — flock wrapper around `git commit` for shared
# bot-squad clones.
#
# Usage (via symlink at ops/bot-squad-bin/safe-commit):
#   safe-commit -- backend/foo.py
#   BOT_SQUAD_ACK_PEERS=1 safe-commit -- backend/foo.py
#
# Problem: git takes `.git/index.lock` atomically via O_EXCL. Concurrent
# `git commit` calls from peer bot-squad sessions don't queue — losers
# get `fatal: Unable to create '.git/index.lock': File exists` and exit
# 128 without ever running. Project-level pre-commit hooks (e.g.
# signal-tracker's `.githooks/pre-commit` peer-activity check) do slow
# per-file `git log` / `git blame` work that widens this window.
#
# Solution: take our own advisory flock *before* git tries to take its
# index lock, so concurrent callers queue instead of crashing. Up to
# BOT_SQUAD_COMMIT_TIMEOUT seconds of wait (default 120). After that
# we fall through to git anyway so the operator sees the real error.
#
# Pass-through: every arg goes straight to `git commit`; exit code is
# git's.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "safe-commit: not inside a git repo" >&2
    exit 2
}

LOCK="${REPO_ROOT}/.git/.bot-squad-commit.lock"
TIMEOUT="${BOT_SQUAD_COMMIT_TIMEOUT:-120}"

# Touch the lockfile so flock has a fd to open. Harmless if it exists.
: > "$LOCK" 2>/dev/null || true

# fd 9 holds the advisory lock for the duration of the git commit.
exec 9>"$LOCK"
if ! flock --timeout="$TIMEOUT" 9; then
    echo "safe-commit: could not acquire commit lock in ${TIMEOUT}s — running git commit anyway, expect possible EEXIST" >&2
fi

git commit "$@"
rc=$?

exec 9>&-
exit $rc
