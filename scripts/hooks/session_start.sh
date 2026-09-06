#!/usr/bin/env bash
# session_start.sh — bot-squad SessionStart hook.
#
# Resolves the project from CWD against config/projects.toml, then prints
# layered vision + open task headlines + active sessions to stdout. Claude
# Code appends this output to the session's context.
#
# Referenced from each project's committed .claude/settings.json. Single
# source of truth for ALL projects.
set -uo pipefail

BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
CFG="$BOT_SQUAD/config/projects.toml"

# Claude Code passes hook event JSON on stdin: {session_id, transcript_path,
# cwd, hook_event_name}. We capture session_id so the registry uses the real
# Claude UUID instead of guessing via jsonl mtime (which races when multiple
# panes share a cwd — exactly the agent-teams case).
HOOK_INPUT="$(cat 2>/dev/null || echo '{}')"
CLAUDE_SID="$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("session_id","") or "")
except Exception: print("")' 2>/dev/null || echo "")"

# T-0203: the hook event `source` is one of startup|resume|clear|compact. We
# branch the printed CONTEXT on it so we stop re-dumping the full
# product+protocol+role banner (~3.2K tok) — and stop triggering a reflexive
# AGENT_INSTRUCTIONS.md re-read (~5.5K tok) — on every resume and every
# compaction. The registry-write + break-pane bookkeeping below still runs
# unconditionally. See vision/audits/session-context-bloat-2026-06-08.md.
HOOK_SOURCE="$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("source","") or "")
except Exception: print("")' 2>/dev/null || echo "")"

# Resolve current project by matching CWD against any of the project's
# clones (repo_path = dev clone, repo_master = master clone). Compares both
# the literal path and the resolved (readlink) path so symlinked layouts
# work in both directions.
slug=$(python3 - "$CFG" "$PWD" <<'PY'
import os, sys, tomllib
cfg_path, cwd = sys.argv[1], sys.argv[2]
real_cwd = os.path.realpath(cwd)
with open(cfg_path, "rb") as f:
    cfg = tomllib.load(f)
def _match(candidate, target):
    if not target:
        return False
    real_target = os.path.realpath(target)
    return (
        candidate == target
        or candidate.startswith(target.rstrip("/") + "/")
        or real_cwd == real_target
        or real_cwd.startswith(real_target.rstrip("/") + "/")
    )
for slug, p in cfg.get("projects", {}).items():
    for key in ("repo_path", "repo_master"):
        if _match(cwd, p.get(key, "")):
            print(slug)
            sys.exit(0)
PY
)

if [ -z "$slug" ]; then
    # Not a registered bot-squad project — silent exit.
    exit 0
fi

DATA="$BOT_SQUAD/data/$slug"

# T-0713: the EDIT-surface path, surfaced in the banner below. Every existing
# workspace guard is DOWNSTREAM of the mistake — `bsq commit` refuses commits
# made in the install clone (T-0651), the deploy aborts on local-only install
# commits (T-0110), a dirty clone gates deploys (T-0225). Nothing fires at EDIT
# time, which is where the wrong turn actually happens: a real near-miss had a
# session make its whole first edit pass against the deploy TARGET
# /home/www/bot-squad because the "edit here, not there" rule is buried ~20
# lines into AGENT_INSTRUCTIONS.md's Workspace-layout section. So name the
# correct clone up front, at session start, instead of adding another
# after-the-fact refusal (deliberately NOT a PreToolUse hook on every Edit
# call — the cost of that on every edit is not worth a P3).
# IN_MASTER tells us which clone this session actually sits in, so a prod/hotfix
# session in the master clone is not told "edit in dev" (its edit surface IS
# master). Emitted as three lines: repo_path, repo_master, and "1"/"" for
# cwd-is-under-master.
_clone_info=$(python3 - "$CFG" "$slug" "$PWD" 2>/dev/null <<'PY'
import os, sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        cfg = tomllib.load(f)
    p = cfg.get("projects", {}).get(sys.argv[2]) or {}
    dev, master = p.get("repo_path", "") or "", p.get("repo_master", "") or ""
    real_cwd = os.path.realpath(sys.argv[3])
    in_master = ""
    if master:
        rm = os.path.realpath(master)
        if real_cwd == rm or real_cwd.startswith(rm.rstrip("/") + "/"):
            in_master = "1"
    print(dev); print(master); print(in_master)
except Exception:
    print(); print(); print()
PY
)
REPO_PATH=$(printf '%s\n' "$_clone_info" | sed -n 1p)
REPO_MASTER=$(printf '%s\n' "$_clone_info" | sed -n 2p)
IN_MASTER=$(printf '%s\n' "$_clone_info" | sed -n 3p)

# Print the edit-surface guard line(s). Skipped when the clone path is unknown
# or IS the install (nothing to warn about then — no clone/target split).
print_edit_surface() {
    local surface note
    if [ -n "$IN_MASTER" ]; then
        surface="$REPO_MASTER"
        note="the MASTER clone — prod releases/hotfixes; ordinary dev work belongs in $REPO_PATH"
    else
        surface="$REPO_PATH"
        note="the dev clone — ALL code edits land here"
    fi
    [ -n "$surface" ] || return 0
    [ "$surface" != "$BOT_SQUAD" ] || return 0
    echo "- EDIT SURFACE       : $surface  ($note)"
    # The install is a deploy TARGET for CODE only: its data/ ops dir (backlog,
    # vision, sessions, scenarios) is explicitly editable in place, and sessions
    # write there constantly — so do NOT blanket-forbid the whole path or the
    # line contradicts the documented rule and gets discounted wholesale.
    echo "  NOT $BOT_SQUAD — that install is a deploy TARGET: code edits there are wiped"
    echo "  by the next deploy and a dirty target blocks deploys. Its data/ ops dir IS"
    echo "  editable in place; everything else there is read-only to you. (T-0713)"
}

# --- Registry write -------------------------------------------------------
# Compute SID from tmux context (skip if not in tmux). Write/update the
# session md so the registry has the real claude_session_id from start time.
sid="$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null || echo "")"

