"""Tests for worker jobs (heartbeat, etc.)."""
from __future__ import annotations

from pathlib import Path

from bot_squad_worker.config import Config
from bot_squad_worker.jobs import heartbeat


def test_heartbeat_creates_file(tmp_config_dir: Path) -> None:
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    assert not cfg.heartbeat_path.exists()
    heartbeat(cfg)
    assert cfg.heartbeat_path.exists()


def test_heartbeat_updates_mtime(tmp_config_dir: Path) -> None:
    cfg = Config.load(tmp_config_dir)
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat_path.touch()
    import os, time
    old = os.path.getmtime(cfg.heartbeat_path)
    time.sleep(0.05)
    heartbeat(cfg)
    new = os.path.getmtime(cfg.heartbeat_path)
    assert new > old
