"""Config loader — parses bot-squad config/projects.toml + config/secrets.toml."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Project:
    slug: str
    display_name: str
    repo_path: Path           # alias for repo_dev — the dev clone
    deploy_branch: str
    master_branch: str
    prod_url: str
    staging_url: str
    dev_url: str
    deploy_targets: tuple[str, ...]
    tg_chat: str
    repo_master: Path | None = None  # master clone for prod deploys + hotfixes

    def repo_for_target(self, target: str) -> Path:
        """Pick the right clone for a deploy target.

        - ``prod``: master clone (separate dir so dev work continues
          uninterrupted while a prod deploy is in flight). Falls back to
          ``repo_path`` if ``repo_master`` is not configured.
        - everything else (``staging``, ``dev``, …): dev clone = ``repo_path``.
        """
        if target == "prod" and self.repo_master is not None:
            return self.repo_master
        return self.repo_path

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
            repo_master=Path(raw["repo_master"]) if raw.get("repo_master") else None,
        )


@dataclass(frozen=True)
class Config:
    config_dir: Path
    projects: dict[str, Project] = field(default_factory=dict)
    tg_bot_token: str = ""
    tg_auth_age_max: int = 86400
    # Quiet hours during which non-urgent TG sends are dropped. UTC. Loaded
    # from <config_dir>/system_settings.toml; defaults match historical
    # 17→05 UTC sleep window for the Tashkent stakeholder.
    tg_quiet_hours_start_utc: int = 17
    tg_quiet_hours_end_utc: int = 5

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

        # system_settings.toml is optional; defaults match the historical hardcoded
        # values so existing deploys behave identically until the admin writes it.
        quiet_start = 17
        quiet_end = 5
        sys_settings = config_dir / "system_settings.toml"
        if sys_settings.exists():
            sys_raw = tomllib.loads(sys_settings.read_text())
            tg_block = sys_raw.get("tg", {}) or {}
            quiet_start = int(tg_block.get("quiet_hours_start_utc", quiet_start))
            quiet_end = int(tg_block.get("quiet_hours_end_utc", quiet_end))

        return cls(
            config_dir=config_dir,
            projects=projects,
            tg_bot_token=sec.get("telegram", {}).get("bot_token", ""),
            tg_auth_age_max=int(sec.get("telegram", {}).get("auth_age_max", 86400)),
            tg_quiet_hours_start_utc=quiet_start,
            tg_quiet_hours_end_utc=quiet_end,
        )