# Resolve task_id once in bash so both the md write and the (later)
# break-pane window name can use it. Convention: .claude/task_id file in
# cwd takes precedence; else a window name shaped like T-NNNN-* counts.
task_id=""
src_window=""
[ -n "$sid" ] && src_window="$(printf '%s' "$sid" | sed -E 's/^S-[^-]+-(.*)-p[0-9]+$/\1/')"
# T-0525: per-process spawn binding channel. The worker spawn launch command
# sets BOT_SQUAD_TASK_ID in THIS claude's environment; read it in preference to
# the shared `.claude/task_id` marker. That marker was a single mutable file in
# the SHARED working tree — under concurrent (cross-cluster) spawns, spawn B's
# write clobbered spawn A's before A's hook read it, cross-wiring A's PRIMARY
# binding to B's task. An env var is per-process and cannot be clobbered. Purely
# ADDITIVE: env-less flows (legacy resume, manual claude) fall through to the
# marker/window resolution below exactly as before.
if [ -n "${BOT_SQUAD_TASK_ID:-}" ]; then
    task_id="$(printf '%s' "$BOT_SQUAD_TASK_ID" | tr -d '[:space:]')"
fi
# T-0324 (H1): the shared-cwd `.claude/task_id` marker read-tier is RETIRED.
# T-0525 already removed every writer (spawn/resume use the per-process env
# above), but the reader survived — so marker residue (pre-fix deploy, an
# external claude) could still cross-wire an env-less session's PRIMARY to a
# task it never owned (the p179→p181 incident). Delete any residue instead of
# reading it, so no later session in this cwd can be poisoned either.
rm -f "$PWD/.claude/task_id" 2>/dev/null || true
if [ -z "$task_id" ] && [ "${src_window#T-}" != "$src_window" ]; then
    # window starts with T-; extract T-NNNN if NNNN is digits
    num="$(printf '%s' "$src_window" | sed -E 's/^T-([0-9]+).*$/\1/')"
    [ -n "$num" ] && [ "$num" != "$src_window" ] && task_id="T-$num"
fi
# T-0345 / T-0324(H1): a constant-team (queue-consumer / triage) session must
# NEVER adopt a task_id — it has no single ticket. This env-level drop covers
# spawn-time channels (env / window name); the md-persisted `owner:
# constant-team` case (resumed sessions carry NO env vars) is enforced again
# inside the python block below, where the existing md is available.
if [ "${BOT_SQUAD_OWNER:-}" = "constant-team" ]; then
    task_id=""
fi

# T-0078: capture the tmux session this pane lives in. Source of truth for
# the "copy `tmux a -t …`" affordance and the discoverability story in the
# tmux-window-strategy initiative. Re-read on every hook fire so a manual
# `tmux move-pane` shows up next time the md is rewritten.
tmux_session=""
if [ -n "${TMUX_PANE:-}" ] && command -v tmux >/dev/null 2>&1; then
    tmux_session="$(tmux display-message -p -t "$TMUX_PANE" '#S' 2>/dev/null || echo "")"
fi

if [ -n "$sid" ] && [ -n "$CLAUDE_SID" ]; then
    mkdir -p "$DATA/sessions"
    SID="$sid" SLUG="$slug" CLAUDE_SID="$CLAUDE_SID" DATA="$DATA" CWD="$PWD" TASK_ID="$task_id" INITIATIVE="${BOT_SQUAD_INITIATIVE:-}" OWNER="${BOT_SQUAD_OWNER:-}" OWNER_USER="${BOT_SQUAD_OWNER_USER:-}" TMUX_SESSION="$tmux_session" HOOK_SOURCE="$HOOK_SOURCE" python3 - <<'PY' 2>/dev/null || true
import fcntl, os, re, tempfile, time
from pathlib import Path

sid        = os.environ["SID"]
slug       = os.environ["SLUG"]
csid       = os.environ["CLAUDE_SID"]
data       = Path(os.environ["DATA"])
cwd        = os.environ["CWD"]
task_id    = os.environ.get("TASK_ID") or ""
initiative = os.environ.get("INITIATIVE") or ""
owner      = os.environ.get("OWNER") or ""
# T-0321: owner_user is the human UI username used for per-user scoping —
# a dedicated field separate from `owner` (which doubles as the constant-team /
# TL-SID binding sentinel). Inline scalar only (T-0075 hook constraint).
owner_user = os.environ.get("OWNER_USER") or ""
tmux_session = os.environ.get("TMUX_SESSION") or ""
window     = sid.rsplit("-p", 1)[0].split("-", 2)[-1] if "-p" in sid else ""
# T-0157: linux user owning this session = the SID's user segment
# (S-<user>-<window>-p<pane>). The same value compute_sid stamped at spawn.
linux_user = sid.split("-", 2)[1] if (sid.startswith("S-") and len(sid.split("-", 2)) >= 2) else ""
now        = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
md_path    = data / "sessions" / f"{sid}.md"

