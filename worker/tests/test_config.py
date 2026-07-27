"""Tests for worker.config."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_squad_worker.config import Config, Project


def test_load_finds_signal_tracker(tmp_config_dir: Path) -> None:
    cfg = Config.load(tmp_config_dir)
    assert "test-project" in cfg.projects
    proj = cfg.projects["test-project"]
    assert isinstance(proj, Project)
    assert proj.slug == "test-project"
    assert proj.repo_path == Path("/tmp/test-repo")
    assert "staging" in proj.deploy_targets


def test_topic_abbrev_default_none(tmp_config_dir: Path) -> None:
    cfg = Config.load(tmp_config_dir)
    assert cfg.projects["test-project"].topic_abbrev is None


def test_topic_abbrev_from_toml(tmp_path: Path) -> None:
    """T-0680: an explicit topic_abbrev overrides the slug in topic names."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.watchrobot]\n'
        'slug = "watchrobot"\n'
        'display_name = "Watchrobot"\n'
        'repo_path = "/tmp/test-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
        'topic_abbrev = "WR"\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = "TESTBOT:TOKEN"\n')
    cfg = Config.load(cfg_dir)
    assert cfg.projects["watchrobot"].topic_abbrev == "WR"


def test_load_missing_projects_toml_raises(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "config")


def test_data_dir_resolves_relative_to_config(tmp_config_dir: Path) -> None:
    # Convention: data/ is sibling of config/.
    cfg = Config.load(tmp_config_dir)
    assert cfg.data_dir == tmp_config_dir.parent / "data"
    assert cfg.sock_path == tmp_config_dir.parent / "data" / "_sock" / "worker.sock"


