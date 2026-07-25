"""T-0662: human-readable label -> session SID aliases.

Orthogonal to ``sessions.sid_display_label`` (T-0636) — that's an AUTO-DERIVED
``"[<slug>] <sid>"`` DISPLAY string, always present, never stakeholder-chosen.
A label here is the opposite: stakeholder-assigned, optional, and exists so a
session can be addressed by a short nickname instead of its long/random SID
(e.g. a user-conversation window keyed on a GU-prefixed id) — filed as a
building block for a future gateway control-phrase ``to-session <label>``
resolver (T-0660 Addendum 1; that resolver itself is a separate, later slice
and NOT built here).

Worker-owned, single-writer JSON in the data dir, atomic write — mirrors
``tg_bindings.py``. GLOBAL rather than per-project: ``compute_sid`` carries no
project slug (T-0636 — two sessions in different projects under the same
role/window can share a SID), so a raw SID isn't project-scoped either, and a
label meant to stand alone in a control phrase (no accompanying
``to-project``) must resolve the same way regardless of which project asked.

Shape on disk (``data/_worker/session_aliases.json``)::

    { "<label>": "<sid>" }

One label always resolves to exactly one sid (plain dict semantics — the
downstream resolver's uniqueness requirement); one sid may carry more than one
label (a session can have several nicknames).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_ALIAS_PATH_RELPATH = ("_worker", "session_aliases.json")

# Lowercase, must start with a letter, only [a-z0-9_-] after that, max 32 chars.
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class InvalidLabelError(ValueError):
    """Raised by :func:`set_alias` when the label or sid fails validation."""


def normalize_label(label: str) -> str:
    """Case/whitespace fold applied on every read AND write, so ``Foo``,
    ``foo``, and `` foo `` all hit the same stored key."""
    return label.strip().lower()


def _validate_label(label: str) -> str:
    norm = normalize_label(label)
    if not norm:
        raise InvalidLabelError("label must not be empty")
    if norm.startswith("s-"):
        # A label shaped like a raw SID would be ambiguous for the future
        # to-session resolver (T-0660 Addendum 1) — it couldn't tell a
        # literal-SID phrase from a label phrase.
        raise InvalidLabelError(
            f"label {label!r} looks like a raw SID (starts with 's-') — "
            "would be ambiguous for a future to-session resolver"
        )
    if not _LABEL_RE.match(norm):
        raise InvalidLabelError(
            f"label {label!r} invalid — must start with a letter, contain "
            "only lowercase letters/digits/hyphen/underscore, max 32 chars"
        )
    return norm


def aliases_path(data_dir: Any) -> Path:
    """Where the global label->sid map is persisted."""
    return Path(data_dir) / Path(*_ALIAS_PATH_RELPATH)


def load_aliases(data_dir: Any) -> dict[str, str]:
    """Return the persisted label->sid map, or ``{}`` when missing/corrupt."""
    path = aliases_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _save(data_dir: Any, mapping: dict[str, str]) -> None:
    """Atomically persist the alias map (tmp write + os.replace)."""
    path = aliases_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def set_alias(data_dir: Any, label: str, sid: str) -> str:
    """Point ``label`` at ``sid``.

    Idempotent/last-write-wins — rebinding an existing label REPLACES it (one
    label -> exactly one sid at any time); multiple labels may point at the
    same sid. Returns the normalized (stored) label. Raises
    :class:`InvalidLabelError` if ``label`` fails the format contract or
    ``sid`` is empty.
    """
    if not sid or not str(sid).strip():
        raise InvalidLabelError("sid must not be empty")
    norm = _validate_label(label)
    mapping = load_aliases(data_dir)
    mapping[norm] = str(sid).strip()
    _save(data_dir, mapping)
    return norm


def remove_alias(data_dir: Any, label: str) -> bool:
    """Remove ``label``. Returns True if one existed (idempotent)."""
    norm = normalize_label(label)
    mapping = load_aliases(data_dir)
    if norm not in mapping:
        return False
    del mapping[norm]
    _save(data_dir, mapping)
    return True


def resolve_alias(data_dir: Any, label: str) -> str | None:
    """``label`` -> sid, or ``None`` when unknown. This is the exact lookup
    shape the future ``to-session <label>`` control-phrase resolver (T-0660
    Addendum 1) will consume."""
    return load_aliases(data_dir).get(normalize_label(label))


def aliases_for_sid(data_dir: Any, sid: str) -> list[str]:
    """Every label currently pointing at ``sid`` (a session may carry more
    than one nickname), sorted for stable display."""
    return sorted(l for l, s in load_aliases(data_dir).items() if s == sid)
