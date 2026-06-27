"""Per-project pinned-session store (T-0437).

A user can PIN a session within a project ("I'm working closely with this one")
so it surfaces distinctly in the Processes view and as a marker on the bound
Board card. The pin is a USER SIGNAL + a hint — it does NOT change operator
orchestration; the operator still drives the work.

Design notes:
  * Pins are per-PROJECT (the brain is per-project, T-0331). For MVP the pin set
    is project-scoped (NOT per-user); a per-user pin view is a noted future
    extension.
  * Stored API-side ONLY. The worker never reads pins, so there is NO worker
    mirror and therefore NO dual-writer divergence risk (cf. the tg_topics.py
    worker-owned store, which is the opposite split). Single writer per file.
  * Atomic write (tmp + os.replace), mirroring the tg_topics.py pattern.

Shape on disk (``data/<slug>/_api/pins.json``)::

    { "<sid>": {"by": "<username>", "at": "<iso8601>"} }

so the UI can show "pinned by X". A missing/empty file means "no pins".
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def pins_path(data_dir: Path, slug: str) -> Path:
    """Where the per-project sid→pin-meta map is persisted."""
    return Path(data_dir) / slug / "_api" / "pins.json"


def load(data_dir: Path, slug: str) -> dict[str, dict]:
    """Return the persisted sid→{by,at} map, or {} when none/unreadable."""
    p = pins_path(data_dir, slug)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("pins: unreadable store at %s — treating as empty", p)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for sid, meta in raw.items():
        m = meta if isinstance(meta, dict) else {}
        out[str(sid)] = {"by": m.get("by"), "at": m.get("at")}
    return out


def _save(data_dir: Path, slug: str, pins: dict[str, dict]) -> None:
    """Atomically persist the sid→{by,at} map."""
    p = pins_path(data_dir, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pins, indent=2))
    os.replace(tmp, p)


def pin(data_dir: Path, slug: str, sid: str, *, by: str, at: str) -> dict:
    """Pin ``sid`` in ``slug``. Idempotent — re-pinning refreshes by/at.

    Returns the stored ``{by, at}`` record.
    """
    pins = load(data_dir, slug)
    pins[sid] = {"by": by, "at": at}
    _save(data_dir, slug, pins)
    return pins[sid]


def unpin(data_dir: Path, slug: str, sid: str) -> bool:
    """Remove ``sid``'s pin. Returns True if it had been pinned (idempotent)."""
    pins = load(data_dir, slug)
    if sid not in pins:
        return False
    del pins[sid]
    _save(data_dir, slug, pins)
    return True


def is_pinned(data_dir: Path, slug: str, sid: str) -> bool:
    """Convenience predicate — is ``sid`` currently pinned in ``slug``?"""
    return sid in load(data_dir, slug)


# ---------------------------------------------------------------------------
# T-0492: per-(user, server) CURRENT-PROJECT routing map.
#
# Distinct from the per-session sid pins above: this is the user's sticky "I'm
# talking to project X" target — the hardwired routing destination for unquoted
# inbound messages (voice-04: "user pins a project or clicks a button; subsequent
# messages auto-attach to it"). Keyed by ``global_user_id`` (the cross-server
# mothership identity, T-0488); the file lives on THIS server's data_dir, so the
# map is inherently per-(user, server) without a server_id in the key. One user
# has exactly one current project per server (a switch REPLACES it).
#
# Stored at the mothership level (the bot's user-communication is centralized
# there, voice-04) rather than under a project dir, since the whole point is to
# choose AMONG projects::
#
#     data/_mothership/current_project.json
#     { "<global_user_id>": {"slug": "<slug>", "at": "<iso8601>"} }
# ---------------------------------------------------------------------------


def current_project_path(data_dir: Path) -> Path:
    """Where the per-(user, server) current-project map is persisted."""
    return Path(data_dir) / "_mothership" / "current_project.json"


def load_current_projects(data_dir: Path) -> dict[str, dict]:
    """Return the persisted global_user_id→{slug,at} map, or {} when none."""
    p = current_project_path(data_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("current_project: unreadable store at %s — treating as empty", p)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for gid, meta in raw.items():
        m = meta if isinstance(meta, dict) else {}
        out[str(gid)] = {"slug": m.get("slug"), "at": m.get("at")}
    return out


def _save_current_projects(data_dir: Path, cur: dict[str, dict]) -> None:
    p = current_project_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, indent=2))
    os.replace(tmp, p)


def get_current_project(data_dir: Path, global_user_id: str) -> str | None:
    """The user's currently-pinned project slug, or ``None`` when unset."""
    rec = load_current_projects(data_dir).get(str(global_user_id))
    return rec.get("slug") if rec else None


def set_current_project(data_dir: Path, global_user_id: str, slug: str, *, at: str) -> dict:
    """Pin (or switch) ``global_user_id``'s current project. Idempotent — a
    re-pin / switch REPLACES the single entry. Returns the stored record."""
    cur = load_current_projects(data_dir)
    cur[str(global_user_id)] = {"slug": slug, "at": at}
    _save_current_projects(data_dir, cur)
    return cur[str(global_user_id)]


def clear_current_project(data_dir: Path, global_user_id: str) -> bool:
    """Drop the user's current-project pin. Returns True if one existed."""
    cur = load_current_projects(data_dir)
    if str(global_user_id) not in cur:
        return False
    del cur[str(global_user_id)]
    _save_current_projects(data_dir, cur)
    return True
