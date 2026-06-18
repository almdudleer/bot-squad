#!/usr/bin/env bash
#
# git-guard.sh — victim-side `git add` guard for shared bot-squad clones (T-0186).
#
# THE HOLE THIS CLOSES
# --------------------
# safe-commit (T-0145) refuses `git commit -a/--all`, and the pre-commit hook
# (T-0093) warns the COMMITTER about peer activity on the files THEY stage.
# Neither protects the VICTIM: a session whose unstaged working-tree edits get
# swept by a *peer's* raw `git add -A` / `git add -u` / `git add <dir>` issued
# OUTSIDE safe-commit. That is the wide-add absorption class (T-0068, T-0145)
# and it caused a live runtime break (`fb01921` swept an in-flight actions.py
# without its config.py field → AttributeError on origin).
#
# Git has no pre-add hook, so the realistic mechanism (per the T-0186 report)
# is a wrapper + education. This script is a PATH-shimmed `git`: it intercepts
# ONLY the `add` subcommand, refuses the wide/dir forms, and `exec`s the real
# git transparently for everything else (so `git commit`, `git status`, … are
# untouched). Stage by EXPLICIT file path instead — the only absorb-proof form.
#
# ENABLEMENT (opt-in — see AGENT_INSTRUCTIONS "Concurrent-commit safety")
# ----------------------------------------------------------------------
#   ln -sf "$BOT_SQUAD/scripts/cli/git-guard.sh" ops/bot-squad-bin/git
#   export PATH="<clone>/ops/bot-squad-bin:$PATH"   # ahead of the real git
# It is deliberately NOT force-injected into every session's global PATH: a
# global `git` shim would also intercept the worker's and deploy clone's git
# calls (larger blast radius than the problem). The deploy-breaking HARM is
# already caught by the cross-module attribute smoke (remedy b) regardless of
# mechanism; this wrapper is the pre-absorption belt for sessions that opt in.
#
# Refused forms (all stage more than your declared files):
#   git add -A | --all | -u | --update | .  | <directory>
# Allowed: explicit file pathspecs — `git add path/to/file.py [more ...]`.
#
# Override (single-tenant clone, you own every pending change), mirroring
# safe-commit's BOT_SQUAD_ALLOW_COMMIT_ALL:
#   BOT_SQUAD_ALLOW_WIDE_ADD=1 git add -A
#
# Exit codes: 3 = refused wide/dir add; otherwise the real git's exit code.

set -uo pipefail

# ---------------------------------------------------------------------------
# Locate the REAL git: the first `git` on PATH that is not this wrapper. Compare
# canonical paths so a symlinked wrapper never re-invokes itself (infinite loop).
# ---------------------------------------------------------------------------
self_real="$(readlink -f "$0" 2>/dev/null || echo "$0")"
real_git=""
IFS=':' read -r -a _path_dirs <<< "$PATH"
for d in "${_path_dirs[@]}"; do
    [ -n "$d" ] || continue
    cand="$d/git"
    [ -x "$cand" ] || continue
    cand_real="$(readlink -f "$cand" 2>/dev/null || echo "$cand")"
    if [ "$cand_real" != "$self_real" ]; then
        real_git="$cand"
        break
    fi
done
if [ -z "$real_git" ]; then
    # Last resort: common absolute paths, then bail loudly.
    for p in /usr/bin/git /bin/git /usr/local/bin/git; do
        [ -x "$p" ] && real_git="$p" && break
    done
fi
if [ -z "$real_git" ]; then
    echo "git-guard: could not locate the real git on PATH" >&2
    exit 127
fi

# Not an `add` invocation → transparent passthrough.
if [ "${1:-}" != "add" ]; then
    exec "$real_git" "$@"
fi

# Escape hatch for single-tenant clones.
if [ "${BOT_SQUAD_ALLOW_WIDE_ADD:-}" = "1" ]; then
    exec "$real_git" "$@"
fi

# ---------------------------------------------------------------------------
# Inspect `git add` args. Refuse the wide/dir forms; allow explicit file paths.
# Walk argv: flags before any `--`, pathspecs after (or any non-dash arg).
# ---------------------------------------------------------------------------
shift  # drop the literal "add"
after_dashdash=0
bad_reason=""
for arg in "$@"; do
    if [ "$after_dashdash" = "1" ]; then
        # explicit pathspec region — still reject "." and directories
        if [ "$arg" = "." ] || { [ -d "$arg" ] && [ ! -f "$arg" ]; }; then
            bad_reason="directory/'.' pathspec '$arg' stages everything under it"
            break
        fi
        continue
    fi
    case "$arg" in
        --)
            after_dashdash=1
            ;;
        -A|--all|--no-ignore-removal)
            bad_reason="'$arg' stages every change in the tree (incl. peers' WIP)"
            break
            ;;
        -u|--update)
            bad_reason="'$arg' stages every tracked modification (incl. peers' WIP)"
            break
            ;;
        .)
            bad_reason="'.' stages everything in the current dir tree"
            break
            ;;
        --*)
            # other long option (e.g. --dry-run, --patch) — harmless, allow
            ;;
        -?*)
            # short cluster (e.g. -n, -p, -f). Flag if it contains 'A' or 'u'
            # (the wide short forms) packed with other flags like `-An`/`-uf`.
            if [ "${arg#*A}" != "$arg" ]; then
                bad_reason="'$arg' contains -A (stages every change in the tree)"
                break
            fi
            if [ "${arg#*u}" != "$arg" ]; then
                bad_reason="'$arg' contains -u (stages every tracked modification)"
                break
            fi
            ;;
        *)
            # a bare (pre-dashdash) pathspec — reject "." / directories
            if [ "$arg" = "." ] || { [ -d "$arg" ] && [ ! -f "$arg" ]; }; then
                bad_reason="directory/'.' pathspec '$arg' stages everything under it"
                break
            fi
            ;;
    esac
done

if [ -n "$bad_reason" ]; then
    cat >&2 <<EOF
git-guard: REFUSED — wide \`git add\`: $bad_reason.

In this shared clone the index is shared by ~10 sessions; a wide/dir add sweeps
a PEER's unstaged working-tree edits into your stage, where your next commit
absorbs them — the T-0068/T-0145 incident class (and the fb01921 live break).

Stage by EXPLICIT file path instead:
  git add path/to/file.py [more/files ...]
  # then commit with:  bsq commit -m "msg" path/to/file.py

Override (single-tenant clone, you own every pending change):
  BOT_SQUAD_ALLOW_WIDE_ADD=1 git add ...
EOF
    exit 3
fi

exec "$real_git" add "$@"
