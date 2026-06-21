"""Atomic write + frontmatter merge helpers for backlog tasks."""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from app.frontmatter import dump_frontmatter
from app.markdown_parser import parse_task


# T-0373: cross-process lock for a task-md read-modify-write. The worker and the
# API mutate the SAME backlog md files from different processes; without a lock
# they raced (lost updates + a shared `.tmp` clobber that 500'd). Both sides
# flock the SAME lockfile path (``<task>.md.lock``) so the critical section is
# mutually exclusive across processes. Mirror this convention in the worker.
LOCK_SUFFIX = ".lock"


@contextlib.contextmanager
def task_lock(path: Path):
    """Hold an exclusive cross-process lock for mutating ``path`` (a task md).

    Wrap the entire read→modify→write in this so a concurrent writer (worker or
    API) can't interleave. The lockfile is ``<path><LOCK_SUFFIX>``; it is created
    if absent and never deleted (deleting it would reintroduce the race)."""
    lock_path = path.parent / (path.name + LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

# Keys allowed in merge_task_update `updates` dict.
# T-0038 adds first-class linkage fields: `initiative` (basename under
# vision/initiatives/), `parent_task` (T-NNNN), `blocked_by` (list of T-NNNN).
# T-0105 adds `session_history` (append-only list of SIDs that worked on
# this task, oldest first). T-0075: the shared writer emits list fields
# INLINE (`[sid1, sid2]`) regardless of write path, so worker- and api-written
# lists are byte-shape-identical and the old block-vs-inline drift is gone.
_ALLOWED_UPDATE_KEYS = frozenset({
    "title", "status", "body", "priority",
    "initiative", "parent_task", "blocked_by",
    "session_history",
    # T-0172: ticket→doc mentions (list of D-NNNN). Kept in sync with each
    # doc's `related_tickets` by routes_docs link/unlink.
    "related_docs",
})

_TASK_ID_RE = re.compile(r"^T-(\d{4,})-")  # T-0371: ids cross the 9999 ceiling


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_task(path: Path, frontmatter: dict, body: str) -> None:
    """Write a task file atomically — UNIQUE tmp then os.replace.

    T-0373: a unique per-writer tmp (``mkstemp``) instead of a shared
    ``<name>.tmp`` so two concurrent writers never clobber each other's tmp (the
    shared name 500'd with FileNotFoundError when one writer's rename moved the
    tmp out from under another)."""
    # Drop None values so missing keys (notably `priority`) don't serialize
    # as `key: null` — keeps frontmatter clean and parser semantics symmetric.
    fm_clean = {k: v for k, v in frontmatter.items() if v is not None}
    fm_str = dump_frontmatter(fm_clean)
    content = f"---\n{fm_str}---\n\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)  # atomic
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def merge_task_update(path: Path, updates: dict, body: str | None = None) -> dict:
    """Read an existing task, apply updates, write atomically. Returns new frontmatter."""
    # Validate update keys
    bad_keys = set(updates) - _ALLOWED_UPDATE_KEYS
    if bad_keys:
        raise ValueError(f"disallowed update keys: {bad_keys!r}")

    # T-0373: lock the whole read→modify→write so a concurrent writer can't
    # interleave between parse_task and write_task (lost-update race).
    with task_lock(path):
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


# T-0335 item 18: append_comment() removed — the ``## Comments`` channel it wrote
# was orphaned (its only caller, POST /backlog/{id}/comments, is cut; the board
# comment kebab posts a Progress note instead).


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
