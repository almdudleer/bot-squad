"""Config loader — parses bot-squad config/projects.toml + config/secrets.toml."""
from __future__ import annotations

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
class Config:
    config_dir: Path
    projects: dict[str, Project] = field(default_factory=dict)
    tg_bot_token: str = ""
    tg_auth_age_max: int = 86400

    @property
    def data_dir(self) -> Path:
        return self.config_dir.parent / "data"

    @property
    def sock_path(self) -> Path:
        return self.data_dir / "_sock" / "worker.sock"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "_worker" / "heartbeat"

    def project_data_dir(self, slug: str) -> Path:
        return self.data_dir / slug

    @classmethod
    def load(cls, config_dir: Path) -> "Config":
        projects_toml = config_dir / "projects.toml"
        if not projects_toml.exists():
            raise FileNotFoundError(f"projects.toml not found: {projects_toml}")
        secrets_toml = config_dir / "secrets.toml"
        if not secrets_toml.exists():
            raise FileNotFoundError(f"secrets.toml not found: {secrets_toml}")
        raw = tomllib.loads(projects_toml.read_text())
        projects = {
            slug: Project.from_toml(p)
            for slug, p in raw.get("projects", {}).items()
        }
        sec = tomllib.loads(secrets_toml.read_text())
        return cls(
            config_dir=config_dir,
            projects=projects,
            tg_bot_token=sec.get("telegram", {}).get("bot_token", ""),
            tg_auth_age_max=int(sec.get("telegram", {}).get("auth_age_max", 86400)),
        )
