"""Per-project user GROUPS — role / access scope / prompt + membership (T-0496).

voice-05 (verbatim): bind users into per-project GROUPS, each with "their own
roles and their own access and their own prompts" (e.g. a developer group =
full bot-squad control; a support group = a scoped "help refine the
manually-validated things toward automation"). The SAME conversational sessions
differ ONLY by their group prompt — this generalizes and replaces the hacky
one-session-bound support bot.

This module is the BACKEND foundation: the durable per-project group registry +
membership map, plus the lookup the conversational role consumes — "which group
is user X in for project Y" -> ``{role, access_scope, prompt}``.

Design (mirrors ``mothership_users_store.py`` — atomic tmp+rename writes, a
class-level mutation lock so concurrent API requests can't interleave their
read-modify-write windows, dataclass + allowlist-by-field reads for
forward-compat on rollback):

* Keyed by ``project_slug``. Each project owns ONE ``groups.json`` under its own
  data dir (``data/<slug>/groups.json``) — group definitions are project-scoped
  CONFIG (voice-05: "this should be configured on project level"), distinct from
  the mothership-centralized conversation history (``conversation_store``).
* Membership maps a mothership ``global_user_id`` (the cross-server identity from
  T-0488) to AT MOST ONE group per project (voice-05 binds a user INTO a group —
  singular). Setting a membership moves the user; there is no multi-group fan-out.
* ``role`` and ``access_scope`` are free-form strings: this store EXPOSES them;
  the conversational role (the T-0478 user-conversation seam) is what loads the
  prompt and ENFORCES the access scope. Keeping them unconstrained here avoids
  prematurely freezing a taxonomy the consumer hasn't pinned down yet.

SEAMS / follow-ups (NOT built here — see T-0496):
* The conversational SESSION loading its group's prompt + enforcing access scope
  = the T-0478 user-conversation role seam. It consumes ``group_for_user`` (the
  store exposes the lookup; the role consumes it). A worker-token read route can
  be added there when the role wires in.
* The web UI for membership management = a separate FE ticket.
* Retiring the hacky one-session support bot = a migration once the role
  consumes groups.

Detach build: this is a project-scoped store with no mothership-identity import
beyond the opaque ``global_user_id`` string, but it is only mounted on the
MOTHERSHIP build (membership keys on the mothership identity).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1

# A project slug must be a single, non-traversing path segment — the same guard
# ``conversation_store`` applies, so a crafted slug can never walk outside the
# project data root when we build ``data/<slug>/groups.json``.
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mint_group_id() -> str:
    # ``grp_`` prefix mirrors ``gu_``/``srv_`` so the id namespace is
    # self-describing wherever it appears (e.g. as a membership value).
    return f"grp_{secrets.token_hex(8)}"


def _safe_slug(slug: str) -> str:
    s = str(slug or "")
    if s in ("", ".", "..") or not _SLUG_RE.match(s) or s.startswith("."):
        raise ValueError(f"unsafe project slug: {slug!r}")
    return s


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    # Free-form role label (e.g. "developer", "support"). The conversational
    # role consumes this; this store does not interpret it.
    role: str = ""
    # Free-form access-scope descriptor (e.g. "full bot-squad control" vs a
    # scoped brief). Exposed here, ENFORCED by the T-0478 role seam.
    access_scope: str = ""
    # The group's prompt — what differentiates otherwise-identical conversational
    # sessions (voice-05).
    prompt: str = ""
    created_at: str = ""

    def to_public(self) -> dict:
        return asdict(self)


class ProjectGroupsStore:
    """File-backed per-project group registry + membership map.

    One ``groups.json`` per project::

        {"version": 1,
         "groups": [{"id": "grp_..", "name": "developers", "role": "...",
                     "access_scope": "...", "prompt": "...", "created_at": "..."}],
         "memberships": {"gu_abc": "grp_.."}}

    The mutation lock is class-level (shared across slugs): a group delete must
    atomically drop its memberships, and the windows are short, so one lock is
    simpler than per-slug locks and correct. Readers stay lockless — the atomic
    tmp+rename write gives a torn-write-free snapshot.
    """

    _lock = threading.Lock()

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    # ---- paths / raw IO -----------------------------------------------------

    def _groups_path(self, slug: str) -> Path:
        s = _safe_slug(slug)
        return self.data_dir / s / "groups.json"

    def _read(self, slug: str) -> dict:
        path = self._groups_path(slug)
        if not path.exists():
            return {"version": SCHEMA_VERSION, "groups": [], "memberships": {}}
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            # Tolerate a torn/garbage file rather than 500 every read — an empty
            # registry is the safe default (a missing membership simply means
            # "no group", which the role seam already handles).
            return {"version": SCHEMA_VERSION, "groups": [], "memberships": {}}
        if not isinstance(data, dict):
            return {"version": SCHEMA_VERSION, "groups": [], "memberships": {}}
        data.setdefault("groups", [])
        data.setdefault("memberships", {})
        return data

    def _write(self, slug: str, groups: list[Group], memberships: dict[str, str]) -> None:
        path = self._groups_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "groups": [asdict(g) for g in groups],
                    "memberships": dict(memberships),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.rename(tmp, path)

    @staticmethod
    def _row_to_group(row: dict) -> Group:
        # Allowlist by dataclass field names so a newer process adding a field
        # doesn't 500 this reader on rollback.
        known = {f.name for f in fields(Group)}
        kept = {k: v for k, v in row.items() if k in known}
        return Group(**kept)

    # ---- group reads --------------------------------------------------------

    def list_groups(self, slug: str) -> list[Group]:
        return [self._row_to_group(r) for r in self._read(slug).get("groups", [])
                if isinstance(r, dict) and r.get("id")]

    def get_group(self, slug: str, group_id: str) -> Group | None:
        for g in self.list_groups(slug):
            if g.id == group_id:
                return g
        return None

    def group_by_name(self, slug: str, name: str) -> Group | None:
        for g in self.list_groups(slug):
            if g.name == name:
                return g
        return None

    # ---- group mutations ----------------------------------------------------

    def create_group(
        self,
        slug: str,
        *,
        name: str,
        role: str = "",
        access_scope: str = "",
        prompt: str = "",
    ) -> Group:
        """Create a group in ``slug``. Raises ``ValueError`` on an empty name or a
        duplicate name within the project (names are the human handle the UI binds
        members to, so they must be unique per project)."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("group name is required")
        entry = Group(
            id=_mint_group_id(),
            name=clean_name,
            role=str(role or ""),
            access_scope=str(access_scope or ""),
            prompt=str(prompt or ""),
            created_at=_utc_now_iso(),
        )
        with self._lock:
            data = self._read(slug)
            groups = [self._row_to_group(r) for r in data["groups"] if isinstance(r, dict) and r.get("id")]
            for existing in groups:
                if existing.name == clean_name:
                    raise ValueError(f"group already exists: {clean_name}")
            groups.append(entry)
            self._write(slug, groups, data.get("memberships", {}))
        return entry

    def update_group(
        self,
        slug: str,
        group_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        access_scope: str | None = None,
        prompt: str | None = None,
    ) -> Group | None:
        """Partial update — only the passed (non-``None``) fields change.
        Idempotent: re-applying the same values is a no-op write. Returns the
        updated group, or ``None`` if no such group. Raises ``ValueError`` if a
        rename collides with another group's name."""
        with self._lock:
            data = self._read(slug)
            groups = [self._row_to_group(r) for r in data["groups"] if isinstance(r, dict) and r.get("id")]
            idx = next((i for i, g in enumerate(groups) if g.id == group_id), None)
            if idx is None:
                return None
            changes: dict = {}
            if name is not None:
                clean_name = str(name).strip()
                if not clean_name:
                    raise ValueError("group name is required")
                for other in groups:
                    if other.id != group_id and other.name == clean_name:
                        raise ValueError(f"group already exists: {clean_name}")
                changes["name"] = clean_name
            if role is not None:
                changes["role"] = str(role)
            if access_scope is not None:
                changes["access_scope"] = str(access_scope)
            if prompt is not None:
                changes["prompt"] = str(prompt)
            updated = replace(groups[idx], **changes)
            groups[idx] = updated
            self._write(slug, groups, data.get("memberships", {}))
            return updated

    def delete_group(self, slug: str, group_id: str) -> bool:
        """Delete a group AND every membership pointing at it (a member of a
        deleted group becomes group-less, i.e. the default conversational
        treatment). Idempotent — a second call returns ``False``."""
        with self._lock:
            data = self._read(slug)
            groups = [self._row_to_group(r) for r in data["groups"] if isinstance(r, dict) and r.get("id")]
            remaining = [g for g in groups if g.id != group_id]
            if len(remaining) == len(groups):
                return False
            memberships = {gid: grp for gid, grp in data.get("memberships", {}).items()
                           if grp != group_id}
            self._write(slug, remaining, memberships)
            return True

    # ---- membership ---------------------------------------------------------

    def set_membership(self, slug: str, global_user_id: str, group_id: str) -> None:
        """Bind ``global_user_id`` to ``group_id`` in ``slug`` (moving them off
        any prior group — a user belongs to AT MOST one group per project).
        Idempotent. Raises ``ValueError`` if the group doesn't exist (so a typo
        can't strand a user on a phantom group)."""
        gid = str(global_user_id or "").strip()
        if not gid:
            raise ValueError("global_user_id is required")
        with self._lock:
            data = self._read(slug)
            groups = [self._row_to_group(r) for r in data["groups"] if isinstance(r, dict) and r.get("id")]
            if not any(g.id == group_id for g in groups):
                raise ValueError(f"unknown group: {group_id}")
            memberships = dict(data.get("memberships", {}))
            memberships[gid] = group_id
            self._write(slug, groups, memberships)

    def remove_membership(self, slug: str, global_user_id: str) -> bool:
        """Drop ``global_user_id``'s membership in ``slug``. Idempotent — returns
        ``False`` if they weren't a member."""
        gid = str(global_user_id or "")
        with self._lock:
            data = self._read(slug)
            memberships = dict(data.get("memberships", {}))
            if gid not in memberships:
                return False
            del memberships[gid]
            groups = [self._row_to_group(r) for r in data["groups"] if isinstance(r, dict) and r.get("id")]
            self._write(slug, groups, memberships)
            return True

    def group_for_user(self, slug: str, global_user_id: str) -> Group | None:
        """THE seam lookup (T-0478): which group is ``global_user_id`` in for
        project ``slug``, as a full ``Group`` (``{role, access_scope, prompt}``)
        — or ``None`` when the user has no group (default treatment). The
        conversational role consumes this to load the prompt + enforce scope."""
        data = self._read(slug)
        group_id = data.get("memberships", {}).get(str(global_user_id or ""))
        if not group_id:
            return None
        for g in self.list_groups(slug):
            if g.id == group_id:
                return g
        return None

    def list_members(self, slug: str, group_id: str) -> list[str]:
        """The ``global_user_id`` list bound to ``group_id`` (sorted, stable)."""
        memberships = self._read(slug).get("memberships", {})
        return sorted(gid for gid, grp in memberships.items() if grp == group_id)
