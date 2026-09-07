#!/usr/bin/env bash
# T-1067: bash does not read a script into memory and parse it up front — it
# reads INCREMENTALLY, BY BYTE OFFSET, as it executes. Rewriting a file a
# shell is currently executing (in place, same inode — an editor save, `git
# checkout` on a WRITABLE file, `>` redirection, `cp` onto an existing dest)
# makes that shell resume at an offset that now points at different bytes.
# There is no error and no diagnostic; the run simply continues into
# whatever text now sits at that position. Positive control proving this
# (and proving this wrapper is immune to it) is on T-1067's Context.
#
# This wrapper removes the hazard for its own target's ENTIRE runtime: it
# snapshots the target to a private path before the target ever starts, and
# the target then reads only that snapshot's bytes for as long as it runs —
# regardless of what happens afterward to the tracked path it was copied
# from. It does NOT protect anything the target reaches by PATH after it
# starts: a sourced sibling, a data file it reads, or another tracked script
# it invokes by path are each exposed through their own separate read, not
# through the wrapped script's bytes. Wrap those too if they are also
# long-running.
#
# Usage:
#   run_from_copy.sh <target.sh> [args...]
#
# Exports into the target's environment (read them to log which snapshot
# actually ran — the whole point of "provably the bytes that ran"):
#   RUN_FROM_COPY_SRC      the original path this copy was taken from
#   RUN_FROM_COPY_PATH     the private snapshot path actually executed
#   RUN_FROM_COPY_SHA256   sha256 of the snapshot (== sha256 of SRC at the
#                          instant of the cp; the target's own log should
#                          record this, not just this wrapper's stdout,
#                          because the wrapper's stdout may not be the
#                          target's own dated log)
#
# Exit code: the target's own exit code (propagated via `set -e` on the
# final statement — this script does no work after the target returns).
set -euo pipefail

SRC="${1:?usage: run_from_copy.sh <target.sh> [args...]}"
shift

if [ ! -f "$SRC" ]; then
  echo "run_from_copy: no such file: $SRC" >&2
  exit 2
fi

COPY_DIR="$(mktemp -d)"
COPY="$COPY_DIR/$(basename "$SRC")"

# Plain `cp` onto a FRESH path in a private mktemp -d: this is a brand-new
# inode nobody else knows the path to, so nobody else editing SRC (or even
# editing COPY by naming it, since nobody has a reason to know it) reaches
# it. chmod 555 below is defense in depth against THIS PROCESS accidentally
# writing to its own snapshot, not the primary mechanism — the primary
# mechanism is that the snapshot's path was never published anywhere SRC's
# editors would target.
cp -- "$SRC" "$COPY"
chmod 555 "$COPY"

SHA256="$(sha256sum "$COPY" | awk '{print $1}')"
echo "run_from_copy: src=$SRC copy=$COPY sha256=$SHA256"

trap 'rm -rf "$COPY_DIR"' EXIT

export RUN_FROM_COPY_SRC="$SRC"
export RUN_FROM_COPY_PATH="$COPY"
export RUN_FROM_COPY_SHA256="$SHA256"

bash "$COPY" "$@"
