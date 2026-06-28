"""T-0480: resolve an OLD initiative reference to the NEW initiative-task T-id.

Initiatives become tasks marked ``kind: initiative`` (D-0038). Old references —
an ``INI-NN`` id, a ``.md`` basename, or a bare stem — must keep resolving so
nothing breaks mid-migration (DoD read-path requirement I5). This module is the
single shared resolver, MIRRORED BYTE-IDENTICALLY in worker + api exactly like
``idalloc`` / ``normalize_id`` (the project's SSOT pattern):

    worker/bot_squad_worker/initiative_resolver.py
    api/app/initiative_resolver.py

It is intentionally PURE (json + regex, no frontmatter import) so the two copies
can stay byte-identical without depending on either tree's parser. The alias
index itself is BUILT by the single-copy migration script (which reads each
initiative-task's ``aka:`` frontmatter — the human-readable SSOT — and writes the
fast json index this module reads).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# T-0371: ids cross the 9999 ceiling — \d{4,}, not \d{4}.
_TASK_ID_RE = re.compile(r"^T-\d{4,}$")

# Persisted at data/<slug>/vision/initiative_aliases.json — {old_ref: T-id}.
_ALIAS_INDEX_RELPATH = ("vision", "initiative_aliases.json")


def normalize_ref(value: str) -> str:
    """Strip EXACTLY ONE trailing literal lowercase ``.md`` — byte-identical to
    the ``normalize_id`` contract (T-0424). An entity id never carries its file
    suffix; compare/key on the stem. Case-sensitive (never ``.MD``); not greedy
    (``x.md.md`` -> ``x.md``); no trimming."""
    return value[:-3] if value.endswith(".md") else value


def _alias_index_path(data_dir, slug: str) -> Path:
    return Path(data_dir) / slug / Path(*_ALIAS_INDEX_RELPATH)


def load_alias_index(data_dir, slug: str) -> dict[str, str]:
    """Read the alias index, keyed on the stem form (one trailing ``.md``
    stripped) so a basename input and a stem input both hit a single stored key.
    A missing or malformed index is treated as empty (no aliases yet)."""
    path = _alias_index_path(data_dir, slug)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {normalize_ref(str(k)): str(v) for k, v in raw.items()}


def resolve_initiative_ref(data_dir, slug: str, ref) -> str | None:
    """Map ``ref`` (a new ``T-NNNN``, an old ``INI-NN``, or a ``.md`` basename /
    bare stem) to the initiative-task ``T-id``, or ``None`` if unknown.

    A ``T-NNNN`` ref is returned as-is (already canonical). Everything else is
    looked up in the alias index built by the migration."""
    if not ref or not str(ref).strip():
        return None
    stem = normalize_ref(str(ref).strip())
    if _TASK_ID_RE.match(stem):
        return stem
    return load_alias_index(data_dir, slug).get(stem)
