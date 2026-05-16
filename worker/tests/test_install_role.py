"""Tests for install_role.is_mothership / warn_if_misconfigured (T-0086)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bot_squad_worker.install_role import (
    MOTHERSHIP_URL,
    is_mothership,
    warn_if_misconfigured,
)


def _write_projects(cfg_dir: Path, body: str) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "projects.toml").write_text(body)


# --- is_mothership ---------------------------------------------------------

def test_flag_true_means_mothership(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'prod_url = "https://other.example.com"\n'
        'deploy_targets = ["staging", "prod"]\n'
        'mothership = true\n',
    )
    assert is_mothership("bot-squad", config_dir=cfg) is True


def test_url_match_means_mothership_even_without_flag(tmp_path: Path) -> None:
    """Belt-and-suspenders: prod_url match implies mothership regardless of flag."""
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        f'prod_url = "{MOTHERSHIP_URL}"\n'
        'deploy_targets = ["staging"]\n',
    )
    assert is_mothership("bot-squad", config_dir=cfg) is True


def test_neither_flag_nor_url_match_means_consumer(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'prod_url = "https://attached-server.example.com"\n'
        'deploy_targets = ["staging"]\n',
    )
    assert is_mothership("bot-squad", config_dir=cfg) is False


def test_flag_false_explicit_falls_back_to_url_check(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        f'prod_url = "{MOTHERSHIP_URL}"\n'
        'deploy_targets = ["staging"]\n'
        'mothership = false\n',
    )
    # URL still matches the hardcoded mothership URL → treat as mothership
    # (this is the "accidental flag-drop on the real mothership" guard).
    assert is_mothership("bot-squad", config_dir=cfg) is True


def test_missing_slug_means_not_mothership(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.signal-tracker]\n'
        'slug = "signal-tracker"\n'
        'prod_url = "https://signal.example.com"\n'
        'deploy_targets = ["staging"]\n',
    )
    assert is_mothership("bot-squad", config_dir=cfg) is False


def test_missing_projects_toml_means_not_mothership(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    assert is_mothership("bot-squad", config_dir=cfg) is False


def test_default_config_dir_uses_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOT_SQUAD env var controls the default config dir."""
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'prod_url = "https://other.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'mothership = true\n',
    )
    monkeypatch.setenv("BOT_SQUAD", str(tmp_path))
    # No explicit config_dir — should pick up via env.
    assert is_mothership("bot-squad") is True


# --- warn_if_misconfigured -------------------------------------------------

def test_warn_mothership_without_prod_target(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'prod_url = "https://other.example.com"\n'
        'deploy_targets = ["staging"]\n'
        'mothership = true\n',
    )
    log = logging.getLogger("test-warn-no-prod")
    with caplog.at_level(logging.WARNING, logger="test-warn-no-prod"):
        warn_if_misconfigured(cfg, slug="bot-squad", log=log)
    assert any("lacks 'prod'" in r.getMessage() for r in caplog.records)


def test_warn_prod_target_without_mothership_flag(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'prod_url = "https://other.example.com"\n'
        'deploy_targets = ["staging", "prod"]\n',
    )
    log = logging.getLogger("test-warn-no-flag")
    with caplog.at_level(logging.WARNING, logger="test-warn-no-flag"):
        warn_if_misconfigured(cfg, slug="bot-squad", log=log)
    assert any("NOT marked mothership" in r.getMessage() for r in caplog.records)


def test_no_warning_when_consistent_mothership(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'prod_url = "https://other.example.com"\n'
        'deploy_targets = ["staging", "prod"]\n'
        'mothership = true\n',
    )
    log = logging.getLogger("test-no-warn-mothership")
    with caplog.at_level(logging.WARNING, logger="test-no-warn-mothership"):
        warn_if_misconfigured(cfg, slug="bot-squad", log=log)
    assert not caplog.records


def test_no_warning_when_consistent_consumer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'prod_url = "https://attached-server.example.com"\n'
        'deploy_targets = ["staging"]\n',
    )
    log = logging.getLogger("test-no-warn-consumer")
    with caplog.at_level(logging.WARNING, logger="test-no-warn-consumer"):
        warn_if_misconfigured(cfg, slug="bot-squad", log=log)
    assert not caplog.records


def test_no_warning_when_slug_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "config"
    _write_projects(
        cfg,
        '[projects.signal-tracker]\n'
        'slug = "signal-tracker"\n'
        'prod_url = "https://signal.example.com"\n'
        'deploy_targets = ["staging", "prod"]\n',
    )
    log = logging.getLogger("test-no-warn-absent")
    with caplog.at_level(logging.WARNING, logger="test-no-warn-absent"):
        warn_if_misconfigured(cfg, slug="bot-squad", log=log)
    assert not caplog.records
