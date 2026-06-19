"""API config — loads projects.toml + auth.toml."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from app.roles import ServerRole, parse_server_role


@dataclass(frozen=True)
class Project:
    slug: str
    display_name: str
    repo_path: Path
    deploy_branch: str
    master_branch: str
    prod_url: str
    staging_url: str
    dev_url: str
    deploy_targets: tuple[str, ...]
    tg_chat: str
    # T-0156: optional forum-thread id for group-chat bindings (paired with
    # tg_chat). None when unset / DM / general feed.
    tg_topic_id: int | None = None
    # T-0051: per-project layout — repo_master is the prod-side clone,
    # repo_workspace is the mother dir that owns both clones (or none of
    # them, in Mode 3 paths-as-they-are). Optional for back-compat: the
    # minimal projects.toml shipped by T-0021 only has repo_path.
    repo_master: Path | None = None
    repo_workspace: Path | None = None

    @classmethod
    def from_toml(cls, raw: dict) -> "Project":
        rm = raw.get("repo_master")
        rw = raw.get("repo_workspace")
        return cls(
            slug=raw["slug"],
            display_name=raw["display_name"],
            repo_path=Path(raw["repo_path"]),
            deploy_branch=raw["deploy_branch"],
            master_branch=raw["master_branch"],
            prod_url=raw["prod_url"],
            staging_url=raw["staging_url"],
            dev_url=raw["dev_url"],
            deploy_targets=tuple(raw["deploy_targets"]),
            tg_chat=str(raw["tg_chat"]),
            tg_topic_id=(
                int(raw["tg_topic_id"])
                if raw.get("tg_topic_id") not in (None, "")
                else None
            ),
            repo_master=Path(rm) if rm else None,
            repo_workspace=Path(rw) if rw else None,
        )


@dataclass(frozen=True)
class ApiConfig:
    config_dir: Path
    projects: dict[str, Project] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return self.config_dir.parent / "data"

    @property
    def sock_path(self) -> Path:
        return self.data_dir / "_sock" / "worker.sock"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "_worker" / "heartbeat"

    def project(self, slug: str) -> Project | None:
        return self.projects.get(slug)

    def project_data_dir(self, slug: str) -> Path:
        return self.data_dir / slug

    @classmethod
    def load(cls, config_dir: Path) -> "ApiConfig":
        raw = tomllib.loads((config_dir / "projects.toml").read_text())
        projects = {
            slug: Project.from_toml(p)
            for slug, p in raw.get("projects", {}).items()
        }
        return cls(config_dir=config_dir, projects=projects)


# Sentinel written by POST /api/me/onboarding/skip. Treated by the frontend
# as "user has seen every step", including ones that don't exist yet — so new
# spotlight beats (T-0014..T-0022) light up dark for users who already skipped
# without an API churn.
ONBOARDING_SKIP_ALL = "__skip_all__"


@dataclass(frozen=True)
class UserMeta:
    """Per-user metadata loaded from auth.toml [user_meta.<username>]."""
    linux_user: str
    # T-0216 Phase A: explicit server-scope role replaces the overloaded
    # `is_admin` bool. `is_admin` survives as a derived compat property so
    # every existing reader (gates, serializer, /auth/me) is untouched. The
    # legacy bool is still read off disk via dual-read in AuthConfig.load.
    server_role: ServerRole = ServerRole.SERVER_MEMBER
    seen_steps: tuple[str, ...] = ()
    # Telegram chat id bound to this user (per T-0019). Empty means unbound.
    # Disambiguated from the per-project `tg_chat` with the `_id` suffix.
    tg_chat_id: str = ""
    # T-0066: GlobalUser uuid this ServerUser is bound to via the
    # worker-attachment flow. Empty/None means legacy local-only user
    # (login still works against the local password_hash). When set, the
    # mothership owns the canonical credentials and per-server state
    # (tg_chat_id, seen_steps) is mirrored into an Attachment record.
    attached_to_global_user: str = ""
    # T-0218: per-project personal notification overrides, keyed by project
    # slug — the un-migrated counterpart to Attachment.project_tg_chat_ids.
    # Sits above the global tg_chat_id in the notify precedence. Empty = none.
    project_tg_chat_ids: dict[str, str] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        """Derived server-admin flag — back-compat for readers predating the
        T-0216 ServerRole field. True iff this user is a server admin."""
        return self.server_role is ServerRole.SERVER_ADMIN


@dataclass(frozen=True)
class AuthConfig:
    users: dict[str, str]                 # username -> bcrypt hash
    user_meta: dict[str, UserMeta]        # username -> metadata
    session_ttl_seconds: int

    def meta_for(self, username: str) -> UserMeta:
        """Return metadata for username; defaults if not configured."""
        m = self.user_meta.get(username)
        if m is not None:
            return m
        # Default: linux_user mirrors the UI username, no admin.
        return UserMeta(linux_user=username, server_role=ServerRole.SERVER_MEMBER)

    @staticmethod
    def _parse_ttl(s: str) -> int:
        m = re.match(r"^(\d+)([smhd])$", s)
        if not m:
            raise ValueError(f"bad TTL: {s!r}")
        n = int(m.group(1))
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return n * unit

    @classmethod
    def load(cls, config_dir: Path) -> "AuthConfig":
        raw = tomllib.loads((config_dir / "auth.toml").read_text())
        users = dict(raw.get("users", {}))
        meta_raw = raw.get("user_meta", {}) or {}
        user_meta: dict[str, UserMeta] = {}
        for name, m in meta_raw.items():
            raw_steps = m.get("seen_steps", [])
            if not isinstance(raw_steps, list):
                raw_steps = []
            raw_proj = m.get("project_tg_chat_ids", {})
            proj_map = (
                {str(k): str(v) for k, v in raw_proj.items()}
                if isinstance(raw_proj, dict)
                else {}
            )
            # T-0216 dual-read: explicit server_role wins; else derive from the
            # legacy is_admin bool so un-migrated rows keep working.
            server_role = parse_server_role(
                m.get("server_role"),
                legacy_is_admin=bool(m.get("is_admin", False)),
            )
            user_meta[name] = UserMeta(
                linux_user=str(m.get("linux_user", name)),
                server_role=server_role,
                seen_steps=tuple(str(s) for s in raw_steps),
                tg_chat_id=str(m.get("tg_chat_id", "")),
                attached_to_global_user=str(m.get("attached_to_global_user", "")),
                project_tg_chat_ids=proj_map,
            )
        ttl_str = raw.get("session", {}).get("ttl", "7d")
        return cls(
            users=users,
            user_meta=user_meta,
            session_ttl_seconds=cls._parse_ttl(ttl_str),
        )
