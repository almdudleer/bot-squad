"""Session manager — list, pause, resume, spawn Claude tmux sessions.

Each function is a pure worker action callable. The worker runs as almdudleer
and has access to the user's tmux server via the default socket.

Session ID (SID) format: ``S-<user>-<window>-p<pane_id_no_pct>``
  e.g. ``S-almdudleer-spec5-smoke-p2``
"""
from __future__ import annotations

import fcntl
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def _derive_role(
    window: str | None,
    task_id: str | None,
    initiative: str | None,
    *,
    extra_task_ids: list | None = None,
    extra_initiatives: list | None = None,
) -> str:
    """Map a session's identity fields → role enum: ``teamlead|dev|operator``.

    Precedence (first match wins):
      1. operator window marker (`operator`, `<x>-operator`) → ``operator``
      2. explicit TL window marker (`<x>-TL`, `<x>_teamlead`, …) → ``teamlead``
      3. default → ``dev``

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
    if _TL_WINDOW_RE.search(w):
        return "teamlead"
    return "dev"


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
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            if v:
                lines.append(f"{k}: [{', '.join(str(i) for i in v)}]")
            else:
                lines.append(f"{k}: []")
        elif v is None:
            lines.append(f"{k}: ~")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    body = "\n".join(lines)
    if atomic:
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(body)
        os.rename(tmp, path)
    else:
        path.write_text(body)


def _read_session_metadata(path: Path) -> dict | None:
    """Parse YAML-like frontmatter from a session metadata file.

    Returns None if the file doesn't exist or has no frontmatter.
    """
    if not path.exists():
        return None
    text = path.read_text()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    fm = parts[1].strip()
    meta: dict = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        # Parse list values like [T-0042, T-0043]
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [x.strip() for x in inner.split(",")] if inner else []
        elif v == "~" or v == "null":
            meta[k] = None
        else:
            meta[k] = v
    return meta


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
    """
    if not initiative:
        return slug
    stem = Path(initiative).stem
    if not stem:
        return slug
    return f"{slug}-{stem}"


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
        try:
            pane_real = pane_cwd.resolve()
        except OSError:
            pane_real = pane_cwd
        try:
            match = (
                pane_cwd == repo_path
                or pane_cwd.is_relative_to(repo_path)
                or pane_real == repo_real
                or pane_real.is_relative_to(repo_real)
            )
            # T-0003: operator pane lives in repo_workspace (parent of the dev
            # clone), e.g. cwd=/home/x/bot-squad while repo_path=/home/x/bot-squad/dev.
            # Accept the parent-cwd case only when bounded by tmux session == slug
            # (every project pane lives in a tmux session named after the slug, per
            # _ensure_project_tmux_session) or window == 'operator' (spawn fixes it
            # in routes_projects.create_project). Either bound prevents over-match
            # to unrelated panes whose cwd happens to be an ancestor of repo_path.
            if not match and (pane.session == slug or pane.window == "operator"):
                match = (
                    repo_path.is_relative_to(pane_cwd)
                    or repo_real.is_relative_to(pane_cwd)
                )
            if not match:
                continue
        except (ValueError, TypeError):
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
            md_tmux_session_val = meta.get("tmux_session")
            md_tmux_session = (
                str(md_tmux_session_val)
                if md_tmux_session_val and md_tmux_session_val != "~"
                else ""
            )
            rows.append({
                "sid": sid,
                "status": display_status,
                # T-0104: no live pane → activity is unambiguously suspended,
                # regardless of what the md frontmatter claims.
                "activity": "suspended",
                "activity_at": None,
                "active_at_prompt": False,
                # T-0141: authoritative role for suspended rows too.
                "role": _derive_role(
                    meta.get("window", ""), md_task_id, md_initiative,
                    extra_task_ids=md_extra_tids,
                    extra_initiatives=md_extra_inits,
                ),
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
                # T-0157: linux user owning this (suspended) session.
                "linux_user": _session_linux_user(sid, meta),
                # T-0078: surface tmux_session so the UI can still suggest the
                # right `tmux a -t …` even after suspend.
                "tmux_session": md_tmux_session,
            })

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


def resume(cfg: Any, slug: str, sid: str, initial_prompt: str | None = None) -> dict:
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


