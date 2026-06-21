"""Session manager — list, pause, resume, spawn Claude tmux sessions.

Each function is a pure worker action callable. The worker runs as almdudleer
and has access to the user's tmux server via the default socket.

Session ID (SID) format: ``S-<user>-<window>-p<pane_id_no_pct>``
  e.g. ``S-almdudleer-spec5-smoke-p2``
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot_squad_worker import frontmatter as _frontmatter


# ---------------------------------------------------------------------------
# T-0104 — activity-derived "running" vs "idle" threshold.
#
# A live claude pane bumps the mtime of its own jsonl transcript every time
# Claude writes (every assistant turn, every tool call). 30s is generous
# enough that a short tool pause does not flip the label, and short enough
# that an idle agent registers as `idle` on the next poll. Centralised here
# so the threshold lives in exactly one place; do not duplicate.
# ---------------------------------------------------------------------------
RUNNING_THRESHOLD_SEC = 30.0


# ---------------------------------------------------------------------------
# T-0046 — "active-at-prompt" threshold (separate from the running/idle one).
#
# An `active` pane whose jsonl has been quiet for this many seconds is
# treated as awaiting human input by the quick-status aggregator. Distinct
# from RUNNING_THRESHOLD_SEC: a multi-step tool chain can quietly run for
# 30-50s between assistant writes and we don't want every such pause to
# flip the project pill to needs-input. 60s is the conservative default.
# ---------------------------------------------------------------------------
IDLE_AT_PROMPT_SECONDS = float(os.environ.get("BOT_SQUAD_IDLE_AT_PROMPT_SECONDS") or 60)


# ---------------------------------------------------------------------------
# T-0233 — aggressive stale-session GC threshold.
#
# Paradigm reframe: each session is a one-time run for a specific task. An
# *exited* (pane-gone) dev whose task is still open but which has not done
# anything in this many seconds is an abandoned/crashed run; archive_dead_teammates
# reaps it (preserving the binding as last_task_id so the still-open task stays
# re-dispatchable) instead of letting it linger in the working set forever.
# Read per-call (not frozen at import) so it is env-tunable on a live worker;
# the default is aggressive-but-safe — a pane-gone run idle > 24h is dead.
# ---------------------------------------------------------------------------
DEFAULT_SESSION_STALE_SEC = 24 * 3600


def session_stale_sec() -> float:
    """Staleness grace (seconds) for the T-0233 exited-stale reaper.

    Reads ``BOT_SQUAD_SESSION_STALE_SEC`` each call (env-tunable on a live
    worker), falling back to ``DEFAULT_SESSION_STALE_SEC``.
    """
    raw = os.environ.get("BOT_SQUAD_SESSION_STALE_SEC")
    try:
        val = float(raw) if raw else DEFAULT_SESSION_STALE_SEC
    except ValueError:
        val = DEFAULT_SESSION_STALE_SEC
    return val if val > 0 else DEFAULT_SESSION_STALE_SEC


def _session_idle_suspend_sec() -> float:
    """T-0335 item-10 / Fork-2 Part B: idle-but-live dev suspend window (seconds).

    Reads ``BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC`` each call (env-tunable on a live
    worker). Ships **DARK** (D2): 0 / unset / garbage ⟹ the idle-suspend arm in
    ``archive_dead_teammates`` is OFF. The operator opts in by setting e.g.
    ``43200`` (the roadmap's recommended 12h). Suspend is reversible
    (the task stays open + re-dispatchable), so a long window is safe to enable.
    """
    raw = os.environ.get("BOT_SQUAD_SESSION_IDLE_SUSPEND_SEC")
    try:
        val = float(raw) if raw else 0.0
    except ValueError:
        return 0.0
    return val if val > 0 else 0.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PaneInfo:
    pane_id: str      # e.g. %2
    window: str
    pid: str
    cwd: str
    command: str
    session: str = ""  # tmux session name (== project slug for project panes)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper so tests can monkeypatch subprocess.run."""
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def list_panes() -> list[PaneInfo]:
    """Return all tmux panes across all sessions/windows for this user.

    Returns an empty list if tmux is not running or no panes exist.
    """
    fmt = "#{pane_id}|#{window_name}|#{pane_pid}|#{pane_current_path}|#{pane_current_command}|#{session_name}"
    result = _run(["tmux", "list-panes", "-a", "-F", fmt])
    if result.returncode != 0:
        return []
    panes = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 5)
        # Tolerate legacy 5-field lines (no session_name) — session defaults to "".
        if len(parts) < 5:
            continue
        panes.append(PaneInfo(
            pane_id=parts[0],
            window=parts[1],
            pid=parts[2],
            cwd=parts[3],
            command=parts[4],
            session=parts[5] if len(parts) >= 6 else "",
        ))
    return panes


def compute_sid(user: str, window: str, pane_id: str) -> str:
    """Compute ``S-<user>-<window>-p<pane_id_no_pct>``.

    pane_id typically looks like ``%2``; we strip the ``%``.
    """
    pane_no_pct = pane_id.lstrip("%")
    return f"S-{user}-{window}-p{pane_no_pct}"


def live_pane_map(user: str | None = None) -> dict[str, str]:
    """Canonical SID → live tmux pane_id, derived from ``list_panes()``.

    The session md ``pane_id`` field is frequently EMPTY for a live session even
    though a real tmux pane exists and the SID encodes it — so consumers must
    resolve liveness from the actual panes (the same ``compute_sid`` matching
    ``suspend()``/``gc_sessions`` use), NOT from the md field. A SID present in
    this map has a live pane; absent ⇒ no live pane (genuinely gone).
    """
    if user is None:
        user = _get_current_user()
    out: dict[str, str] = {}
    for p in list_panes():
        try:
            out[compute_sid(user, p.window, p.pane_id)] = p.pane_id
        except Exception:
            continue
    return out


