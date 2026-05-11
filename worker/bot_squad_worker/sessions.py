"""Session manager — list, pause, resume, spawn Claude tmux sessions.

Each function is a pure worker action callable. The worker runs as almdudleer
and has access to the user's tmux server via the default socket.

Session ID (SID) format: ``S-<user>-<window>-p<pane_id_no_pct>``
  e.g. ``S-almdudleer-spec5-smoke-p2``
"""
from __future__ import annotations

import os
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

    for pane in panes:
        pane_cwd = Path(pane.cwd) if pane.cwd else None
        if pane.command != "claude":
            continue
        if pane_cwd is None:
            continue
        try:
            if not (pane_cwd == repo_path or pane_cwd.is_relative_to(repo_path)):
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

        # Check if there's an existing metadata file with started_at
        session_file = _session_file(data_dir, slug, sid)
        started_at = None
        if session_file.exists():
            existing = _read_session_metadata(session_file)
            if existing:
                started_at = existing.get("started_at")

        rows.append({
            "sid": sid,
            "status": "active",
            "window": pane.window,
            "cwd": pane.cwd,
            "started_at": started_at,
            "last_prompt_at": last_prompt_at,
            "claude_uuid": claude_uuid,
            "linked_tasks": linked_tasks,
        })

    # --- Paused sessions from metadata files ---
    sessions_dir = data_dir / slug / "sessions"
    if sessions_dir.exists():
        for meta_file in sorted(sessions_dir.glob("*.md")):
            meta = _read_session_metadata(meta_file)
            if meta is None:
                continue
            if meta.get("status") != "paused":
                continue
            sid = meta.get("sid", meta_file.stem)
            if sid in active_sids:
                continue  # already listed as active
            rows.append({
                "sid": sid,
                "status": "paused",
                "window": meta.get("window", ""),
                "cwd": meta.get("cwd", ""),
                "started_at": meta.get("started_at"),
                "last_prompt_at": meta.get("paused_at"),
                "claude_uuid": meta.get("claude_uuid"),
                "linked_tasks": meta.get("linked_tasks") or [],
            })

    return rows


def pause(cfg: Any, slug: str, sid: str) -> dict:
    """Pause a running Claude session.

    1. Find the pane for this SID.
    2. Write metadata file.
    3. Send Ctrl-C then /exit then Enter.
    4. Wait up to 10s for pane to disappear; force-kill if not.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: unknown project slug {slug!r}")

    user = _get_current_user()
    user_home = _get_user_home()
    data_dir = cfg.data_dir

    # Find the pane matching this SID
    panes = list_panes()
    target_pane: PaneInfo | None = None
    for pane in panes:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    if target_pane is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: no active pane found for SID {sid!r}")

    claude_uuid = discover_claude_uuid(target_pane.cwd, user_home)
    linked_tasks = _scan_linked_tasks(data_dir, slug, sid, claude_uuid)

    # Write metadata before sending kill signal (so state is recoverable)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta: dict = {
        "sid": sid,
        "status": "paused",
        "window": target_pane.window,
        "cwd": target_pane.cwd,
        "claude_uuid": claude_uuid if claude_uuid else "~",
        "started_at": "~",
        "paused_at": now,
        "linked_tasks": linked_tasks,
    }
    meta_file = _session_file(data_dir, slug, sid)
    _write_session_metadata(meta_file, meta)

    # Graceful exit: Ctrl-C, then /exit Enter
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "C-c", ""])
    time.sleep(0.3)
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "/exit", "Enter"])

    # Wait up to 10s for pane to disappear
    deadline = time.time() + 10.0
    while time.time() < deadline:
        panes_now = list_panes()
        ids_now = {p.pane_id for p in panes_now}
        if target_pane.pane_id not in ids_now:
            break
        time.sleep(0.5)
    else:
        # Force-kill
        _run(["tmux", "kill-pane", "-t", target_pane.pane_id])

    return {"ok": True, "paused": True}


def resume(cfg: Any, slug: str, sid: str) -> dict:
    """Resume a paused Claude session.

    1. Read metadata file.
    2. Check no live pane already has the same claude_uuid.
    3. spawn new-window with claude --resume <uuid>.
    4. Discover new pane, compute new SID.
    5. Rename metadata file, update status.
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
    if meta.get("status") != "paused":
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: session {sid!r} is not paused (status={meta.get('status')!r})")

    cwd = meta.get("cwd", str(project.repo_path))
    window = meta.get("window", "claude")
    claude_uuid = meta.get("claude_uuid")

    # Guard: no live pane with the same UUID
    if claude_uuid and claude_uuid != "~":
        existing_panes = list_panes()
        for pane in existing_panes:
            if pane.command == "claude":
                existing_uuid = _get_user_home()
                # Check if any live pane is in the same cwd with same uuid
                # (we can't introspect the UUID from the pane directly,
                #  so we check if any live pane matches the exact cwd)
                if pane.cwd == cwd:
                    from bot_squad_worker.actions import ActionError
                    raise ActionError(
                        f"resume: a live claude session already exists in {cwd!r}. "
                        f"Pause or kill it first."
                    )

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    # Spawn new window
    if claude_uuid and claude_uuid != "~":
        cmd = f"claude --resume {claude_uuid}"
    else:
        cmd = "claude"

    result = _run([
        "tmux", "new-window", "-d",
        "-n", window,
        "-c", cwd,
        cmd,
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


def spawn(cfg: Any, slug: str, window: str, initial_prompt: str | None = None) -> dict:
    """Spawn a new Claude session in the project's repo.

    Opens a new tmux window, starts claude (no resume), and optionally
    sends an initial_prompt after a short delay.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: unknown project slug {slug!r}")

    cwd = str(project.repo_path)
    user = _get_current_user()

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    result = _run([
        "tmux", "new-window", "-d",
        "-n", window,
        "-c", cwd,
        "claude",
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

    # Send initial prompt if provided
    if initial_prompt:
        time.sleep(2)
        _run(["tmux", "send-keys", "-t", new_pane.pane_id, initial_prompt, "Enter"])

    return {"ok": True, "sid": new_sid}
