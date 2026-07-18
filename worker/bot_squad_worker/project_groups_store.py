"""Worker-side read-only mirror of ``api/app/project_groups_store.py`` (T-0496,
T-0591 F5.10 seam consumption).

Mirror of the ``group_for_user`` lookup only — duplicated by design (worker and
api are independent packages, no cross-import; see ``task_body.py`` for the
established precedent). The worker needs this at ``ensure_user_conversation``
boot-prompt assembly time, in-process, before a session ever runs (so an HTTP
round-trip through the API isn't the right seam here) — it reads the same
``data/<slug>/groups.json`` the api-side store writes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_slug(slug: str) -> str:
    s = str(slug or "")
    if s in ("", ".", "..") or not _SLUG_RE.match(s) or s.startswith("."):
        raise ValueError(f"unsafe project slug: {slug!r}")
    return s


def group_for_user(data_dir: Path, slug: str, global_user_id: str) -> dict | None:
    """Which group ``global_user_id`` is in for ``slug``, as
    ``{id, name, role, access_scope, prompt, created_at}`` — or ``None`` when
    the user has no group (default treatment). Tolerates a missing/garbage
    ``groups.json`` the same way the api-side store does: no group, not a
    crash."""
    path = Path(data_dir) / _safe_slug(slug) / "groups.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    group_id = (data.get("memberships") or {}).get(str(global_user_id or ""))
    if not group_id:
        return None
    for row in data.get("groups") or []:
        if isinstance(row, dict) and row.get("id") == group_id:
            return row
    return None