def test_secrets_loads(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_bot_token == "TESTBOT:TOKEN"


def test_secrets_decrypts_encrypted_bot_token(tmp_config_dir: Path, monkeypatch) -> None:
    # T-0179: a key-encrypted bot_token at rest is transparently decrypted on load.
    from cryptography.fernet import Fernet

    from bot_squad_worker import secret_crypto

    monkeypatch.setenv("BOT_SQUAD_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    enc = secret_crypto.encrypt("REALBOT:SECRET")
    assert enc.startswith("enc:")  # sanity: actually encrypted
    (tmp_config_dir / "secrets.toml").write_text(
        f'[telegram]\nbot_token = "{enc}"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_bot_token == "REALBOT:SECRET"


def test_secrets_legacy_plaintext_read_even_with_key(tmp_config_dir: Path, monkeypatch) -> None:
    # T-0179 migration: a not-yet-migrated plaintext token stays readable.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOT_SQUAD_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "LEGACY:PLAINTEXT"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_bot_token == "LEGACY:PLAINTEXT"


def test_secrets_enc_token_without_key_raises_loud(tmp_config_dir: Path, monkeypatch) -> None:
    # T-0179 landmine: an enc: token with no key must fail loud, never be read
    # as ciphertext-as-if-plaintext.
    from cryptography.fernet import Fernet

    from bot_squad_worker import secret_crypto

    monkeypatch.setenv("BOT_SQUAD_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    enc = secret_crypto.encrypt("REALBOT:SECRET")
    monkeypatch.delenv("BOT_SQUAD_SECRETS_KEY", raising=False)
    (tmp_config_dir / "secrets.toml").write_text(
        f'[telegram]\nbot_token = "{enc}"\n'
    )
    with pytest.raises(secret_crypto.SecretCryptoError):
        Config.load(tmp_config_dir)


def test_stall_settings_default(tmp_config_dir: Path) -> None:
    # T-0155: defaults when system_settings.toml is absent.
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_stall_minutes == 15
    assert cfg.tg_remote_control_url == ""


def test_default_chat_id_default_empty(tmp_config_dir: Path) -> None:
    # T-0171: absent system_settings.toml → no per-server default chat.
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_default_chat_id == ""


def test_default_chat_id_from_system_settings(tmp_config_dir: Path) -> None:
    # T-0171: admin sets the per-server (detached) default chat via [tg].
    (tmp_config_dir / "system_settings.toml").write_text(
        '[tg]\ndefault_chat_id = "404580642"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_default_chat_id == "404580642"


def test_tg_proxy_url_default_empty(tmp_config_dir: Path) -> None:
    # T-0194: absent system_settings.toml → no TG egress proxy (direct).
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_proxy_url == ""


def test_tg_proxy_url_from_system_settings(tmp_config_dir: Path) -> None:
    # T-0194: admin sets a per-installation TG egress proxy via [tg].proxy_url.
    (tmp_config_dir / "system_settings.toml").write_text(
        '[tg]\nproxy_url = "http://153.80.195.83:8888"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_proxy_url == "http://153.80.195.83:8888"


def test_voice_defaults(tmp_config_dir: Path) -> None:
    # T-0386: absent [voice] → self-hosted faster-whisper/small defaults.
    cfg = Config.load(tmp_config_dir)
    assert cfg.voice_engine == "faster-whisper"
    assert cfg.voice_model == "small"
    # Flag-off-safe: voice intake is DISABLED by default (operator directive) —
    # enabled at the 1-time stakeholder TG setup, never silently on at deploy.
    assert cfg.voice_enabled is False


def test_voice_from_system_settings(tmp_config_dir: Path) -> None:
    # T-0386: admin swaps the STT engine/model via [voice] + flips the kill-switch.
    (tmp_config_dir / "system_settings.toml").write_text(
        '[voice]\nengine = "whisper-api"\nmodel = "large-v3"\nenabled = true\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.voice_engine == "whisper-api"
    assert cfg.voice_model == "large-v3"
    assert cfg.voice_enabled is True


def test_voice_prompt_defaults_inert(tmp_config_dir: Path) -> None:
    # T-0747 DoD 3: absent config → no glossary, no VAD. Deploying the key must
    # change nothing until an operator writes one.
    cfg = Config.load(tmp_config_dir)
    assert cfg.voice_initial_prompt == ""
    assert cfg.voice_vad_filter is False


def test_voice_prompt_inert_when_voice_block_has_no_prompt(tmp_config_dir: Path) -> None:
    # T-0747: a [voice] block that predates this key (the LIVE install's shape)
    # still yields the inert default rather than tripping on the missing key.
    (tmp_config_dir / "system_settings.toml").write_text(
        '[voice]\nenabled = true\nengine = "faster-whisper"\nmodel = "small"\n'
        'max_duration_sec = 0\ntranscribe_timeout_sec = 900\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.voice_initial_prompt == ""
    assert cfg.voice_vad_filter is False
    assert cfg.voice_model == "small"


def test_voice_initial_prompt_from_system_settings(tmp_config_dir: Path) -> None:
    # T-0747: the glossary is an operator-editable [voice] key, read like model/engine.
    (tmp_config_dir / "system_settings.toml").write_text(
        '[voice]\ninitial_prompt = "bot-squad, Claude, sudo, tmux"\nvad_filter = true\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.voice_initial_prompt == "bot-squad, Claude, sudo, tmux"
    assert cfg.voice_vad_filter is True


def test_stall_settings_from_system_settings(tmp_config_dir: Path) -> None:
    # T-0155: admin overrides via [tg] in system_settings.toml.
    (tmp_config_dir / "system_settings.toml").write_text(
        "[tg]\n"
        "stall_minutes = 20\n"
        'remote_control_url = "https://claude.ai/code?session={sid}"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_stall_minutes == 20
    assert cfg.tg_remote_control_url == "https://claude.ai/code?session={sid}"


def test_recycle_defaults(tmp_config_dir: Path) -> None:
    # T-0563/T-0566/T-0613: absent system_settings.toml → the shared default
    # allowlist (recycle_gate.DEFAULT_RECYCLE_PROJECTS — bot-squad +
    # watchrobot), 20k-token compact threshold.
    from bot_squad_worker.recycle_gate import DEFAULT_RECYCLE_PROJECTS

    cfg = Config.load(tmp_config_dir)
    assert cfg.recycle_projects == DEFAULT_RECYCLE_PROJECTS
    assert cfg.recycle_projects == ("bot-squad", "watchrobot")
    assert cfg.recycle_compact_min_context_tokens == 20000


def test_recycle_from_system_settings(tmp_config_dir: Path) -> None:
    # T-0563: admin extends the allowlist + tunes the compact threshold via
    # [recycle] in system_settings.toml.
    (tmp_config_dir / "system_settings.toml").write_text(
        '[recycle]\nprojects = ["bot-squad", "watchrobot"]\n'
        'compact_min_context_tokens = 15000\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.recycle_projects == ("bot-squad", "watchrobot")
    assert cfg.recycle_compact_min_context_tokens == 15000


def test_tasks_dedupe_threshold_default(tmp_config_dir: Path) -> None:
    # T-0577: absent system_settings.toml -> conservative default (0.9).
    cfg = Config.load(tmp_config_dir)
    assert cfg.tasks_dedupe_threshold == 0.9


def test_tasks_dedupe_threshold_from_system_settings(tmp_config_dir: Path) -> None:
    # T-0577: admin tunes the task_new dedupe-vs-create gate via [tasks].
    (tmp_config_dir / "system_settings.toml").write_text(
        '[tasks]\ndedupe_threshold = 0.75\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tasks_dedupe_threshold == 0.75


def test_secrets_missing_raises(tmp_path: Path) -> None:
    # Build a config dir that has projects.toml but NOT secrets.toml.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        'repo_path = "/tmp/test-repo"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    with pytest.raises(FileNotFoundError, match="secrets.toml"):
        Config.load(cfg_dir)
