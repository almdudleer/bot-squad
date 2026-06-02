"""APScheduler wiring."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

from bot_squad_worker.config import Config
from bot_squad_worker import autoupdate as _autoupdate
from bot_squad_worker.jobs import (
    autopilot_tick,
    autoupdate_apply_tick,
    autoupdate_tick,
    binding_gc_tick,
    constant_team_tick,
    deploy_monitor,
    drift_check_tick,
    heartbeat,
    oauth_refresh,
    tg_listener_tick,
    tg_stall_tick,
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
    # binding_gc_tick: T-0072/0073/0077 — once per minute, reconcile the
    # on-disk SessionMd graph with live tmux state (zombie repair) and
    # strip duplicate primary task_id claimants (stale-binding repair).
    # max_instances=1 + coalesce keeps overlapping ticks from racing on the
    # same mds; the tick is idempotent so a missed run is no problem.
    sched.add_job(
        binding_gc_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="binding_gc",
        max_instances=1,
        coalesce=True,
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

    # drift_check_tick: T-0149 — enforce ticket focus by injecting a
    # drift-check reminder into live dev sessions that have gone too long
    # without touching their ticket, or whose recent activity is off-task.
    # Gentler cadence than binding_gc (it types into live panes); gated by
    # BOT_SQUAD_DRIFT_MINUTES (0 = disabled). max_instances=1 so an inject
    # that runs long never overlaps the next pass.
    sched.add_job(
        drift_check_tick,
        "interval",
        seconds=120,
        args=[cfg],
        id="drift_check",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # autopilot_tick: T-0153 — watchdog for prompt-driven, time-boxed autopilot
    # runs. Fires every 60s; each autopilot self-throttles to its own
    # watchdog_minutes cadence and only re-pings once its no-progress window
    # reaches the configured stall threshold. max_instances=1 so a long
    # delivery never overlaps the next pass.
    sched.add_job(
        autopilot_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="autopilot",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # tg_stall_tick: T-0155 — escalate agents blocked on the stakeholder to TG
    # when the block has aged past tg_stall_minutes AND the tmux window isn't
    # being watched. One-shot per marker; self-throttling, so 60s is safe.
    sched.add_job(
        tg_stall_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="tg_stall",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # constant_team_tick: T-0154 — keep long-lived teams (prod-support, user-
    # feedback, …) staffed. Demand-driven: only spawns when there's pending
    # work and the team is below team_size, so an idle queue is a no-op.
    # max_instances=1 + the per-team spawn cooldown keep overlapping ticks
    # from double-spawning. Kill switch: BOT_SQUAD_CONSTANT_TEAMS_DISABLED=1.
    sched.add_job(
        constant_team_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="constant_team",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    return sched
