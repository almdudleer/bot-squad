"""Atomic write + frontmatter merge helpers for backlog tasks."""
from __future__ import annotations

import fcntl
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.markdown_parser import parse_task

# Keys allowed in merge_task_update `updates` dict.
# T-0038 adds first-class linkage fields: `initiative` (basename under
# vision/initiatives/), `parent_task` (T-NNNN), `blocked_by` (list of T-NNNN).
# T-0105 adds `session_history` (append-only list of SIDs that worked on
# this task, oldest first). Worker writes the field inline-format
# (`[sid1, sid2]`) via sessions.py; the api PATCH path lets us backfill
# block-format via the yaml-dump writer when needed.
_ALLOWED_UPDATE_KEYS = frozenset({
    "title", "status", "body", "priority",
    "initiative", "parent_task", "blocked_by",
    "session_history",
})

_TASK_ID_RE = re.compile(r"^T-(\d{4})-")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_task(path: Path, frontmatter: dict, body: str) -> None:
    """Write a task file atomically — .tmp then os.rename."""
    # Drop None values so missing keys (notably `priority`) don't serialize
    # as `key: null` — keeps frontmatter clean and parser semantics symmetric.
    fm_clean = {k: v for k, v in frontmatter.items() if v is not None}
    fm_str = yaml.safe_dump(fm_clean, allow_unicode=True, sort_keys=False)
    content = f"---\n{fm_str}---\n\n{body}"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)


def merge_task_update(path: Path, updates: dict, body: str | None = None) -> dict:
    """Read an existing task, apply updates, write atomically. Returns new frontmatter."""
    # Validate update keys
    bad_keys = set(updates) - _ALLOWED_UPDATE_KEYS
    if bad_keys:
        raise ValueError(f"disallowed update keys: {bad_keys!r}")

    task = parse_task(path)
    # Build updated frontmatter from existing, minus non-frontmatter keys
    fm = {k: v for k, v in task.items() if k not in ("body", "path")}

    # Backfill created from mtime if missing
    if "created" not in fm:
        mtime = os.stat(path).st_mtime
        fm["created"] = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    # Apply updates
    for k, v in updates.items():
        if k != "body":
            fm[k] = v

    fm["updated"] = _now_utc_iso()

    new_body = body if body is not None else task["body"]
    write_task(path, fm, new_body)
    return fm


def append_comment(path: Path, comment_body: str, author: str) -> None:
    """Append a comment to the ## Comments section, creating it if absent."""
    if not comment_body.strip():
        raise ValueError("empty comment body")

    task = parse_task(path)
    fm = {k: v for k, v in task.items() if k not in ("body", "path")}
    body = task["body"]

    # Backfill created if missing
    if "created" not in fm:
        mtime = os.stat(path).st_mtime
        fm["created"] = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    fm["updated"] = _now_utc_iso()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    comment_block = f"### {today} {author}\n\n{comment_body.strip()}\n"

    if "## Comments" in body:
        body = body.rstrip("\n") + "\n\n" + comment_block
    else:
        body = body.rstrip("\n") + "\n\n## Comments\n\n" + comment_block

    write_task(path, fm, body)


def allocate_next_id(backlog_dir: Path) -> str:
    """Scan T-NNNN-*.md, return T-{max+1:04d}. Caller must hold flock."""
    max_n = 0
    for f in backlog_dir.glob("T-*.md"):
        m = _TASK_ID_RE.match(f.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"T-{max_n + 1:04d}"


def slugify(s: str, max_len: int = 60) -> str:
    """Convert a string to kebab-case ASCII slug."""
    # Normalize unicode (decompose accented chars, etc.)
    s = unicodedata.normalize("NFKD", s)
    # Keep only ASCII letters, digits, spaces, hyphens
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s)
    s = s.lower().strip()
    # Collapse whitespace + hyphens into single hyphen
    s = re.sub(r"[\s-]+", "-", s)
    s = s.strip("-")
    return s[:max_len]
