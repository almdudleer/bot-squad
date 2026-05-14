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


def test_user_meta_defaults_seen_steps_empty(tmp_bot_squad: Path) -> None:
    auth = AuthConfig.load(tmp_bot_squad / "config")
    assert auth.meta_for("testuser").seen_steps == ()


def test_user_meta_reads_seen_steps(tmp_bot_squad: Path) -> None:
    (tmp_bot_squad / "config" / "auth.toml").write_text(
        '[users]\n'
        'testuser = "$2b$12$brMg3j40OitJrhlJAmnzlu/U09ybQSGcrfWx.HriIFALc59M.jP1W"\n'
        '[user_meta.testuser]\n'
        'linux_user = "almdudleer"\n'
        'is_admin = true\n'
        'seen_steps = ["srv.intro", "srv.9_1.help_spotlight"]\n'
        '[session]\nttl = "7d"\n'
    )
    auth = AuthConfig.load(tmp_bot_squad / "config")
    meta = auth.meta_for("testuser")
    assert meta.seen_steps == ("srv.intro", "srv.9_1.help_spotlight")