def _append_task_session_history(backlog_dir: Path, task_id: str, sid: str) -> bool:
    """T-0105: append `sid` to the task md's `session_history:` frontmatter
    list. Append-only, idempotent (de-duped — if `sid` is already in the
    list, no-op) and atomic (tmp + rename).

    Inline-list format: ``session_history: [SID, SID, ...]`` — chosen so
    the line-based worker readers (sessions/intersession/autonomous) can
    pick it up. Block-yaml-format lists written by the api PATCH path
    would be invisible here (same hazard as the existing `blocked_by`
    field — audit Bug #4); inline format is the worker's source of truth.

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
    m = re.match(r"\A---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return False
    fm_block = m.group(1)
    body = m.group(2)
    fm_lines = fm_block.splitlines()

    history_idx = -1
    existing: list[str] = []
    for i, ln in enumerate(fm_lines):
        stripped = ln.lstrip()
        if stripped.startswith("session_history:"):
            history_idx = i
            _, _, val = stripped.partition(":")
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                if inner:
                    existing = [x.strip() for x in inner.split(",") if x.strip() and x.strip() != "~"]
            break

    if sid in existing:
        return False  # idempotent — de-dup, preserve order

    new_list = existing + [sid]
    new_line = f"session_history: [{', '.join(new_list)}]"

    if history_idx >= 0:
        fm_lines[history_idx] = new_line
    else:
        insert_at = len(fm_lines)
        for i, ln in enumerate(fm_lines):
            if ln.lstrip().startswith("status:"):
                insert_at = i + 1
                break
        fm_lines.insert(insert_at, new_line)

    new_fm = "\n".join(fm_lines)
    content = f"---\n{new_fm}\n---\n{body}"
    if not body.startswith("\n"):
        content = f"---\n{new_fm}\n---\n\n{body}"
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
    m = re.match(r"\A---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return False
    fm_block = m.group(1)
    body = m.group(2)
    fm_lines = fm_block.splitlines()
    for ln in fm_lines:
        if ln.lstrip().startswith("initiative:"):
            return False  # already set — don't clobber
    # Insert after the `status:` line for stable ordering; if no status line,
    # append at the end of frontmatter.
    insert_at = len(fm_lines)
    for i, ln in enumerate(fm_lines):
        if ln.lstrip().startswith("status:"):
            insert_at = i + 1
            break
    fm_lines.insert(insert_at, f"initiative: {initiative}")
    new_fm = "\n".join(fm_lines)
    content = f"---\n{new_fm}\n---\n{body}"
    if not body.startswith("\n"):
        content = f"---\n{new_fm}\n---\n\n{body}"
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


def _deliver_prompt(pane_id: str, text: str) -> None:
    """Reliably deliver a prompt into a claude composer (T-0126/T-0144 fix).

    Loads ``text`` into a dedicated tmux paste buffer and pastes it in
    bracketed-paste mode (``paste-buffer -p``), then submits with a *separate*
    Enter. This is the operator's proven manual recovery (load-buffer +
    paste-buffer + Enter): unlike ``send-keys`` of a long literal — which can
    interleave with the TUI, mis-escape, or exceed argv limits, and whose
    chained Enter lands *inside* the bracketed-paste wrap rather than
    submitting — a paste-buffer lands the whole prompt in claude's input box
    atomically as one block (embedded newlines stay newlines, not submits),
    and the trailing standalone Enter is what submits it.

    Single-line prompts remain safest (one paste, one Enter, one message);
    a multi-line prompt is still pasted as a single block and submitted once.
    """
    import re as _re
    digits = _re.sub(r"[^0-9]", "", pane_id) or "x"
    buf = f"bsq-prompt-{digits}"
    # set-buffer takes the data as an argument (`--` guards a leading dash),
    # so delivery is testable without stdin plumbing.
    _run(["tmux", "set-buffer", "-b", buf, "--", text])
    _run(["tmux", "paste-buffer", "-t", pane_id, "-b", buf, "-p", "-d"])
    time.sleep(0.4)
    _run(["tmux", "send-keys", "-t", pane_id, "Enter"])


def spawn(
    cfg: Any,
    slug: str,
    window: str,
    initial_prompt: str | None = None,
    task_id: str | None = None,
    initiative: str | None = None,
    owner: str | None = None,
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
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: unknown project slug {slug!r}")

    cwd = str(project.repo_path)
    user = _get_current_user()

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
    try:
        seed_meta_file = _session_file(cfg.data_dir, slug, new_sid)
        seed_meta = _read_session_metadata(seed_meta_file) or {}
        seed_meta.setdefault("sid", new_sid)
        seed_meta["tmux_session"] = target_session
        # T-0157: stamp the spawning linux user so the SessionMd carries an
        # explicit user mark (the SID prefix already encodes it, but the field
        # makes per-user listing/grouping robust to SID rotation).
        seed_meta.setdefault("linux_user", user)
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
        owner = _find_owner(data_dir, slug, task_id=task_id)
        if owner is not None and owner != sid:
            raise ActionError(f"bind_task: task {task_id} already bound to {owner}")

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
    text = matches[0].read_text()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "status":
            return v.strip().strip('"').strip("'") or None
    return None


def _initiative_exists(data_dir: Path, slug: str, initiative: str) -> bool:
    init = (initiative or "").strip()
    if not init or init == "~":
        return True  # nothing bound → nothing stale
    return (data_dir / slug / "vision" / "initiatives" / init).exists()


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
    user_prefix = f"S-{user}-"
    live_panes = list_panes()
    live_sids = {compute_sid(user, p.window, p.pane_id) for p in live_panes}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

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
        if is_live:
            # Live dev: only trim when verified-done (task closed).
            if has_task and st == "closed":
                reason = "live-closed"
        else:
            # Exited dev: archive when delivered or orphaned.
            if not has_task:
                reason = "exited-no-task"
            elif st is None:
                reason = "exited-task-missing"
            elif st in ("totest", "closed"):
                reason = f"exited-{st}"
        if reason is None:
            continue

        if is_live:
            try:
                suspend(cfg, slug, sid)
            except Exception:
                pass  # if it won't suspend, still record the archive intent
            meta = _read_session_metadata(md) or meta

        # Best-effort: kill a lingering tmux window in this team's session.
        win = meta.get("window")
        sess = meta.get("tmux_session") or slug
        if win:
            for p in live_panes:
                if p.window == win and (p.session or slug) == sess:
                    _run(["tmux", "kill-window", "-t", p.pane_id])
                    break

        meta["status"] = "suspended"
        meta.setdefault("suspended_at", now)
        if has_task:
            meta["last_task_id"] = tid
            meta["task_id"] = "~"
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
