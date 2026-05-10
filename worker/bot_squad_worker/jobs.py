"""Time-driven jobs the worker runs via APScheduler.

v1 ships only the heartbeat. Spec #3 adds deploy_monitor, kick_stuck,
morning_health, etc.
"""
from __future__ import annotations

from bot_squad_worker.config import Config


def heartbeat(cfg: Config) -> None:
    """Touch heartbeat file so the API can show worker liveness."""
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat_path.touch()
