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


def _get_claude_projects_dir() -> Path:
    """Return the path to ~/.claude/projects.

    Checks CLAUDE_PROJECTS_DIR env var first (useful in Docker); falls back to
    the running user's home directory.
    """
    env_override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if env_override:
        return Path(env_override)
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def _encode_repo_path(repo_path: Path) -> str:
    """Encode a repo path to Claude's project directory naming convention.

    Claude stores sessions under ~/.claude/projects/<encoded-cwd>/.
    The encoding replaces every '/' and '_' with '-'.  The leading '-'
    (from the leading '/') is kept as-is — do NOT strip it.

    Example: /home/almdudleer/signal_tracker_mgmt
          →  -home-almdudleer-signal-tracker-mgmt
    """
    s = str(repo_path)
    return s.replace("/", "-").replace("_", "-")


def _resolve_jsonl(repo_path: Path, claude_uuid: str) -> Path | None:
    """Resolve the .jsonl path for a given repo + claude UUID.

    Returns None if the path does not exist.
    """
    projects_dir = _get_claude_projects_dir()
    encoded = _encode_repo_path(repo_path)
    jsonl_path = projects_dir / encoded / f"{claude_uuid}.jsonl"
    return jsonl_path if jsonl_path.exists() else None


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

    jsonl_path = _resolve_jsonl(project.repo_path, claude_uuid)
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
