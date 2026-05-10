#!/usr/bin/env bash
# Print SID for the current tmux context, including pane id so duplicate
# window names ("claude") still produce distinct SIDs.
# Format: S-<user>-<window>-p<pane>
#
# Exit codes:
#   0 — SID printed on stdout
#   1 — not in tmux (or tmux not available); nothing printed
set -uo pipefail

if [ -z "${TMUX:-}" ] || ! command -v tmux >/dev/null 2>&1; then
    exit 1
fi

user=$(id -un 2>/dev/null || echo u)
user=${user//[^A-Za-z0-9_]/_}

target=""
[ -n "${TMUX_PANE:-}" ] && target="-t $TMUX_PANE"

# shellcheck disable=SC2086
window=$(tmux display-message -p $target -F '#W' 2>/dev/null) || exit 1
window=${window//[^A-Za-z0-9_-]/_}
[ -z "$window" ] && exit 1

# Pane id like "%5"; strip the leading % so it stays alphanumeric in SIDs.
# shellcheck disable=SC2086
pane_raw=$(tmux display-message -p $target -F '#{pane_id}' 2>/dev/null) || exit 1
pane=${pane_raw#%}

echo "S-${user}-${window}-p${pane}"