# T-0949: this hook is the FOURTH concurrent read-modify-writer of the session
# md (the worker's idle_timeout / graceful_exit / telemetry ticks are the other
# three), and the fields it manages — the _INFLIGHT_RECYCLE set below — are
# exactly the ones those ticks arm. Unlocked, a hook fire landing between a
# tick's read and its write silently dropped whichever side wrote first. Take
# the SAME `<file>.lock` flock the worker takes (mdlock.LOCK_SUFFIX /
# sessions.session_md_lock), around the whole read→write below.
md_path.parent.mkdir(parents=True, exist_ok=True)
_lock_fd = os.open(str(md_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(_lock_fd, fcntl.LOCK_EX)

# Preserve existing started_at + task_id + initiative + extras + owner
# (existing wins if non-empty). T-0080: owner is the UI username stamp
# passed in via BOT_SQUAD_OWNER at spawn time; preserve once stamped so
# resumes / break-pane SID rotations don't lose attribution.
#
# T-0075 CONSTRAINT — this hook is the ONE acceptable line-based frontmatter
# parser (the rest of the codebase now shares bot_squad_worker.frontmatter /
# app.frontmatter, a pyyaml pair). It is safe ONLY because every field it
# reads here is INLINE-ONLY: scalars (task_id / initiative / owner / status /
# started_at) and inline list literals (extra_task_ids: [a, b], parsed below
# by a bracket regex). The shared writer always emits lists inline and None as
# `~`, so block-style YAML never reaches this reader. Do NOT add a reader for a
# field that the writer could emit block-style — route it through the shared
# parser instead.
#
# T-0616 (D-0053 root cause) — every frontmatter field NOT managed by the
# template below is preserved VERBATIM. This rewrite used to rebuild the md
# from the template alone, silently dropping whatever another module had
# stamped on it (idle_timeout's idle_recycle_phase / idle_recycle_armed_at,
# `bsq morph`'s role, recycle_exempt,
# resume stamps). SessionStart fires with source=compact the moment a
# /compact completes — so the drop erased the in-flight recycle phase and
# idle_timeout re-armed every recycle: the stakeholder's double-compact
# (p11/p23/p29) and p8's re-compact loop. Passthrough stays within the T-0075
# constraint: unknown lines are re-emitted byte-for-byte, never parsed; a
# continuation line (indented / block-style) rides with its key's fate.
#
# ONE deliberate exception: idle_timeout's IN-FLIGHT recycle phase
# (idle_recycle_phase / idle_recycle_armed_at) is only meaningful across the
# source=compact fire — the /compact the recycle itself sent. On any other
# source (startup/resume/clear — someone deliberately started a new life for
# this pane/session) a surviving phase stamp is definitionally stale, and
# preserving it would hand the next idle tick a timed-out finalize that
# terminates the fresh session. Pre-T-0616 the clobber cleared it by
# accident; keep clearing it ON PURPOSE, everywhere except mid-recycle.
#
# T-0617: compact-and-stay (exempt user sessions) runs its OWN in-flight pair
# (compact_stay_phase / compact_stay_armed_at) through the identical hazard —
# it too sends a /compact and needs the source=compact fire that follows to
# NOT clobber its arm stamp, or idle_timeout would re-arm a second /compact on
# top of the first (the exact double-compact this exception exists to avoid).
# Same rule, same reasoning, separate fields. ``compact_stay_last_at`` is NOT
# in this set on purpose — it is a completed-fact stamp (the anti-loop guard
# for the rest of the cache window), not in-flight state, so it must survive
# every hook fire the same as any other unmanaged field, not just compact.
_MANAGED = {
    "sid", "status", "window", "cwd", "claude_uuid", "task_id", "initiative",
    "extra_task_ids", "extra_initiatives", "started_at", "owner",
    "owner_user", "tmux_session", "linux_user",
}
_INFLIGHT_RECYCLE = {
    "idle_recycle_phase", "idle_recycle_armed_at", "idle_recycle_mark",
    # T-0945: the compact_exit plan sends a /compact BETWEEN the handoff and
    # the terminate, so its "did the session write its forward-state" fact has
    # to survive the source=compact fire that /compact triggers — same hazard,
    # same rule, as the three fields above it.
    "idle_recycle_wrote_state",
    "compact_stay_phase", "compact_stay_armed_at",
    # T-0954: compact-and-stay now hands off BEFORE it squeezes, so its
    # ARM-time mark rides the same source=compact fire as the pair above
    # it — losing the mark would read as "it never wrote" and abandon a
    # handoff that had already landed.
    "compact_stay_mark",
    # T-0945: graceful_exit's pre-exit handoff wait. It never sends a /compact,
    # so in practice this only ever CLEARS — which is the point: a stale
    # "writing" phase surviving into a fresh life would hand the next tick a
    # timed-out finalize and exit the new session.
    "exit_handoff_phase", "exit_handoff_armed_at", "exit_handoff_mark",
}
hook_source = os.environ.get("HOOK_SOURCE") or ""
existing = {}
passthrough = []
if md_path.exists():
    text = md_path.read_text()
    if text.startswith("---"):
        try:
            fm = text.split("---", 2)[1]
            keep = False
            for line in fm.splitlines():
                if not line.strip():
                    continue
                m = re.match(r"([A-Za-z0-9_-]+)\s*:", line)
                if m:
                    k, _, v = line.partition(":")
                    existing[k.strip()] = v.strip()
                    keep = m.group(1) not in _MANAGED and not (
                        m.group(1) in _INFLIGHT_RECYCLE
                        and hook_source != "compact")
                if keep:
                    passthrough.append(line)
        except Exception:
            pass

if not task_id:
    task_id = existing.get("task_id") or ""
if task_id == "~":
    task_id = ""

if not initiative:
    initiative = existing.get("initiative") or ""
if initiative == "~":
    initiative = ""

if not owner:
    owner = existing.get("owner") or ""
if owner == "~":
    owner = ""

if not owner_user:
    owner_user = existing.get("owner_user") or ""
if owner_user == "~":
    owner_user = ""

# T-0078: tmux says which tmux session the pane lives in; prefer that, fall
# back to the md's prior value so an out-of-tmux re-run preserves the field.
if not tmux_session:
    tmux_session = existing.get("tmux_session") or ""
if tmux_session == "~":
    tmux_session = ""

# T-0157: prefer the SID-derived user; fall back to any prior md value so a
# legacy md (pre-T-0157) keeps whatever it had if the SID can't be parsed.
if not linux_user:
    linux_user = existing.get("linux_user") or ""
if linux_user == "~":
    linux_user = ""

# T-0324 (H1): a constant-team session must NEVER hold a primary single-ticket
# binding, whatever channel proposed it — env, window name, or a previously
# cross-wired value preserved from the existing md. This is the md-aware twin
# of the bash-level BOT_SQUAD_OWNER guard above (resumed sessions have no env
# vars, only the persisted `owner:`), and it self-heals an already-poisoned
# md on the next hook fire. Mirrors the bind_task owner=constant-team refusal.
if owner == "constant-team":
    task_id = ""

started_at = existing.get("started_at") or "~"
if started_at == "~" or not started_at:
    started_at = now

# Phase 9: extras managed by bind_task / bind_initiative actions, not the
# spawn. Preserve them verbatim across re-runs of this hook (resume, etc.).
extra_task_ids   = existing.get("extra_task_ids")   or "[]"
extra_initiatives = existing.get("extra_initiatives") or "[]"

extra_lines = "".join(line + "\n" for line in passthrough)
_body = (
    "---\n"
    f"sid: {sid}\n"
    f"status: active\n"
    f"window: {window}\n"
    f"cwd: {cwd}\n"
    f"claude_uuid: {csid}\n"
    f"task_id: {task_id or '~'}\n"
    f"initiative: {initiative or '~'}\n"
    f"extra_task_ids: {extra_task_ids}\n"
    f"extra_initiatives: {extra_initiatives}\n"
    f"started_at: {started_at}\n"
    f"owner: {owner or '~'}\n"
    f"owner_user: {owner_user or '~'}\n"
    f"tmux_session: {tmux_session or '~'}\n"
    f"linux_user: {linux_user or '~'}\n"
    + extra_lines +
    "---\n"
)
# T-0949: unique tmp + os.replace — the worker's atomic write used ONE shared
# `<name>.tmp` per md, so a racing writer could clobber the other's tmp.
_fd, _tmp = tempfile.mkstemp(dir=str(md_path.parent), prefix=md_path.name + ".",
                             suffix=".tmp")
try:
    with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
        _fh.write(_body)
    os.replace(_tmp, md_path)
finally:
    if os.path.exists(_tmp):
        os.unlink(_tmp)
    fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    os.close(_lock_fd)
PY
fi

# --- Break-pane into its own window (async, post-init) --------------------
# Claude's agent-teams feature lands teammates as split panes inside the
# lead's window. We want each teammate in its own named window so SIDs are
# stable, list_panes shows distinct windows, and humans can tab between
# teammates. Doing this synchronously races with Claude's startup send-keys
# init — backgrounding with a sleep avoids that. Only acts on panes with
# siblings; one-pane windows (e.g. the lead spawned via tmux new-window)
# are already correct, no-op.
#
# Bug #4/#6: the md is written above with the PRE-break-pane window name
# (the lead's window), so after break-pane the md filename's SID no longer
# matches the live pane's actual SID. Fix: after break-pane succeeds,
# recompute the SID from the new window name, write a NEW md at the new
# path with all fields preserved, then delete the old md.
if [ -n "${TMUX_PANE:-}" ] && command -v tmux >/dev/null 2>&1; then
    new_win_name="${task_id:-}"
    if [ -z "$new_win_name" ]; then
        # Try to extract an --agent-name from a claude process attached to
        # this pane. claude's pane subprocesses share the tmux pane PID;
        # walk /proc to find a `claude ... --agent-name X` command line.
        pane_pid="$(tmux display-message -p -t "$TMUX_PANE" -F '#{pane_pid}' 2>/dev/null || echo "")"
        agent_name=""
        if [ -n "$pane_pid" ]; then
            agent_name="$(PANE_PID="$pane_pid" python3 - <<'PY' 2>/dev/null
import os, pathlib, re
target = int(os.environ["PANE_PID"])

def children(pid):
    out = []
    for p in pathlib.Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            st = (p / "status").read_text()
        except Exception:
            continue
        m = re.search(r"^PPid:\s+(\d+)", st, re.M)
        if m and int(m.group(1)) == pid:
            out.append(int(p.name))
    return out

# BFS descendants of the pane's shell looking for `claude` with --agent-name.
seen, queue = set(), [target]
while queue:
    pid = queue.pop(0)
    if pid in seen:
        continue
    seen.add(pid)
    try:
        cmd = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except Exception:
        cmd = ""
    parts = cmd.split("\x00")
    if any("claude" in p for p in parts):
        for i, tok in enumerate(parts):
            if tok == "--agent-name" and i + 1 < len(parts):
                name = parts[i + 1].strip()
                # sanitize for tmux window name
                name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
                if name:
                    print(name)
                    raise SystemExit
    queue.extend(children(pid))
PY
)"
        fi
        if [ -n "$agent_name" ]; then
            new_win_name="$agent_name"
        else
            # Last-resort fallback: short Claude UUID prefixed with team-
            # (not claude-) to signal "agent-teams teammate".
            short="$(printf '%s' "$CLAUDE_SID" | cut -c1-8)"
            new_win_name="team-${short:-anon}"
        fi
    fi
    pane="$TMUX_PANE"
    name="$new_win_name"
    # T-0103: target the TL pane's *own* tmux session, not the project
    # slug. Agent-teams teammates spawn as sub-panes inside the lead's
    # window; the lead may live in <slug>-<initiative> (e.g.
    # bot-squad-multi_server). Hardcoding $slug as the destination would
    # send the teammate window into the main project session beside the
    # operator instead of next to its TL. Fall back to slug if the tmux
    # query fails for any reason.
    target_session="$(tmux display-message -p -t "$TMUX_PANE" '#S' 2>/dev/null || echo "$slug")"
    if [ -z "$target_session" ]; then
        target_session="$slug"
    fi
    user="$(whoami 2>/dev/null || id -un 2>/dev/null || echo u)"
    old_sid="$sid"
    data_dir="$DATA"
    # setsid + nohup detaches from claude's process group so the wait
    # survives the hook return. tmux flags: -s is the SOURCE pane to break,
    # -t is the DESTINATION (new window goes into the project's tmux
    # session, never the user's attached session).
    #
    # After break-pane: compute new SID from the post-rename window and
    # migrate the md (write new path first, then unlink old — atomic from
    # a reader's perspective: at no point is there zero md).
    setsid -f bash -c "
        sleep 4
        win=\$(tmux display-message -p -t '$pane' -F '#{window_id}' 2>/dev/null) || exit 0
        cnt=\$(tmux list-panes -t \"\$win\" 2>/dev/null | wc -l)
        if [ \"\$cnt\" -gt 1 ]; then
            if tmux break-pane -s '$pane' -t '$target_session:' -n '$name' 2>/dev/null; then
                # Migrate the md: read old, rewrite at new SID path, delete old.
                OLD_SID='$old_sid' USER_NAME='$user' PANE='$pane' DATA='$data_dir' python3 - <<'PY' 2>/dev/null || true
import os, re
from pathlib import Path
import subprocess

old_sid = os.environ['OLD_SID']
user    = os.environ['USER_NAME']
pane    = os.environ['PANE']
data    = Path(os.environ['DATA'])

# Re-query tmux for the post-break-pane window name + pane id.
def tmux_q(fmt):
    r = subprocess.run(['tmux', 'display-message', '-p', '-t', pane, '-F', fmt],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''

new_window = tmux_q('#W')
pane_raw   = tmux_q('#{pane_id}')
# T-0078: re-query the post-break-pane tmux session so the SessionMd
# reflects the destination ('<slug>-<initiative>' for an initiative TL,
# '<slug>' for the legacy main session). NOTE (T-0568): this whole block
# rides inside the outer setsid double-quoted string — backticks here are
# LIVE command substitution to the outer shell, never use them.
new_tmux_session = tmux_q('#S')
if not new_window or not pane_raw:
    raise SystemExit
new_window = re.sub(r'[^A-Za-z0-9_-]', '_', new_window)
pane_no_pct = pane_raw.lstrip('%')
new_sid = f'S-{user}-{new_window}-p{pane_no_pct}'

if new_sid == old_sid:
    # break-pane succeeded but window/pane look identical — no-op.
    raise SystemExit

old_md = data / 'sessions' / f'{old_sid}.md'
new_md = data / 'sessions' / f'{new_sid}.md'
if not old_md.exists():
    raise SystemExit

text = old_md.read_text()
# Rewrite sid: and window: lines; keep everything else verbatim.
def sub_field(t, key, value):
    pat = re.compile(rf'^{re.escape(key)}:.*$', re.M)
    if pat.search(t):
        return pat.sub(f'{key}: {value}', t, count=1)
    return t

def upsert_field(t, key, value):
    # T-0078: like sub_field but inserts a line before the closing '---'
    # when the key is absent (so a legacy md without tmux_session picks
    # the field up on its next break-pane migration).
    pat = re.compile(rf'^{re.escape(key)}:.*$', re.M)
    if pat.search(t):
        return pat.sub(f'{key}: {value}', t, count=1)
    fm_close = re.compile(r'^---\s*$', re.M)
    matches = list(fm_close.finditer(t))
    if len(matches) >= 2:
        idx = matches[1].start()
        return t[:idx] + f'{key}: {value}\n' + t[idx:]
    return t

text = sub_field(text, 'sid', new_sid)
text = sub_field(text, 'window', new_window)
if new_tmux_session:
    text = upsert_field(text, 'tmux_session', new_tmux_session)

# Atomic-ish: write new first, then unlink old. If they're the same path
# (defensive: can't happen given the new_sid==old_sid early-exit above)
# do nothing destructive.
new_md.parent.mkdir(parents=True, exist_ok=True)
new_md.write_text(text)
if old_md.resolve() != new_md.resolve():
    try:
        old_md.unlink()
    except FileNotFoundError:
        pass


# T-0072: migrate the peer-bus inbox triple from old_sid → new_sid so any
# pre-rotation messages stay readable and peers still addressing old_sid
# don't land in a dead inbox. Best-effort via the worker socket — if the
# worker is down the hook still succeeds.
import json as _t72_json
slug_for_sock = (data.parts[-1] if data.parts else '')
_t72_payload = _t72_json.dumps({
    'slug': slug_for_sock,
    'old_sid': old_sid,
    'new_sid': new_sid,
})
try:
    subprocess.run(
        ['curl', '-sS', '--max-time', '3',
         '--unix-socket', '/home/www/bot-squad/data/_sock/worker.sock',
         '-X', 'POST', '-H', 'Content-Type: application/json',
         '-d', _t72_payload,
         'http://w/actions/peer_rebind_sid'],
        capture_output=True, timeout=5, check=False,
    )
except Exception:
    pass

# T-0105: SID rotation — append new_sid to session_history of every
# task this session was bound to (primary + extras). The common
# agent-teams case has no task_id at break-pane time (lead binds the
# teammate AFTER break-pane completes), so this is a defensive no-op
# for vanilla teammate spawns. Covers the rare case where a dev
# session already bound to T-NNNN gets broken-pane'd.
task_ids: list[str] = []
m_task = re.search(r'^task_id:\s*(.*)$', text, re.M)
if m_task:
    tid = m_task.group(1).strip()
    if tid and tid != '~':
        task_ids.append(tid)
m_extras = re.search(r'^extra_task_ids:\s*\[([^\]]*)\]\s*$', text, re.M)
if m_extras:
    for raw in m_extras.group(1).split(','):
        tid = raw.strip()
        if tid and tid != '~':
            task_ids.append(tid)

if task_ids:
    backlog = data / 'backlog'
    for tid in task_ids:
        cands = sorted(backlog.glob(tid + '-*.md'))
        if not cands:
            continue
        tpath = cands[0]
        try:
            ttext = tpath.read_text()
        except OSError:
            continue
        tm = re.match(r'\A---\n(.*?)\n---\n(.*)', ttext, re.DOTALL)
        if not tm:
            continue
        fm_lines = tm.group(1).splitlines()
        tbody = tm.group(2)
        h_idx, existing = -1, []
        for i, ln in enumerate(fm_lines):
            stripped = ln.lstrip()
            if stripped.startswith('session_history:'):
                h_idx = i
                _, _, vv = stripped.partition(':')
                vv = vv.strip()
                if vv.startswith('[') and vv.endswith(']'):
                    inner = vv[1:-1].strip()
                    if inner:
                        existing = [x.strip() for x in inner.split(',') if x.strip() and x.strip() != '~']
                break
        if new_sid in existing:
            continue  # idempotent
        new_list = existing + [new_sid]
        joined = ', '.join(new_list)
        new_line = 'session_history: [' + joined + ']'
        if h_idx >= 0:
            fm_lines[h_idx] = new_line
        else:
            insert_at = len(fm_lines)
            for i, ln in enumerate(fm_lines):
                if ln.lstrip().startswith('status:'):
                    insert_at = i + 1
                    break
            fm_lines.insert(insert_at, new_line)
        new_fm = '\n'.join(fm_lines)
        content = '---\n' + new_fm + '\n---\n' + tbody
        if not tbody.startswith('\n'):
            content = '---\n' + new_fm + '\n---\n\n' + tbody
        tmp = tpath.parent / (tpath.name + '.tmp')
        try:
            tmp.write_text(content, encoding='utf-8')
            os.rename(tmp, tpath)
        except OSError:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
PY
            fi
        fi
    " </dev/null >/dev/null 2>&1 || true
fi

print_section() {
    echo
    echo "=== $1 ==="
}

# Resolve role early so per-role sections below can branch on it. Role is
# derived from the WINDOW NAME markers via bsq_derive_role — the bash mirror of
# the worker's bot_squad_worker.sessions._derive_role (the single source of
# truth for the role enum, T-0175). This replaces the old task_id heuristic
# (task-less ⟹ teamlead), which mislabelled operator sessions as teamlead and
# surfaced teamlead.md / "## Your role: TEAMLEAD" for operators (T-0041). An
# operator window (…-operator) now correctly resolves to operator.md; a
# teamlead window (…-TL) to teamlead.md; everything else to dev.md. So the hook
# banner and the UI/list_sessions badge now agree byte-for-behavior.
# shellcheck source=scripts/hooks/derive_role.sh
. "$BOT_SQUAD/scripts/hooks/derive_role.sh"
ROLE="$(bsq_derive_role "$src_window")"

# T-0203: resume / compact already carry the orientation (or, for compact, its
# summary). Emit only a tiny re-anchor + active sessions, and explicitly
# suppress the reflexive AGENT_INSTRUCTIONS.md re-read that dominated
# post-compact token cost. The lifecycle bookkeeping above already ran.
if [ "$HOOK_SOURCE" = "resume" ] || [ "$HOOK_SOURCE" = "compact" ]; then
    ROLE_UC="$(printf '%s' "$ROLE" | tr '[:lower:]' '[:upper:]')"
    print_section "BOT-SQUAD — ${HOOK_SOURCE} (orientation unchanged)"
    echo "You are ${sid:-this session} — role: ${ROLE_UC}${task_id:+, task ${task_id}}."
    # T-0507 anti-drift: a compact/resume fires precisely when context has
    # accumulated — the exact moment the stakeholder warned the target can
    # silently shift ("the target ... should not be shifted by ... if they
    # accumulate some context", Part B). Re-anchor task-bound sessions on the
    # STATED ask. Gated on task_id so task-less sessions (operator/TL) skip it.
    if [ -n "$task_id" ]; then
        echo
        echo "ANTI-DRIFT CHECK (${task_id}) — context just grew; re-anchor on the STATED ask:"
        echo "  Re-read ${task_id}'s \`## Verbatim request\` — that string is your target, not the"
        echo "  evolved mental model your accumulated context suggests. Ask yourself: is what I'm"
        echo "  doing now still targeting ${task_id}? Has scope shifted? If it drifted, return to"
        echo "  the ask (or \`bsq ticket note ${task_id}\` WHY you defer). See the bot-squad-provenance skill."
    fi
    echo "Message bus = \`bsq\`: \`bsq inbox check\` drains mail; \`bsq peer send <to> \"…\"\` sends."
    # T-0713: repeated on resume/compact even though this branch is deliberately
    # lean. A compact is precisely when a remembered PATH goes stale, and the
    # near-miss (a first edit pass into the deploy target) costs more than the
    # ~30 tokens: the downstream guards only catch it after the work is wasted.
    print_edit_surface
    echo
    echo "Your role contract, AGENT_INSTRUCTIONS.md and the team protocol are UNCHANGED"
    echo "from before the ${HOOK_SOURCE}. Re-read a specific file — or run \`bsq brief\` for"
    echo "the full orientation — ONLY if your memory of a rule/path is stale. Do NOT"
    echo "reflexively re-read AGENT_INSTRUCTIONS.md; open it on the specific need."
    if [ -d "$DATA/sessions" ]; then
        active=$(grep -l 'status: active' "$DATA/sessions"/*.md 2>/dev/null | wc -l)
        if [ "$active" -gt 0 ]; then
            print_section "ACTIVE SESSIONS"
            for f in "$DATA/sessions"/*.md; do
                grep -q 'status: active' "$f" 2>/dev/null && echo "- $(basename "$f" .md)"
            done
        fi
    fi
    exit 0
fi

# 1. Product description (small, anchors orientation). The big stuff —
# AGENT_INSTRUCTIONS.md and the full active-initiative spec — is pointed
# at, not pasted, so the per-session-start context stays lean. Agents
# read those once on their first action.
if [ -f "$DATA/vision/product.md" ]; then
    print_section "PRODUCT"
    cat "$DATA/vision/product.md"
fi

active_init=""
if [ -n "${BOT_SQUAD_INITIATIVE:-}" ]; then
    # Per-session binding: this TL was spawned with a specific initiative.
    # A project may have many active initiatives at once; each TL is bound
    # to exactly one of them (or none, for ad-hoc sessions).
    active_init="$BOT_SQUAD_INITIATIVE"
fi

print_section "ORIENTATION — read on demand (not piped in every turn, to keep context lean)"
# T-0713: FIRST line of the section on purpose — this is the one path rule a
# session needs BEFORE its first Edit call, not on demand.
print_edit_surface
echo "- Your role contract : $BOT_SQUAD/api/app/resources/roles/$ROLE.md  (git-tracked SSOT — what your spawn brief uses; T-0198)"
echo "  (your rules — read on your FIRST action if this session wasn't spawned with a brief)"
echo "- AGENT_INSTRUCTIONS  : $DATA/AGENT_INSTRUCTIONS.md  (recipes/paths — open on the specific need)"
if [ -n "$active_init" ] && [ -f "$DATA/vision/initiatives/$active_init" ]; then
    echo "- Your initiative    : $DATA/vision/initiatives/$active_init  (your bound scope)"
fi
echo "- Constitution       : $DATA/vision/constitution.md  (governance — consult when in doubt)"
echo "- Knowledge home rule: framework how-to -> skills; project-specific -> project docs ($DATA/docs/architecture/D-0040); what goes in memory vs files vs tasks ($DATA/docs/architecture/D-0041)"
echo "- Full briefing      : run \`bsq brief\`  (product + team protocol + role contract + your bindings)"

# Phase 9: surface extra bindings (multi-task devs, multi-initiative TLs).
# Read the live session md (rewritten above) to pull the current extras list.
if [ -n "$sid" ] && [ -f "$DATA/sessions/$sid.md" ]; then
    SID="$sid" SLUG="$slug" DATA="$DATA" ROLE="$ROLE" python3 - <<'PY' 2>/dev/null || true
import os
from pathlib import Path

sid  = os.environ["SID"]
data = Path(os.environ["DATA"])
role = os.environ.get("ROLE", "")
md   = data / "sessions" / f"{sid}.md"
if not md.exists():
    raise SystemExit

text = md.read_text()
if not text.startswith("---"):
    raise SystemExit
fm = text.split("---", 2)[1]
meta = {}
for line in fm.strip().splitlines():
    if ":" in line:
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()

def parse_list(v):
    v = (v or "").strip()
    if not (v.startswith("[") and v.endswith("]")):
        return []
    inner = v[1:-1].strip()
    if not inner:
        return []
    return [x.strip() for x in inner.split(",") if x.strip() and x.strip() != "~"]

extras_t = parse_list(meta.get("extra_task_ids", ""))
extras_i = parse_list(meta.get("extra_initiatives", ""))
primary_t = meta.get("task_id", "")
primary_i = meta.get("initiative", "")
if primary_t == "~":
    primary_t = ""
if primary_i == "~":
    primary_i = ""

# Devs: show task bindings if there are extras to surface.
if role == "dev" and extras_t:
    parts = [f"{primary_t} (primary)" if primary_t else "(no primary)"]
    parts.extend(extras_t)
    print()
    print(f"Your bound tasks: {', '.join(parts)} — read each task md.")

# TLs: same for initiatives.
if role == "teamlead" and extras_i:
    parts = [f"{primary_i} (primary)" if primary_i else "(no primary)"]
    parts.extend(extras_i)
    print()
    print(f"Your bound initiatives: {', '.join(parts)} — coordinate all of them.")
PY
fi

# 3. Open task headlines (TL only — devs are focused on their single task,
# the backlog is noise + a scope-expansion temptation for them).
if [ "$ROLE" = "teamlead" ] && [ -d "$DATA/backlog" ]; then
    print_section "OPEN BACKLOG"
    python3 - "$DATA/backlog" <<'PY'
import math, os, re, sys
try:
    import yaml
except ImportError:
    print("(open backlog scan unavailable: pyyaml not installed)")
    sys.exit(0)
backlog = sys.argv[1]
tasks = []
for fn in os.listdir(backlog):
    if not fn.endswith(".md"): continue
    p = os.path.join(backlog, fn)
    with open(p) as f: t = f.read()
    m = re.match(r"\A---\n(.*?)\n---\n", t, re.DOTALL)
    if not m: continue
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError: continue
    if meta.get("status") not in ("open", "in_progress", "reopened"): continue
    raw = meta.get("priority")
    if isinstance(raw, bool):
        prio = None
    elif isinstance(raw, int):
        prio = raw
    elif isinstance(raw, str):
        try: prio = int(raw.strip())
        except (ValueError, TypeError): prio = None
    else:
        prio = None
    # Missing → +inf so they sort last. Secondary sort by filename for stability.
    sort_key = (prio if prio is not None else math.inf, fn)
    tasks.append((sort_key, meta))
tasks.sort(key=lambda x: x[0])
shown = 0
for _, meta in tasks:
    print(f"- [{meta.get('id','?')}] {meta.get('title','?')}")
    shown += 1
    if shown >= 30:
        print("  ... (truncated; see ops/bot-squad/backlog/ for the rest)")
        break
PY
fi

# 3b. What the fleet's recent dispatches actually cost (T-0909, operator only).
#
# The policy — "size the model to the ticket, prefer sonnet for simple work"
# (T-0866) — lived only as prose in operator.md for a week and did not hold:
# of 198 dev dispatches measured over that week, ONE ran Sonnet. `bsq spawn`
# now refuses an unstated model choice, and this is the other half — the number
# the operator SEES, so drift is self-evident at boot instead of silent.
#
# Deliberately routed through `bsq` rather than reading the ledger inline: one
# implementation of the number, not a third copy of the pricing table. Wrapped
# in `timeout` because a session start must never hang on the worker socket,
# and failure is silent (no banner beats a scary banner about a missing file).
if [ "$ROLE" = "operator" ] && command -v bsq >/dev/null 2>&1; then
    _mc="$(timeout 5 bsq model compliance --limit 25 --role dev 2>/dev/null || true)"
    if [ -n "$_mc" ]; then
        print_section "MODEL SPEND — YOUR LAST 25 DEV DISPATCHES (T-0909)"
        echo "$_mc"
        echo
        echo "\`bsq spawn\` REFUSES a dev dispatch that does not state its model:"
        echo "  simple ticket -> \`--model sonnet\` (one-file edit, mechanical refactor,"
        echo "                   test-add, read-only audit, DoD already states the fix)"
        echo "  needs judgement the brief lacks -> \`--model opus --why \"<reason>\"\`"
        echo "The reason is recorded and read back above. Close call -> take Sonnet."
    fi
    unset _mc
fi

# 4. Active sessions
if [ -d "$DATA/sessions" ]; then
    active=$(grep -l 'status: active' "$DATA/sessions"/*.md 2>/dev/null | wc -l)
    if [ "$active" -gt 0 ]; then
        print_section "ACTIVE SESSIONS"
        for f in "$DATA/sessions"/*.md; do
            grep -q 'status: active' "$f" 2>/dev/null && echo "- $(basename "$f" .md)"
        done
    fi
fi

# 4b. Is the worktree guard actually WIRED? (T-0826)
#
# The guard scripts under scripts/hooks/ are git-tracked; the `PreToolUse` entry
# that makes them fire lives in `.claude/settings.json`, which is per-clone and
# GITIGNORED. So the two can come apart, and the way they come apart is silent:
# `api/app/project_scaffold.py::_render_claude_settings` writes a three-hook
# template, and it does not clobber an existing file — but a re-scaffold into a
# fresh or emptied `.claude/` would leave the guard scripts present and NOTHING
# CALLING THEM.
#
# ⚠ The defect that motivates this check is not "new clones lack the guard". It
# is that a clone which HAD a P1 guard can lose it with no signal anywhere — a
# protection whose absence is indistinguishable from its presence, which is the
# exact failure class the guard itself exists to fix (a bare `git stash` swept
# five sessions' work and the tree read CLEAN, exit 0).
#
# So: warn only when the guard is INSTALLED BUT UNWIRED. If the scripts are not
# there either, this clone simply does not have the guard and there is nothing
# to report. Arming it everywhere is a separate, larger decision — see
# scripts/hooks/README-git-guards.md.
if [ -n "$REPO_PATH" ] && [ -x "$REPO_PATH/scripts/hooks/bsq-worktree-guard.sh" ]; then
    # ⚠ CHECK THE PROPERTY THAT MATTERS, NOT THE ONE THAT IS EASY TO CHECK.
    # "a PreToolUse entry mentioning bsq-pretooluse-git exists" is the easy one,
    # and it is WHERE rather than WHICH: settings.json can name a path that no
    # longer exists, which is precisely how watchrobot disarmed themselves —
    # they RENAMED the guard mid-ticket and nothing noticed. So the command has
    # to resolve to a file that is actually there and actually executable. This
    # still stops short of proving the hook REFUSES anything; that costs a
    # subprocess on every session start, and it is what
    # scripts/hooks/test_worktree_guard.sh is for.
    _guard_wired=$(python3 - "$REPO_PATH/.claude/settings.json" 2>/dev/null <<'GUARD_EOF' || echo no
import json, os, sys
try:
    hooks = (json.load(open(sys.argv[1])).get("hooks") or {}).get("PreToolUse") or []
except Exception:
    print("no"); raise SystemExit(0)
for entry in hooks:
    for h in (entry or {}).get("hooks") or []:
        cmd = str((h or {}).get("command") or "")
        if "bsq-pretooluse-git" not in cmd:
            continue
        path = cmd.split()[0] if cmd.split() else ""
        print("yes" if os.access(path, os.X_OK) else "stale:%s" % path)
        raise SystemExit(0)
print("no")
GUARD_EOF
)
    if [ "${_guard_wired#stale:}" != "$_guard_wired" ]; then
        print_section "⚠ WORKTREE GUARD IS WIRED TO A PATH THAT DOES NOT RUN (T-0826)"
        cat <<GUARD_STALE
$REPO_PATH/.claude/settings.json points PreToolUse at
  ${_guard_wired#stale:}
which is missing or not executable — so the hook fails and NOTHING refuses
\`git stash\` / \`reset --hard\` / \`checkout -- .\` / \`restore .\` / \`clean -fd\`.
A RENAME does this silently; it is how watchrobot disarmed themselves mid-ticket.
Point it at $REPO_PATH/scripts/hooks/bsq-pretooluse-git.py and re-run
  bash $REPO_PATH/scripts/hooks/test_worktree_guard.sh
GUARD_STALE
    elif [ "$_guard_wired" = "no" ]; then
        print_section "⚠ WORKTREE GUARD IS INSTALLED BUT NOT ARMED (T-0826)"
        cat <<GUARD_WARN
scripts/hooks/bsq-worktree-guard.sh is present, but $REPO_PATH/.claude/settings.json
has no PreToolUse hook calling bsq-pretooluse-git.py — so NOTHING refuses
\`git stash\` / \`reset --hard\` / \`checkout -- .\` / \`restore .\` / \`clean -fd\`
in this shared tree. On 2026-07-30 one bare stash swept 24 files across six
tickets out of five sessions, and the tree read CLEAN afterwards.

Re-arm (additive — keep SessionStart/UserPromptSubmit/Stop):
  "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
    "command": "$REPO_PATH/scripts/hooks/bsq-pretooluse-git.py"}]}]

Then confirm: bash $REPO_PATH/scripts/hooks/test_worktree_guard.sh
GUARD_WARN
    fi
fi

# 5. Message bus — give every session the recipe for cross-session messaging
# via the `bsq` CLI. TLs are expected to keep a `bsq inbox wait` armed
# in the background; devs use it as a backup channel (their primary is the
# native agent-teams chat).
if [ -n "$sid" ]; then
    print_section "MESSAGE BUS"
    cat <<BUS_EOF
Cross-session messaging = the \`bsq\` CLI (\`bsq --help\` = full reference):
  bsq inbox check                  # drain new messages addressed to you
  bsq peer send <target> "<text>"  # <target> = a SID or role (teamlead/dev/all)
\`bsq peer send\` also injects "check mail" into the recipient's pane — when you
see "check mail" in your composer, run \`bsq inbox check\`. Your SID is: $sid
BUS_EOF
fi

# T-0203: the full team protocol (team_protocol.md, ~1.6K tok) and the full
# role contract (roles/<role>.md, ~1K tok) are NO LONGER auto-dumped here.
# They were pushed on every fire (~2.5K tok) and, on a `bsq spawn`, the role
# contract + hard rules are ALREADY inlined into the session's first message —
# pure duplication. Both are now surfaced on demand via `bsq brief` and pointed
# at in the ORIENTATION section above. The team protocol stays editable at
# data/<slug>/vision/team_protocol.md (Workflow UI); `bsq brief` reads it live.

exit 0
