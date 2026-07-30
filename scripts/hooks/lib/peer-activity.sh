#!/usr/bin/env bash
#
# peer-activity.sh — shared peer-activity enumeration for this clone's git
# guards. Sourced by `scripts/hooks/bsq-worktree-guard.sh`.
#
# PROVENANCE — LIFTED, NOT REIMPLEMENTED (T-0826)
# ----------------------------------------------
# This file is watchrobot's `.githooks/lib/peer-activity.sh` (their T-0433,
# shipped at eeacb17e plus the uncommitted blame-cap they added on top of it),
# taken wholesale at 2026-07-30T13:41Z. It was offered for lift precisely so a
# second independent implementation of the same guard does not exist —
# duplicate divergence is this repo's top bug class (T-0818: solve it once).
# Parts marked ADAPTED differ, and each says why. Found a defect? Tell
# watchrobot's T-0433 rather than forking it.
#
# WHY OUR SIDE NEEDED IT (T-0826)
# -------------------------------
# 2026-07-30T12:55:49Z, in watchrobot's dev clone: one bare `git stash` swept
# the entire working tree — 24 files across six tickets, FIVE live sessions'
# uncommitted work — into stash@{0}. The tree then read CLEAN: no error, no
# warning, no attribution, exit 0. Brand-new UNTRACKED files went in too, with
# only two parents on the stash object, because a DIFFERENT session had already
# `git add`ed them: THE INDEX IS SHARED, so a bare stash takes whatever any peer
# happens to have staged.
#
# The identical command does identical damage in OUR clone. Our COMMIT path is
# guarded — `bsq commit`'s co-edit audit (`_coedit_audit`/`_report_coedit`,
# T-0752) refuses and enumerates peer activity — and our worktree-destroying
# verbs were guarded by PROSE ONLY. That asymmetry is the whole ticket.
#
# Two report modes, because "what is about to be destroyed" differs per verb:
#
#   cached    — the staged diff. What a commit would write.
#   worktree  — HEAD → working tree. What a revert-to-HEAD would destroy.
#
# ATTRIBUTION RULE (non-negotiable): attribute work by CONTENT — ticket tags in
# the diff — and NEVER by adjacency, line count, or position in a `git status`
# listing. Every misattribution during the incident came from the latter:
# +130/+55 line counts and two files sitting next to each other in a status
# listing were read as authorship, and the wrong session was nearly told to
# commit a peer's unfinished feature. Line counts are printed here as DAMAGE
# SIZE only and are never used to infer an owner.

# ---------------------------------------------------------------------------
# pa_recent_commits <lookback_arg> <path>
#
# Commits that touched <path> inside the lookback window. In this clone every
# session commits as the same identity, so the value is the hash + subject: an
# agent recognizes peer work by TOPIC (the ticket tag in the subject), which is
# content, not by the author field, which is always identical.
# ---------------------------------------------------------------------------
pa_recent_commits() {
    local lookback_arg="$1" path="$2"
    git log --since="$lookback_arg" --pretty=format:'%h  %ar  %an  %s' -- "$path" 2>/dev/null
}

# ---------------------------------------------------------------------------
# pa_fresh_deletions <mode> <lookback_arg> <parent> <path>
#
# Lines this change DELETES that were ADDED inside the lookback window — the
# specific "wiped a freshly-landed peer line" pattern. Blame-based, so it only
# sees lines that reached a commit.
#
# ADAPTED (T-0826): NEVER RUN IN `worktree` MODE. Two independent reasons, the
# second measured by watchrobot's p531 on their real tree:
#
#   1. The signal is WRONG there. A worktree-destroying verb reverts to HEAD,
#      which RESTORES committed lines — it does not delete them. What it
#      destroys is UNCOMMITTED work, which blame structurally cannot see. So a
#      blame walk in worktree mode reports damage that is not about to happen
#      and stays silent about the damage that is.
#   2. The cost is unbounded in the worst case. Two git processes per deleted
#      line: eight wholesale file deletions (~2200 lines) took their guard 77
#      SECONDS — past the hook's timeout, at which point the guard fails CLOSED
#      on legitimate work exactly when the tree is messiest. 77s → 0.89s once
#      worktree mode stopped blaming.
#
# The cap below still bounds `cached` mode, which is watchrobot's commit-guard
# path and is kept here so the two copies stay diffable.
# ---------------------------------------------------------------------------
pa_fresh_deletions() {
    local mode="$1" lookback_arg="$2" parent="$3" path="$4"
    [ -n "$parent" ] || return 0
    [ "$mode" = "cached" ] || return 0

    local deleted_lines
    deleted_lines=$(git diff --cached --unified=0 -- "$path" 2>/dev/null | pa__deleted_line_numbers)
    [ -n "$deleted_lines" ] || return 0

    local max_lines="${PA_MAX_BLAME_LINES:-200}"
    local n_lines truncated=0
    n_lines=$(printf '%s\n' "$deleted_lines" | grep -c . )
    if [ "$n_lines" -gt "$max_lines" ]; then
        deleted_lines=$(printf '%s\n' "$deleted_lines" | head -n "$max_lines")
        truncated=1
    fi

    local cutoff ln blame bhash bts msg out=""
    cutoff=$(date -d "$lookback_arg" +%s 2>/dev/null)
    for ln in $deleted_lines; do
        blame=$(git blame -L "$ln,$ln" --porcelain "$parent" -- "$path" 2>/dev/null | head -1)
        [ -z "$blame" ] && continue
        bhash=$(echo "$blame" | awk '{print $1}')
        bts=$(git show -s --format=%ct "$bhash" 2>/dev/null)
        if [ -n "$bts" ] && [ -n "$cutoff" ] && [ "$bts" -gt "$cutoff" ]; then
            msg=$(git show -s --format='%h  %ar  %an  %s' "$bhash" 2>/dev/null)
            out="${out}  line $ln  ←  $msg"$'\n'
        fi
    done
    if [ "$truncated" = "1" ]; then
        out="${out}  … only the first ${max_lines} of ${n_lines} deleted lines were blamed"$'\n'
    fi
    printf '%s' "$out"
}

