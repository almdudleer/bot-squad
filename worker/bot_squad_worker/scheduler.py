"""APScheduler wiring."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from bot_squad_worker.config import Config
from bot_squad_worker.jobs import heartbeat


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
    return sched
