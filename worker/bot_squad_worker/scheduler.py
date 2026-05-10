"""APScheduler wiring."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from bot_squad_worker.config import Config
from bot_squad_worker.jobs import deploy_monitor, heartbeat, kick_stuck, oauth_refresh


def build_scheduler(cfg: Config) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")

    sched.add_job(
        heartbeat,
        "interval",
        seconds=60,
        args=[cfg],
        id="heartbeat",
        replace_existing=True,
    )
    sched.add_job(
        deploy_monitor,
        "interval",
        seconds=60,
        args=[cfg],
        id="deploy_monitor",
        replace_existing=True,
    )
    sched.add_job(
        kick_stuck,
        "cron",
        hour=11,
        minute=59,
        args=[cfg],
        id="kick_stuck",
        replace_existing=True,
    )
    # oauth_refresh: v1 placeholder — checks claude binary reachable.
    # Full token-rotation port from cctv-backend deferred to a later spec.
    sched.add_job(
        oauth_refresh,
        "interval",
        hours=6,
        args=[cfg],
        id="oauth_refresh",
        replace_existing=True,
    )

    return sched
