#!/usr/bin/env bash
#
# install-hooks.sh — arm this clone's git hooks. (T-0658)
#
# The hooks are tracked in the repository; `core.hooksPath` is NOT. It is set
# per clone and no checkout carries it, so a clone that nobody ran this in has
# no gate at all — measured 2026-08-15: of the four watchrobot clones on this
# box, only `dev` had it set, and `master`, the clone prod is published from,
# did not.
#
# WHY THIS REFUSES INSTEAD OF JUST SETTING THE VALUE
# --------------------------------------------------
# The setting and the files have to arrive in that order. `pre-commit` and
# `pre-push` source `lib/commit-policy.sh` and `lib/push-policy.sh`; with
# `core.hooksPath` pointed at a checkout that predates them, each hook fails to
# source its own library, the check function is then "command not found", and
# the `|| exit 1` next to it REJECTS EVERY COMMIT AND EVERY PUSH in that clone.
# In the release clone that is a stopped deploy. So this script verifies the
# checkout first and refuses, naming what is missing, rather than arming a hook
# that cannot run.
#
# ⛔ Running this in `master` or `deploy` is an operator decision. Those are the
# live release path: a hook that misfires there stops a deploy at the worst
# moment. This script is the one reviewed command for it — it is not something a
# ticket should run on its way past.
#
#   ./.githooks/install-hooks.sh          arm this clone
#   ./.githooks/install-hooks.sh --check  report only, change nothing

set -uo pipefail

# The clone this arms is the one you are STANDING IN, not the one this file was
# copied from. Resolving it from the script's own path instead would mean that
# running dev's copy while cd'd into `master` reports on dev and reports
# "already armed" — a guard answering about a repository other than the one the
# command targets, which is the exact way a guard comes to read as green while
# the thing it was pointed at is untouched.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOP="$(git rev-parse --show-toplevel 2>/dev/null)"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ -z "$TOP" ]; then
    echo "install-hooks: not inside a git clone (cwd: $PWD)" >&2
    exit 2
fi

case "$HOOK_DIR" in
    "$TOP"/*) ;;
    *) cat >&2 <<EOF
install-hooks: REFUSING — this script lives outside the clone it would arm.

  script:   $HOOK_DIR
  clone:    $TOP

Run the target clone's OWN copy, so the files being verified are the files that
will actually run:
  cd $TOP && ./.githooks/install-hooks.sh
EOF
       exit 5 ;;
esac

REQUIRED=(
    "pre-commit"
    "pre-push"
    "lib/peer-activity.sh"
    "lib/commit-policy.sh"
    "lib/push-policy.sh"
    "allowed-identities"
    "allowed-lookalike-paths"
)

missing=()
for f in "${REQUIRED[@]}"; do
    [ -f "$TOP/.githooks/$f" ] || missing+=("$f")
done

current="$(git -C "$TOP" config --get core.hooksPath 2>/dev/null || true)"

printf 'clone:            %s\n' "$TOP"
printf 'branch:           %s\n' "$(git -C "$TOP" rev-parse --abbrev-ref HEAD 2>/dev/null)"
printf 'core.hooksPath:   %s\n' "${current:-<unset>}"

if [ "${#missing[@]}" -gt 0 ]; then
    printf 'guard files:      MISSING %s\n\n' "${missing[*]}"
    cat >&2 <<EOF
REFUSING to set core.hooksPath in this clone.

This checkout does not carry the guard files, so the hooks would be unable to
source their own libraries — and a hook that fails to source its library does
not fail open, it REJECTS EVERY COMMIT AND PUSH here.

Bring the files in first (merge/fast-forward this clone to a commit that has
them), then run this again. Nothing was changed.
EOF
    exit 3
fi

printf 'guard files:      all present\n'

if [ "$current" = ".githooks" ]; then
    printf '\nAlready armed — nothing to do.\n'
    exit 0
fi

if [ "$CHECK_ONLY" = "1" ]; then
    printf '\nWOULD set core.hooksPath=.githooks (checkout is ready). Nothing changed.\n'
    exit 0
fi

git -C "$TOP" config core.hooksPath .githooks || exit 4
printf '\ncore.hooksPath set to .githooks — identity, secret-path and push gates are now armed here.\n'
printf 'Prove it without committing anything:\n'
printf '  %s/.githooks/test_commit_policy.sh\n' "$TOP"
printf '  %s/.githooks/test_push_policy.sh\n' "$TOP"
