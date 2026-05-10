"""API config — loads projects.toml + auth.toml."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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

    @classmethod
    def from_toml(cls, raw: dict) -> "Project":
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


@dataclass(frozen=True)
class AuthConfig:
    allowed_ids: tuple[int, ...]
    session_ttl_seconds: int

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
        tg = raw["telegram_login"]
        return cls(
            allowed_ids=tuple(int(i) for i in tg["allowed_ids"]),
            session_ttl_seconds=cls._parse_ttl(tg["session_ttl"]),
        )
