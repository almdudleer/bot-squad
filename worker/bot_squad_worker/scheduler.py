"""APScheduler wiring."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

from bot_squad_worker.config import Config
from bot_squad_worker.jobs import (
    autonomous_tick,
    deploy_monitor,
    heartbeat,
    kick_stuck,
    oauth_refresh,
    tg_listener_tick,
)

if TYPE_CHECKING:
    pass

# Module-level started_at timestamp, set once at import time.
_STARTED_AT: datetime = datetime.now(timezone.utc)


def state_for_api(sched: BackgroundScheduler, cfg: Config) -> dict:
    """Return a serialisable dict describing the current scheduler state."""
    jobs = []
    for j in sched.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        })

    hb_age = None
    hb = cfg.heartbeat_path
    if hb.exists():
        try:
            hb_age = time.time() - hb.stat().st_mtime
        except OSError:
            pass

    return {
        "jobs": jobs,
        "worker_started_at": _STARTED_AT.isoformat(),
        "last_heartbeat_age_seconds": hb_age,
    }


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
    # tg_listener: poll Telegram for replies and route them into sessions (spec #7).
    sched.add_job(
        tg_listener_tick,
        "interval",
        seconds=30,
        args=[cfg],
        id="tg_listener",
        replace_existing=True,
    )
    # autonomous_tick: run one orchestrator step per project (spec #8).
    sched.add_job(
        autonomous_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="autonomous_tick",
        replace_existing=True,
    )

    return sched
