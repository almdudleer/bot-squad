"""Parse YAML-frontmatter markdown files (backlog tasks, etc.)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml


class ParseError(Exception):
    pass


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


def parse_task(path: Path) -> dict:
    text = path.read_text()
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ParseError(f"no YAML frontmatter in {path}")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ParseError(f"bad YAML in {path}: {e}")
    if not isinstance(meta, dict):
        raise ParseError(f"frontmatter is not a mapping in {path}")
    body = m.group(2).lstrip("\n")
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
