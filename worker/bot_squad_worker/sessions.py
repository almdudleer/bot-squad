"""Session manager — list, pause, resume, spawn Claude tmux sessions.

Each function is a pure worker action callable. The worker runs as almdudleer
and has access to the user's tmux server via the default socket.

Session ID (SID) format: ``S-<user>-<window>-p<pane_id_no_pct>``
  e.g. ``S-almdudleer-spec5-smoke-p2``
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    fmt = "#{pane_id}|#{window_name}|#{pane_pid}|#{pane_current_path}|#{pane_current_command}"
    result = _run(["tmux", "list-panes", "-a", "-F", fmt])
    if result.returncode != 0:
        return []
    panes = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        panes.append(PaneInfo(
            pane_id=parts[0],
            window=parts[1],
            pid=parts[2],
            cwd=parts[3],
            command=parts[4],
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
      ``cwd.replace('/', '-').lstrip('-')``
    Returns None if no project dir or no .jsonl files exist.
    """
    encoded = cwd.replace("/", "-").lstrip("-")
    proj_dir = Path(user_home) / ".claude" / "projects" / encoded
    if not proj_dir.exists():
        return None
    jsonl_files = list(proj_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    # Latest by mtime → that's the active session
    latest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
    return latest.stem  # filename without .jsonl = UUID


def _get_user_home() -> str:
    """Return the home directory for the current user."""
    return str(Path.home())


def _session_file(data_dir: Path, slug: str, sid: str) -> Path:
    return data_dir / slug / "sessions" / f"{sid}.md"


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


def _ensure_project_tmux_session(slug: str, cwd: str) -> None:
    """Ensure a long-lived tmux session named after the project exists.

    Per the active-context-manager model: one tmux session per project,
    panes/windows live inside it. Survives across spawn/resume cycles.
    Caller must guarantee the session is created before any new-window.
    """
    has = _run(["tmux", "has-session", "-t", slug])
    if has.returncode == 0:
        return
    # Create detached; -n _init parks a placeholder window we never use for
    # claude. claude windows are added via tmux new-window -t <slug>:.
    _run([
        "tmux", "new-session", "-d",
        "-s", slug,
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
            if not match:
                continue
        except (ValueError, TypeError):
            continue

        sid = compute_sid(user, pane.window, pane.pane_id)
        active_sids.add(sid)

        claude_uuid = discover_claude_uuid(pane.cwd, user_home)
        linked_tasks = _scan_linked_tasks(data_dir, slug, sid, claude_uuid)

        # Check for last_prompt_at via .claude/last_user_prompt_ts mtime
        last_prompt_at = None
        prompt_ts_file = repo_path / ".claude" / "last_user_prompt_ts"
        if prompt_ts_file.exists():
            try:
                last_prompt_at = prompt_ts_file.stat().st_mtime
            except OSError:
                pass

        # Check if there's an existing metadata file with started_at + task_id
        session_file = _session_file(data_dir, slug, sid)
        started_at = None
        task_id: str | None = None
        initiative: str = ""
        # Default to "active" for live panes; if the md frontmatter says
        # paused (Ctrl-C'd but pane left open) reflect that — otherwise
        # the UI shows every live pane as active even when the user paused it.
        live_status = "active"
        if session_file.exists():
            existing = _read_session_metadata(session_file)
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

        # Phase 9: extras for multi-binding. Empty list when unset.
        extra_task_ids: list[str] = []
        extra_initiatives: list[str] = []
        paused_at_meta: Any = None
        archived_flag = False
        if session_file.exists():
            existing = _read_session_metadata(session_file)
            if existing:
                etids = existing.get("extra_task_ids")
                if isinstance(etids, list):
                    extra_task_ids = [t for t in etids if t and t != "~"]
                einits = existing.get("extra_initiatives")
                if isinstance(einits, list):
                    extra_initiatives = [i for i in einits if i and i != "~"]
                paused_at_meta = existing.get("paused_at")
                archived_flag = str(existing.get("archived", "")).lower() == "true"

        rows.append({
            "sid": sid,
            "status": live_status,
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
            rows.append({
                "sid": sid,
                "status": display_status,
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

    # One tmux session per project — create lazily, never killed.
    _ensure_project_tmux_session(slug, cwd)

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
        "-t", f"{slug}:",
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

    return {"ok": True, "sid": new_sid}


def spawn(
    cfg: Any,
    slug: str,
    window: str,
    initial_prompt: str | None = None,
    task_id: str | None = None,
    initiative: str | None = None,
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

    # One tmux session per project — create lazily, never killed.
    _ensure_project_tmux_session(slug, cwd)

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    # bash -lc so claude (in ~/.local/bin) is on PATH — the worker's
    # systemd env does not include the user's local bin directory.
    # --dangerously-skip-permissions: see resume() rationale above.
    # BOT_SQUAD_INITIATIVE: per-session initiative override (Phase 4).
    if initiative:
        # Basic safety: only basename, must end .md, no slashes/..
        clean = initiative.strip()
        if "/" in clean or ".." in clean or not clean.endswith(".md"):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid initiative name {initiative!r}")
        shell_cmd = f"BOT_SQUAD_INITIATIVE={shlex.quote(clean)} claude --dangerously-skip-permissions"
    else:
        shell_cmd = "claude --dangerously-skip-permissions"

    result = _run([
        "tmux", "new-window", "-d",
        "-t", f"{slug}:",
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

    # Send initial prompt if provided. Two-phase: text first, brief pause,
    # then a *separate* Enter. tmux wraps long text as a bracketed-paste
    # escape sequence; an Enter inside the paste isn't a submit, so the
    # standalone Enter that follows the wrap-end is what submits the prompt
    # to claude's input box.
    if initial_prompt:
        time.sleep(2)
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
