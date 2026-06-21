"""Config loader — parses bot-squad config/projects.toml + config/secrets.toml."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import secret_crypto


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
    # T-0156: optional forum-thread id for group-chat bindings. When tg_chat is
    # a forum-enabled group, project-bound TG sends target this thread via
    # message_thread_id. None → the group's general feed (or a plain DM).
    tg_topic_id: int | None = None
    repo_master: Path | None = None  # master clone for prod deploys + hotfixes
    # T-0143: local CI/CD deploy clone — a throwaway checkout parallel to
    # dev/master, force-synced to origin/<deploy_branch> on each deploy. When
    # set, non-prod deploys run from here instead of the shared dev clone, so
    # dev-tree dirtiness stops gating deploys. None → legacy in-place behaviour.
    repo_deploy: Path | None = None
    # T-0296: the mother dir that owns the dev+master clones (for the clones
    # read-model's "workspace present ✓/✗"). Mirrors the API config field.
    repo_workspace: Path | None = None
    # T-0184: per-project opt-in to the drift-check tick (T-0149). The worker is
    # multi-project, so an unconditional sweep nags dev sessions in EVERY project
    # — a signal-tracker dev once got a bot-squad-style drift nag for an unrelated
    # ticket. Only projects that explicitly set ``drift_enforcement = true`` get
    # the tick; everyone else is left alone. Default False = safe (opt-in).
    drift_enforcement: bool = False

    def repo_for_target(self, target: str) -> Path:
        """Pick the clone the deploy recipe EXECUTES in for ``target``.

        - ``prod``: master clone (separate dir so dev work continues
          uninterrupted while a prod deploy is in flight). Falls back to
          ``repo_path`` if ``repo_master`` is not configured.
        - everything else (``staging``, ``dev``, …): the deploy clone
          (``repo_deploy``) if configured — a CI checkout synced to origin —
          else the dev clone = ``repo_path`` (legacy in-place behaviour).
        """
        if target == "prod" and self.repo_master is not None:
            return self.repo_master
        if self.repo_deploy is not None:
            return self.repo_deploy
        return self.repo_path

    def editing_repo_for_target(self, target: str) -> Path:
        """The human/agent EDITING clone that feeds ``target``.

        This is the clone whose local-only (unpushed) commits the deploy
        commit-guard checks (T-0110/T-0116): a deploy ships ``origin/<branch>``,
        so unpushed commits here would be silently OMITTED from the release.
        Independent of ``repo_deploy`` (which only ever tracks origin).

        - ``prod``: master clone if configured, else dev clone.
        - everything else: dev clone = ``repo_path``.
        """
        if target == "prod" and self.repo_master is not None:
            return self.repo_master
        return self.repo_path

    def uses_deploy_clone(self, target: str) -> bool:
        """True when ``target`` runs in a separate, origin-synced deploy clone
        (i.e. the exec clone differs from the editing clone)."""
        return self.repo_for_target(target) != self.editing_repo_for_target(target)

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
            tg_topic_id=(
                int(raw["tg_topic_id"])
                if raw.get("tg_topic_id") not in (None, "")
                else None
            ),
            repo_master=Path(raw["repo_master"]) if raw.get("repo_master") else None,
            repo_deploy=Path(raw["repo_deploy"]) if raw.get("repo_deploy") else None,
            repo_workspace=Path(raw["repo_workspace"]) if raw.get("repo_workspace") else None,
            drift_enforcement=bool(raw.get("drift_enforcement", False)),
        )


@dataclass(frozen=True)
class Config:
    config_dir: Path
    projects: dict[str, Project] = field(default_factory=dict)
    tg_bot_token: str = ""
    tg_auth_age_max: int = 86400
    # T-0171: per-server default Telegram chat id for the LOCAL (detached /
    # standalone) bot. Used as the chat fallback when a tg_notify carries
    # neither an explicit chat_id nor a project slug. Loaded from
    # system_settings.toml [tg].default_chat_id. Empty → fall back to the first
    # registered project's tg_chat (legacy behaviour).
    tg_default_chat_id: str = ""
    # Quiet hours during which non-urgent TG sends are dropped. UTC. Loaded
    # from <config_dir>/system_settings.toml; defaults match historical
    # 17→05 UTC sleep window for the Tashkent stakeholder.
    tg_quiet_hours_start_utc: int = 17
    tg_quiet_hours_end_utc: int = 5
    # T-0155 stall-watchdog: minutes an agent may stay blocked on the operator
    # (peer_send to an operator-role session, no reply) before the worker
    # auto-fires a TG ping — but only when the tmux window is not being watched.
    # 0 disables the watchdog entirely. Loaded from system_settings.toml [tg].
    tg_stall_minutes: int = 15
    # Optional remote-control URL template included in the escalation TG message
    # so the stakeholder can resume the session in the Claude app. ``{sid}`` and
    # ``{session}`` are substituted when present. Empty → fall back to a
    # ``tmux attach -t <session>`` hint.
    tg_remote_control_url: str = ""
    # T-0194: per-installation Telegram egress proxy. When set, ALL Telegram API
    # calls (tg.py send + tg_listener getUpdates/sendMessage) route through this
    # proxy — the durable, TG-only replacement for the blunt global HTTPS_PROXY
    # env on the worker systemd unit (the T-0192 lift-and-shift). socks5://,
    # http://, or https://. Loaded from system_settings.toml [tg].proxy_url.
    # Empty → direct egress (httpx trust_env still applies). socks5:// needs the
    # httpx[socks] extra (a worker dependency).
    tg_proxy_url: str = ""
    # T-0247: MAX (max.ru) DM channel — the second notification channel,
    # mirroring the TG fields above. bot_token is encrypted at rest in
    # secrets.toml [max] (same scheme as telegram.bot_token); the non-secret
    # bits live in system_settings.toml [max]. recipient_kind selects whether
    # the configured id is a group chat ("chat_id") or a direct user DM
    # ("user_id"). Empty token → max.MaxClient.send is a silent no-op.
    max_bot_token: str = ""
    max_default_chat_id: str = ""
    max_proxy_url: str = ""
    max_recipient_kind: str = "chat_id"
    # T-0386: voice-feedback transcription seam (INI-04 Phase 2). engine selects
    # the STT backend (default self-hosted faster-whisper, config-swappable);
    # model is the engine-specific model size. Loaded from system_settings.toml
    # [voice]. faster-whisper is a lazy import — only needed when voice intake is
    # actually used.
    voice_engine: str = "faster-whisper"
    voice_model: str = "small"

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
        # T-0179: secret values are encrypted at rest (enc: prefix) when a
        # BOT_SQUAD_SECRETS_KEY is configured. decrypt() transparently returns
        # legacy plaintext as-is, and raises loud on an enc: value it can't
        # decrypt (never silently reads ciphertext as the token).
        bot_token = secret_crypto.decrypt(sec.get("telegram", {}).get("bot_token", ""))
        # T-0247: MAX bot token, encrypted at rest the same way as the TG token.
        max_bot_token = secret_crypto.decrypt(sec.get("max", {}).get("bot_token", ""))

        # system_settings.toml is optional; defaults match the historical hardcoded
        # values so existing deploys behave identically until the admin writes it.
        quiet_start = 17
        quiet_end = 5
        stall_minutes = 15
        remote_control_url = ""
        default_chat_id = ""
        proxy_url = ""
        # T-0247: MAX [max] non-secret config (token lives in secrets.toml).
        max_default_chat_id = ""
        max_proxy_url = ""
        max_recipient_kind = "chat_id"
        voice_engine = "faster-whisper"
        voice_model = "small"
        sys_settings = config_dir / "system_settings.toml"
        if sys_settings.exists():
            sys_raw = tomllib.loads(sys_settings.read_text())
            tg_block = sys_raw.get("tg", {}) or {}
            quiet_start = int(tg_block.get("quiet_hours_start_utc", quiet_start))
            quiet_end = int(tg_block.get("quiet_hours_end_utc", quiet_end))
            stall_minutes = int(tg_block.get("stall_minutes", stall_minutes))
            remote_control_url = str(tg_block.get("remote_control_url", remote_control_url))
            default_chat_id = str(tg_block.get("default_chat_id", default_chat_id))
            proxy_url = str(tg_block.get("proxy_url", proxy_url))
            max_block = sys_raw.get("max", {}) or {}
            max_default_chat_id = str(max_block.get("default_chat_id", max_default_chat_id))
            max_proxy_url = str(max_block.get("proxy_url", max_proxy_url))
            max_recipient_kind = str(max_block.get("recipient_kind", max_recipient_kind))
            voice_block = sys_raw.get("voice", {}) or {}
            voice_engine = str(voice_block.get("engine", voice_engine))
            voice_model = str(voice_block.get("model", voice_model))

        return cls(
            config_dir=config_dir,
            projects=projects,
            tg_bot_token=bot_token,
            tg_auth_age_max=int(sec.get("telegram", {}).get("auth_age_max", 86400)),
            tg_quiet_hours_start_utc=quiet_start,
            tg_quiet_hours_end_utc=quiet_end,
            tg_stall_minutes=stall_minutes,
            tg_remote_control_url=remote_control_url,
            tg_default_chat_id=default_chat_id,
            tg_proxy_url=proxy_url,
            max_bot_token=max_bot_token,
            max_default_chat_id=max_default_chat_id,
            max_proxy_url=max_proxy_url,
            max_recipient_kind=max_recipient_kind,
            voice_engine=voice_engine,
            voice_model=voice_model,
        )