# Parse `-<start>[,<count>]` out of each hunk header into one line number per
# deleted line. Kept as its own function so both modes share one parser.
pa__deleted_line_numbers() {
    awk '/^@@/ {
        match($0, /-[0-9]+(,[0-9]+)?/, a)
        split(substr(a[0], 2), b, ",")
        start = b[1]
        cnt = (length(b[2]) ? b[2] : 1)
        for (i = 0; i < cnt; i++) print start + i
    }' 2>/dev/null
}

# ---------------------------------------------------------------------------
# pa_collect_report <mode> <lookback_hours> <path>...
#
# Builds the report the guard prints. Sets two globals (bash has no clean
# multi-value return):
#   PA_REPORT         — the formatted per-path report
#   PA_WARNINGS_FOUND — 1 if anything worth showing was found, else 0
# ---------------------------------------------------------------------------
pa_collect_report() {
    local mode="$1" lookback_hours="$2"
    shift 2

    local lookback_arg="${lookback_hours} hours ago"
    local parent
    parent=$(git rev-parse --verify HEAD 2>/dev/null || echo "")

    PA_REPORT=""
    PA_WARNINGS_FOUND=0

    local f recent fresh
    for f in "$@"; do
        [ -z "$f" ] && continue
        recent=$(pa_recent_commits "$lookback_arg" "$f")
        fresh=$(pa_fresh_deletions "$mode" "$lookback_arg" "$parent" "$f")

        if [ -n "$recent" ] || [ -n "$fresh" ]; then
            PA_WARNINGS_FOUND=1
            PA_REPORT="${PA_REPORT}
$(pa_bold "── $f ──")"
            if [ -n "$recent" ]; then
                PA_REPORT="${PA_REPORT}
recent commits touching this file (last ${lookback_hours}h):
$(echo "$recent" | sed 's/^/  /')
"
            fi
            if [ -n "$fresh" ]; then
                PA_REPORT="${PA_REPORT}
$(pa_red "you are deleting lines that were added in the last ${lookback_hours}h:")
${fresh}"
            fi
        fi
    done
}

# ---------------------------------------------------------------------------
# The session roster, fetched AT MOST ONCE per guard run.
#
# ADAPTED (T-0826): watchrobot resolve the roster from ONE place, the clone the
# library lives in. That is wrong on our side and would have produced a
# silently-empty roster — i.e. every path reported UNATTRIBUTED, which reads
# exactly like "no peer work here". `bsq` derives the project from cwd against
# `config/projects.toml`, and MEASURED: `bsq team status --json` run from
# /home/www/bot-squad (the install, where a deployed copy of this file sits)
# exits 2 with "CWD is not inside any registered project". So we try the repo
# actually being guarded FIRST and fall back to this file's own clone, and we
# require the output to PARSE before accepting it — an unregistered cwd yields
# empty stdout, not an error we would otherwise notice.
#
# Degrades to empty (never blocks, never hangs) if the worker socket is down —
# a guard that hangs on a roster lookup would be worse than one that says
# "unattributed". Callers decide what an empty roster means; the guard treats it
# as "assume shared", because refusing costs a re-run and permitting cost five
# sessions their working trees.
# ---------------------------------------------------------------------------
PA_ROSTER_JSON=""
PA_ROSTER_TRIED=0
# $$ is the parent shell's pid even when read inside a `$( )` subshell, so this
# path is shared by every caller of this library within one guard run.
PA_ROSTER_CACHE="${TMPDIR:-/tmp}/.bsq-guard-roster-$$"

