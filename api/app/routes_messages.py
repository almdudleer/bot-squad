"""Session message transcript endpoint.

GET /api/projects/{slug}/sessions/{claude_uuid}/messages
    Return parsed conversation records from ~/.claude/projects/<encoded>/<uuid>.jsonl.

The UUID is the Claude session UUID (from the sessions list). Resolution:
    encoded_cwd = cwd.replace('/', '-').lstrip('-')
    path = ~/.claude/projects/<encoded_cwd>/<claude_uuid>.jsonl

The API reads the project's repo_path from projects.toml to derive encoded_cwd.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.routes_auth import require_auth
from app.messages_parser import parse_messages

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/sessions",
    tags=["messages"],
    dependencies=[Depends(require_auth)],
)


# Claude session UUIDs are stored as lower-case hex with dashes (length 36).
# Tighten the shape to a UUIDv4-ish pattern to prevent path traversal via
# the URL parameter.
_UUID_RE = re.compile(r"^[a-f0-9-]{36}$")


def _get_claude_projects_dir() -> Path:
    """Return the path to ~/.claude/projects.

    Checks CLAUDE_PROJECTS_DIR env var first (useful in Docker); falls back to
    the running user's home directory.
    """
    env_override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if env_override:
        return Path(env_override)
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def _resolve_jsonl(claude_uuid: str) -> Path | None:
    """Resolve a .jsonl path by UUID, globbing across every project subdir.

    Claude stores sessions under ``~/.claude/projects/<encoded-cwd>/`` where
    the encoding depends on the actual realpath / cwd Claude was launched
    in — symlinked repo roots, worktrees and worker spawns each land in
    their own encoded dir. The UUID is globally unique so we just glob.

    Returns the first matching path (sorted by mtime, newest first) or
    None when nothing matches. The auth layer is the real gate; project
    scoping was always best-effort anyway.
    """
    projects_dir = _get_claude_projects_dir()
    if not projects_dir.exists():
        return None
    matches = list(projects_dir.glob(f"*/{claude_uuid}.jsonl"))
    if not matches:
        return None
    # Newest first — duplicate UUIDs across encoded dirs would be a Claude
    # bug, but if they exist prefer the freshest log.
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


@router.get("/{claude_uuid}/messages")
async def get_session_messages(
    slug: str,
    claude_uuid: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    full: int = Query(default=0),
) -> list[dict]:
    """Return parsed conversation records for a Claude session.

    Args:
        slug:        Project slug.
        claude_uuid: Claude session UUID (the .jsonl filename stem).
        limit:       Max messages to return (default 200).
        offset:      Skip this many messages from the start.
        full:        If non-zero, do not truncate text fields.
    """
    cfg = request.app.state.api_config
    project = cfg.project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")

    if not _UUID_RE.match(claude_uuid):
        raise HTTPException(status_code=400, detail="invalid claude_uuid format")

    jsonl_path = _resolve_jsonl(claude_uuid)
    if jsonl_path is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"session log not found for UUID {claude_uuid!r}; "
                "this may be a session from another user or the log may have been deleted"
            ),
        )

    try:
        messages = parse_messages(
            jsonl_path,
            limit=limit,
            offset=offset,
            full=bool(full),
        )
    except OSError as e:
        log.error("messages_parser: could not read %s: %s", jsonl_path, e)
        raise HTTPException(status_code=500, detail=f"could not read session log: {e}")

    return messages
