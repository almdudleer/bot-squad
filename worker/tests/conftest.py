"""Worker test fixtures."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Quiet-hours dropping is time-dependent; tests should always exercise the
# send path regardless of wall-clock.
os.environ.setdefault("BOT_SQUAD_DISABLE_QUIET_HOURS", "1")


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Provide a writable bot-squad data dir for a single test."""
    (tmp_path / "_sock").mkdir()
    (tmp_path / "_worker").mkdir()
    return tmp_path


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Provide a config dir with a minimal projects.toml and secrets.toml."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.toml").write_text(
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
    (cfg / "secrets.toml").write_text(
        '[telegram]\n'
        'bot_token = "TESTBOT:TOKEN"\n'
        'auth_age_max = 86400\n'
    )
    return cfg
