"""APScheduler wiring."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from bot_squad_worker.config import Config
from bot_squad_worker import autoupdate as _autoupdate
from bot_squad_worker.operator_redrive import operator_tick
from bot_squad_worker.uc_redrive import uc_redrive_tick
from bot_squad_worker.tg_direct_reply import tick as tg_answer_owed_tick
from bot_squad_worker.routines import monitor_tick, routine_tick
from bot_squad_worker.jobs import (
    autopilot_tick,
    autoupdate_apply_tick,
    autoupdate_tick,
    backoff_tick,
    binding_gc_tick,
    park_tick,
    recovery_tick,
    constant_team_tick,
    task_lifecycle_tick,
    deploy_monitor_one,
    drift_check_tick,
    graceful_exit_tick,
    heartbeat,
    idle_timeout_tick,
    input_flush_tick,
    oauth_refresh,
    outbound_drain_tick,
    outbound_liveness_tick,
    stall_sweep_tick,
    telemetry_tick,
    tg_listener_tick,
    tg_stall_tick,
    voice_audio_gc_tick,
    worker_restart_catchup_tick,
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
    # T-0212: a wedged deploy now blocks its own thread for the whole build
    # (up to the 30-min hard timeout) instead of the old single serial sweep.
    # With one deploy job per project (below) several can run at once, so widen
    # the default ThreadPoolExecutor (10) to keep long deploys from starving the
    # quick lifecycle ticks (heartbeat, binding_gc, telemetry, …).
    sched = BackgroundScheduler(
        timezone="UTC",
        executors={"default": ThreadPoolExecutor(max_workers=30)},
    )

    sched.add_job(
        heartbeat,
        "interval",
        seconds=60,
        args=[cfg],
        id="heartbeat",
        replace_existing=True,
        # T-0744: fire the FIRST heartbeat NOW, not 60s in (apscheduler's default
        # for an interval trigger is start + interval). The API learns a worker is
        # up only from this file, so a 60s-late first write meant every restart
        # looked unconverged for a full extra minute — 62 of the 73 seconds
        # measured on the T-0743 deploy. It also gates the in-flight marker's
        # clear, which now rides this job. Idempotent (an atomic overwrite of one
        # small file), so an extra run at boot costs nothing.
        next_run_time=datetime.now(timezone.utc),
    )
    # deploy_monitor (T-0212): ONE job PER PROJECT, not a single serial sweep.
    # Previously a lone deploy_monitor job (apscheduler default max_instances=1)
    # iterated every project serially AND blocked on each recipe subprocess for
    # up to the 30-min timeout — so a hung build in one project head-of-line-
    # blocked every other project's deploys and every subsequent tick logged
    # "skipped: maximum number of running instances reached (1)" (the live
    # watchrobot jam that blocked T-0211). Per-project jobs (each
    # max_instances=1 + coalesce) isolate a wedged project: its job holds only
    # its own instance; sibling projects run on their own scheduler threads.
    # New projects are picked up on the next worker restart (config is loaded
    # once at startup, same as before).
    for _slug in cfg.projects:
        sched.add_job(
            deploy_monitor_one,
            "interval",
            seconds=60,
            args=[cfg, _slug],
            id=f"deploy_monitor:{_slug}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    # worker_restart_catchup (T-0717): fire a worker restart that T-0305's
    # rate-limit cap DEFERRED, once the window elapses and no deploy is in
    # flight. Without this the deferral was a silent DROP — a worker-code deploy
    # landing inside the 5-min window left the worker running the previous
    # commit indefinitely while the deploy reported success.
    sched.add_job(
        worker_restart_catchup_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="worker_restart_catchup",
        max_instances=1,
        coalesce=True,
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
    # telemetry_tick: T-0210 — resource telemetry sampler. Sibling of
    # binding_gc (its own job) so a slow transcript read can't delay the
    # lifecycle reconcilers. max_instances=1 + coalesce keeps overlapping
    # samples from racing on the per-session record files; the sampler is
    # idempotent (offset-based incremental read) so a missed run is harmless.
    sched.add_job(
        telemetry_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="telemetry",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # idle_timeout_tick: T-0466 / M1-F1.3 — ~1h cache-window recycle of stale
    # waiting sessions (record-and-exit via the universal-compact handoff) +
    # postpone / auto-postpone protocol. Sibling of telemetry (its own 60s job so
    # a slow transcript read can't delay it); the 60s cadence drives the in-flight
    # handoff finalize promptly while the recycle window itself is ~1h. No-op under
    # BOT_SQUAD_IDLE_TIMEOUT=0. max_instances=1 + coalesce; idempotent.
    sched.add_job(
        idle_timeout_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="idle_timeout",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # graceful_exit_tick: T-0465 / M1-F1.2 — the uniform lifecycle's work-done →
    # graceful-exit path. Suspends (NO relaunch) an active session whose
    # assignment is DONE — a task-bound role whose task reached totest/closed, or
    # an operator whose backlog is empty — once it has gone quiet (post-done
    # grace) and its pane is idle/composer-ready. Sibling of idle_timeout (its own
    # 60s job): the two cover the two "never block indefinitely" outcomes
    # (done→exit / waiting→recycle). No-op under BOT_SQUAD_GRACEFUL_EXIT=0.
    # max_instances=1 + coalesce; idempotent (a suspended session is skipped next
    # pass).
    sched.add_job(
        graceful_exit_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="graceful_exit",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # input_flush_tick: T-0469 / M1-F1.6 — deferred-delivery pass for the input
    # multiplexer. Re-attempts delivery of per-sid input queues that were
    # deferred because the composer was busy (user mid-typing / mid-generation)
    # at write time, so a batch lands once the composer frees up without needing
    # a fresh write. No-op under BOT_SQUAD_INPUT_MUX=0. max_instances=1 +
    # coalesce; idempotent (still-busy queue re-defers, empty queue no-ops).
    sched.add_job(
        input_flush_tick,
        "interval",
        seconds=15,
        args=[cfg],
        id="input_flush",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # voice_audio_gc_tick: T-0433 P3 — reap triaged/aged voice-note audio blobs so
    # feedback/_audio can't grow unbounded. Low-frequency (every 6h); idempotent.
    sched.add_job(
        voice_audio_gc_tick,
        "interval",
        hours=6,
        args=[cfg],
        id="voice_audio_gc",
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

    # stall_sweep_tick: T-0696 — capture every live pane and auto-answer a
    # known stuck-interactive-TUI prompt (e.g. the Fable-5 usage-credits
    # gate) that would otherwise sit blocked on stdin forever, invisible to
    # drift-check/idle-timeout. Self-throttling per-marker (retry cooldown +
    # capped attempts before escalating), so 60s is safe. No-op under
    # BOT_SQUAD_STALL_SWEEP=0.
    sched.add_job(
        stall_sweep_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="stall_sweep",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # backoff_tick: WS-4 S2 (T-0249) — run-survival parallelism governor. 30s
    # (faster than the 60s telemetry sampler) so we react within ~half a sample
    # to Claude rate-limit / 5h-usage-limit pressure. No-op under
    # BOT_SQUAD_BACKOFF=0. Self-contained AIMD step; max_instances=1.
    sched.add_job(
        backoff_tick,
        "interval",
        seconds=30,
        args=[cfg],
        id="backoff",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # recovery_tick: WS-4 S4 (T-0251) — auto stall-recovery for dead/exited dev
    # sessions whose task still needs work. Default OFF (BOT_SQUAD_RECOVERY=1 to
    # opt in); inert no-op until enabled. 60s is plenty (a dead pane is not
    # urgent). max_instances=1; the respawn-or-park step never raises.
    sched.add_job(
        recovery_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="recovery",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # park_tick: WS-4 S5 (T-0252) — 5h-usage-limit park+retry. Default OFF
    # (BOT_SQUAD_PARK_RETRY=1); inert until enabled. 60s; the marker debounce
    # spans samples so the cadence is part of the safety margin. max_instances=1.
    sched.add_job(
        park_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="park",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # operator_tick: T-0474 / M2-F2.1 — operator re-drive cadence. Respawns the
    # operator (with its standing 'clear the backlog' task) whenever a project's
    # backlog has pending work AND the user has not paused AND no operator is
    # currently live — the continuity mechanism that keeps the operator
    # continuously scheduled WITHOUT a persistent session (clarification-01).
    # Continue-vs-respawn rides dispatch.live_operator_sids (the T-0472 one-
    # operator seam); a paused project or a spawn deferred under capacity/quota
    # backpressure ("stalls out of time") re-drives nothing. Kill switch:
    # BOT_SQUAD_OPERATOR_REDRIVE=0. max_instances=1 + coalesce keeps overlapping
    # ticks from racing the spawn path; idempotent so a missed run is harmless.
    sched.add_job(
        operator_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="operator_redrive",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # uc_redrive_tick: T-0622 — re-drive a user-conversation attendant whose
    # reply turn died (e.g. a 429 storm) before it answered, so a stakeholder
    # message never sits unanswered. Gated on the SAME pressure signal the
    # backoff governor uses (never redrive into a live storm) plus the T-0104
    # idle activity enum (never interrupt an in-flight reply). T-0794: the ping
    # cadence is the stakeholder's — ~5 min, ~15 min, then every ~30 for as long
    # as the message hangs — so the 60s interval here is the RESOLUTION of that
    # schedule, not its rate. Kill switch: BOT_SQUAD_UC_REDRIVE=0.
    # max_instances=1 + coalesce keeps overlapping ticks from racing the nudge.
    sched.add_job(
        uc_redrive_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="uc_redrive",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # tg_answer_owed tick: T-0770 — the stakeholder wrote into a topic bound
    # DIRECTLY to one session; did that session actually answer back into the
    # topic? uc_redrive above structurally cannot see this path (a direct-mode
    # message is recorded as an `fyi` from `system:direct-reply`, not as an
    # unanswered `author: "user"` record — and must stay that way). The verdict
    # comes from the outbound spool, and a spool that cannot answer yields BLIND
    # rather than an accusation. Kill switch: BOT_SQUAD_TG_ANSWER_OWED=0.
    sched.add_job(
        tg_answer_owed_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="tg_answer_owed",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # routine_tick: T-0464 / M1-F1.1 — fire DUE Routines (declared rule +
    # schedule trigger) → spawn one session per due tick to do the routine's work
    # per its instruction. Built on this existing tick, NOT a new daemon (grounded
    # in "simplest condition is schedule" + reuse of scheduler.py). 60s cadence is
    # the resolution of the schedule trigger; each tick advances next_run_at past
    # now so a routine fires exactly once per due tick. max_instances=1 + coalesce
    # keep overlapping ticks from racing the spawn path; idempotent (a missed run
    # just fires on the next tick). Kill switch: BOT_SQUAD_ROUTINES=0.
    sched.add_job(
        routine_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="routines",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # monitor_tick: T-0603 / D-0048 — the hybrid-trigger fast sweep. Code-only
    # probes watch declared metrics ("даже каждые пять секунд"); AI attaches
    # ONLY on a persist-confirmed threshold breach, so the 5s cadence costs
    # zero tokens while healthy. One fast tick scanning due monitors, NOT
    # per-monitor apscheduler jobs (dynamic add/remove would need a registry↔
    # scheduler reconciliation — a whole new failure class); the registry list
    # is dir-mtime-cached so 5s doesn't re-read every md. max_instances=1 +
    # coalesce plus a per-routine single-flight guard keep a slow probe from
    # piling up. Kill switch: BOT_SQUAD_MONITORS=0.
    sched.add_job(
        monitor_tick,
        "interval",
        seconds=5,
        args=[cfg],
        id="monitors",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # task_lifecycle_tick: T-0589 — stakeholder-task lifecycle notifications
    # into the TG conversation. 60s frontmatter sweep + sidecar diff; batches a
    # sweep's transitions into one message and defers through quiet hours (the
    # unstamped sidecar retries), so the cadence only bounds latency, not spam.
    # max_instances=1 + coalesce; idempotent (unchanged statuses never re-fire).
    # Kill switch: BOT_SQUAD_TASK_NOTIFY=0.
    sched.add_job(
        task_lifecycle_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="task_lifecycle",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # outbound_drain_tick: T-0755 — mirror delivered outbound messages from the
    # transports' local spool into the conversation store, so the thread reads
    # as a real interleaved transcript instead of an inbox. 30s: the failure
    # this prevents is a human reading a RECENT window and mistaking drain lag
    # for silence. max_instances=1 + coalesce; idempotent (line cursor), and the
    # tick swallows its own errors so a store/API hiccup can't kill the
    # scheduler thread.
    sched.add_job(
        outbound_drain_tick,
        "interval",
        seconds=30,
        args=[cfg],
        id="outbound_drain",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # outbound_liveness_tick: T-0759 — watch the outbound log for SILENT DECAY.
    # The tick above records; this one asks whether it still is, by comparing
    # independent witnesses that a send happened (tg_reply_map, the debounce
    # markers) against sends the log accounts for. 300s: what it watches decays
    # over weeks, and it announces only a state that has held, so a tighter
    # cadence would buy nothing but flap. max_instances=1 + coalesce; the tick
    # swallows its own errors and the check reports its own blindness rather
    # than reporting health it cannot see.
    sched.add_job(
        outbound_liveness_tick,
        "interval",
        seconds=300,
        args=[cfg],
        id="outbound_liveness",
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