pa__lib_clone_root() {
    (cd "$(dirname "${BASH_SOURCE[0]}")/../../.." 2>/dev/null && pwd) || true
}

pa__roster() {
    [ "$PA_ROSTER_TRIED" = "1" ] && return 0
    PA_ROSTER_TRIED=1

    local guarded lib_root candidate seen="" raw

    # A previous caller in this same guard run already paid for it. Bounded by
    # age: pids are recycled, so an old file with a matching name is a DIFFERENT
    # process's roster and must not be served as this one's.
    if [ -f "$PA_ROSTER_CACHE" ] \
       && [ -z "$(find "$PA_ROSTER_CACHE" -mmin +1 2>/dev/null)" ]; then
        PA_ROSTER_JSON="$(cat -- "$PA_ROSTER_CACHE" 2>/dev/null)"
        [ -n "$PA_ROSTER_JSON" ] && return 0
    fi

    # Test seam. `bsq team status` hits the live worker socket, so a selftest
    # that called it would be neither hermetic nor able to fabricate the
    # multi-session tree it needs to assert against. A file here replaces the
    # roster wholesale; an unreadable or unparseable one leaves the roster
    # EMPTY, which every caller treats as "assume shared" — so a broken seam
    # fails closed rather than quietly disarming the guard.
    if [ -n "${BOT_SQUAD_GUARD_ROSTER_FILE:-}" ]; then
        raw=$(cat -- "${BOT_SQUAD_GUARD_ROSTER_FILE}" 2>/dev/null) || return 0
        [ -n "$raw" ] || return 0
        printf '%s' "$raw" | timeout 5 python3 -c 'import json,sys; sys.exit(0 if isinstance(json.load(sys.stdin), list) else 1)' 2>/dev/null || return 0
        PA_ROSTER_JSON="$raw"
        (umask 077; printf '%s' "$raw" > "$PA_ROSTER_CACHE") 2>/dev/null
        return 0
    fi

    guarded=$(git rev-parse --show-toplevel 2>/dev/null || true)
    lib_root=$(pa__lib_clone_root)

    for candidate in "$guarded" "$lib_root"; do
        [ -n "$candidate" ] || continue
        [ -d "$candidate" ] || continue
        case "$seen" in *"|${candidate}|"*) continue ;; esac
        seen="${seen}|${candidate}|"

        raw=$(cd "$candidate" && timeout 10 bsq team status --json 2>/dev/null) || continue
        [ -n "$raw" ] || continue
        printf '%s' "$raw" | timeout 5 python3 -c 'import json,sys; sys.exit(0 if isinstance(json.load(sys.stdin), list) else 1)' 2>/dev/null || continue
        PA_ROSTER_JSON="$raw"
        (umask 077; printf '%s' "$raw" > "$PA_ROSTER_CACHE") 2>/dev/null
        return 0
    done
    return 0
}

# ---------------------------------------------------------------------------
# pa_roster_available
#
# Exit 0 if the roster was fetched AND parsed, 1 otherwise. Signalled by EXIT
# CODE, not by a global: every caller reads these helpers as `x=$(f)`, which is a
# COMMAND SUBSTITUTION, i.e. a subshell — a global set inside never propagates
# back. watchrobot's p531 lost a whole feature to exactly that (their solo
# allowance was dead on arrival while looking correct in review).
#
# It exists because "the roster could not be read" and "the roster was read and
# holds no ticket→sid rows" are DIFFERENT FACTS that an empty owner_map cannot
# tell apart. Printing "(roster unavailable)" for the second is an unknown
# rendered as a specific WRONG known — p531 hit it in a run where the roster
# returned two sessions, neither carrying a task_id.
#
# Call it from the guard's top-level scope: that populates the run's cache, so
# the later subshell calls cost nothing.
# ---------------------------------------------------------------------------
pa_roster_available() {
    pa__roster
    [ -n "$PA_ROSTER_JSON" ]
}

# ---------------------------------------------------------------------------
# pa_ticket_owner_map
#
# Emits `T-NNNN<TAB>SID` for every live session's task, so a guard can name the
# session that owns a ticket tag it found in a diff. This is the CONTENT side of
# the attribution rule: the tag comes out of the change itself.
# ---------------------------------------------------------------------------
pa_ticket_owner_map() {
    pa__roster
    [ -n "$PA_ROSTER_JSON" ] || return 0
    printf '%s' "$PA_ROSTER_JSON" | timeout 10 python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(rows, list):
    sys.exit(0)
for r in rows:
    if not isinstance(r, dict) or r.get("status") != "active":
        continue
    sid = r.get("sid") or ""
    ids = [r.get("task_id")] + list(r.get("extra_task_ids") or [])
    for t in ids:
        if t and sid:
            print("%s\t%s" % (t, sid))
' 2>/dev/null
}

