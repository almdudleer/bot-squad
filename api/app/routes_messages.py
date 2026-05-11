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


def _resolve_jsonl(repo_path: Path, claude_uuid: str) -> Path | None:
    """Resolve the .jsonl path for a given repo + claude UUID.

    Returns None if the path does not exist.
    """
    user_home = Path(os.path.expanduser("~"))
    encoded = str(repo_path).replace("/", "-").lstrip("-")
    jsonl_path = user_home / ".claude" / "projects" / encoded / f"{claude_uuid}.jsonl"
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
