"""Parse YAML-frontmatter markdown files (backlog tasks, etc.).

T-0075: the actual frontmatter split/parse lives in the shared
``app.frontmatter`` module (byte-identical mirror in the worker), so every
task-md/session-md reader uses ONE pyyaml-based parser and block- vs
inline-style YAML lists can no longer drift. This module keeps the
task-specific ``priority`` coercion + ``body``/``path`` shape on top of it.
"""
from __future__ import annotations

from pathlib import Path

from app.frontmatter import FrontmatterError
from app.frontmatter import parse as _parse_frontmatter


class ParseError(Exception):
    pass


def parse_task(path: Path) -> dict:
    text = path.read_text()
    try:
        meta, body = _parse_frontmatter(text)
    except FrontmatterError as e:
        raise ParseError(f"{e} in {path}") from e
    # `priority` is an int sort key for Kanban ordering. Coerce loose values
    # (str ints, floats) → int; anything else (missing, non-numeric) → None.
    raw_priority = meta.get("priority")
    if isinstance(raw_priority, bool):
        priority = None
    elif isinstance(raw_priority, int):
        priority = raw_priority
    elif isinstance(raw_priority, str):
        try:
            priority = int(raw_priority.strip())
        except (ValueError, TypeError):
            priority = None
    else:
        priority = None
    out = {
        **meta,
        "body": body,
        "path": str(path),
    }
    out["priority"] = priority
    return out