# ---------------------------------------------------------------------------
# pa_sessions_in_repo <repo_root>
#
# How many live sessions are working INSIDE <repo_root>, from their recorded
# cwd. Prints a count; prints `unknown` if the roster could not be read.
#
# ADAPTED (T-0826) — this function is ours, and it exists to bound a false
# refusal the widened predicate creates. watchrobot's guard refuses a wide stash
# whenever anything is dirty, on the reasoning that the agent named nothing so
# it cannot have meant "and also revert four peers' work". That reasoning holds
# in a SHARED clone and only there. Our five families include `git reset --hard
# <prev-tag>`, which is the documented PROD ROLLBACK step in the release TL's
# role contract (api/app/resources/roles/prod-teamlead.md) and runs in a
# single-tenant deploy clone. Refusing a rollback because the deploy clone
# happens to be dirty would put this guard in the way of an incident response —
# the worst possible time to be blocked by a safety device.
#
# So: shared (≥2 sessions here, or roster unreadable) → the wide refusal stands.
# Solo → the wide op is allowed with a warning, because there is no peer whose
# work it could take.
# ---------------------------------------------------------------------------
pa_sessions_in_repo() {
    local root="$1"
    pa__roster
    if [ -z "$PA_ROSTER_JSON" ]; then
        echo "unknown"
        return 0
    fi
    printf '%s' "$PA_ROSTER_JSON" | ROOT="$root" timeout 10 python3 -c '
import json, os, sys
root = os.path.realpath(os.environ.get("ROOT") or ".")
try:
    rows = json.load(sys.stdin)
except Exception:
    print("unknown"); sys.exit(0)
if not isinstance(rows, list):
    print("unknown"); sys.exit(0)
n = 0
for r in rows:
    if not isinstance(r, dict) or r.get("status") not in ("active", "paused"):
        continue
    cwd = r.get("cwd") or ""
    if not cwd:
        continue
    try:
        cwd = os.path.realpath(cwd)
    except Exception:
        continue
    if cwd == root or cwd.startswith(root + os.sep):
        n += 1
print(n)
' 2>/dev/null || echo "unknown"
}

# ---------------------------------------------------------------------------
# pa_tags_in_uncommitted <path>
#
# Ticket tags (T-NNNN) appearing in the UNCOMMITTED content of <path> — added
# lines of the HEAD→worktree diff for a tracked path, whole-file for an
# untracked one. Deliberately reads the change, not its neighbours.
# ---------------------------------------------------------------------------
pa_tags_in_uncommitted() {
    local path="$1" text
    if git ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
        text=$(git diff HEAD -- "$path" 2>/dev/null | grep '^+' 2>/dev/null)
    else
        text=$(cat -- "$path" 2>/dev/null)
    fi
    printf '%s' "$text" \
        | grep -oE 'T-[0-9]{4}' 2>/dev/null \
        | sort -u \
        | tr '\n' ' ' \
        | sed 's/ *$//'
}

# ---------------------------------------------------------------------------
# pa_stash_label
#
# A self-identifying stash message: who made it, for which ticket, and when. The
# single most useful piece of forensics during the 2026-07-30 incident was that
# the pre-deploy stash NAMED ITSELF (`pre-deploy compose <id>`) while the
# damaging one read `WIP on bot_squad/dev: <sha>` — naming neither an owner nor
# a reason. That difference is the only reason the two were distinguishable at a
# glance during recovery, so this makes naming the default rather than a habit.
#
# Format: bsq:<sid-or-user>:<task>:<utc timestamp>
# ---------------------------------------------------------------------------
pa_stash_label() {
    local sid="${BOT_SQUAD_SID:-${BSQ_SID:-}}"
    if [ -z "$sid" ] && [ -x /home/www/bot-squad/scripts/hooks/hook_my_sid.sh ]; then
        sid="$(/home/www/bot-squad/scripts/hooks/hook_my_sid.sh 2>/dev/null || echo "")"
    fi
    [ -z "$sid" ] && sid="$(id -un 2>/dev/null || echo unknown)"
    printf 'bsq:%s:%s:%s' \
        "$sid" \
        "${BOT_SQUAD_TASK_ID:-no-task}" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

# Colour helpers, shared so both guards look alike. Prefixed to avoid colliding
# with anything in a sourcing script.
pa_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
pa_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
pa_bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
