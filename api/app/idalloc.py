"""Atomic, per-type ID allocation across all bot-squad entity types (T-0174).

CANONICAL COPY:  worker/bot_squad_worker/idalloc.py
MIRROR:          api/app/idalloc.py

These two files MUST stay byte-identical — the worker (systemd) and the API
(docker) run in separate Python environments and do not import each other, so
the only thing they share is the on-disk data mount. The mirror registry in
`worker/tests/test_module_mirrors.py` (T-0743) asserts the two files match
byte-for-byte, and `scripts/lint/module_mirrors.py` runs that on every push;
keep this module dependency-free (stdlib only, no package-relative imports) so
the copy stays valid in both.

Why this exists
---------------
Before T-0174 every entity type allocated IDs its own way: tickets had TWO
independent allocators (the worker's ``task_new`` locked ``.task-id.lock`` while
the API's ``POST /backlog`` locked ``.lock`` — different files, so a web create
and an agent ``task new`` could hand out the same T-NNNN), and initiatives/docs
had nothing. That hand-picking is the root cause of the ID-collision incidents
(T-0042, T-0114, T-0120).

The model
---------
Each entity type has one fcntl-locked counter file at
``data/<slug>/_counters/<type>.txt`` holding the last-allocated integer.
Allocation is::

    next = max(counter_value, max_existing_file_id) + 1

The ``max_existing_file_id`` scan does two jobs for free:

* **Migration** — on first allocation the counter file is absent, so the scan
  seeds the counter from the highest existing entity (``max(existing) + 1``,
  exactly what the DoD asks for). No separate migration script.
* **Self-heal** — if someone hand-creates a file with a higher id (the
  documented emergency fallback), the next allocation still won't collide.

The flock is taken on the counter file itself, so concurrent callers — across
processes AND across the worker/API container boundary on the shared mount —
serialise. A crash between allocate and file-write merely burns one id; ids are
not scarce.
"""
from __future__ import annotations

import fcntl
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EntityType:
    key: str  # counter-file stem AND the value callers pass as `type`
    prefix: str  # id prefix before the number (e.g. "T", "UC", "INI")
    width: int  # zero-pad width of the numeric part
    scan_subdir: str  # dir (relative to the project data dir) to scan for ids
    scan_recursive: bool  # whether existing-id scan walks subdirectories


# The extensible registry. Adding a new entity type = one entry here (plus a
# bsq verb / route that calls allocate_id with its key).
ENTITY_TYPES: dict[str, EntityType] = {
    "task": EntityType("task", "T", 4, "backlog", False),
    "doc": EntityType("doc", "D", 4, "docs", True),
    # Initiatives are rare; a 2-digit pad is plenty. Legacy initiatives are
    # slug-named (non-numeric) and are ignored by the scan, so new ones start
    # at INI-01 while old ones keep working.
    "initiative": EntityType("initiative", "INI", 2, "vision/initiatives", False),
    # T-0464: Routines (Process Paradigm M1) — a declared rule that spawns a
    # session on a trigger (schedule first). Stored as md under
    # data/<slug>/routines/<R-NNNN>-*.md (non-recursive scan), keyed off its own
    # counter, prefix "R-".
    "routine": EntityType("routine", "R", 4, "routines", False),
}


class IdAllocError(ValueError):
    """Raised for an unknown entity type or a bad slug/data_dir."""


def format_id(type_key: str, n: int) -> str:
    """Render ``n`` as the canonical id for ``type_key`` (e.g. (\"task\", 7) -> \"T-0007\")."""
    et = ENTITY_TYPES.get(type_key)
    if et is None:
        raise IdAllocError(f"unknown entity type: {type_key!r}")
    return f"{et.prefix}-{n:0{et.width}d}"


def _id_pattern(prefix: str) -> "re.Pattern[str]":
    # Matches "<prefix>-<digits>" at the start of a filename, with the digits
    # delimited by "-", "." or end-of-stem so "T-12-foo.md" -> 12 but a
    # hypothetical "TX-1.md" never matches the "T" prefix.
    return re.compile(rf"^{re.escape(prefix)}-(\d+)(?:[-.]|$)")


_FRONTMATTER_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)


def _frontmatter_id(path: Path) -> str | None:
    """Best-effort read of a md file's own ``id:`` frontmatter value, or None."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    fm_end = text.find("\n---", 4) if text.startswith("---\n") else -1
    if fm_end < 0:
        return None
    m = _FRONTMATTER_ID_RE.search(text[:fm_end])
    return m.group(1).strip() if m else None


def scan_existing_max(project_dir: Path, et: EntityType) -> int:
    """Highest numeric id of ``et`` already on disk under ``project_dir`` (0 if none).

    Scans two ways and takes the max (T-0231): by FILENAME prefix (the
    original behavior — cheap, covers the common case), and by each file's
    own ``id:`` frontmatter value regardless of filename (defense in depth —
    a file's filename numeric prefix and its declared ``id:`` can diverge
    after a manual edit, rename, or tombstone, which the filename-only scan
    can't see). Without the second pass, a hand-authored file whose
    frontmatter claims an id but whose filename doesn't start with it would
    be invisible to this allocator and a fresh ``allocate_id`` could mint a
    real ``id:`` collision (the T-0030 / T-0222 incidents).
    """
    scan_dir = project_dir / et.scan_subdir
    if not scan_dir.is_dir():
        return 0
    pat = _id_pattern(et.prefix)
    globber = scan_dir.rglob if et.scan_recursive else scan_dir.glob
    max_n = 0
    for p in globber(f"{et.prefix}-*"):
        m = pat.match(p.name)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    id_pat = re.compile(rf"^{re.escape(et.prefix)}-(\d+)$")
    for p in globber("*.md"):
        fid = _frontmatter_id(p)
        if not fid:
            continue
        m = id_pat.match(fid)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n


def allocate_id(data_dir: Path, slug: str, type_key: str) -> str:
    """Atomically allocate and return the next id for ``type_key`` in ``slug``.

    ``data_dir`` is the install data root (the dir that contains per-project
    subdirs); the project's data lives at ``data_dir / slug``. Allocation is
    serialised by an fcntl.flock on ``data_dir/slug/_counters/<type>.txt`` and
    is safe across processes and across the worker/API container boundary on a
    shared mount. Returns the formatted id (e.g. ``"T-0175"``); the caller is
    responsible for writing the entity file.
    """
    et = ENTITY_TYPES.get(type_key)
    if et is None:
        raise IdAllocError(f"unknown entity type: {type_key!r}")
    if not slug or "/" in slug or slug in (".", ".."):
        raise IdAllocError(f"bad slug: {slug!r}")

    data_dir = Path(data_dir)
    project_dir = data_dir / slug
    counters_dir = project_dir / "_counters"
    counters_dir.mkdir(parents=True, exist_ok=True)
    counter_path = counters_dir / f"{et.key}.txt"

    # "a+" creates the file if absent without truncating an existing one; we
    # hold the exclusive lock across the read-modify-write so no other caller
    # can interleave between our scan and our write-back.
    with open(counter_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read().strip()
            counter = int(raw) if raw.isdigit() else 0
            scanned = scan_existing_max(project_dir, et)
            nxt = max(counter, scanned) + 1
            fh.seek(0)
            fh.truncate()
            fh.write(str(nxt))
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    return format_id(type_key, nxt)
