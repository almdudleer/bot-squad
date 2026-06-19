"""GlobalUser + Attachment persistence for the mothership (T-0066).

Lives next to ``mothership_store.py`` (servers registry) and follows the
same pattern: atomic writes via tmp+rename, dataclass + ``replace``, a
class-level mutation lock so concurrent HTTP requests in the same process
can't interleave their read-modify-write windows.

Two on-disk surfaces:

- ``DATA_DIR/_mothership/users.json`` — GlobalUser registry (cross-server
  identity; canonical password lives here).
- ``DATA_DIR/_mothership/attachments/<global_user_id>/<server_id>.json``
  — per-(user × server) binding; holds per-server state that used to live
  in UserMeta (tg_chat_id, seen_steps, last_seen_at).

Detach build: delete this file alongside ``mothership_store.py`` — nothing
outside the centralization layer imports it.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path

from app.roles import GlobalRole, global_role_for, parse_global_role


SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mint_global_user_id() -> str:
    # ``gu_`` prefix mirrors the ``srv_`` prefix on AttachedServer so the two
    # id namespaces are self-describing when they share a JSON payload.
    return f"gu_{secrets.token_hex(12)}"


@dataclass(frozen=True)
class GlobalUser:
    id: str
    username: str
    password_hash: str
    created_at: str
    display_name: str = ""
    email: str = ""
    timezone: str = "UTC"
    # T-0216 Phase A: explicit global-scope role replaces the overloaded
    # is_super_admin bool. is_super_admin survives as a derived compat property.
    global_role: GlobalRole = GlobalRole.GLOBAL_MEMBER

    @property
    def is_super_admin(self) -> bool:
        """Derived global-admin flag — back-compat for readers predating the
        T-0216 GlobalRole field."""
        return self.global_role is GlobalRole.GLOBAL_ADMIN

    def to_public(self) -> dict:
        """Projection consumed by ``GET /api/m/users``. Password hash never
        leaves the registry — strip it before any FE-facing serialisation.
        Keeps the ``is_super_admin`` compat key (FE-facing) alongside the
        canonical ``global_role`` so the frontend is untouched in Phase A."""
        d = asdict(self)
        d.pop("password_hash", None)
        d["global_role"] = self.global_role.value
        d["is_super_admin"] = self.is_super_admin
        return d


@dataclass(frozen=True)
class Attachment:
    global_user_id: str
    server_id: str
    server_username: str
    attached_at: str
    tg_chat_id: str = ""
    seen_steps: tuple[str, ...] = ()
    last_seen_at: str | None = None
    # T-0218: per-(user × server) personal notification overrides, keyed by
    # project slug. Sits above the per-server ``tg_chat_id`` in the worker's
    # notify precedence (project -> server -> global). Empty = no overrides.
    project_tg_chat_ids: dict[str, str] = field(default_factory=dict)


class MothershipUsersStore:
    """File-backed GlobalUser + Attachment registry.

    Mutation lock is shared across both surfaces because a GlobalUser delete
    must atomically drop all its Attachments; readers stay lockless (atomic
    rename gives a torn-write-free snapshot).
    """

    _lock = threading.Lock()

    def __init__(self, root: Path) -> None:
        self.root = root
        self.users_path = root / "users.json"

    # ---- users.json read / write --------------------------------------------

    def _read(self) -> dict:
        if not self.users_path.exists():
            return {"version": SCHEMA_VERSION, "users": []}
        with self.users_path.open(encoding="utf-8") as f:
            return json.load(f)

    def list_users(self) -> list[GlobalUser]:
        # Allowlist by dataclass field names so a future on-disk addition by
        # a newer process doesn't 500 this reader on rollback.
        known = {f.name for f in fields(GlobalUser)}
        out: list[GlobalUser] = []
        for u in self._read().get("users", []):
            kept = {k: v for k, v in u.items() if k in known}
            # T-0216 dual-read: explicit global_role wins; else derive from the
            # legacy is_super_admin bool (is_super_admin is a property now, so
            # it never survives the allowlist — derive before constructing).
            kept["global_role"] = parse_global_role(
                u.get("global_role"),
                legacy_is_super_admin=bool(u.get("is_super_admin", False)),
            )
            out.append(GlobalUser(**kept))
        return out

    def _write_users(self, users: list[GlobalUser]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.users_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"version": SCHEMA_VERSION, "users": [self._user_to_row(u) for u in users]},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.rename(tmp, self.users_path)

    @staticmethod
    def _user_to_row(u: "GlobalUser") -> dict:
        """Serialize a GlobalUser for users.json. T-0216: emit global_role as
        its string value (canonical) plus a legacy is_super_admin mirror so a
        rollback to pre-T-0216 code still reads admin status correctly."""
        row = asdict(u)
        row["global_role"] = u.global_role.value
        row["is_super_admin"] = u.is_super_admin
        return row

    # ---- user lookups -------------------------------------------------------

    def get_user(self, user_id: str) -> GlobalUser | None:
        for u in self.list_users():
            if u.id == user_id:
                return u
        return None

    def user_by_username(self, username: str) -> GlobalUser | None:
        for u in self.list_users():
            if u.username == username:
                return u
        return None

    # ---- user mutations -----------------------------------------------------

    def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        display_name: str = "",
        email: str = "",
        timezone_name: str = "UTC",
        is_super_admin: bool = False,
    ) -> GlobalUser:
        """Create a GlobalUser. Raises ``ValueError`` on duplicate username."""
        entry = GlobalUser(
            id=_mint_global_user_id(),
            username=username,
            password_hash=password_hash,
            created_at=_utc_now_iso(),
            display_name=display_name,
            email=email,
            timezone=timezone_name,
            global_role=global_role_for(is_super_admin),
        )
        with self._lock:
            users = self.list_users()
            for existing in users:
                if existing.username == username:
                    raise ValueError(f"global user already exists: {username}")
            users.append(entry)
            self._write_users(users)
        return entry

    def upsert_user_by_username(
        self,
        *,
        username: str,
        password_hash: str,
        display_name: str = "",
        email: str = "",
        timezone_name: str = "UTC",
        is_super_admin: bool = False,
    ) -> tuple[GlobalUser, bool]:
        """Insert if absent, return existing if present.

        Returns ``(entry, created)`` — used by the migration script to be
        idempotent across re-runs and partial migrations. Already-existing
        rows are returned unchanged (we never overwrite an established
        password hash from a stale ServerUser row).
        """
        with self._lock:
            users = self.list_users()
            for existing in users:
                if existing.username == username:
                    return existing, False
            entry = GlobalUser(
                id=_mint_global_user_id(),
                username=username,
                password_hash=password_hash,
                created_at=_utc_now_iso(),
                display_name=display_name,
                email=email,
                timezone=timezone_name,
                global_role=global_role_for(is_super_admin),
            )
            users.append(entry)
            self._write_users(users)
            return entry, True

    # ---- attachments --------------------------------------------------------

    def _attachments_dir(self, global_user_id: str) -> Path:
        return self.root / "attachments" / global_user_id

    def _attachment_path(self, global_user_id: str, server_id: str) -> Path:
        return self._attachments_dir(global_user_id) / f"{server_id}.json"

    def get_attachment(self, global_user_id: str, server_id: str) -> Attachment | None:
        path = self._attachment_path(global_user_id, server_id)
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        known = {f.name for f in fields(Attachment)}
        kept = {k: v for k, v in data.items() if k in known}
        # Tuple field: JSON gives us a list back; the dataclass declares a
        # tuple so equality + hashing stay stable across reload.
        if "seen_steps" in kept:
            kept["seen_steps"] = tuple(str(s) for s in (kept["seen_steps"] or ()))
        return Attachment(**kept)

    def list_attachments_for_user(self, global_user_id: str) -> list[Attachment]:
        out: list[Attachment] = []
        d = self._attachments_dir(global_user_id)
        if not d.is_dir():
            return out
        for path in sorted(d.iterdir()):
            if path.suffix != ".json":
                continue
            try:
                attachment = self.get_attachment(global_user_id, path.stem)
            except (OSError, json.JSONDecodeError):
                continue
            if attachment is not None:
                out.append(attachment)
        return out

    def upsert_attachment(
        self,
        *,
        global_user_id: str,
        server_id: str,
        server_username: str,
        tg_chat_id: str = "",
        seen_steps: tuple[str, ...] = (),
        last_seen_at: str | None = None,
        project_tg_chat_ids: dict[str, str] | None = None,
    ) -> tuple[Attachment, bool]:
        """Create-or-update an Attachment. Returns ``(entry, created)``.

        Update path preserves ``attached_at`` from the existing row — that
        timestamp records the FIRST binding, not the latest write.

        ``project_tg_chat_ids=None`` PRESERVES the existing per-project override
        map (so an unrelated per-server ``tg_chat_id`` write doesn't wipe it);
        pass an explicit dict to replace it.
        """
        with self._lock:
            existing = self.get_attachment(global_user_id, server_id)
            created = existing is None
            attached_at = existing.attached_at if existing is not None else _utc_now_iso()
            if project_tg_chat_ids is None:
                project_tg_chat_ids = dict(existing.project_tg_chat_ids) if existing else {}
            entry = Attachment(
                global_user_id=global_user_id,
                server_id=server_id,
                server_username=server_username,
                attached_at=attached_at,
                tg_chat_id=tg_chat_id,
                seen_steps=tuple(seen_steps),
                last_seen_at=last_seen_at,
                project_tg_chat_ids=dict(project_tg_chat_ids),
            )
            d = self._attachments_dir(global_user_id)
            d.mkdir(parents=True, exist_ok=True)
            path = self._attachment_path(global_user_id, server_id)
            tmp = path.with_suffix(".tmp")
            payload = asdict(entry)
            # asdict() converts tuples to lists already; JSON requires that.
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.rename(tmp, path)
            return entry, created

    def touch_last_seen(self, global_user_id: str, server_id: str) -> Attachment | None:
        with self._lock:
            existing = self.get_attachment(global_user_id, server_id)
            if existing is None:
                return None
            updated = replace(existing, last_seen_at=_utc_now_iso())
            path = self._attachment_path(global_user_id, server_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(updated), indent=2), encoding="utf-8")
            os.rename(tmp, path)
            return updated
