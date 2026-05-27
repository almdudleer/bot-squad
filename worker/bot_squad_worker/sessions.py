"""Session manager — list, pause, resume, spawn Claude tmux sessions.

Each function is a pure worker action callable. The worker runs as almdudleer
and has access to the user's tmux server via the default socket.

Session ID (SID) format: ``S-<user>-<window>-p<pane_id_no_pct>``
  e.g. ``S-almdudleer-spec5-smoke-p2``
"""
from __future__ import annotations

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


def _write_session_metadata(path: Path, meta: dict) -> None:
    """Write a session metadata file with YAML frontmatter."""
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
    path.write_text("\n".join(lines))


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


def _scan_linked_tasks(data_dir: Path, slug: str, sid: str, claude_uuid: str | None) -> list[str]:
    """Scan backlog T-*.md files for tasks with a matching session field."""
    backlog_dir = data_dir / slug / "backlog"
    if not backlog_dir.exists():
        return []
    linked = []
    for task_file in sorted(backlog_dir.glob("T-*.md")):
        meta = _read_session_metadata(task_file)
        if meta is None:
            continue
        session_val = meta.get("session", "")
        if session_val and (session_val == sid or (claude_uuid and session_val == claude_uuid)):
            task_id = task_file.stem  # T-0042
            linked.append(task_id)
    return linked


def _get_current_user() -> str:
    """Return the OS username."""
    import getpass
    return getpass.getuser()


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
        linked_tasks = _scan_linked_tasks(data_dir, slug, sid, claude_uuid)

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
            "window": pane.window,
            "cwd": pane.cwd,
            "started_at": started_at,
            "last_prompt_at": last_prompt_at,
            "claude_uuid": claude_uuid,
            "task_id": task_id,
            "initiative": initiative,
            "linked_tasks": linked_tasks,
            "extra_task_ids": extra_task_ids,
            "extra_initiatives": extra_initiatives,
            "paused_at": paused_at_meta,
            "suspended_at": None,
            "archived": archived_flag,
            "owner": owner_meta,
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
            rows.append({
                "sid": sid,
                "status": display_status,
                # T-0104: no live pane → activity is unambiguously suspended,
                # regardless of what the md frontmatter claims.
                "activity": "suspended",
                "activity_at": None,
                "active_at_prompt": False,
                "window": meta.get("window", ""),
                "cwd": meta.get("cwd", ""),
                "started_at": meta.get("started_at"),
                "last_prompt_at": meta.get("suspended_at") or meta.get("paused_at"),
                "claude_uuid": meta.get("claude_uuid"),
                "task_id": md_task_id,
                "initiative": md_initiative,
                "linked_tasks": meta.get("linked_tasks") or [],
                "extra_task_ids": md_extra_tids,
                "extra_initiatives": md_extra_inits,
                "paused_at": meta.get("paused_at"),
                "suspended_at": meta.get("suspended_at"),
                "archived": md_archived,
                "owner": md_owner,
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
    linked_tasks = _scan_linked_tasks(data_dir, slug, sid, claude_uuid)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # T-0080: preserve owner field across suspend/resume so per-user
    # listing filters keep working after a session is suspended.
    owner_val = existing.get("owner") or "~"
    meta: dict = {
        "sid": sid,
        "status": "suspended",
        "window": target_pane.window,
        "cwd": target_pane.cwd,
        "claude_uuid": claude_uuid if claude_uuid else "~",
        "task_id": task_id,
        "started_at": started_at,
        "suspended_at": now,
        "linked_tasks": linked_tasks,
        "owner": owner_val,
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


def resume(cfg: Any, slug: str, sid: str) -> dict:
    """Resume a Claude session — handles paused, suspended, and zombie cases.

    - status=paused with live pane → just clear paused status; user types in tmux.
    - status=paused with no live pane → resurrect (window was closed externally).
    - status=suspended → resurrect (new window + ``claude --resume <uuid>``).
    - status=active with no live pane (zombie) → resurrect.
    - status=active with live pane → error (use pause/suspend first).
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
    meta.pop("paused_at", None)
    new_meta_file = _session_file(data_dir, slug, new_sid)
    _write_session_metadata(new_meta_file, meta)

    # Remove old metadata file if SID changed
    if new_sid != sid and meta_file.exists():
        meta_file.unlink()

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

    # Send initial prompt if provided. Two-phase: text first, brief pause,
    # then a *separate* Enter. tmux wraps long text as a bracketed-paste
    # escape sequence; an Enter inside the paste isn't a submit, so the
    # standalone Enter that follows the wrap-end is what submits the prompt
    # to claude's input box.
    #
    # T-0126: a fixed sleep before send-keys lost the prompt on a loaded
    # host (Claude's TUI startup can exceed several seconds). Poll for the
    # composer prompt marker `❯` via capture-pane and only then type. If
    # the marker never appears within the budget, raise — the spawned pane
    # is still alive, so the caller can recover via inject_input.
    if initial_prompt:
        if not _wait_for_claude_composer_ready(new_pane.pane_id):
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"spawn: claude composer never showed ❯ for sid {new_sid} within "
                f"{_COMPOSER_READY_TIMEOUT_SEC:.0f}s — initial_prompt not delivered "
                "(pane is up; recover via inject_input)"
            )
        _run(["tmux", "send-keys", "-t", new_pane.pane_id, initial_prompt])
        time.sleep(0.4)
        _run(["tmux", "send-keys", "-t", new_pane.pane_id, "Enter"])

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

    owner = _find_owner(data_dir, slug, task_id=task_id)
    if owner is not None and owner != sid:
        raise ActionError(f"bind_task: task {task_id} already bound to {owner}")

    extras = list(meta.get("extra_task_ids") or [])
    extras = [t for t in extras if t and t != "~"]
    extras.append(task_id)
    meta["extra_task_ids"] = extras
    _write_session_metadata(meta_file, meta)

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
