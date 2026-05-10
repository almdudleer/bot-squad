"""API test fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_bot_squad(tmp_path: Path) -> Path:
    """Provide a complete bot-squad layout for tests."""
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "_sock").mkdir(parents=True)
    (tmp_path / "data" / "_worker").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "backlog").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "vision").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "feedback").mkdir(parents=True)
    (tmp_path / "data" / "test-project" / "sessions").mkdir(parents=True)

    (tmp_path / "config" / "projects.toml").write_text(
        '[projects.test-project]\n'
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        'repo_path = "/tmp/test-repo"\n'
        'deploy_branch = "agent_team/dev"\n'
        'master_branch = "master"\n'
        'prod_url = "https://example.com"\n'
        'staging_url = "https://staging.example.com"\n'
        'dev_url = "https://dev.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (tmp_path / "config" / "auth.toml").write_text(
        '[telegram_login]\n'
        'bot_token = "TESTBOT:TOKEN"\n'
        'allowed_ids = [12345]\n'
        'session_ttl = "7d"\n'
        'auth_age_max = 86400\n'
    )
    return tmp_path
