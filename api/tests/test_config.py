from pathlib import Path

import pytest

from app.config import ApiConfig, AuthConfig


def test_api_config_loads_projects(tmp_bot_squad: Path) -> None:
    cfg = ApiConfig.load(tmp_bot_squad / "config")
    assert "test-project" in cfg.projects


def test_auth_config_loads(tmp_bot_squad: Path) -> None:
    auth = AuthConfig.load(tmp_bot_squad / "config")
    assert "testuser" in auth.users
    assert auth.session_ttl_seconds == 7 * 24 * 3600


def test_session_ttl_parses_human_durations() -> None:
    assert AuthConfig._parse_ttl("7d") == 7 * 24 * 3600
    assert AuthConfig._parse_ttl("12h") == 12 * 3600
    assert AuthConfig._parse_ttl("30m") == 30 * 60
    with pytest.raises(ValueError):
        AuthConfig._parse_ttl("garbage")