def discover_claude_uuid(cwd: str, user_home: str) -> str | None:
    """Return the UUID (filename stem) of the most recent .jsonl for this cwd.

    Encodes cwd using the standard claude path-encoding:
      ``cwd.replace('/', '-')``
    Claude keeps the leading dash (e.g. ``/home/alice/repo`` →
    ``-home-alice-repo``); stripping it produces a path that doesn't exist
    on disk and causes this function to always return None for real cwds.
    Returns None if no project dir or no .jsonl files exist.
    """
    encoded = cwd.replace("/", "-")
    proj_dir = Path(user_home) / ".claude" / "projects" / encoded
    if not proj_dir.exists():
        return None
    jsonl_files = list(proj_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    # Latest by mtime → that's the active session
    latest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
    return latest.stem  # filename without .jsonl = UUID


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _pane_claude_uuid_from_proc(pane_pid: str, user_home: str) -> str | None:
    """T-0120: return the AUTHORITATIVE claude_uuid for a tmux pane via /proc.

    Walks /proc descendants of ``pane_pid`` for a ``claude`` process whose
    cmdline carries ``--resume <uuid>`` or ``--session-id <uuid>`` — that's
    the uuid the live session is writing to. Returns None if no matching
    descendant is found (e.g. fresh spawn whose cmdline is just ``claude
    --dangerously-skip-permissions``; that case is left for the
    discover_claude_uuid fallback). user_home is reserved for future use
    (e.g. a fd-based probe) and kept in the signature for parity with
    discover_claude_uuid.

    Disambiguates panes that share a cwd: ``discover_claude_uuid()`` returns
    the cwd's mtime-latest jsonl — the SAME uuid for every pane in the cwd —
    so the uuid-keyed md fallback would map every such pane to one md.
    Reading each pane's claude process directly gives a per-pane uuid.
    Pattern mirrors the descendant walk in scripts/hooks/session_start.sh.

    Why cmdline and not /proc/<pid>/fd: claude does NOT keep its transcript
    .jsonl open as a long-lived fd — it opens, appends, closes per write —
    so an fd scan races with each turn boundary. The cmdline is stable for
    the lifetime of the claude process.
    """
    del user_home  # reserved; see docstring
    try:
        target = int(pane_pid)
    except (ValueError, TypeError):
        return None
    children: dict[int, list[int]] = {}
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                st = (entry / "status").read_text()
            except OSError:
                continue
            m = re.search(r"^PPid:\s+(\d+)", st, re.M)
            if m:
                children.setdefault(int(m.group(1)), []).append(int(entry.name))
    except OSError:
        return None
    queue: list[int] = [target]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            cmd = ""
        parts = cmd.split("\x00")
        if parts and any(p.endswith("claude") or p == "claude" for p in parts[:1]):
            for i, tok in enumerate(parts):
                if tok in ("--resume", "--session-id") and i + 1 < len(parts):
                    candidate = parts[i + 1].strip()
                    if _UUID_RE.match(candidate):
                        return candidate
        queue.extend(children.get(pid, []))
    return None


def _pane_activity_at(cwd: str, claude_uuid: str | None, user_home: str) -> float | None:
    """T-0104: return the per-pane activity timestamp (epoch seconds) or None.

    Reads the mtime of the pane's own jsonl transcript file
    (``~/.claude/projects/<encoded_cwd>/<uuid>.jsonl``). Claude appends to
    that file on every assistant turn / tool call, so a fresh mtime means
    the pane is actively writing.

    Per-pane granularity comes from claude_uuid — each pane has its own
    UUID and its own jsonl. We intentionally do NOT consult
    ``<cwd>/.claude/last_user_prompt_ts`` here: that file lives at cwd
    level and is bumped by every claude pane sharing the repo, so it
    can't distinguish per-pane activity in the common bot-squad setup
    where multiple panes share a single repo cwd.

    Returns None when claude_uuid is unknown or the jsonl is missing
    (e.g. brand-new pane whose first write hasn't happened yet) — the
    caller treats this as "no recent activity".
    """
    if not claude_uuid:
        return None
    # T-0118: claude keeps the leading dash on the encoded cwd. Don't strip it.
    encoded = cwd.replace("/", "-")
    jsonl_path = Path(user_home) / ".claude" / "projects" / encoded / f"{claude_uuid}.jsonl"
    try:
        return jsonl_path.stat().st_mtime
    except OSError:
        return None


def _peer_heartbeat_at(data_dir: Path, slug: str, sid: str) -> float | None:
    """T-0037: return mtime of the peer-bus heartbeat file for this SID, or None.

    intersession.inbox_read / inbox_wait touch ``data/<slug>/_chat/heartbeat-<sid>``
    each time they run (wait re-touches every ``_HEARTBEAT_INTERVAL`` seconds
    while armed). A long-idle TL whose only activity is an armed inbox_wait
    has a stale jsonl mtime but a fresh heartbeat — folding the heartbeat into
    ``activity_at`` keeps the "Last activity" column truthful for those
    sessions instead of showing "5h ago" for a session that's polling now.
    """
    hb = data_dir / slug / "_chat" / f"heartbeat-{sid}"
    try:
        return hb.stat().st_mtime
    except OSError:
        return None


def _is_active_at_prompt(
    live_status: str,
    activity_at: float | None,
    now: float,
    *,
    threshold_sec: float | None = None,
) -> bool:
    """T-0046: True iff a live `active` pane has been idle long enough that
    the human is the bottleneck (Claude finished its turn, awaiting input).

    Coarse — pure pane-mtime check, no composer-text probe. Threshold lives
    above RUNNING_THRESHOLD_SEC so normal tool-chain pauses (30-50s between
    assistant writes) don't get mistaken for "needs-input"; only persistent
    quiet on an active pane does. ``paused`` is handled separately by the
    aggregator (Ctrl-C is its own needs-input case).

    Conservative on missing data: ``activity_at is None`` returns False —
    we can't measure idle time without a write timestamp; the next poll
    will re-evaluate once a jsonl appears.
    """
    if live_status != "active":
        return False
    if activity_at is None:
        return False
    if threshold_sec is None:
        threshold_sec = IDLE_AT_PROMPT_SECONDS
    return (now - activity_at) >= threshold_sec


def _derive_activity(
    live_status: str,
    activity_at: float | None,
    now: float,
    *,
    threshold_sec: float = RUNNING_THRESHOLD_SEC,
) -> str:
    """T-0104: map (live md status, activity_at, now) → canonical activity enum.

    Enum: ``running | idle | paused | suspended``. ``suspended`` is set by
    the caller for the no-live-pane path; this helper only handles the
    live-pane derivation.

    - ``paused`` (md says Ctrl-C'd) stays ``paused`` — distinct from idle
      so the UI can offer Resume.
    - Live pane + recent jsonl mtime (< threshold_sec) → ``running``.
    - Live pane + stale or missing mtime → ``idle``.

    Activity-derived: never trusts a self-reported `status: active` in the
    md when the jsonl tells a different story.
    """
    if live_status == "paused":
        return "paused"
    if activity_at is not None and (now - activity_at) < threshold_sec:
        return "running"
    return "idle"


# ---------------------------------------------------------------------------
# T-0141 — authoritative session role.
#
# The sessions list used to infer role purely from task_id presence ("no
# task_id ⟹ teamlead"). On a project driven by agent-teams — where the lead
# break-panes each teammate into a feature-named window and binds its task_id
# only afterwards (if at all) — nearly every task-less row rendered as a
# teamlead (stakeholder note 9: "almost all the sessions … are teamleads,
# something's leaking them"). The leak is the *default*: an ambiguous,
# marker-less, task-less session fell into the teamlead bucket.
#
# Role is now positively derived. A session is a teamlead only with real
# evidence — an explicit `-TL`/`_tl`/`teamlead` window marker. The operator pane
# is its own role. The fall-through default is "dev", never "teamlead".
#
# T-0175: the earlier "initiative binding with no task ⇒ teamlead" heuristic
# still leaked — a dev that finishes its task (task_id cleared to ~) keeps its
# initiative and flipped to teamlead. Initiative/task bindings no longer change
# the role; teamlead requires an explicit window marker. Genuine worker-spawned
# TLs carry a `-TL`/`_teamlead` window, so they are unaffected.
#
# The separator-guarded `tl` match (`(^|[-_])tl$`) avoids false positives on
# words that merely end in "tl" (e.g. `some-ctl`).
# ---------------------------------------------------------------------------
_TL_WINDOW_RE = re.compile(r"(?:^|[-_])(?:tl|teamlead)$", re.IGNORECASE)
_OPERATOR_WINDOW_RE = re.compile(r"(?:^|[-_])operator$", re.IGNORECASE)
# T-0197: prod-teamlead + qa become first-class spawnable roles. The prod-TL
# marker (`prod-tl` / `prod_teamlead`) must be tested BEFORE _TL_WINDOW_RE — a
# `…-prod-tl` window also ends in `tl`, so plain-TL precedence would otherwise
# swallow it. `prod-ops-tl` does NOT match (no `prod` adjacent to the trailing
# `-tl`), so genuine dev-side prod-ops TLs stay `teamlead`.
_PROD_TL_WINDOW_RE = re.compile(r"(?:^|[-_])prod[-_](?:tl|teamlead)$", re.IGNORECASE)
_QA_WINDOW_RE = re.compile(r"(?:^|[-_])qa$", re.IGNORECASE)

# T-0176 #3: grouping bucket for sessions with no live tmux session — keeps the
# sessions-list grouping honest against `tmux list-sessions`.
_NO_TMUX_SESSION = "(no tmux session)"


def _derive_role(
    window: str | None,
    task_id: str | None,
    initiative: str | None,
    *,
    extra_task_ids: list | None = None,
    extra_initiatives: list | None = None,
) -> str:
    """Map a session's identity fields → role enum:
    ``operator|prod-teamlead|qa|teamlead|dev``.

    Precedence (first match wins):
      1. operator window marker (`operator`, `<x>-operator`) → ``operator``
      2. prod-TL window marker (`prod-tl`, `<x>_prod_teamlead`, …)
         → ``prod-teamlead`` (T-0197 — BEFORE plain TL, since a prod-tl window
         also ends in `tl`)
      3. qa window marker (`qa`, `<x>-qa`) → ``qa`` (T-0197)
      4. explicit TL window marker (`<x>-TL`, `<x>_teamlead`, …) → ``teamlead``
      5. default → ``dev``

    T-0175: ``task_id`` / ``initiative`` (and their ``extra_*`` lists) no longer
    influence the role — they are accepted for call-site compatibility but a
    teamlead is recognised *only* by an explicit window marker. A task-less,
    initiative-bound, marker-less session is a dev (it is most often a dev that
    finished its task), not a teamlead.

    `~` is the registry's "unset" sentinel and is treated as absent.
    """
    w = (window or "").strip()
    if _OPERATOR_WINDOW_RE.search(w):
        return "operator"
    if _PROD_TL_WINDOW_RE.search(w):
        return "prod-teamlead"
    if _QA_WINDOW_RE.search(w):
        return "qa"
    if _TL_WINDOW_RE.search(w):
        return "teamlead"
    return "dev"


def _cwd_matches_repo(
    cwd: Path | str | None,
    repo_path: Path,
    repo_real: Path,
    *,
    allow_parent: bool = False,
) -> bool:
    """True when ``cwd`` belongs to the project's repo, for pane filtering and
    role-badge validation.

    Mirrors the active-pane cwd filter (T-0003): a cwd matches when it IS the
    repo, lives UNDER it, or — via resolved symlinks (symlinked dev clones) —
    matches the real target. ``repo_real`` is ``repo_path.resolve()`` (passed in
    so callers resolve it once). When ``allow_parent`` is set (operator panes
    live in the workspace *parent* of the dev clone, e.g. cwd=/home/x/bot-squad
    while repo=/home/x/bot-squad/dev), also accept a cwd that is an ANCESTOR of
    the repo. Returns False on a None/unparseable cwd.
    """
    if cwd is None or cwd == "":
        return False
    cwd_path = cwd if isinstance(cwd, Path) else Path(cwd)
    try:
        cwd_real = cwd_path.resolve()
    except OSError:
        cwd_real = cwd_path
    try:
        match = (
            cwd_path == repo_path
            or cwd_path.is_relative_to(repo_path)
            or cwd_real == repo_real
            or cwd_real.is_relative_to(repo_real)
        )
        if not match and allow_parent:
            match = (
                repo_path.is_relative_to(cwd_path)
                or repo_real.is_relative_to(cwd_path)
            )
        return match
    except (ValueError, TypeError):
        return False


def _get_user_home() -> str:
    """Return the home directory for the current user."""
    return str(Path.home())


def _session_file(data_dir: Path, slug: str, sid: str) -> Path:
    return data_dir / slug / "sessions" / f"{sid}.md"


def _find_session_md(sessions_dir: Path, sid: str, claude_uuid: str | None) -> Path | None:
    """Resolve a session md by SID, falling back to a claude_uuid scan.

    A tmux window rename leaves the metadata file at the pre-rename SID
    (e.g. ``S-alice-teamlead-p10.md``) while the live pane has computed a
    fresh SID (e.g. ``S-alice-newname-p10.md``). The SID-keyed lookup
    misses, so as a last resort scan the sessions dir for a file whose
    ``claude_uuid:`` field matches the live pane's uuid — the uuid is
    rename-invariant since it identifies the claude transcript, not the
    tmux address.
    """
    direct = sessions_dir / f"{sid}.md"
    if direct.exists():
        return direct
    if not claude_uuid or not sessions_dir.exists():
        return None
    for md in sessions_dir.glob("*.md"):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        if meta.get("claude_uuid") == claude_uuid:
            return md
    return None


def _write_session_metadata(path: Path, meta: dict, *, atomic: bool = False) -> None:
    """Write a session metadata file with YAML frontmatter.

    ``atomic=True`` writes to a sibling ``*.tmp`` then ``os.rename`` — used by
    the gc reconcilers (T-0073 / T-0077) so a concurrent reader never sees a
    half-written md.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # T-0075: delegate serialization to the shared frontmatter writer (lists
    # inline, None as `~`, timestamps unquoted). Map the legacy "~" string
    # sentinel → None so it still emits as `~` (unquoted) and round-trips to
    # None, byte-matching the pre-T-0075 hand-rolled output.
    norm = {k: (None if v == "~" else v) for k, v in meta.items()}
    body = f"---\n{_frontmatter.dump_frontmatter(norm)}---\n"
    if atomic:
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(body)
        os.rename(tmp, path)
    else:
        path.write_text(body)


def _read_session_metadata(path: Path) -> dict | None:
    """Parse YAML frontmatter from a session metadata file (T-0075: shared
    pyyaml parser, so block- and inline-style lists read identically).

    Returns None if the file doesn't exist or has no frontmatter.
    """
    if not path.exists():
        return None
    parsed = _frontmatter.parse_or_none(path.read_text())
    return parsed[0] if parsed is not None else None


def _parent_sid_of(meta: dict | None) -> str:
    """T-0128: read the persisted ``parent_sid`` from a session md, or "".

    Treats the ``~`` unset sentinel and missing field as absent (returns "").
    The empty string signals "legacy session" so the web tree falls back to
    the task→initiative→TL heuristic for that row.
    """
    if not meta:
        return ""
    v = meta.get("parent_sid")
    if v and v != "~":
        return str(v)
    return ""


def _get_current_user() -> str:
    """Return the OS username."""
    import getpass
    return getpass.getuser()


def _linux_user_from_sid(sid: str) -> str:
    """T-0157: extract the linux user from an SID ``S-<user>-<window>-p<pane>``.

    The SID's user segment is the linux login the session's tmux server runs
    as (set by ``compute_sid``). Usernames carry no ``-`` in practice (the
    same assumption ``WorkerRouter.for_sid`` makes), so ``split("-", 2)[1]``
    is the user. Returns "" when the SID doesn't parse (legacy / non-SID).
    """
    if sid and sid.startswith("S-"):
        parts = sid.split("-", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return ""


def _session_linux_user(sid: str, meta: dict | None) -> str:
    """T-0157: the linux user owning a session — explicit field wins, else SID.

    Prefer the ``linux_user`` frontmatter stamped at spawn time; fall back to
    the SID prefix so pre-T-0157 session mds (no field) still report a user.
    """
    if meta:
        v = meta.get("linux_user")
        if v and v != "~":
            return str(v)
    return _linux_user_from_sid(sid)


# T-0200: a tmux session name must never carry a shell/tmux metacharacter. A
# live incident produced the session `bot-squad-"prod-support` from an initiative
# value of `"prod-support.md` (a stray YAML quote that survived a flat parse).
# We sanitise the derived stem to this conservative charset so no quote / space /
# `$(...)` / etc. can ever leak into a `tmux new-session -s <name>` argument.
_SESSION_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _strip_surrounding_quotes(v: str) -> str:
    """Strip one layer of matching surrounding single/double quotes (T-0208).

    Quoted YAML scalars (`initiative: "x.md"`) are now the norm — the shared
    pyyaml dump (T-0075) and ``bsq task new`` both emit them. When such a
    value reaches spawn() unstripped, the trailing quote fails the
    ``.endswith(".md")`` validator. bsq's read_frontmatter strips upstream;
    this is the worker-side safety net.
    """
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _tmux_session_name(slug: str, initiative: str | None) -> str:
    """T-0001: per-initiative tmux session routing.

    Without an initiative, panes live in the project's main session named
    after ``slug`` — the operator and TL-less devs share that pane real
    estate. With an initiative, the spawned TL (and any devs spawned with
    the same initiative arg) land in a sibling session named
    ``<slug>-<initiative-stem>`` so the stakeholder can attach to one
    initiative team without the operator pane competing for the screen,
    and so initiatives don't accumulate windows in the main session.

    ``initiative`` is the basename of a file under ``vision/initiatives/``
    (e.g. ``multi-server-installation-process.md``); the stem (``Path.stem``)
    is what gets appended. Empty / None → main session.

    T-0200: the stem is sanitised to ``[A-Za-z0-9._-]`` so a malformed
    initiative value (e.g. a stray quote) can't produce a session name like
    ``bot-squad-"prod-support``. A stem that sanitises to empty → main session.
    """
    if not initiative:
        return slug
    stem = _SESSION_NAME_SAFE_RE.sub("", Path(initiative).stem)
    if not stem:
        return slug
    return f"{slug}-{stem}"


# T-0200: the tmux pane command for a Claude session is either the top-level
# ``claude`` binary or a version-named binary (e.g. ``2.1.139``) that Claude Code
# spawns for agent-teams subagents. Shared by ``list_sessions`` grouping and the
# ``gc_tmux_sessions`` reaper so both agree on what counts as a live claude pane.
_CLAUDE_CMD_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _is_claude_command(command: str) -> bool:
    return command == "claude" or bool(_CLAUDE_CMD_RE.match(command or ""))


def _ensure_project_tmux_session(slug: str, cwd: str, initiative: str | None = None) -> None:
    """Ensure a long-lived tmux session for this slug (or slug+initiative) exists.

    Per the active-context-manager model: one tmux session per project,
    panes/windows live inside it. Survives across spawn/resume cycles.
    Caller must guarantee the session is created before any new-window.

    T-0001: when ``initiative`` is set, ensure (and reuse) a sibling session
    named ``<slug>-<initiative-stem>`` instead of the main project session.
    """
    target = _tmux_session_name(slug, initiative)
    has = _run(["tmux", "has-session", "-t", target])
    if has.returncode == 0:
        return
    # Create detached; -n _init parks a placeholder window we never use for
    # claude. claude windows are added via tmux new-window -t <target>:.
    _run([
        "tmux", "new-session", "-d",
        "-s", target,
        "-c", cwd,
        "-n", "_init",
    ])


# ---------------------------------------------------------------------------
# Session manager actions
# ---------------------------------------------------------------------------

def list_sessions(cfg: Any, slug: str) -> list[dict]:
    """List all Claude sessions for a project (active + paused).

    Active sessions are discovered via tmux list-panes.
    Paused sessions are read from data/<slug>/sessions/*.md.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"list_sessions: unknown project slug {slug!r}")

    repo_path = Path(project.repo_path)
    # Dereference symlinks so symlinked dev clones (e.g. signal_tracker/dev
    # → signal_tracker_mgmt) match panes whose cwd is the real target.
    try:
        repo_real = repo_path.resolve()
    except OSError:
        repo_real = repo_path
    data_dir = cfg.data_dir
    user = _get_current_user()
    user_home = _get_user_home()

    # --- Discover active panes ---
    active_sids: set[str] = set()
    rows: list[dict] = []

    try:
        panes = list_panes()
    except Exception:
        panes = []

    # T-0176 #3: the set of tmux sessions that are actually live right now (have
    # ≥1 pane). Suspended rows group by this — a stored tmux_session that no
    # longer appears here is stale and falls into the "(no tmux session)" bucket,
    # so the UI grouping matches `tmux list-sessions` instead of stale metadata.
    live_tmux_sessions = {p.session for p in panes if p.session}

    import re as _re_cmd
    _claude_version_re = _re_cmd.compile(r"^\d+\.\d+\.\d+$")
    for pane in panes:
        pane_cwd = Path(pane.cwd) if pane.cwd else None
        # Accept top-level "claude" plus the version-named binaries Claude Code
        # uses for agent-teams subagents, e.g. "2.1.139" — these are spawned
        # from ~/.local/share/claude/versions/<ver> and tmux reports the basename.
        if pane.command != "claude" and not _claude_version_re.match(pane.command):
            continue
        if pane_cwd is None:
            continue
        # T-0003: operator pane lives in repo_workspace (parent of the dev
        # clone), e.g. cwd=/home/x/bot-squad while repo_path=/home/x/bot-squad/dev.
        # Accept the parent-cwd case only when bounded by tmux session == slug
        # (every project pane lives in a tmux session named after the slug, per
        # _ensure_project_tmux_session) or window == 'operator' (spawn fixes it
        # in routes_projects.create_project). Either bound prevents over-match
        # to unrelated panes whose cwd happens to be an ancestor of repo_path.
        # T-0220: this match is factored into _cwd_matches_repo, shared with the
        # suspended-row role-badge validation below.
        allow_parent = pane.session == slug or pane.window == "operator"
        if not _cwd_matches_repo(
            pane_cwd, repo_path, repo_real, allow_parent=allow_parent
        ):
            continue

        sid = compute_sid(user, pane.window, pane.pane_id)
        active_sids.add(sid)

        # T-0120: prefer the pane's /proc-walked uuid over the cwd-mtime
        # guess. discover_claude_uuid returns the same value for every pane
        # sharing a cwd, so the uuid fallback in _find_session_md would map
        # multiple panes to one md (3 active TLs in /home/almdudleer/bot-squad-mgmt
        # all attributed to the same started_at + initiative on staging).
        # /proc walk reads the uuid the live claude process has open — that's
        # per-pane. discover_claude_uuid stays as the fallback when the walk
        # finds nothing (e.g. claude not yet exec'd in a brand-new pane).
        claude_uuid = (
            _pane_claude_uuid_from_proc(pane.pid, user_home)
            or discover_claude_uuid(pane.cwd, user_home)
        )

        # Check for last_prompt_at via .claude/last_user_prompt_ts mtime
        last_prompt_at = None
        prompt_ts_file = repo_path / ".claude" / "last_user_prompt_ts"
        if prompt_ts_file.exists():
            try:
                last_prompt_at = prompt_ts_file.stat().st_mtime
            except OSError:
                pass

        # Resolve the session md — SID first, then claude_uuid fallback.
        # T-0118: a tmux window rename moves the live pane's computed SID
        # away from the on-disk md filename; the uuid-keyed fallback
        # recovers started_at / task_id / etc. for renamed-window panes.
        sessions_dir_path = data_dir / slug / "sessions"
        session_md_path = _find_session_md(sessions_dir_path, sid, claude_uuid)
        existing = _read_session_metadata(session_md_path) if session_md_path else None

        # T-0176 #4: a claude `/rename` changes the live tmux window name; sync
        # the stored SessionMd display label to match (it used to lag behind the
        # pre-rename window). uuid identity + the frozen sid/filename (the
        # peer-bus address) are untouched — only the human label is refreshed.
        if (
            existing is not None
            and session_md_path is not None
            and pane.window
            and existing.get("window") != pane.window
        ):
            existing["window"] = pane.window
            try:
                _write_session_metadata(session_md_path, existing, atomic=True)
            except Exception:
                pass

        started_at = None
        task_id: str | None = None
        initiative: str = ""
        # Default to "active" for live panes; if the md frontmatter says
        # paused (Ctrl-C'd but pane left open) reflect that — otherwise
        # the UI shows every live pane as active even when the user paused it.
        live_status = "active"
        # Phase 9: extras for multi-binding. Empty list when unset.
        extra_task_ids: list[str] = []
        extra_initiatives: list[str] = []
        paused_at_meta: Any = None
        archived_flag = False
        owner_meta: str = ""  # T-0080 — UI-username owner stamp; "" = legacy
        owner_user_meta: str = ""  # T-0321 — per-user-scoping username; "" = legacy
        if existing:
            started_at = existing.get("started_at")
            tid = existing.get("task_id")
            if tid and tid != "~":
                task_id = tid
            init_val = existing.get("initiative")
            if init_val and init_val != "~":
                initiative = init_val
            md_status = existing.get("status", "")
            if md_status == "paused":
                live_status = "paused"
            etids = existing.get("extra_task_ids")
            if isinstance(etids, list):
                extra_task_ids = [t for t in etids if t and t != "~"]
            einits = existing.get("extra_initiatives")
            if isinstance(einits, list):
                extra_initiatives = [i for i in einits if i and i != "~"]
            paused_at_meta = existing.get("paused_at")
            archived_flag = str(existing.get("archived", "")).lower() == "true"
            own_val = existing.get("owner")
            if own_val and own_val != "~":
                owner_meta = str(own_val)
            ou_val = existing.get("owner_user")
            if ou_val and ou_val != "~":
                owner_user_meta = str(ou_val)
            # If the md was resolved via uuid fallback (stale SID after a
            # window rename), mark the stored SID as active too so the
            # suspended-md loop below doesn't double-emit the same session.
            stored_sid = existing.get("sid")
            if stored_sid and stored_sid != sid:
                active_sids.add(stored_sid)

        # T-0104: activity-derived status. The existing `status` (md/zombie)
        # is preserved for back-compat callers and action-button routing;
        # `activity` is the canonical label-display enum derived from the
        # jsonl mtime probe. Two fields — not a replacement — per the
        # binding-audit "don't replace existing status logic, extend it".
        jsonl_at = _pane_activity_at(pane.cwd, claude_uuid, user_home)
        heartbeat_at = _peer_heartbeat_at(data_dir, slug, sid)
        # Fold jsonl mtime and peer-bus heartbeat into a single timestamp.
        # max() with None: pick whichever is non-None, or the larger when both.
        candidates = [t for t in (jsonl_at, heartbeat_at) if t is not None]
        activity_at = max(candidates) if candidates else None
        now_ts = time.time()
        activity = _derive_activity(live_status, activity_at, now_ts)
        # T-0046: an `active` pane whose jsonl has been quiet for
        # IDLE_AT_PROMPT_SECONDS is reclassified as needs-input by the
        # quick-status aggregator. The raw `status` stays "active" so
        # action-button routing (pause/suspend) is unaffected.
        active_at_prompt = _is_active_at_prompt(live_status, activity_at, now_ts)

        rows.append({
            "sid": sid,
            "status": live_status,
            "activity": activity,
            "activity_at": activity_at,
            "active_at_prompt": active_at_prompt,
            # T-0141: authoritative role — no longer "task-less ⟹ teamlead".
            "role": _derive_role(
                pane.window, task_id, initiative,
                extra_task_ids=extra_task_ids,
                extra_initiatives=extra_initiatives,
            ),
            "window": pane.window,
            "cwd": pane.cwd,
            "started_at": started_at,
            "last_prompt_at": last_prompt_at,
            "claude_uuid": claude_uuid,
            "task_id": task_id,
            "initiative": initiative,
            "extra_task_ids": extra_task_ids,
            "extra_initiatives": extra_initiatives,
            "paused_at": paused_at_meta,
            "suspended_at": None,
            "archived": archived_flag,
            "owner": owner_meta,
            "owner_user": owner_user_meta,
            # T-0128: persisted spawn-time parent (the SID that requested this
            # spawn). Empty string when unset (legacy session) — the web tree
            # falls back to the task→initiative→TL heuristic in that case.
            "parent_sid": _parent_sid_of(existing),
            # T-0157: linux user that owns this session (explicit field, else
            # SID prefix). For active panes the SID's user IS the worker's user.
            "linux_user": _session_linux_user(sid, existing),
            # T-0078: live tmux session name — for the "copy `tmux a -t …`"
            # affordance the UI offers. Comes from list-panes' session_name
            # field; falls back to whatever the md has if the pane row
            # didn't carry one (legacy 5-field tmux output).
            "tmux_session": (
                pane.session
                or (existing.get("tmux_session") if existing else "")
                or ""
            ),
        })

    # --- Non-active sessions from metadata files ---
    # Surface: explicitly paused/suspended sessions AND zombies (md says
    # "active" but the pane is gone — e.g. user closed the tmux window).
    # Anything with no live pane and a claude_uuid is resurrectable.
    sessions_dir = data_dir / slug / "sessions"
    if sessions_dir.exists():
        for meta_file in sorted(sessions_dir.glob("*.md")):
            meta = _read_session_metadata(meta_file)
            if meta is None:
                continue
            sid = meta.get("sid", meta_file.stem)
            if sid in active_sids:
                continue  # listed as active above
            status = meta.get("status", "")
            # Display status: keep "paused" only if pane is still alive;
            # otherwise anything with no pane is "suspended" (resurrectable).
            display_status = "suspended"
            if status == "paused":
                # Pane is gone (we already filtered out alive SIDs) — treat as suspended.
                display_status = "suspended"
            elif status == "suspended":
                display_status = "suspended"
            elif status == "active":
                # Zombie: registry says active but pane is gone.
                display_status = "suspended"
            else:
                # Unknown status — surface as suspended so user can resurrect.
                display_status = "suspended"
            md_task_id = meta.get("task_id")
            if md_task_id == "~":
                md_task_id = None
            md_initiative = meta.get("initiative")
            if not md_initiative or md_initiative == "~":
                md_initiative = ""
            md_extra_tids = meta.get("extra_task_ids") or []
            if not isinstance(md_extra_tids, list):
                md_extra_tids = []
            md_extra_tids = [t for t in md_extra_tids if t and t != "~"]
            md_extra_inits = meta.get("extra_initiatives") or []
            if not isinstance(md_extra_inits, list):
                md_extra_inits = []
            md_extra_inits = [i for i in md_extra_inits if i and i != "~"]
            md_archived = str(meta.get("archived", "")).lower() == "true"
            md_owner_val = meta.get("owner")
            md_owner = str(md_owner_val) if (md_owner_val and md_owner_val != "~") else ""
            md_owner_user_val = meta.get("owner_user")
            md_owner_user = str(md_owner_user_val) if (md_owner_user_val and md_owner_user_val != "~") else ""
            md_tmux_session_val = meta.get("tmux_session")
            md_tmux_session_raw = (
                str(md_tmux_session_val)
                if md_tmux_session_val and md_tmux_session_val != "~"
                else ""
            )
            # T-0176 #3: group by LIVE tmux. Keep the stored tmux session only if
            # it still has live panes; otherwise this suspended row falls into the
            # "(no tmux session)" bucket rather than a phantom group.
            md_tmux_session = (
                md_tmux_session_raw
                if md_tmux_session_raw and md_tmux_session_raw in live_tmux_sessions
                else _NO_TMUX_SESSION
            )
            # T-0141: authoritative role for suspended rows too.
            md_role = _derive_role(
                meta.get("window", ""), md_task_id, md_initiative,
                extra_task_ids=md_extra_tids,
                extra_initiatives=md_extra_inits,
            )
            # T-0220 (T-0169 audit §5): an ACTIVE pane's elevated role is already
            # backed by a cwd filter (above). For a SUSPENDED row the role comes
            # from the persisted window name ALONE — so a row whose window claims
            # an elevated role (operator/teamlead/prod-tl/qa) but whose persisted
            # cwd doesn't actually belong to this project would render a
            # misleading badge. Validate the persisted cwd with the SAME matching
            # logic the active path uses; on mismatch, neutralize to the safe
            # "dev" default and flag the row for audit. A matching cwd — or an
            # absent cwd (legacy rows that never persisted one) — leaves the role
            # untouched, so existing good rows don't regress.
            md_cwd = meta.get("cwd", "")
            if md_cwd == "~":
                md_cwd = ""
            role_cwd_mismatch = False
            if md_role != "dev" and md_cwd:
                if not _cwd_matches_repo(
                    md_cwd, repo_path, repo_real,
                    allow_parent=(md_role == "operator"),
                ):
                    md_role = "dev"
                    role_cwd_mismatch = True
            rows.append({
                "sid": sid,
                "status": display_status,
                # T-0104: no live pane → activity is unambiguously suspended,
                # regardless of what the md frontmatter claims.
                "activity": "suspended",
                "activity_at": None,
                "active_at_prompt": False,
                "role": md_role,
                # T-0220: True when an elevated window-derived role was
                # neutralized because the persisted cwd didn't match the project.
                "role_cwd_mismatch": role_cwd_mismatch,
                "window": meta.get("window", ""),
                "cwd": meta.get("cwd", ""),
                "started_at": meta.get("started_at"),
                "last_prompt_at": meta.get("suspended_at") or meta.get("paused_at"),
                "claude_uuid": meta.get("claude_uuid"),
                "task_id": md_task_id,
                "initiative": md_initiative,
                "extra_task_ids": md_extra_tids,
                "extra_initiatives": md_extra_inits,
                "paused_at": meta.get("paused_at"),
                "suspended_at": meta.get("suspended_at"),
                "archived": md_archived,
                "owner": md_owner,
                "owner_user": md_owner_user,
                # T-0128: persisted spawn-time parent for suspended rows too.
                "parent_sid": _parent_sid_of(meta),
                # T-0157: linux user owning this (suspended) session.
                "linux_user": _session_linux_user(sid, meta),
                # T-0078: surface tmux_session so the UI can still suggest the
                # right `tmux a -t …` even after suspend.
                "tmux_session": md_tmux_session,
            })

    # T-0285: stamp an explicit awaiting-input flag from the tg_stall blocked
    # markers (the agent peer_send-ed an operator and is waiting on a reply).
    # This is the precise "this one is waiting on you" signal — distinct from
    # the T-0346 paused/idle-at-prompt heuristic. Best-effort; the import is
    # lazy because tg_stall imports sessions back (cycle-safe at call time).
    try:
        from bot_squad_worker import tg_stall as _tg_stall
        _blocked = _tg_stall.blocked_sids(cfg, slug)
    except Exception:  # noqa: BLE001 — never let the watchdog wedge the list
        _blocked = set()
    for _r in rows:
        _r["awaiting_input"] = _r.get("sid") in _blocked

    return rows


def pause(cfg: Any, slug: str, sid: str) -> dict:
    """Pause a running Claude session — INTERRUPT ONLY.

    Sends Ctrl-C to the pane so Claude stops whatever it's doing and returns
    to its prompt. The pane stays open; the user can type into it directly,
    or click Resume in the UI to re-mark status active. To FREE RESOURCES,
    use suspend() instead.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: unknown project slug {slug!r}")

    user = _get_current_user()
    data_dir = cfg.data_dir

    panes = list_panes()
    target_pane: PaneInfo | None = None
    for pane in panes:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    if target_pane is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: no active pane found for SID {sid!r}")

    # Update registry status; preserve everything else.
    meta_file = _session_file(data_dir, slug, sid)
    existing = _read_session_metadata(meta_file) or {}
    existing["status"] = "paused"
    existing["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_session_metadata(meta_file, existing)

    # Just the interrupt — no /exit, no kill-pane.
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "C-c", ""])
    return {"ok": True, "paused": True}


def suspend(cfg: Any, slug: str, sid: str) -> dict:
    """Suspend a Claude session — close the pane to free resources.

    The registry md is preserved (with claude_uuid). Use resume() to
    resurrect: a new tmux window is spawned with ``claude --resume <uuid>``.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"suspend: unknown project slug {slug!r}")

    user = _get_current_user()
    user_home = _get_user_home()
    data_dir = cfg.data_dir

    panes = list_panes()
    target_pane: PaneInfo | None = None
    for pane in panes:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    meta_file = _session_file(data_dir, slug, sid)
    existing = _read_session_metadata(meta_file) or {}

    # If no live pane, treat as already suspended — just normalise the md.
    if target_pane is None:
        existing["status"] = "suspended"
        existing.setdefault("started_at", "~")
        existing.setdefault("task_id", "~")
        existing.setdefault("claude_uuid", existing.get("claude_uuid", "~"))
        _write_session_metadata(meta_file, existing)
        return {"ok": True, "suspended": True, "already_gone": True}

    claude_uuid = existing.get("claude_uuid")
    if not claude_uuid or claude_uuid == "~":
        claude_uuid = discover_claude_uuid(target_pane.cwd, user_home)
    started_at = existing.get("started_at") or "~"
    task_id = existing.get("task_id") or "~"

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # T-0080: preserve owner field across suspend/resume so per-user
    # listing filters keep working after a session is suspended.
    owner_val = existing.get("owner") or "~"
    # T-0321: same for owner_user (the per-user-scoping username).
    owner_user_val = existing.get("owner_user") or "~"
    # T-0078: preserve tmux_session across suspend → resume so a stale
    # SessionMd still carries the last-known session name (used by the
    # UI's resurrect affordance and by the one-shot backfill script).
    # Fall back to the live pane's session name when the md was missing.
    tmux_sess_val = existing.get("tmux_session")
    if not tmux_sess_val or tmux_sess_val == "~":
        tmux_sess_val = target_pane.session or "~"
    meta: dict = {
        "sid": sid,
        "status": "suspended",
        "window": target_pane.window,
        "cwd": target_pane.cwd,
        "claude_uuid": claude_uuid if claude_uuid else "~",
        "task_id": task_id,
        "started_at": started_at,
        "suspended_at": now,
        "owner": owner_val,
        "owner_user": owner_user_val,
        "tmux_session": tmux_sess_val,
    }
    _write_session_metadata(meta_file, meta)

    # Graceful exit then force-kill if needed.
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "C-c", ""])
    time.sleep(0.3)
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "/exit", "Enter"])

    deadline = time.time() + 10.0
    while time.time() < deadline:
        ids_now = {p.pane_id for p in list_panes()}
        if target_pane.pane_id not in ids_now:
            break
        time.sleep(0.5)
    else:
        _run(["tmux", "kill-pane", "-t", target_pane.pane_id])

    return {"ok": True, "suspended": True}


def resume(cfg: Any, slug: str, sid: str, initial_prompt: str | None = None,
           task_id: str | None = None) -> dict:
    """Resume a Claude session — handles paused, suspended, and zombie cases.

    - status=paused with live pane → just clear paused status; user types in tmux.
    - status=paused with no live pane → resurrect (window was closed externally).
    - status=suspended → resurrect (new window + ``claude --resume <uuid>``).
    - status=active with no live pane (zombie) → resurrect.
    - status=active with live pane → error (use pause/suspend first).

    T-0150: when ``initial_prompt`` is provided, it is delivered into the
    resumed composer (same composer-ready poll + paste-buffer path as
    ``spawn``). This powers the "resume an existing expert with a delta brief"
    flow — instead of spawning a fresh dev that re-researches from scratch, the
    caller resurrects the session that already did the related work and hands it
    the new, related task. Only delivered on the resurrect path (a new pane);
    a paused-but-live session is left for the user to type into directly.

    T-0166: when ``task_id`` is provided AND the session has no primary task
    (``task_id`` is ``~``/empty — the usual state of a suspended *expert* whose
    own task closed and was stripped to ``last_task_id`` by gc), the resumed
    session adopts ``task_id`` as its primary: we stamp ``meta["task_id"]`` and
    drop the ``.claude/task_id`` marker so the SessionStart hook agrees. Without
    this, ``bsq spawn <ticket> --bundle …`` auto-resuming an expert left the
    session with no primary, and the follow-up ``bind_task`` (which requires a
    primary) failed for *every* bundled ticket. An existing primary is left
    untouched — the new ticket then rides in via ``extra_task_ids`` as before.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    user = _get_current_user()

    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: no metadata found for SID {sid!r}")

    cwd = meta.get("cwd", str(project.repo_path))
    window = meta.get("window", "claude")
    claude_uuid = meta.get("claude_uuid")
    status = meta.get("status", "")

    # T-0166: adopt a primary task on resume when the session has none. A
    # suspended expert whose own task closed has had its primary stripped to
    # ``last_task_id`` by gc_dead_bindings, so it carries no ``task_id``; the
    # caller (bsq spawn auto-resume) passes the new primary here so the resumed
    # session is a real dev again and the follow-up bind_task has a primary to
    # attach extras to. Only fills an EMPTY primary — an expert still holding an
    # active task keeps it (the new ticket lands in extra_task_ids via bind).
    cur_primary = meta.get("task_id")
    if task_id and (not cur_primary or cur_primary == "~"):
        meta["task_id"] = task_id
        # Drop the marker the SessionStart hook reads first, so its rewrite
        # stamps the same primary (mirrors spawn()'s contract).
        try:
            marker_dir = Path(cwd) / ".claude"
            marker_dir.mkdir(parents=True, exist_ok=True)
            (marker_dir / "task_id").write_text(task_id.strip())
        except OSError:
            # Best-effort: meta["task_id"] above is the source of truth; the
            # hook preserves it via existing.get("task_id") when no marker.
            pass
    # T-0001: resurrect into the same tmux session the spawn put us in.
    # A TL spawned with an initiative lives in `<slug>-<initiative-stem>`;
    # without that routing, resume would dump it back into the main
    # project session next to the operator pane.
    init_meta = meta.get("initiative")
    resume_initiative = init_meta if (init_meta and init_meta != "~") else None

    # Is the original pane still alive?
    panes_now = list_panes()
    live_pane = None
    for pane in panes_now:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            live_pane = pane
            break

    if live_pane is not None:
        if status == "paused":
            # Just clear paused: user types in the tmux pane to continue.
            meta["status"] = "active"
            meta.pop("paused_at", None)
            _write_session_metadata(meta_file, meta)
            return {"ok": True, "sid": sid, "in_place": True}
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"resume: session {sid!r} has a live pane and is not paused — "
            f"nothing to do. Pause or suspend it first if you meant to restart."
        )

    # T-0001: resurrect into the same tmux session the spawn put us in
    # (main `<slug>` or sibling `<slug>-<initiative-stem>`).
    target_session = _tmux_session_name(slug, resume_initiative)
    _ensure_project_tmux_session(slug, cwd, resume_initiative)

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    # Spawn new window inside the project's tmux session. Use bash -lc so
    # the user's profile is sourced — claude lives in ~/.local/bin which is
    # NOT on the systemd-default PATH the worker inherits.
    # --dangerously-skip-permissions: agent-team sessions cannot pause and
    # ask the human at night; settings.json permissions.allow doesn't cover
    # writes to .claude/ which are needed for the task_id marker. The
    # stakeholder has explicitly opted into this risk class.
    if claude_uuid and claude_uuid != "~":
        cmd = f"claude --dangerously-skip-permissions --resume {claude_uuid}"
    else:
        cmd = "claude --dangerously-skip-permissions"

    result = _run([
        "tmux", "new-window", "-d",
        "-t", f"{target_session}:",
        "-n", window,
        "-c", cwd,
        "bash", "-lc", cmd,
    ])
    if result.returncode != 0:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: tmux new-window failed: {result.stderr}")

    # Discover new pane
    time.sleep(0.5)
    post_panes = list_panes()
    new_panes = [p for p in post_panes if p.pane_id not in pre_panes]
    if not new_panes:
        # Fallback: find by window name
        new_panes = [p for p in post_panes if p.window == window]
    if not new_panes:
        from bot_squad_worker.actions import ActionError
        raise ActionError("resume: could not locate new pane after tmux new-window")

    # Take the one with the highest numeric pane ID (most recently created)
    new_pane = max(new_panes, key=lambda p: int(p.pane_id.lstrip("%")) if p.pane_id.lstrip("%").isdigit() else 0)
    new_sid = compute_sid(user, new_pane.window, new_pane.pane_id)

    # Update metadata
    meta["status"] = "active"
    meta["sid"] = new_sid
    # T-0078: resurrect into the same tmux session the spawn put us in;
    # reflect that on the SessionMd so list_sessions surfaces the correct
    # `tmux a -t …` target without waiting for the hook.
    meta["tmux_session"] = target_session
    meta.pop("paused_at", None)
    new_meta_file = _session_file(data_dir, slug, new_sid)
    _write_session_metadata(new_meta_file, meta)

    # Remove old metadata file if SID changed
    if new_sid != sid and meta_file.exists():
        meta_file.unlink()

    # T-0072: migrate the peer-bus inbox triple to the new SID so messages
    # already in the pre-rotation inbox stay readable and peers still
    # addressing the old SID don't get silently dropped into a dead file.
    # Best-effort: a failure here doesn't break resume; it just leaves an
    # orphan inbox the migration script can mop up.
    if new_sid != sid:
        try:
            from bot_squad_worker import intersession as _is
            _is.rebind_sid(cfg, slug, sid, new_sid)
        except OSError:
            pass

    # T-0105: SID rotation — append the rotated SID to every task this
    # session was bound to (primary + extras). The pre-rotation SID is
    # already in history (from spawn / bind_task); now both old and new
    # remain for forensics. Idempotent if for some reason new_sid == sid.
    rotated_task_ids: list[str] = []
    primary_task = meta.get("task_id")
    if primary_task and primary_task != "~":
        rotated_task_ids.append(primary_task)
    extras_raw = meta.get("extra_task_ids") or []
    if isinstance(extras_raw, list):
        rotated_task_ids.extend(t for t in extras_raw if t and t != "~")
    if rotated_task_ids:
        backlog_dir = data_dir / slug / "backlog"
        for tid in rotated_task_ids:
            try:
                _append_task_session_history(backlog_dir, tid, new_sid)
            except OSError:
                pass

    # T-0150: deliver the delta brief into the resumed composer, same proven
    # path spawn() uses (composer-ready poll, then bracketed paste-buffer + a
    # separate Enter). If the composer never shows ❯, raise so the caller can
    # recover via inject_input — the pane is up, only the prompt didn't land.
    if initial_prompt:
        if not _wait_for_claude_composer_ready(new_pane.pane_id):
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"resume: claude composer never showed ❯ for sid {new_sid} within "
                f"{_COMPOSER_READY_TIMEOUT_SEC:.0f}s — initial_prompt not delivered "
                "(pane is up; recover via inject_input)"
            )
        _deliver_prompt(new_pane.pane_id, initial_prompt)

    return {"ok": True, "sid": new_sid}


def _append_task_session_history(
    backlog_dir: Path, task_id: str, sid: str, ts: str | None = None
) -> bool:
    """T-0105: append `sid` to the task md's `session_history:` frontmatter
    list. Append-only, idempotent (de-duped — if `sid` is already in the
    list, no-op) and atomic (tmp + rename).

    Inline-list format: ``session_history: [SID, SID, ...]`` — chosen so
    the line-based worker readers (sessions/intersession/autonomous) can
    pick it up. Block-yaml-format lists written by the api PATCH path
    would be invisible here (same hazard as the existing `blocked_by`
    field — audit Bug #4); inline format is the worker's source of truth.

    T-0291: alongside the SID, stamp a sidecar ``session_history_ts:`` map
    (``{SID: first-touch-ISO}``) so the UI can show a real first-touch time
    for a session that has since gone suspended/archived/legacy (the live
    `/sessions` join would otherwise render `—`). The map is first-touch-wins:
    because the SID dedup short-circuits below, an existing SID is never
    re-stamped. It's a parallel block-YAML field read only via the shared
    pyyaml parser — `session_history` stays the canonical inline SID list.
    `ts` defaults to now (UTC); callers/tests may pin it.

    Creates the field if absent, inserted after ``status:`` for stable
    ordering. Returns True iff the file was modified.

    Best-effort: returns False on any I/O or parse failure — the binding
    write itself is the source of truth, the task-md stamp is a forensic
    convenience.
    """
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        return False
    path = matches[0]
    try:
        text = path.read_text()
    except OSError:
        return False
    parsed = _frontmatter.parse_or_none(text)  # T-0075: shared parser
    if parsed is None:
        return False
    meta, body = parsed

    existing = _frontmatter.as_list(meta.get("session_history"))
    if sid in existing:
        return False  # idempotent — de-dup, preserve order (first-touch ts kept)

    new_list = existing + [sid]
    # T-0291: stamp this SID's first-touch ts into the sidecar map.
    stamp = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw_ts = meta.get("session_history_ts")
    ts_map = dict(raw_ts) if isinstance(raw_ts, dict) else {}
    ts_map.setdefault(sid, stamp)  # first-touch wins (sid is new here anyway)

    if "session_history" in meta:
        meta["session_history"] = new_list
        meta["session_history_ts"] = ts_map
    else:
        # Insert after `status` for stable ordering (else append at end).
        rebuilt: dict = {}
        inserted = False
        for k, v in meta.items():
            rebuilt[k] = v
            if k == "status":
                rebuilt["session_history"] = new_list
                rebuilt["session_history_ts"] = ts_map
                inserted = True
        if not inserted:
            rebuilt["session_history"] = new_list
            rebuilt["session_history_ts"] = ts_map
        meta = rebuilt

    content = _frontmatter.dump(meta, body)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)
    return True


def _write_task_initiative_if_absent(backlog_dir: Path, task_id: str, initiative: str) -> bool:
    """T-0038: stamp `initiative: <basename>` into the task md's frontmatter
    if no `initiative:` field is present.

    Existing-wins: if the task already has an initiative (even a different
    one), the file is left alone — the operator's manual classification is
    authoritative.

    Returns True iff the file was modified.
    """
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        return False
    path = matches[0]
    try:
        text = path.read_text()
    except OSError:
        return False
    parsed = _frontmatter.parse_or_none(text)  # T-0075: shared parser
    if parsed is None:
        return False
    meta, body = parsed
    if "initiative" in meta:
        return False  # already set (even if `~`) — don't clobber
    # Insert after the `status` key for stable ordering; if no status key,
    # append at the end of frontmatter.
    rebuilt: dict = {}
    inserted = False
    for k, v in meta.items():
        rebuilt[k] = v
        if k == "status":
            rebuilt["initiative"] = initiative
            inserted = True
    if not inserted:
        rebuilt["initiative"] = initiative
    content = _frontmatter.dump(rebuilt, body)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)
    return True


# T-0126: composer-ready poll. The `❯` rune is rendered by Claude Code's
# TUI inside the input box and never appears in the bash prompt that runs
# before claude takes over the pane, so its presence is a reliable signal
# that the composer accepts keystrokes. Total budget = 15s; interval 0.3s
# keeps the polling pressure on tmux well under one request per claude
# render frame on a loaded host.
_COMPOSER_READY_TIMEOUT_SEC = 15.0
_COMPOSER_READY_POLL_INTERVAL_SEC = 0.3

# T-0201: confirm-then-Enter knobs for _deliver_prompt. The old blind
# `time.sleep(0.4)` was too short for a large (~140-line) bracketed paste —
# Claude's composer had not finished ingesting the paste when Enter landed, so
# the Enter was swallowed / landed inside the bracketed-paste wrapper and the
# brief sat unsubmitted as "[Pasted text #1 +N lines]". We now (1) poll until
# the paste actually shows in the composer, then (2) send Enter and poll until
# the composer clears, re-sending Enter up to a cap so a genuinely stuck pane
# fails loudly rather than looping forever. Read at call time so tests can
# monkeypatch them to bound runtime.
_PASTE_LANDED_TIMEOUT_SEC = 8.0
_PASTE_LANDED_POLL_INTERVAL_SEC = 0.3
_SUBMIT_MAX_RETRIES = 5
_SUBMIT_CONFIRM_TIMEOUT_SEC = 4.0
_SUBMIT_CONFIRM_POLL_INTERVAL_SEC = 0.3


def _wait_for_claude_composer_ready(pane_id: str) -> bool:
    """Poll ``tmux capture-pane`` until Claude's composer prompt rune appears.

    Returns True as soon as ``❯`` shows up in the pane buffer, or False if
    the budget elapses with no marker. Used by ``spawn()`` to avoid the
    initial_prompt-drop regression (T-0126): send-keys against a pane that
    is still in bash / claude-init swallows the text or the Enter.

    Reads ``_COMPOSER_READY_TIMEOUT_SEC`` / ``_COMPOSER_READY_POLL_INTERVAL_SEC``
    at call time (not def time) so tests can monkeypatch the module-level
    constants to bound runtime.
    """
    timeout_sec = _COMPOSER_READY_TIMEOUT_SEC
    interval_sec = _COMPOSER_READY_POLL_INTERVAL_SEC
    iterations = max(1, int(timeout_sec / interval_sec))
    for _ in range(iterations):
        cap = _run(["tmux", "capture-pane", "-t", pane_id, "-p"])
        if cap.returncode == 0 and "❯" in cap.stdout:
            return True
        time.sleep(interval_sec)
    return False


def _composer_content(pane_id: str) -> str | None:
    """Return the text Claude's composer currently holds, or None if the pane
    can't be captured / has no composer line.

    The composer prompt line is ``❯ <content>``. ``""`` means an empty composer
    (``❯`` with nothing after it); a non-empty string means the composer holds
    pasted/typed content — the literal text for a short paste, or a
    ``[Pasted text #N +M lines]`` placeholder for a large bracketed paste
    (verified live, T-0201). The composer is always rendered at the BOTTOM of
    the TUI, so we take the last ``❯`` line: even if the conversation
    scrollback (or the pasted brief itself) contains a ``❯`` above, the input
    box is the bottom-most one.
    """
    cap = _run(["tmux", "capture-pane", "-t", pane_id, "-p"])
    if cap.returncode != 0:
        return None
    content: str | None = None
    for line in cap.stdout.splitlines():
        idx = line.find("❯")
        if idx != -1:
            content = line[idx + 1:].strip()
    return content


def _deliver_prompt(pane_id: str, text: str) -> None:
    """Reliably deliver a prompt into a claude composer (T-0126/T-0144/T-0201).

    Loads ``text`` into a dedicated tmux paste buffer and pastes it in
    bracketed-paste mode (``paste-buffer -p``), then submits with a *separate*
    Enter. This is the operator's proven manual recovery (load-buffer +
    paste-buffer + Enter): unlike ``send-keys`` of a long literal — which can
    interleave with the TUI, mis-escape, or exceed argv limits, and whose
    chained Enter lands *inside* the bracketed-paste wrap rather than
    submitting — a paste-buffer lands the whole prompt in claude's input box
    atomically as one block (embedded newlines stay newlines, not submits),
    and the trailing standalone Enter is what submits it.

    T-0201: the submit is now confirm-then-Enter instead of a blind
    ``time.sleep(0.4)``. A large (~140-line) bracketed paste takes ~1s to fully
    ingest; a 0.4s sleep let the Enter land before the paste settled, so the
    brief sat unsubmitted as "[Pasted text #1 +N lines]". We (1) poll until the
    paste actually appears in the composer, then (2) send Enter and poll until
    the composer clears, re-sending Enter up to ``_SUBMIT_MAX_RETRIES`` so a
    genuinely stuck pane fails loudly rather than looping forever.

    Raises ``ActionError`` if the paste never lands or the composer never
    clears — the pane is up and the text is in the buffer, so the caller can
    recover via ``inject_input`` / a manual Enter (same contract as the
    composer-ready check the callers run just before this).
    """
    from bot_squad_worker.actions import ActionError

    import re as _re
    digits = _re.sub(r"[^0-9]", "", pane_id) or "x"
    buf = f"bsq-prompt-{digits}"
    # T-0201: load the text via `load-buffer -` (stdin), NOT `set-buffer -- <arg>`.
    # `set-buffer` passes the data as a command ARGUMENT, which tmux's own
    # command parser rejects with "command too long" for a large (~140-line)
    # brief — verified live — so the buffer was never created and nothing got
    # pasted. `load-buffer -` streams the body in over stdin with no length
    # limit, and (unlike a temp file) sidesteps the worker's read-only /tmp.
    _run(["tmux", "load-buffer", "-b", buf, "-"], input=text)
    _run(["tmux", "paste-buffer", "-t", pane_id, "-b", buf, "-p", "-d"])

    # (1) Confirm the paste landed in the composer (non-empty) before Enter.
    land_iters = max(1, int(_PASTE_LANDED_TIMEOUT_SEC / _PASTE_LANDED_POLL_INTERVAL_SEC))
    landed = False
    for _ in range(land_iters):
        if _composer_content(pane_id):
            landed = True
            break
        time.sleep(_PASTE_LANDED_POLL_INTERVAL_SEC)
    if not landed:
        raise ActionError(
            f"_deliver_prompt: paste never appeared in the composer for pane "
            f"{pane_id} within {_PASTE_LANDED_TIMEOUT_SEC:.0f}s — prompt not "
            "delivered (pane is up; recover via inject_input)"
        )

    # (2) Submit; confirm the composer cleared, re-sending Enter up to the cap.
    confirm_iters = max(1, int(_SUBMIT_CONFIRM_TIMEOUT_SEC / _SUBMIT_CONFIRM_POLL_INTERVAL_SEC))
    for _attempt in range(_SUBMIT_MAX_RETRIES):
        _run(["tmux", "send-keys", "-t", pane_id, "Enter"])
        for _ in range(confirm_iters):
            time.sleep(_SUBMIT_CONFIRM_POLL_INTERVAL_SEC)
            if _composer_content(pane_id) == "":
                return  # composer cleared -> the prompt was submitted
        # still showing the paste -> Enter was swallowed; resend on next loop.
    raise ActionError(
        f"_deliver_prompt: composer never cleared after {_SUBMIT_MAX_RETRIES} "
        f"Enter attempts for pane {pane_id} — prompt may be unsubmitted "
        "(pane is up; recover via inject_input / a manual Enter)"
    )


def spawn(
    cfg: Any,
    slug: str,
    window: str,
    initial_prompt: str | None = None,
    task_id: str | None = None,
    initiative: str | None = None,
    owner: str | None = None,
    parent_sid: str | None = None,
    owner_user: str | None = None,
) -> dict:
    """Spawn a new Claude session in the project's repo.

    Opens a new tmux window, starts claude (no resume), and optionally
    sends an initial_prompt after a short delay.

    If task_id is provided, writes ``.claude/task_id`` in the project's
    repo *before* spawning so the SessionStart hook links the new session
    to that backlog task automatically.

    If initiative is provided (a filename under vision/initiatives/), the
    SessionStart hook is told via the BOT_SQUAD_INITIATIVE env var to use
    that file instead of the project's global active_initiative. Lets the
    stakeholder spawn multiple TLs on different initiatives in parallel.

    T-0001: when ``initiative`` is set, the new tmux window is routed into
    a sibling session named ``<slug>-<initiative-stem>`` instead of the
    main ``<slug>`` session (which hosts the operator pane). Devs spawned
    with the same initiative arg join that same sibling session — keeping
    initiative-team traffic separated per-team and the operator pane
    uncluttered. ``initiative=None`` keeps the legacy behaviour.

    If owner is provided (T-0080), the spawned session md gets stamped
    with ``owner: <username>`` so per-user listing filters can scope
    results without relying on the SID linux_user prefix. The owner is
    the UI username from the JWT claims, not the linux_user.

    T-0128: when ``parent_sid`` is provided, it is the SID of the session
    that requested this spawn (operator→TL, TL→dev). It is stamped into the
    new session's md frontmatter so the session-tree is reliable across
    worker restarts instead of being reconstructed from the
    task→initiative→TL heuristic. Strictly optional and backward-compatible:
    legacy spawns / callers that omit it leave the field unset, and the
    ``backfill_parent_sid`` pass populates it for them via the heuristic.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: unknown project slug {slug!r}")

    # T-0321: when the caller didn't pass an explicit owner_user (the human
    # scoping username), derive it from `owner` — so TL-spawned devs and
    # constant-team sessions still scope to a real user, not the sentinel.
    if owner_user is None:
        owner_user = _derive_owner_user(cfg, slug, owner)

    # T-0239: enforce the user-set parallel-sessions cap BEFORE any spawn side
    # effect (the task_id marker, tmux session, pane). At/over cap is a
    # capacity-reached refusal so the task stays pending, never a silent drop.
    _enforce_parallel_cap(cfg)
    # T-0306: same for the max_total_tokens budget (output tokens this quota
    # period). 0 = unlimited; over-budget refuses so the task stays pending.
    _enforce_token_cap(cfg)

    # T-0208: defense-in-depth — tolerate a quoted frontmatter scalar passed
    # through from a ticket (`initiative: "x.md"`). The bsq read_frontmatter
    # fix strips these upstream, but a caller (older bsq, raw worker action)
    # may still hand us `"x.md"`; normalize once here so the validator, the
    # task-md initiative stamp, the tmux session name, and the env var all
    # see the bare value.
    if initiative:
        initiative = _strip_surrounding_quotes(initiative.strip())

    cwd = str(project.repo_path)
    user = _get_current_user()

    # Item 3 (audit Fork-2 Part A): claim the task under bind_task's
    # ``.task-claim.lock`` for the WHOLE spawn span (check → spawn → seed-meta
    # stamp) so a concurrent spawn for the SAME task can't interleave and
    # double-bind. Refuse at the OPEN if a live session already owns it (before
    # writing the marker / opening a tmux window — no wasted session). The lock
    # is held until the seed-meta stamp below makes the new session a live owner,
    # then closed (closing the fd releases the flock); on any error path the
    # frame unwinds and CPython drops + closes the fd, releasing the lock. This
    # converts gc_stale_bindings from correctness-critical to a crash-only
    # backstop (see its docstring).
    _claim_fd = None
    if task_id:
        _claim_backlog = cfg.data_dir / slug / "backlog"
        _claim_backlog.mkdir(parents=True, exist_ok=True)
        _claim_fd = open(_claim_backlog / ".task-claim.lock", "w")
        fcntl.flock(_claim_fd, fcntl.LOCK_EX)
        _claim_live = _live_task_owner(cfg.data_dir, slug, task_id.strip())
        if _claim_live is not None:
            _claim_fd.close()
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"spawn: task {task_id.strip()} already bound to live session "
                f"{_claim_live}; refusing dup-bind"
            )

    # Drop the task_id marker so SessionStart picks it up.
    if task_id:
        marker_dir = project.repo_path / ".claude"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "task_id").write_text(task_id.strip())

    # T-0038: when both a task and an initiative are known at spawn time,
    # stamp the initiative onto the task md so it's queryable without grep
    # (board group-by, filter, etc.). Existing initiative wins — operator
    # classification is authoritative.
    if task_id and initiative:
        try:
            _write_task_initiative_if_absent(
                cfg.data_dir / slug / "backlog",
                task_id.strip(),
                initiative.strip(),
            )
        except OSError:
            # Best-effort: the spawn itself is the source of truth; the task
            # md stamp is a convenience for the UI.
            pass

    # T-0001: initiative-keyed tmux session routing — TL spawns with an
    # initiative arg land in a sibling `<slug>-<initiative-stem>` session,
    # not the main `<slug>` session shared with the operator. Devs spawned
    # with the same initiative arg join that sibling session, keeping the
    # operator pane uncluttered.
    target_session = _tmux_session_name(slug, initiative)
    _ensure_project_tmux_session(slug, cwd, initiative)

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    # bash -lc so claude (in ~/.local/bin) is on PATH — the worker's
    # systemd env does not include the user's local bin directory.
    # --dangerously-skip-permissions: see resume() rationale above.
    # BOT_SQUAD_INITIATIVE: per-session initiative override (Phase 4).
    # BOT_SQUAD_OWNER (T-0080): per-session owner stamp picked up by the
    # SessionStart hook and written into the SessionMd frontmatter.
    env_prefix_parts: list[str] = []
    if initiative:
        # Basic safety: only basename, must end .md, no slashes/..
        clean = initiative.strip()
        if "/" in clean or ".." in clean or not clean.endswith(".md"):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid initiative name {initiative!r}")
        env_prefix_parts.append(f"BOT_SQUAD_INITIATIVE={shlex.quote(clean)}")
    if owner:
        # Username sanity: alnum + _.- only. The username is API-provided
        # (JWT claim) but we still defence-in-depth-validate before shoving
        # it into a shell env-var assignment.
        owner_clean = owner.strip()
        if not owner_clean or not re.match(r"^[A-Za-z0-9_.-]+$", owner_clean):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid owner {owner!r}")
        env_prefix_parts.append(f"BOT_SQUAD_OWNER={shlex.quote(owner_clean)}")
    if owner_user:
        # T-0321: the human UI username for per-user scoping (distinct from
        # `owner`, which doubles as the constant-team/TL-SID binding sentinel).
        ou_clean = owner_user.strip()
        if not ou_clean or not re.match(r"^[A-Za-z0-9_.-]+$", ou_clean):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid owner_user {owner_user!r}")
        env_prefix_parts.append(f"BOT_SQUAD_OWNER_USER={shlex.quote(ou_clean)}")
    env_prefix = (" ".join(env_prefix_parts) + " ") if env_prefix_parts else ""
    shell_cmd = f"{env_prefix}claude --dangerously-skip-permissions"

    result = _run([
        "tmux", "new-window", "-d",
        "-t", f"{target_session}:",
        "-n", window,
        "-c", cwd,
        "bash", "-lc", shell_cmd,
    ])
    if result.returncode != 0:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: tmux new-window failed: {result.stderr}")

    # Discover new pane
    time.sleep(0.5)
    post_panes = list_panes()
    new_panes = [p for p in post_panes if p.pane_id not in pre_panes]
    if not new_panes:
        new_panes = [p for p in post_panes if p.window == window]
    if not new_panes:
        from bot_squad_worker.actions import ActionError
        raise ActionError("spawn: could not locate new pane after tmux new-window")

    new_pane = max(new_panes, key=lambda p: int(p.pane_id.lstrip("%")) if p.pane_id.lstrip("%").isdigit() else 0)
    new_sid = compute_sid(user, new_pane.window, new_pane.pane_id)

    # T-0078: pre-stamp tmux_session on the SessionMd so the field is
    # populated even before the SessionStart hook fires (and survives the
    # hook's rewrite, which now preserves tmux_session via env passthrough).
    # We use the deterministic _tmux_session_name(slug, initiative) value;
    # the hook's `tmux display-message #S` read against the live pane will
    # converge on the same string. pane.session would also work but legacy
    # 5-field tmux output drops it.
    seed_meta_file = _session_file(cfg.data_dir, slug, new_sid)
    seed_meta = _read_session_metadata(seed_meta_file) or {}
    seed_meta.setdefault("sid", new_sid)
    seed_meta["tmux_session"] = target_session
    # T-0157: stamp the spawning linux user so the SessionMd carries an
    # explicit user mark (the SID prefix already encodes it, but the field
    # makes per-user listing/grouping robust to SID rotation).
    seed_meta.setdefault("linux_user", user)
    # T-0128: stamp the requesting session's SID as parent_sid so the
    # session-tree survives worker restart without the heuristic. Only
    # when provided and non-self; never clobber an already-set value.
    if parent_sid:
        ps = str(parent_sid).strip()
        if ps and ps != "~" and ps != new_sid and not seed_meta.get("parent_sid"):
            seed_meta["parent_sid"] = ps
    if task_id:
        # Item 3: stamp the claim so the new session is IMMEDIATELY a live task
        # owner (before the SessionStart hook runs), closing the dup-bind TOCTOU.
        # This write is correctness-critical under the claim lock — it MUST raise
        # on failure (a swallowed error silently reopens the race), so it is NOT
        # wrapped in the best-effort OSError guard the no-task path keeps.
        seed_meta["task_id"] = task_id.strip()
        seed_meta["status"] = "active"
        _write_session_metadata(seed_meta_file, seed_meta)
        # Now a live owner — release the claim flock (closing the fd unlocks).
        if _claim_fd is not None:
            _claim_fd.close()
            _claim_fd = None
    else:
        try:
            _write_session_metadata(seed_meta_file, seed_meta)
        except OSError:
            # Best-effort: the hook will populate the field next time it fires.
            pass

    # T-0105: stamp the freshly-spawned SID into the task md's
    # session_history list so the task carries forensics for *every*
    # session that worked on it, not just the current binding. Best-effort.
    if task_id:
        try:
            _append_task_session_history(
                cfg.data_dir / slug / "backlog",
                task_id.strip(),
                new_sid,
            )
        except OSError:
            pass

    # Deliver the initial prompt once the composer is up.
    #
    # T-0126 first gated delivery on a `❯`-composer poll (kept below) but still
    # typed via `send-keys <long-literal>`, which is the unreliable part the
    # operator worked around by hand: a long send-keys literal can interleave
    # with the TUI, mis-escape, or exceed limits, and the trailing Enter lands
    # inside tmux's bracketed-paste wrap instead of submitting.
    #
    # T-0144 root-causes it: deliver via the operator's proven manual recovery
    # — load the text into a tmux paste buffer and paste it (bracketed-paste,
    # length-safe, atomic), then a SEPARATE Enter to submit. See
    # `_deliver_prompt`. The composer-ready poll still guards against typing
    # before claude's TUI exists; if `❯` never appears, raise so the caller can
    # recover via inject_input (the pane is up).
    if initial_prompt:
        if not _wait_for_claude_composer_ready(new_pane.pane_id):
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"spawn: claude composer never showed ❯ for sid {new_sid} within "
                f"{_COMPOSER_READY_TIMEOUT_SEC:.0f}s — initial_prompt not delivered "
                "(pane is up; recover via inject_input)"
            )
        _deliver_prompt(new_pane.pane_id, initial_prompt)

    return {"ok": True, "sid": new_sid}


# ---------------------------------------------------------------------------
# Phase 9: multi-binding helpers
# ---------------------------------------------------------------------------

def _full_task_set(meta: dict) -> set[str]:
    """Return {primary, *extras} of task IDs for a session md frontmatter."""
    out: set[str] = set()
    tid = meta.get("task_id")
    if tid and tid != "~":
        out.add(tid)
    extras = meta.get("extra_task_ids") or []
    if isinstance(extras, list):
        for t in extras:
            if t and t != "~":
                out.add(t)
    return out


def _full_initiative_set(meta: dict) -> set[str]:
    """Return {primary, *extras} of initiative basenames for a session md."""
    out: set[str] = set()
    init = meta.get("initiative")
    if init and init != "~":
        out.add(init)
    extras = meta.get("extra_initiatives") or []
    if isinstance(extras, list):
        for i in extras:
            if i and i != "~":
                out.add(i)
    return out


def _find_owner(
    data_dir: Path,
    slug: str,
    *,
    task_id: str | None = None,
    initiative: str | None = None,
) -> str | None:
    """Scan all session mds; return SID of the session that already holds the
    given task_id or initiative (primary or extras). None if free.
    """
    sess_dir = data_dir / slug / "sessions"
    if not sess_dir.exists():
        return None
    for md in sorted(sess_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        sid = meta.get("sid", md.stem)
        if task_id and task_id in _full_task_set(meta):
            return sid
        if initiative and initiative in _full_initiative_set(meta):
            return sid
    return None


# T-0237 Layer-1: strict task<->session binding cap. The reframe ("each task is
# a one-time run executed by a tightly-capped set of sessions") fixes the cap at
# ONE live session per task (operator-confirmed S1). Suspended/archived holders
# are historical records (their binding is GC'd to last_task_id) and never count
# toward the cap, so a fresh run can always rebind an abandoned task.
BINDING_CAP = 1


def _is_live_holder(meta: dict) -> bool:
    """Whether a session counts toward a task's binding cap: a running process
    (status active/paused), not a suspended/archived historical record.

    Uses the persisted ``status`` (which ``gc_sessions`` keeps tick-synced with
    real tmux panes) rather than a live pane scan, so the bind path stays a pure
    data operation under the claim flock.
    """
    if str(meta.get("archived", "")).lower() == "true":
        return False
    return str(meta.get("status", "")).lower() in ("active", "paused")


def _live_task_owner(
    data_dir: Path, slug: str, task_id: str, *, exclude_sid: str | None = None
) -> str | None:
    """SID of a *live* session (see ``_is_live_holder``) already holding
    ``task_id`` (primary or extras), or None. Suspended/archived holders are
    skipped — they do not gatekeep a rebind under the T-0237 cap.

    T-0402: ``_is_live_holder`` trusts the persisted ``status: active`` alone,
    but a crashed dev's md lingers ``active`` (gc_sessions can't reconcile an
    empty-pane_id md — the T-0134 guard). Such a phantom would PERMANENTLY
    gatekeep its task ('already bound to live session {dead}'), the exact
    failure this fn promises to prevent. So a holder must ALSO map to a pane
    running a live claude agent (``_live_agent_sids``) — the same reconcile the
    T-0397 ``_count_live_sessions`` fix (d0b3cdc) applies to the parallel cap.
    Computed once per call (a tmux + /proc scan), so the bind path is no longer
    a pure data op but stays under the claim flock.
    """
    sess_dir = data_dir / slug / "sessions"
    if not sess_dir.exists():
        return None
    live = _live_agent_sids()
    for md in sorted(sess_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        sid = meta.get("sid", md.stem)
        if exclude_sid and sid == exclude_sid:
            continue
        if task_id in _full_task_set(meta) and _is_live_holder(meta) and sid in live:
            return sid
    return None


# T-0239 slice 2: enforce the user-set resource caps at spawn-time. The caps
# live in the API's system_settings.toml ([caps] section, written by
# /api/system-settings); the worker reads them FRESH on each spawn so a cap
# change takes effect on the next spawn after a worker restart (the PUT returns
# restart_required=True). 0 / absent = unlimited (a fresh/legacy install is
# uncapped — back-compat).
def _caps_config_dir(cfg: Any) -> Path:
    """The config dir holding system_settings.toml. Prefers cfg.config_dir;
    falls back to the install-root layout (``<data_dir>/../config``) so a test
    cfg that carries only data_dir still resolves it."""
    cd = getattr(cfg, "config_dir", None)
    if cd:
        return Path(cd)
    return Path(cfg.data_dir).parent / "config"


def _read_caps(config_dir: Path) -> dict:
    """Fresh-read the [caps] section from system_settings.toml. Missing file /
    unparseable / missing keys → unlimited (0). Negative → 0 (unlimited)."""
    path = Path(config_dir) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {"max_parallel_sessions": 0, "max_total_tokens": 0}
    caps = raw.get("caps", {}) or {}

    def _c(key: str) -> int:
        try:
            v = int(caps.get(key, 0))
        except (TypeError, ValueError):
            v = 0
        return v if v > 0 else 0

    return {
        "max_parallel_sessions": _c("max_parallel_sessions"),
        "max_total_tokens": _c("max_total_tokens"),
    }


def _pane_has_live_claude(pane_pid: str) -> bool:
    """True iff a live ``claude`` process exists in this pane's /proc subtree.

    T-0397: pane EXISTENCE is not agent liveness. When claude exits, its tmux
    pane routinely lingers as a bash shell — that dead-claude pane holds no
    agent and must not consume a parallel-cap slot. Conversely a claude session
    briefly running a Bash *tool* shows ``pane_current_command == bash`` while
    claude is still alive as the pane's parent, so a foreground-command check
    would flap; the /proc-subtree walk (mirrors ``_pane_claude_uuid_from_proc``)
    is the stable signal — claude is found whether idle, busy, or fresh.
    """
    try:
        root = int(pane_pid)
    except (ValueError, TypeError):
        return False
    children: dict[int, list[int]] = {}
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                st = (entry / "status").read_text()
            except OSError:
                continue
            m = re.search(r"^PPid:\s+(\d+)", st, re.M)
            if m:
                children.setdefault(int(m.group(1)), []).append(int(entry.name))
    except OSError:
        return False
    queue: list[int] = [root]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            parts = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\x00")
        except OSError:
            parts = []
        if parts and (parts[0] == "claude" or parts[0].endswith("/claude")):
            return True
        queue.extend(children.get(pid, []))
    return False


def _live_agent_sids() -> set[str]:
    """SIDs whose tmux pane has a LIVE claude process — the real agent sessions
    the parallel cap should count (T-0397).

    Stricter than ``live_pane_map`` (which keys every pane, including the bash
    shells that dead-claude panes fall back to): a dead-claude pane consumes no
    agent slot, so it must not count. Liveness is verified against THIS worker's
    tmux server (the current linux user); a different user's sessions are
    reconciled by their own per-user worker and are not visible here.
    """
    user = _get_current_user()
    out: set[str] = set()
    for p in list_panes():
        if not _pane_has_live_claude(p.pid):
            continue
        try:
            out.add(compute_sid(user, p.window, p.pane_id))
        except Exception:
            continue
    return out


def _count_live_sessions(cfg: Any) -> int:
    """Count live sessions across every registered project — the cap is a
    system-wide resource limit.

    T-0397: a session counts only if it is a live-holder (status active/paused,
    not archived) AND its SID maps to a pane with a live claude agent
    (``_live_agent_sids``). The persisted ``status`` field ALONE is unreliable:
    ``gc_sessions`` cannot reconcile a dead session whose md ``pane_id`` is empty
    (routinely empty for live sessions), so phantoms linger as ``active`` — and
    even a dead-claude pane that fell back to bash would pass a mere
    pane-existence check. Trusting them inflated the count (15/15 while only ~9
    agents were live) and made ``_enforce_parallel_cap`` silently refuse spawns
    at a false ceiling. ``backoff._live_count`` delegates here, so the AIMD
    effective_limit and the caps meter (items 7/22) inherit the corrected count.
    """
    live = _live_agent_sids()
    n = 0
    for slug in getattr(cfg, "projects", {}) or {}:
        sess_dir = cfg.data_dir / slug / "sessions"
        if not sess_dir.exists():
            continue
        for md in sess_dir.glob("*.md"):
            meta = _read_session_metadata(md)
            if meta and _is_live_holder(meta) and meta.get("sid", md.stem) in live:
                n += 1
    return n


def _enforce_parallel_cap(cfg: Any) -> None:
    """Raise ActionError if spawning would exceed the EFFECTIVE concurrency.

    Two layers (T-0239 cap + T-0249 backoff governor):
      * the hard ``max_parallel_sessions`` cap is the ceiling (0 = unlimited);
      * the WS-4 backoff governor depresses the effective limit BELOW the cap
        under Claude rate-limit / 5h-usage-limit pressure.
    Admission refuses (the task stays pending/QUEUED, never a silent drop — the
    T-0237 S4 contract) once live sessions reach the effective limit, and the
    message distinguishes a hard-cap refusal from a backoff (pressure) refusal.
    """
    from bot_squad_worker import backoff as _backoff

    cap = _read_caps(_caps_config_dir(cfg))["max_parallel_sessions"]
    effective = _backoff.effective_limit(cfg)  # already clamped to the ceiling
    live = _count_live_sessions(cfg)
    if live < effective:
        return
    from bot_squad_worker.actions import ActionError
    if cap > 0 and effective >= cap:
        raise ActionError(
            f"spawn: capacity reached — {live}/{cap} parallel sessions live "
            f"(max_parallel_sessions cap); spawn refused, task stays pending"
        )
    raise ActionError(
        f"spawn: backoff — {live}/{effective} effective concurrency "
        f"(rate-limit/usage-limit pressure; hard cap={cap or 'unlimited'}); "
        f"spawn refused, task stays QUEUED, retry on ramp-up"
    )


def caps_utilization(cfg: Any) -> dict:
    """The live, ENFORCED resource-cap utilization for the UI meter (T-0335
    items 7 + 22).

    Returns the exact numbers spawn admission gates on — so the meter measures
    what is actually enforced, not a parallel estimate. All system-wide (caps
    are a global resource limit):

      max_parallel_sessions : hard concurrency ceiling (0 = unlimited)
      effective_limit       : ceiling depressed by the AIMD backoff governor
                              ("12/15, throttled to 8"); the 10_000 unlimited
                              sentinel is normalised to 0 so the wire uses the
                              same 0=unlimited convention as the cap
      live_sessions         : sessions currently counted against the ceiling
      max_total_tokens      : output-token budget per quota period (0 = unlimited)
      output_since_anchor   : output tokens spent since the [quota] anchor (the
                              number enforced against max_total_tokens, item 7)
    """
    from bot_squad_worker import backoff as _backoff
    caps = _read_caps(_caps_config_dir(cfg))
    effective = _backoff.effective_limit(cfg)
    if caps["max_parallel_sessions"] == 0 and effective >= _backoff._UNLIMITED:
        effective = 0  # unlimited + no pressure → 0 on the wire (not the sentinel)
    return {
        "max_parallel_sessions": caps["max_parallel_sessions"],
        "effective_limit": effective,
        "live_sessions": _count_live_sessions(cfg),
        "max_total_tokens": caps["max_total_tokens"],
        "output_since_anchor": _output_since_anchor(cfg),
    }


def _anchor_key(cfg: Any) -> str:
    """The current ``[quota].set_at`` (or '' if unset) — the budget-period key.
    Re-anchoring (operator changes set_at) rebases the token-budget baseline."""
    path = Path(_caps_config_dir(cfg)) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    return str((raw.get("quota") or {}).get("set_at", "") or "")


def _project_output_total(cfg: Any) -> int:
    """Sum ``output_tokens_cum_total`` across every project's ``_quota.json`` —
    the run-wide cumulative output-token counter the telemetry sampler maintains."""
    total = 0
    for slug in getattr(cfg, "projects", {}) or {}:
        q = cfg.data_dir / slug / "_worker" / "telemetry" / "_quota.json"
        try:
            total += int(json.loads(q.read_text()).get("output_tokens_cum_total", 0))
        except (OSError, ValueError, TypeError):
            continue
    return total


def _token_baseline_path(cfg: Any) -> Path:
    return cfg.data_dir / "_worker" / "token_cap" / "baseline.json"


def _output_since_anchor(cfg: Any) -> int:
    """Output tokens since the current quota anchor (T-0306 semantics B).

    Persists a ``{anchor_key, baseline_total}`` and rebases it whenever the
    ``[quota].set_at`` changes — so the budget frees on re-anchor. Returns
    ``max(0, current_total - baseline_total)``.
    """
    key = _anchor_key(cfg)
    total = _project_output_total(cfg)
    p = _token_baseline_path(cfg)
    try:
        prev = json.loads(p.read_text())
    except (OSError, ValueError):
        prev = {}
    if prev.get("anchor_key") != key:
        prev = {"anchor_key": key, "baseline_total": total}
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / (p.name + ".tmp")
        tmp.write_text(json.dumps(prev))
        os.rename(tmp, p)
    return max(0, total - int(prev.get("baseline_total", 0)))


def _enforce_token_cap(cfg: Any) -> None:
    """Raise ActionError if spawning would exceed the max_total_tokens budget.

    Budget per quota period (T-0306, operator semantics B): output tokens since
    the ``[quota]`` anchor vs the ``[caps].max_total_tokens`` cap. ``0`` =
    unlimited (a no-op). Refusal keeps the task pending (the T-0237 S4 contract),
    mirroring ``_enforce_parallel_cap``.
    """
    cap = _read_caps(_caps_config_dir(cfg))["max_total_tokens"]
    if cap <= 0:
        return
    since = _output_since_anchor(cfg)
    if since >= cap:
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"spawn: token budget reached — {since}/{cap} output tokens this "
            f"quota period (max_total_tokens cap); spawn refused, task stays "
            f"pending until the budget resets (re-anchor)"
        )


def _coordinator_user(cfg: Any) -> str:
    """The configured coordinator UI username (``[admin].coordinator_user``)."""
    path = Path(_caps_config_dir(cfg)) / "system_settings.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    return str((raw.get("admin") or {}).get("coordinator_user", "") or "")


def _derive_owner_user(cfg: Any, slug: str, owner: str | None) -> str | None:
    """T-0321: derive the human per-user-scoping username when no explicit
    owner_user was passed. ``owner`` is overloaded:
      * a plain username  → that username;
      * ``constant-team`` → the coordinator user (system-managed teams);
      * a TL-SID (``S-…``) → the TL's own human (its md owner_user/owner),
        resolved one hop (mirrors the T-0135 API-side hop);
      * anything else / unresolvable → None (legacy → owner-based scoping).
    """
    if not owner or owner == "~":
        return None
    if owner == "constant-team":
        return _coordinator_user(cfg) or None
    if owner.startswith("S-"):
        meta = _read_session_metadata(_session_file(cfg.data_dir, slug, owner))
        if meta:
            cand = meta.get("owner_user") or meta.get("owner")
            if (cand and cand != "~" and not str(cand).startswith("S-")
                    and cand != "constant-team"):
                return str(cand)
        return None
    return owner  # already a username


def bind_task(cfg: Any, slug: str, sid: str, task_id: str) -> dict:
    """Append task_id to a dev session's extra_task_ids.

    Validates: session exists, session is a dev (has primary task_id), task
    file exists, task isn't already bound elsewhere. Sends a peer notification.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"bind_task: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"bind_task: no session metadata for SID {sid!r}")

    # T-0185: a constant-team / queue-consumer session must never carry a
    # single-ticket binding — it consumes a line-queue and has no single-ticket
    # DoD, so a bound ticket is meaningless (and triggers false drift nags). The
    # p38 incident wired an unrelated unassigned ticket onto a feedback-triage
    # session; refuse the bind outright with a clear error rather than the
    # generic "not a dev session" below. (defence-in-depth: drift.py also skips
    # these, so even a binding that slips in via another path won't nag.)
    if str(meta.get("owner") or "") == "constant-team":
        raise ActionError(
            f"bind_task: session {sid!r} is a constant-team/queue-consumer "
            f"(owner=constant-team) — single-ticket bindings are not allowed"
        )

    primary = meta.get("task_id")
    if not primary or primary == "~":
        raise ActionError(f"bind_task: session {sid!r} is not a dev session (no primary task_id)")

    backlog_dir = data_dir / slug / "backlog"
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise ActionError(f"bind_task: task not found: {task_id}")

    if task_id == primary or task_id in (meta.get("extra_task_ids") or []):
        # Already bound to this session — idempotent success.
        extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
        return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "already_bound": True}

    # T-0157: multi-user claim lock — first-to-claim wins. Two users (or two
    # concurrent dispatches) can race the check-then-write below; without a
    # lock both pass `_find_owner` (each sees the task free) and both write,
    # double-binding the task. Serialize the find+write under a per-project
    # flock so exactly one claimant wins; the loser sees the freshly-written
    # owner and raises. The lock file lives beside the backlog (shared store)
    # so it is visible across the coordinator + per-user worker processes.
    claim_lock = backlog_dir / ".task-claim.lock"
    claim_lock.parent.mkdir(parents=True, exist_ok=True)
    with open(claim_lock, "w") as _lockf:
        fcntl.flock(_lockf, fcntl.LOCK_EX)
        # T-0237 Layer-1: enforce the binding cap (= 1 LIVE session per task)
        # under the claim flock. Only *live* holders count — a suspended or
        # archived prior holder is history and must not block a fresh run
        # (otherwise an abandoned/crashed task is permanently un-rebindable).
        # S4: at capacity we refuse with a clear "capacity reached" state that
        # names the live holder; the task simply stays pending (the caller
        # surfaces it / queues it), never a silent drop or a double-bind.
        live_owner = _live_task_owner(data_dir, slug, task_id, exclude_sid=sid)
        if live_owner is not None:
            raise ActionError(
                f"bind_task: capacity reached (cap={BINDING_CAP}) — task "
                f"{task_id} is already bound to live session {live_owner}; "
                f"task stays pending"
            )

        # Re-read under the lock so a concurrent bind to THIS session (its own
        # extras growing) isn't clobbered by our stale snapshot — last write
        # wins on lost-update otherwise.
        meta = _read_session_metadata(meta_file) or meta
        extras = list(meta.get("extra_task_ids") or [])
        extras = [t for t in extras if t and t != "~"]
        if task_id not in extras:
            extras.append(task_id)
        meta["extra_task_ids"] = extras
        _write_session_metadata(meta_file, meta)
    # flock released on close

    # T-0105: stamp the binding SID into the new task's session_history.
    # Best-effort; the SessionMd write above is the source of truth.
    try:
        _append_task_session_history(backlog_dir, task_id, sid)
    except OSError:
        pass

    # T-0038: if the dev's session carries an initiative, propagate it to
    # the newly-bound task md (existing-wins). Lets multi-binding keep the
    # task-to-initiative graph consistent without operator intervention.
    sess_init = meta.get("initiative")
    if sess_init and sess_init != "~":
        try:
            _write_task_initiative_if_absent(backlog_dir, task_id, sess_init)
        except OSError:
            pass

    # Read the task title for a friendlier message.
    title = ""
    try:
        task_meta = _read_session_metadata(matches[0])
        if task_meta:
            title = str(task_meta.get("title") or "").strip()
    except Exception:
        pass

    text = (
        f"[BIND_TASK from stakeholder] Also work on {task_id}"
        + (f": {title}" if title else "")
        + f". Read data/{slug}/backlog/{matches[0].name} for scope."
    )
    try:
        from bot_squad_worker import intersession as _is
        _is.send(cfg, slug, "stakeholder", sid, text)
    except Exception:
        # Peer notify is best-effort; the binding itself is the source of truth.
        pass

    return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras}


def set_drift_paused(cfg: Any, slug: str, sid: str, paused: bool) -> dict:
    """T-0184: per-session off-ramp for the drift-check tick (T-0149).

    ``bsq drift off`` sets ``drift_paused: true`` on the SessionMd; ``bsq drift
    on`` clears it. ``drift.drift_check`` reads this flag and skips paused
    sessions. Resolves the md by SID with the rename-tolerant claude_uuid
    fallback so a window-renamed session can still pause itself. Idempotent.

    Returns ``{ok, sid, drift_paused}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"set_drift_paused: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    md_path = _find_session_md(sessions_dir, sid, None)
    if md_path is None:
        raise ActionError(f"set_drift_paused: no session metadata for SID {sid!r}")
    meta = _read_session_metadata(md_path)
    if meta is None:
        raise ActionError(f"set_drift_paused: unreadable session metadata for SID {sid!r}")

    if paused:
        meta["drift_paused"] = True
    else:
        meta.pop("drift_paused", None)
    _write_session_metadata(md_path, meta, atomic=True)
    return {"ok": True, "sid": meta.get("sid", sid), "drift_paused": bool(paused)}


def archive_session(cfg: Any, slug: str, sid: str) -> dict:
    """Mark a session as archived in its frontmatter.

    Rules:
      - session must exist
      - session must be in 'suspended' state (no live pane). Active or
        paused sessions can't be archived — suspend first.

    Idempotent: archiving an already-archived session returns ok=True.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"archive_session: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"archive_session: no metadata for SID {sid!r}")

    # The session must have no live pane. Check tmux directly so we can't
    # rely on a stale md status flag.
    user = _get_current_user()
    for pane in list_panes():
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            raise ActionError(
                f"archive_session: {sid!r} still has a live pane — suspend first"
            )

    status = meta.get("status", "")
    if status not in ("suspended", "paused", "active"):
        # paused/active here mean stale md flags (we already verified no
        # live pane), so allow the archive — normalise to suspended first.
        pass

    meta["status"] = "suspended"
    meta["archived"] = "true"
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "archived": True}


def unarchive_session(cfg: Any, slug: str, sid: str) -> dict:
    """Clear the archived flag on a session md."""
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unarchive_session: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unarchive_session: no metadata for SID {sid!r}")

    if "archived" in meta:
        meta.pop("archived", None)
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "archived": False}


def bind_initiative(cfg: Any, slug: str, sid: str, initiative: str) -> dict:
    """Append initiative basename to a TL session's extra_initiatives.

    Validates: session exists, session is a TL (no primary task_id),
    initiative file exists, initiative isn't already bound elsewhere.
    Sends a peer notification.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"bind_initiative: unknown project slug {slug!r}")

    # Safety: basename-only, must end .md, no traversal.
    clean = (initiative or "").strip()
    if not clean or "/" in clean or ".." in clean or not clean.endswith(".md"):
        raise ActionError(f"bind_initiative: invalid initiative name {initiative!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"bind_initiative: no session metadata for SID {sid!r}")

    primary_task = meta.get("task_id")
    if primary_task and primary_task != "~":
        raise ActionError(f"bind_initiative: session {sid!r} is a dev session, not a teamlead")

    init_path = data_dir / slug / "vision" / "initiatives" / clean
    if not init_path.exists():
        raise ActionError(f"bind_initiative: initiative not found: {clean}")

    primary_init = meta.get("initiative")
    if clean == primary_init or clean in (meta.get("extra_initiatives") or []):
        extras = [i for i in (meta.get("extra_initiatives") or []) if i and i != "~"]
        return {"ok": True, "sid": sid, "initiative": clean, "extras": extras, "already_bound": True}

    owner = _find_owner(data_dir, slug, initiative=clean)
    if owner is not None and owner != sid:
        raise ActionError(f"bind_initiative: initiative {clean} already bound to {owner}")

    extras = list(meta.get("extra_initiatives") or [])
    extras = [i for i in extras if i and i != "~"]
    extras.append(clean)
    meta["extra_initiatives"] = extras
    _write_session_metadata(meta_file, meta)

    text = (
        f"[BIND_INITIATIVE from stakeholder] Also coordinate {clean}. "
        f"Read data/{slug}/vision/initiatives/{clean} for context."
    )
    try:
        from bot_squad_worker import intersession as _is
        _is.send(cfg, slug, "stakeholder", sid, text)
    except Exception:
        pass

    return {"ok": True, "sid": sid, "initiative": clean, "extras": extras}


def unbind_task(cfg: Any, slug: str, sid: str, task_id: str) -> dict:
    """Remove a task binding from a dev session.

    If `task_id` matches the session's primary task_id, the call fails —
    the primary is the session's identity. Removes from extra_task_ids
    otherwise. Idempotent if the task isn't bound.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unbind_task: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unbind_task: no session metadata for SID {sid!r}")

    primary = meta.get("task_id")
    if primary == task_id:
        raise ActionError(
            f"unbind_task: {task_id} is the primary task of {sid!r}; "
            "cannot unbind the session's identity"
        )

    extras = list(meta.get("extra_task_ids") or [])
    extras = [t for t in extras if t and t != "~"]
    if task_id not in extras:
        return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "changed": False}
    extras = [t for t in extras if t != task_id]
    meta["extra_task_ids"] = extras
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "changed": True}


def unbind_initiative(cfg: Any, slug: str, sid: str, initiative: str) -> dict:
    """Remove an initiative from a TL session's bindings.

    If the initiative matches the primary `initiative` field, that field is
    cleared (leaving extras intact). Otherwise the value is filtered out of
    `extra_initiatives`. Returns the resulting extras list.

    Idempotent: unbinding a not-present initiative returns ok=True.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unbind_initiative: unknown project slug {slug!r}")

    clean = (initiative or "").strip()
    if not clean or "/" in clean or ".." in clean:
        raise ActionError(f"unbind_initiative: invalid initiative name {initiative!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unbind_initiative: no session metadata for SID {sid!r}")

    primary_init = meta.get("initiative")
    extras = list(meta.get("extra_initiatives") or [])
    extras = [i for i in extras if i and i != "~"]

    changed = False
    if primary_init == clean:
        meta["initiative"] = "~"
        changed = True
    if clean in extras:
        extras = [i for i in extras if i != clean]
        meta["extra_initiatives"] = extras
        changed = True

    if changed:
        _write_session_metadata(meta_file, meta)

    return {"ok": True, "sid": sid, "initiative": clean, "extras": extras, "changed": changed}


# ---------------------------------------------------------------------------
# Binding-graph reconcilers (T-0073 / T-0077)
# ---------------------------------------------------------------------------

def _current_user_sid_prefix() -> str:
    """Return ``S-<linux_user>-`` so gc helpers can scope to their own user.

    The worker can only see its own linux user's tmux server via ``list_panes``;
    another user's sessions would be misclassified as zombies without this.
    SessionMd filenames embed the linux user as the second SID segment, so a
    prefix-match keeps reconcilers safely user-scoped without needing
    cross-user coordination.
    """
    return f"S-{_get_current_user()}-"


def gc_sessions(cfg: Any, slug: str) -> dict:
    """T-0077: flip md ``status: active`` to ``suspended`` when no live pane.

    Walks ``data/<slug>/sessions/*.md`` filtered to the current linux user.
    For each SessionMd claiming ``status: active`` whose SID is not in
    ``list_panes()``, rewrites the md atomically with ``status: suspended``
    + ``suspended_at: <now>``. Original ``started_at`` and ``claude_uuid``
    are preserved so the session remains resurrectable via ``resume()``.
    Skips mds with ``archived: true`` (operator intent).

    Returns ``{"ok": True, "scanned": N, "repaired": K, "sids": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_sessions: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "repaired": 0, "sids": []}

    user = _get_current_user()
    user_prefix = f"S-{user}-"
    live_panes = list_panes()
    live_sids = {compute_sid(user, p.window, p.pane_id) for p in live_panes}
    live_pane_ids = {p.pane_id for p in live_panes}

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    scanned = 0
    repaired: list[str] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        if meta.get("status") != "active":
            continue
        sid = meta.get("sid", md.stem)
        if sid in live_sids:
            continue
        if str(meta.get("archived", "")).lower() == "true":
            continue
        # T-0134: require an explicit pane_id on SessionMd to flag suspended.
        # Sessions without a recorded pane_id are unverifiable (legacy schema,
        # or claude running in a non-bot-squad tmux pane) — skip them rather
        # than false-flag as zombie.
        recorded_pane_id = meta.get("pane_id")
        if not recorded_pane_id or recorded_pane_id == "~":
            continue
        # Verified suspect: pane_id is recorded but no longer in tmux list-panes.
        if recorded_pane_id in live_pane_ids:
            continue
        meta["status"] = "suspended"
        meta["suspended_at"] = now
        _write_session_metadata(md, meta, atomic=True)
        repaired.append(sid)
    return {"ok": True, "scanned": scanned, "repaired": len(repaired), "sids": repaired}


# T-0200: idle grace before an empty per-initiative/per-team tmux session is
# reaped. Default 1h (the DoD threshold). A session created seconds ago by an
# in-flight spawn is well inside this window, so the reaper never races a
# just-spawned team. Override via env for tests / faster local cleanup.
_TMUX_GC_IDLE_SEC = float(os.environ.get("BOT_SQUAD_TMUX_GC_IDLE_SEC") or 3600)
# T-0350: a demand-driven constant-team sibling (user-feedback etc.) whose triage
# dev has exited is just a lingering empty bare-shell — it has finished its
# queue and has no reason to wait the full hour. Reap it on a much shorter grace
# so the empty session doesn't accumulate (the recurring leak the operator kept
# killing by hand). Still > a spawn settle window so we never race a mid-spawn.
_CONSTANT_TMUX_GC_IDLE_SEC = float(os.environ.get("BOT_SQUAD_CONSTANT_TMUX_GC_IDLE_SEC") or 120)


def gc_tmux_sessions(cfg: Any, slug: str) -> dict:
    """T-0200: reap idle, empty per-initiative/per-team tmux sessions.

    The T-0001 per-initiative routing creates a sibling tmux session
    ``<slug>-<stem>`` for each initiative/constant team, each parking a never-used
    ``_init`` placeholder window. When the team's claude windows exit, the
    ``_init`` window keeps the otherwise-empty session alive forever — the
    "7 spurious ``bot-squad-<initiative>`` sessions" the stakeholder hit. This
    reconciler enforces the invariant *a sibling tmux session exists only while
    its team has a live claude pane*: any ``<slug>-*`` session with **zero** live
    claude panes whose last activity is older than ``_TMUX_GC_IDLE_SEC`` is
    killed. The on-disk Team md is untouched (``reconcile_teams`` keeps the
    project team), so "idle teams keep on-disk entity but no tmux session".

    Guard rails:
      - The bare ``<slug>`` main session is NEVER reaped (project home; hosts the
        operator pane and is recreated on demand anyway).
      - Only sessions whose panes are rooted in this project's repo cwd are
        eligible, so a coincidentally ``<slug>-``prefixed session belonging to
        another project / a human is spared.
      - A session with ≥1 live claude pane is spared regardless of idle time.

    Same pass serves the periodic GC (DoD #2) and the one-shot migration of
    pre-existing empties (DoD #4): an old spurious session is already idle past
    the grace, so it is reaped on the first tick it is seen.

    Returns ``{"ok": True, "reaped": [<name>, ...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_tmux_sessions: unknown project slug {slug!r}")

    prefix = f"{slug}-"
    repo_path = Path(project.repo_path)
    try:
        repo_real = repo_path.resolve()
    except OSError:
        repo_real = repo_path

    # Bucket live panes by tmux session: claude-pane count + are any panes rooted
    # in this project (the ownership gate).
    claude_counts: dict[str, int] = {}
    rooted: dict[str, bool] = {}
    for p in list_panes():
        sess = p.session
        if not sess:
            continue
        if _is_claude_command(p.command):
            claude_counts[sess] = claude_counts.get(sess, 0) + 1
        if not rooted.get(sess) and p.cwd:
            # T-0220: shared with the active-pane / suspended-row cwd match.
            if _cwd_matches_repo(Path(p.cwd), repo_path, repo_real):
                rooted[sess] = True

    res = _run(["tmux", "list-sessions", "-F", "#{session_name}|#{session_activity}"])
    if res.returncode != 0:
        # No tmux server / no sessions — nothing to reap.
        return {"ok": True, "reaped": []}

    # T-0350: which sibling stems are demand-driven constant teams (shorter grace).
    try:
        from bot_squad_worker.constant_teams import constant_team_stems
        ct_stems = constant_team_stems(cfg, slug)
    except Exception:  # noqa: BLE001
        ct_stems = set()

    now = time.time()
    reaped: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, activity_s = line.partition("|")
        if name == slug or not name.startswith(prefix):
            continue  # main session or another project — never reap
        if claude_counts.get(name, 0) > 0:
            continue  # staffed — has a live claude pane
        if not rooted.get(name):
            continue  # not rooted in this project's repo — not ours
        try:
            activity = float(activity_s)
        except (TypeError, ValueError):
            activity = 0.0
        # T-0350: a finished constant-team sibling reaps fast; everything else
        # keeps the long grace.
        grace = _CONSTANT_TMUX_GC_IDLE_SEC if name[len(prefix):] in ct_stems else _TMUX_GC_IDLE_SEC
        if now - activity < grace:
            continue  # still within the idle grace (e.g. mid-spawn)
        kill = _run(["tmux", "kill-session", "-t", name])
        if kill.returncode == 0:
            reaped.append(name)
    return {"ok": True, "reaped": reaped}


def _started_at_key(value: Any) -> tuple[int, str]:
    """Sort key for ``started_at`` — newer wins. Missing values rank lowest."""
    if not value or value == "~":
        return (0, "")
    return (1, str(value))


def resolve_session(cfg: Any, slug: str, ident: str) -> dict | None:
    """T-0176 #1/#2 addressability shim: resolve a display SID *or* a claude_uuid
    to its canonical SessionMd, following ``merged_into`` so an address to a
    deduped-away zombie redirects to the surviving keeper.

    This is the guarantee that the dedup migration (and the eventual UUID re-key,
    [[T-0187]]) never invalidates a live address: a peer-bus caller can pass the
    current ``S-<user>-<window>-pN`` SID *or* the uuid and reach the same live
    session. Returns the resolved meta dict, or None if nothing matches.
    """
    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return None

    def _lookup(key: str) -> dict | None:
        direct = sessions_dir / f"{key}.md"
        if direct.exists():
            return _read_session_metadata(direct)
        for md in sessions_dir.glob("*.md"):
            meta = _read_session_metadata(md)
            if meta is None:
                continue
            if meta.get("sid") == key or meta.get("claude_uuid") == key:
                return meta
        return None

    meta = _lookup(ident)
    if meta is None:
        return None
    seen: set[str] = set()
    while meta is not None:
        mi = meta.get("merged_into")
        if not mi or mi == "~" or mi in seen:
            break
        seen.add(mi)
        nxt = _lookup(mi)
        if nxt is None:
            break
        meta = nxt
    return meta


def dedup_sessions(cfg: Any, slug: str, *, dry_run: bool = True) -> dict:
    """T-0176 #5/#6: collapse duplicate SessionMds to one keeper per logical
    session, marking the rest ``merged_into: <keeper>`` so the UI can show one
    row per logical session instead of the p92..p99 parade.

    Two failure modes (see scenarios/T-0176):

      * **Mode A — same ``claude_uuid``**: one conversation that produced several
        SessionMds because the tmux pane id rotated across a restart
        (e.g. ``multi-p8`` / ``multi-p9`` sharing one uuid). Unambiguous.
      * **Mode B — distinct uuids, same ``window`` + task**: a spawn storm
        (e.g. ``T-0080-p92..p99``), eight separate claude launches all bound to
        one task in one window. Keyed on **task AND window** — never task alone —
        and **constant-team sessions are excluded** (owner ``constant-team`` or a
        ``constant_team: true`` initiative), so a feedback processor mis-bound to
        someone else's task is never swept into their cluster.

    Keeper = the live session if any, else the most-recent ``started_at``. Live
    sessions are never archived. ``dry_run=True`` (default) reports the merges it
    *would* make without writing. Idempotent: already-``merged_into`` rows are
    left alone.

    Returns ``{ok, dry_run, merged_count, merges: [{loser, keeper, mode}, ...]}``.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker.constant_teams import constant_team_stems

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"dedup_sessions: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "dry_run": dry_run, "merged_count": 0, "merges": []}

    user = _get_current_user()
    user_prefix = f"S-{user}-"
    live_sids = {compute_sid(user, p.window, p.pane_id) for p in list_panes()}
    const_stems = constant_team_stems(cfg, slug)

    rows: list[tuple[str, dict, Any]] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        rows.append((meta.get("sid", md.stem), meta, md))

    def _keeper_key(item: tuple[str, dict, Any]) -> tuple:
        sid, meta, _ = item
        live = 1 if sid in live_sids else 0
        return (live, _started_at_key(meta.get("started_at")), sid)

    def _already_merged(meta: dict) -> bool:
        return bool(meta.get("merged_into") and meta.get("merged_into") != "~")

    def _is_constant(meta: dict) -> bool:
        if str(meta.get("owner") or "") == "constant-team":
            return True
        stem = Path(str(meta.get("initiative") or "")).stem
        return bool(stem and stem in const_stems)

    def _task_key(meta: dict) -> str | None:
        for field in ("task_id", "last_task_id"):
            v = meta.get(field)
            if v and v != "~":
                return str(v)
        return None

    merged: dict[str, tuple[str, str]] = {}  # loser_sid -> (keeper_sid, mode)

    def _collapse(members: list[tuple[str, dict, Any]], mode: str) -> None:
        if len(members) < 2:
            return
        keeper = max(members, key=_keeper_key)[0]
        for sid, meta, _ in members:
            if sid == keeper or sid in merged or sid in live_sids:
                continue
            # Migration guardrail (T-0176): NEVER archive a status=active row.
            # The destructive part targets dead zombies only; an active-but-not-
            # live row is left for gc_sessions to suspend first.
            if str(meta.get("status") or "").lower() == "active":
                continue
            if _already_merged(meta):
                continue
            merged[sid] = (keeper, mode)

    # Mode A — exact: same claude_uuid.
    by_uuid: dict[str, list[tuple[str, dict, Any]]] = {}
    for sid, meta, md in rows:
        u = meta.get("claude_uuid")
        if not u or u == "~":
            continue
        by_uuid.setdefault(u, []).append((sid, meta, md))
    for members in by_uuid.values():
        _collapse(members, "uuid")

    # Mode B — heuristic: same (task, window), constant teams excluded.
    by_taskwin: dict[tuple[str, str], list[tuple[str, dict, Any]]] = {}
    for sid, meta, md in rows:
        if sid in merged or _is_constant(meta):
            continue
        task = _task_key(meta)
        if task is None:
            continue
        by_taskwin.setdefault((task, meta.get("window") or ""), []).append((sid, meta, md))
    for members in by_taskwin.values():
        _collapse(members, "task-window")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    row_by_sid = {sid: (meta, md) for sid, meta, md in rows}
    if not dry_run:
        for loser, (keeper, _mode) in merged.items():
            meta, md = row_by_sid[loser]
            meta["merged_into"] = keeper
            meta["archived"] = "true"
            if meta.get("status") == "active":
                meta["status"] = "suspended"
                meta.setdefault("suspended_at", now)
            meta["archive_reason"] = f"merged-into:{keeper}"
            _write_session_metadata(md, meta, atomic=True)

    merges = [
        {"loser": loser, "keeper": keeper, "mode": mode}
        for loser, (keeper, mode) in sorted(merged.items())
    ]
    return {"ok": True, "dry_run": dry_run, "merged_count": len(merges), "merges": merges}


def gc_stale_bindings(cfg: Any, slug: str) -> dict:
    """T-0073: strip stale primary ``task_id`` from sessions losing a dup race.

    Item 3 (audit Fork-2 Part A): this is now a CRASH-ONLY BACKSTOP, not the
    primary dup-bind defence. ``spawn`` refuses a dup-bind at the open under the
    ``.task-claim.lock`` and stamps ``task_id+status:active`` so a fresh session
    is immediately a live owner — so a >1-claimant group can only arise from a
    crash between the claim check and the stamp (or legacy pre-Item-3 data), not
    from a normal concurrent spawn. Kept as defence-in-depth.

    For each ``task_id`` claimed by >1 SessionMd (under the current linux
    user's prefix), picks the winner = (live pane AND latest ``started_at``)
    or (latest ``started_at``) when no claimant is live. Strips ``task_id``
    from the losers, preserving the old value under ``last_task_id`` for
    forensics, and marks ``archive_reason: stale-binding``. Does NOT touch
    sessions with ``archived: true`` (operator already classified them) and
    does NOT touch a session that is the lone claimant of its ``task_id``.

    Returns ``{"ok": True, "scanned": N, "stripped": K, "details": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_stale_bindings: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "stripped": 0, "details": []}

    user = _get_current_user()
    user_prefix = f"S-{user}-"
    live_sids = {compute_sid(user, p.window, p.pane_id) for p in list_panes()}

    # Group SessionMds by primary task_id (current user only).
    by_task: dict[str, list[tuple[Path, dict, str]]] = {}
    scanned = 0
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        tid = meta.get("task_id")
        if not tid or tid == "~":
            continue
        sid = meta.get("sid", md.stem)
        by_task.setdefault(tid, []).append((md, meta, sid))

    details: list[dict] = []
    for task_id, claimants in by_task.items():
        if len(claimants) < 2:
            continue
        # Winner: prefer (live, latest started_at). Sort descending — first wins.
        def _rank(item: tuple[Path, dict, str]) -> tuple[int, tuple[int, str]]:
            _, m, s = item
            return (1 if s in live_sids else 0, _started_at_key(m.get("started_at")))
        ordered = sorted(claimants, key=_rank, reverse=True)
        winner_md, winner_meta, winner_sid = ordered[0]
        for md, meta, sid in ordered[1:]:
            if str(meta.get("archived", "")).lower() == "true":
                # Operator already archived; still strip the dangling task_id so
                # _find_owner stops blocking new binds, but don't double-mark.
                meta["last_task_id"] = meta.get("task_id")
                meta["task_id"] = "~"
                _write_session_metadata(md, meta, atomic=True)
                details.append({
                    "sid": sid, "task_id": task_id, "winner": winner_sid,
                    "stripped": True, "archived_already": True,
                })
                continue
            meta["last_task_id"] = meta.get("task_id")
            meta["task_id"] = "~"
            meta["archive_reason"] = "stale-binding"
            _write_session_metadata(md, meta, atomic=True)
            details.append({
                "sid": sid, "task_id": task_id, "winner": winner_sid,
                "stripped": True, "archived_already": False,
            })

    return {"ok": True, "scanned": scanned, "stripped": len(details), "details": details}


def _task_status(data_dir: Path, slug: str, task_id: str) -> str | None:
    """Return a backlog task's ``status`` frontmatter, or None if the file is gone.

    Resolves ``data/<slug>/backlog/<task_id>-*.md`` (the canonical naming). A
    missing file yields None so callers can treat "task deleted" distinctly
    from "task closed". Parsing is the same minimal frontmatter reader the
    rest of the worker uses.
    """
    backlog_dir = data_dir / slug / "backlog"
    if not backlog_dir.exists():
        return None
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        direct = backlog_dir / f"{task_id}.md"
        if direct.exists():
            matches = [direct]
    if not matches:
        return None
    parsed = _frontmatter.parse_or_none(matches[0].read_text())  # T-0075
    if parsed is None:
        return None
    status = parsed[0].get("status")
    if status is None:
        return None
    return str(status) or None


def _initiative_exists(data_dir: Path, slug: str, initiative: str) -> bool:
    init = (initiative or "").strip()
    if not init or init == "~":
        return True  # nothing bound → nothing stale
    return (data_dir / slug / "vision" / "initiatives" / init).exists()


def backfill_parent_sid(cfg: Any, slug: str) -> dict:
    """T-0128: populate ``parent_sid`` for sessions that predate the field.

    The spawn path now stamps ``parent_sid`` (the SID that requested the
    spawn) at creation time, but legacy sessions — and sessions created by
    Claude Code's native agent-teams feature, which never goes through
    ``sessions.spawn`` — lack it. This idempotent reconciler fills the gap
    using the *same heuristic* the web tree falls back to: a session nests
    under the team-lead of the team that lists it (``teams.tl_for_sid``,
    which resolves the tmux-session-keyed Team projection rebuilt by
    ``reconcile_teams``). Mirrors the rendering rule "a dev nests under its
    TL" but persists the result so it survives a worker restart.

    Strictly fill-once: a session that already has a ``parent_sid`` is left
    untouched — the spawn-time value is authoritative and never overwritten.
    Runs after ``reconcile_teams`` in the binding-gc tick so the Team
    projection it consults is fresh.

    Only DEV rows (carrying a primary ``task_id``) are backfilled — this
    mirrors the web tree's ``isDevRow`` gate. Non-dev rows (TL / prod-tl / qa)
    are tree roots unless an explicit spawn-time ``parent_sid`` was stamped, so
    the heuristic must never invent a parent for them. Non-dev rows are also
    self-healed against an earlier over-eager backfill: an operator is always
    a root (any ``parent_sid`` cleared), and for other non-dev rows the value
    is cleared only when it matches the heuristic's fingerprint
    (``== tl_for_sid(row)``) — preserving a genuine spawn-time parent such as
    operator→TL.

    Returns ``{"ok": True, "scanned": N, "filled": K, "corrected": C,
    "details": [...]}`` where ``corrected`` counts cleared non-dev rows.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker import teams as _teams

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"backfill_parent_sid: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "filled": 0, "details": []}

    scanned = 0
    filled = 0
    corrected = 0
    details: list[dict] = []
    for md in sorted(sessions_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        sid = meta.get("sid", md.stem)
        role = _derive_role(
            meta.get("window"), meta.get("task_id"), meta.get("initiative")
        )
        tid = meta.get("task_id")
        is_dev = bool(tid and tid != "~")

        if not is_dev:
            # Non-dev rows (operator / TL / prod-tl / qa) are tree ROOTS in the
            # web tree, which nests only dev rows — so the tl_for_sid heuristic
            # must never invent a parent for them. Self-heal artifacts an
            # earlier over-eager backfill left behind: an operator is *always*
            # a root (clear unconditionally); for other non-dev rows clear only
            # when the stored value matches what the heuristic would produce
            # (its fingerprint), so a genuine spawn-time parent (e.g.
            # operator→TL) is preserved.
            if _parent_sid_of(meta):
                try:
                    heuristic = _teams.tl_for_sid(cfg, slug, sid)
                except Exception:
                    heuristic = None
                if role == "operator" or (
                    heuristic and _parent_sid_of(meta) == heuristic
                ):
                    meta.pop("parent_sid", None)
                    try:
                        _write_session_metadata(md, meta, atomic=True)
                    except OSError:
                        continue
                    corrected += 1
                    details.append({"sid": sid, "cleared": True})
            continue

        # Dev row. Fill-once: never overwrite an existing (spawn-time) parent.
        if _parent_sid_of(meta):
            continue
        try:
            parent = _teams.tl_for_sid(cfg, slug, sid)
        except Exception:
            parent = None
        if not parent or parent == sid:
            continue
        meta["parent_sid"] = parent
        try:
            _write_session_metadata(md, meta, atomic=True)
        except OSError:
            continue
        filled += 1
        details.append({"sid": sid, "parent_sid": parent})
    return {
        "ok": True,
        "scanned": scanned,
        "filled": filled,
        "corrected": corrected,
        "details": details,
    }


def gc_dead_bindings(cfg: Any, slug: str) -> dict:
    """T-0142: clear task/initiative bindings whose target is closed or gone.

    Refreshes every SessionMd's bindings against the *current* state on disk
    (recomputed each dispatch tick, per the T-0142 DoD):

      - primary ``task_id`` pointing at a backlog task that is ``closed`` or
        whose md is missing → stripped to ``last_task_id`` (this is the live
        symptom the stakeholder reported: suspended devs still showing the
        old task_id in the sessions UI long after the task closed).
      - entries in ``extra_task_ids`` for closed/missing tasks → dropped.
      - primary ``initiative`` whose file is missing → stripped to
        ``last_initiative``; missing entries in ``extra_initiatives`` dropped.

    Bindings for ``open`` / ``in_progress`` / ``totest`` / ``reopened`` tasks
    are left untouched — they are still meaningfully claimed. Duplicate-claim
    races are a separate pass (``gc_stale_bindings``); this pass is about
    *dead* targets, not contested ones.

    Returns ``{"ok": True, "scanned": N, "cleared": K, "details": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"gc_dead_bindings: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "cleared": 0, "details": []}

    data_dir = cfg.data_dir
    user = _get_current_user()
    user_prefix = f"S-{user}-"
    # statuses that mean the binding is still legitimately held
    LIVE_STATUSES = {"open", "in_progress", "totest", "reopened", "planned"}

    scanned = 0
    details: list[dict] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        sid = meta.get("sid", md.stem)
        changed = False

        # --- primary task_id ---
        tid = meta.get("task_id")
        if tid and tid != "~":
            st = _task_status(data_dir, slug, tid)
            if st is None or st == "closed":
                meta["last_task_id"] = tid
                meta["task_id"] = "~"
                meta["archive_reason"] = f"dead-binding:task-{'missing' if st is None else 'closed'}"
                changed = True
                details.append({"sid": sid, "cleared": tid,
                                "reason": "missing" if st is None else "closed"})

        # --- extra_task_ids ---
        extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
        kept_extras = []
        for et in extras:
            st = _task_status(data_dir, slug, et)
            if st is None or st == "closed":
                changed = True
                details.append({"sid": sid, "cleared": et,
                                "reason": "extra-missing" if st is None else "extra-closed"})
            else:
                kept_extras.append(et)
        if kept_extras != extras:
            meta["extra_task_ids"] = kept_extras

        # --- primary initiative ---
        init = meta.get("initiative")
        if init and init != "~" and not _initiative_exists(data_dir, slug, init):
            meta["last_initiative"] = init
            meta["initiative"] = "~"
            changed = True
            details.append({"sid": sid, "cleared": init, "reason": "initiative-missing"})

        # --- extra_initiatives ---
        einits = [i for i in (meta.get("extra_initiatives") or []) if i and i != "~"]
        kept_inits = [i for i in einits if _initiative_exists(data_dir, slug, i)]
        if kept_inits != einits:
            meta["extra_initiatives"] = kept_inits
            changed = True

        if changed:
            _write_session_metadata(md, meta, atomic=True)

    return {"ok": True, "scanned": scanned, "cleared": len(details), "details": details}


def _parse_ts_epoch(value: Any) -> float | None:
    """Best-effort epoch seconds from a frontmatter timestamp, or None.

    Timestamps are written as ``strftime("%Y-%m-%dT%H:%M:%SZ")`` strings, but
    pyyaml parses ISO-8601 timestamps into ``datetime`` objects on read — so a
    value may be either a ``datetime`` or a string depending on whether it has
    round-tripped through a load. Handle both; a naive datetime/string is
    assumed UTC (every timestamp this codebase writes is UTC).
    """
    if value is None or value == "~" or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _session_idle_age(meta: dict, user_home: str, now_epoch: float) -> float | None:
    """Seconds since this session last did anything, or None if undeterminable.

    Prefers the live Claude transcript activity time (most accurate); falls back
    to the on-disk ``suspended_at`` / ``updated_at`` / ``started_at`` stamps.
    Returns ``None`` when no signal exists so the caller can decline to reap a
    session whose age it cannot positively establish.
    """
    act = _pane_activity_at(
        str(meta.get("cwd") or ""), meta.get("claude_uuid"), user_home
    )
    if act is not None:
        return max(0.0, now_epoch - act)
    for key in ("suspended_at", "updated_at", "started_at"):
        ts = _parse_ts_epoch(meta.get(key))
        if ts is not None:
            return max(0.0, now_epoch - ts)
    return None


def archive_dead_teammates(cfg: Any, slug: str) -> dict:
    """T-0142/T-0144: auto-archive cleanly-delivered dev teammates.

    Runs on every dispatch tick so a TL never has to manually archive a dev
    whose work is done. Operates only on **dev-role** sessions (a TL/operator
    is never auto-archived) under two rules:

      1. **Exited + delivered.** A dev whose claude pane is gone AND whose task
         is ``totest`` / ``closed`` / missing (or had no binding) is the clean
         post-totest exit the stakeholder wants archived: flip
         ``status: suspended`` + ``archived: true``, clear its task binding,
         and best-effort kill any lingering tmux window. This makes a dev
         *zombie impossible* (T-0144): no live pane + done ⟹ archived, never
         left ``active`` with a stale binding.

      2. **Live + verified-done.** A dev still holding a live pane whose task
         is ``closed`` (TL-verified) is suspended (pane closed) then archived —
         the aggressive working-set trim. We deliberately do NOT force-suspend
         a *live* ``totest`` dev: the TL may still be iterating review with it,
         and killing live in-progress work is the one irreversible mistake to
         avoid. Crashed in-progress devs (pane gone, task still open) are left
         resumable for ``gc_sessions`` to mark suspended.

         T-0202: ``gc_dead_bindings`` runs *earlier in the same tick* and
         strips a just-closed primary ``task_id`` to ``~`` (preserving it as
         ``last_task_id``), so a live verified-done dev never actually
         presents here as live+closed — it presents as live + no current
         task. Rule 2 therefore also consults ``last_task_id``: a live dev
         with no current task, no surviving ``extra_task_ids``, an *idle*
         pane, and ``last_task_id`` → ``closed`` is trimmed with reason
         ``live-last-closed``. Closed-only (never ``totest``) preserves the
         rule-2 safety above; the idle guard (``IDLE_AT_PROMPT_SECONDS`` of
         jsonl quiet) additionally spares a pane that is still mid-write.

      3. **Exited + stale (T-0233).** An *exited* (pane-gone) dev whose task is
         still open (``open`` / ``in_progress`` / ``reopened`` / ``planned``)
         but whose one-time run has done nothing past ``session_stale_sec()``
         (``BOT_SQUAD_SESSION_STALE_SEC``, default 24h) is a crashed/abandoned
         run — reaped with reason ``exited-stale``. The binding is preserved as
         ``last_task_id`` (the task stays open and re-dispatchable to a fresh
         session, per kill-not-resume). Only a run whose age can be positively
         established (transcript activity, else ``suspended_at`` / ``updated_at``
         / ``started_at``) is eligible; LIVE panes and non-dev (operator/TL)
         sessions are never stale-reaped — the role and live guards above hold.

    Returns ``{"ok": True, "scanned": N, "archived": K, "sids": [...]}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"archive_dead_teammates: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "scanned": 0, "archived": 0, "sids": []}

    data_dir = cfg.data_dir
    user = _get_current_user()
    user_home = _get_user_home()
    user_prefix = f"S-{user}-"
    live_panes = list_panes()
    # T-0211: map each live pane to its FULL SID (window + pane_id). The
    # kill-lingering-window cleanup below must target a pane by this SID, never
    # by window name: window names are derived from the ticket slug and recur
    # across respawns, so a dead session's window name collides with a
    # *different* live dev's window in the same tmux session — and a name match
    # would kill that live sibling. `user` equals every processed sid's
    # linux_user because the md loop below is filtered to `user_prefix`, so this
    # keying is identical to the `sid` values it is matched against.
    live_pane_by_sid = {
        compute_sid(user, p.window, p.pane_id): p for p in live_panes
    }
    live_sids = set(live_pane_by_sid)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    now_epoch = time.time()

    scanned = 0
    archived: list[str] = []
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        scanned += 1
        if str(meta.get("archived", "")).lower() == "true":
            continue
        sid = meta.get("sid", md.stem)
        role = _derive_role(
            meta.get("window"), meta.get("task_id"), meta.get("initiative"),
            extra_task_ids=[t for t in (meta.get("extra_task_ids") or []) if t and t != "~"],
            extra_initiatives=[i for i in (meta.get("extra_initiatives") or []) if i and i != "~"],
        )
        if role != "dev":
            continue

        is_live = sid in live_sids
        tid = meta.get("task_id")
        has_task = bool(tid and tid != "~")
        st = _task_status(data_dir, slug, tid) if has_task else None
        reason = None
        ltid = None
        if is_live:
            # Live dev: only trim when verified-done (task closed).
            if has_task and st == "closed":
                reason = "live-closed"
            elif not has_task:
                # T-0202: the binding for a just-closed task was already
                # stripped by gc_dead_bindings earlier in this tick, so the
                # verified-done signal lives in last_task_id. Trim only when
                # the dev holds no other live work (gc_dead_bindings has
                # already pruned closed extras, so any survivor is a real
                # claim) and its pane is idle — a missing jsonl activity
                # signal counts as idle, mirroring _derive_activity.
                ltid = meta.get("last_task_id")
                extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
                if (
                    ltid and ltid != "~" and not extras
                    and _task_status(data_dir, slug, ltid) == "closed"
                ):
                    activity_at = _pane_activity_at(
                        str(meta.get("cwd") or ""), meta.get("claude_uuid"), user_home
                    )
                    if activity_at is None or (now_epoch - activity_at) >= IDLE_AT_PROMPT_SECONDS:
                        reason = "live-last-closed"
            if reason is None:
                # T-0335 item-10 (Fork-2 Part B): an idle-but-live dev — pane gone
                # quiet past the suspend window — is suspended so it stops holding
                # a slot, even with work still open. Ships DARK (window 0 = OFF).
                # Spared when it is awaiting TG input (blocked_sids: a real "waiting
                # on you" signal, not a leak) or its pane is still active. A session
                # with no transcript activity signal is spared (age unknowable). The
                # task binding is preserved as last_task_id below and the task stays
                # open — reversible, re-dispatchable to a fresh session
                # (kill-not-resume); a non-TG long wait reads as idle, which is why
                # this is opt-in.
                idle_window = _session_idle_suspend_sec()
                if idle_window > 0:
                    try:
                        from bot_squad_worker import tg_stall as _tg_stall
                        _blocked = _tg_stall.blocked_sids(cfg, slug)
                    except Exception:  # noqa: BLE001 — never let it wedge the sweep
                        _blocked = set()
                    if sid not in _blocked:
                        activity_at = _pane_activity_at(
                            str(meta.get("cwd") or ""), meta.get("claude_uuid"), user_home
                        )
                        if activity_at is not None and (now_epoch - activity_at) >= idle_window:
                            reason = "idle-suspend"
        else:
            # Exited dev: archive when delivered or orphaned.
            if not has_task:
                reason = "exited-no-task"
            elif st is None:
                reason = "exited-task-missing"
            elif st in ("totest", "closed"):
                reason = f"exited-{st}"
            else:
                # T-0233: the task is still open (open/in_progress/reopened/
                # planned) but this one-time run's pane is gone and it has done
                # nothing past the staleness grace — a crashed/abandoned run.
                # Reap it so it stops lingering in the working set; the binding
                # is preserved below as last_task_id (the task itself stays open
                # and re-dispatchable to a fresh session). Only an exited run
                # whose age we can positively establish is eligible — a session
                # with no age signal is left alone.
                age = _session_idle_age(meta, user_home, now_epoch)
                if age is not None and age >= session_stale_sec():
                    reason = "exited-stale"
        if reason is None:
            continue

        if is_live:
            try:
                suspend(cfg, slug, sid)
            except Exception:
                pass  # if it won't suspend, still record the archive intent
            meta = _read_session_metadata(md) or meta

        # Best-effort: kill THIS session's own lingering tmux window — matched
        # by full SID (window + pane_id), never by window name. T-0211: window
        # names recur across respawns of the same ticket, so a name match could
        # (and did) hit a live *sibling* dev's pane in the same tmux session.
        # An exited dev has no pane here (no-op); a live dev was already closed
        # by suspend() above, so this only mops up a pane that outlived its
        # claude.
        own_pane = live_pane_by_sid.get(sid)
        if own_pane is not None:
            _run(["tmux", "kill-window", "-t", own_pane.pane_id])

        meta["status"] = "suspended"
        meta.setdefault("suspended_at", now)
        if has_task:
            meta["last_task_id"] = tid
            meta["task_id"] = "~"
        elif reason == "live-last-closed":
            # suspend() rewrote the md with a fixed key set that drops
            # last_task_id — restore the signal that justified this trim.
            meta["last_task_id"] = ltid
        meta["archived"] = "true"
        meta["archive_reason"] = f"auto-archive:{reason}"
        _write_session_metadata(md, meta, atomic=True)
        archived.append(sid)

    return {"ok": True, "scanned": scanned, "archived": len(archived), "sids": archived}


_WINDOW_SANITISE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitise_window(name: str) -> str:
    """Window-name token used in SID derivation (mirrors hook_my_sid.sh)."""
    return _WINDOW_SANITISE_RE.sub("_", (name or "").strip())


def sync_session_name(cfg: Any, slug: str, sid: str, name: str) -> dict:
    """T-0142: rename a session from a single source of truth (the tmux window).

    The session name is one value reflected in three surfaces — the UI/sessions
    row label, the tmux window name, and (cosmetically) the claude tab title.
    The tmux window name is the source of truth; this action is the single
    mutation point the UI and ``bsq team rename`` call so all surfaces stay in
    sync within a tick.

    For a **live** session, renames the tmux window (which rotates the
    pane-derived SID), migrates the SessionMd to the new SID path, rebinds the
    peer-bus inbox triple (T-0072), and re-reconciles the team roster. For a
    **suspended** session (no live pane), updates the registry ``window`` field
    in place — the rename takes visual effect immediately and the SID will
    settle on the next resume.

    Returns ``{"ok": True, "sid": <old>, "new_sid": <new>, "name": <window>}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"sync_session_name: unknown project slug {slug!r}")

    clean = _sanitise_window(name)
    if not clean:
        raise ActionError(f"sync_session_name: invalid name {name!r}")

    data_dir = cfg.data_dir
    user = _get_current_user()
    sessions_dir = data_dir / slug / "sessions"

    # Resolve the md (SID-keyed, falling back to a claude_uuid scan for a
    # session whose window was already renamed out from under its SID).
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        meta_file2 = _find_session_md(sessions_dir, sid, None)
        if meta_file2 is not None:
            meta_file = meta_file2
            meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"sync_session_name: no metadata for SID {sid!r}")

    # Find the live pane for this SID.
    target_pane: PaneInfo | None = None
    for pane in list_panes():
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    if target_pane is None:
        # Suspended/dead: display-rename only, no SID rotation.
        meta["window"] = clean
        _write_session_metadata(meta_file, meta)
        return {"ok": True, "sid": sid, "new_sid": sid, "name": clean}

    # Live: rename the tmux window, rotate the SID, migrate md + peer bus.
    _run(["tmux", "rename-window", "-t", target_pane.pane_id, clean])
    new_sid = compute_sid(user, clean, target_pane.pane_id)

    meta["sid"] = new_sid
    meta["window"] = clean
    new_meta_file = _session_file(data_dir, slug, new_sid)
    _write_session_metadata(new_meta_file, meta)
    if new_sid != sid and meta_file.exists() and meta_file != new_meta_file:
        meta_file.unlink()

    if new_sid != sid:
        try:
            from bot_squad_worker import intersession as _is
            _is.rebind_sid(cfg, slug, sid, new_sid)
        except Exception:
            pass  # peer-bus rebind is best-effort; gc will reconcile
        try:
            from bot_squad_worker import teams as _teams
            _teams.reconcile_teams(cfg, slug)
        except Exception:
            pass

    return {"ok": True, "sid": sid, "new_sid": new_sid, "name": clean}
