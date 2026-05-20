"""APScheduler wiring."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

from bot_squad_worker.config import Config
from bot_squad_worker import autoupdate as _autoupdate
from bot_squad_worker.jobs import (
    autoupdate_apply_tick,
    autoupdate_tick,
    deploy_monitor,
    heartbeat,
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
    # autonomous_tick: DISABLED 2026-05-12 — autonomous work is frozen pending
    # the new operating model. The autonomous module + actions remain on disk
    # but no background tick fires. Re-enable here when the model is ready.

    # autoupdate_tick: poll mothership release feed (T-0083). No-op on the
    # mothership itself (self-exclusion via T-0086). Cadence is configurable
    # via BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS (default 900s = 15min).
    sched.add_job(
        autoupdate_tick,
        "interval",
        seconds=_autoupdate.interval_seconds(),
        args=[cfg],
        id="autoupdate",
        replace_existing=True,
    )
    # autoupdate_apply_tick: drain the apply queue (T-0084). Runs at a
    # shorter cadence than the poll so a freshly-enqueued release starts
    # applying promptly. max_instances=1 prevents overlapping applies (one
    # apply can take many minutes; the next tick must not pile on).
    sched.add_job(
        autoupdate_apply_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="autoupdate_apply",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    return sched
