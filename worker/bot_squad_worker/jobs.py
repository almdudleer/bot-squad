"""Time-driven jobs the worker runs via APScheduler.

v1 ships only the heartbeat. Spec #3 adds deploy_monitor, oauth_refresh.
"""
from __future__ import annotations

import json as _json
import logging

from bot_squad_worker.config import Config

log = logging.getLogger(__name__)


def heartbeat(cfg: Config) -> None:
    """Touch heartbeat file so the API can show worker liveness."""
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat_path.touch()


def deploy_monitor(cfg: Config) -> None:
    """Sweep ALL registered projects (serial), running each one's deploy tick.

    Retained as a manual all-projects sweep + the test entrypoint. Production
    no longer schedules THIS as a single job: T-0212 registers one
    ``deploy_monitor_one`` apscheduler job PER PROJECT so a hung build can't
    head-of-line-block the others (see scheduler.build_scheduler). Per-project
    exceptions are caught and logged; one bad project doesn't kill the sweep.
    """
    for slug in cfg.projects:
        try:
            deploy_monitor_one(cfg, slug)
        except Exception:
            log.exception("deploy_monitor: unhandled error for project %s", slug)


def deploy_monitor_one(cfg: Config, slug: str) -> None:
    """Run the deploy lifecycle for ONE project: reap stale orphans, then run
    the next queued deploy.

    Registered as its own apscheduler job per project (``max_instances=1``),
    so a wedged build in project A holds only A's instance — sibling projects'
    jobs run independently on their own scheduler threads (T-0212 per-project
    isolation). The two stages are guarded separately so a reaper error never
    skips the deploy and vice-versa.
    """
    project = cfg.projects.get(slug)
    if project is None:
        return
    try:
        _reap_project_orphans(cfg, slug, project)
    except Exception:
        log.exception("deploy_monitor: orphan reap failed for %s", slug)
    try:
        _run_project_deploy(cfg, slug, project)
    except Exception:
        log.exception("deploy_monitor: deploy run failed for %s", slug)


def _reap_project_orphans(cfg: Config, slug: str, project: object) -> None:
    """Sweep stale processing/ orphans for ``slug`` and alert the operator.

    A run stranded in processing/ (crashed worker, or a pre-T-0212
    TimeoutExpired) is moved to processed/.fail and a TARGETED operator alert
    fires — so a wedged run can't linger for days unnoticed (the 2-week-old
    watchrobot orphans the ticket cites).
    """
    from bot_squad_worker import deploy as _deploy

    reaped = _deploy.reap_orphans(cfg, slug)
    for orphan in reaped:
        hrs = (orphan.get("age_seconds") or 0) / 3600.0
        _alert_operators(
            cfg, slug, project,
            f"🧟 deploy ORPHAN reaped — {slug}/{orphan.get('target')} job "
            f"{orphan.get('queue_id')} sat in processing/ for {hrs:.1f}h "
            f"(crashed/killed run, never finished) → swept to "
            f"processed/.fail.{_deploy.RC_ORPHAN}. The queue is now unblocked.",
        )


def _run_project_deploy(cfg: Config, slug: str, project: object) -> None:
    """Pop and run one deploy for ``slug``, sending TG pings.

    The "🚚 starting" ping is gated on the per-target tree being clean —
    without that gate, the monitor pings at 60s cadence forever when a
    dirty tree blocks the queue, and the operator gets spam with no
    success/fail follow-up (root cause of the deploy_monitor spam reported
    2026-05-18). Dirty-deferred state is logged, not pinged.
    """
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.actions import _get_tg_client

    queued = _deploy.list_queued(cfg, slug)
    if not queued:
        return

    # Pause gate (T-0094) — checked before everything else. When the
    # PAUSED.json marker exists, defer silently. The TG ping at pause
    # time already told the operator; no per-tick reminder spam.
    if _deploy.is_paused(cfg, slug) is not None:
        log.info("deploy_monitor: %s deferred — PAUSED", slug)
        return

    # Peek at the oldest queued job's target so we can run the per-target
    # cleanliness check before any user-visible TG ping. (run_next() will
    # re-read the file anyway; this peek doesn't move anything.)
    try:
        peek = _json.loads(queued[0].read_text())
        target = peek.get("target")
    except Exception:
        log.exception("deploy_monitor: %s could not read queue head %s", slug, queued[0])
        return
    if not target:
        log.warning("deploy_monitor: %s queue head %s has no target", slug, queued[0])
        return

    if not _deploy.is_clean_for_target(cfg, slug, target):
        # Tree is dirty — defer silently. No ping yet; we'll pick it up
        # again next tick once a peer commits or stashes their WIP.
        log.info("deploy_monitor: %s/%s deferred — tree dirty", slug, target)
        return

    tg = _get_tg_client(cfg)
    chat_id = project.tg_chat  # type: ignore[attr-defined]
    sid = "deploy_monitor"

    # Tree is clean — ping at start and at finish. TG outages must not
    # block the deploy itself: api.telegram.org went unreachable from this
    # host once (IPv6 default with no route) and the uncaught network error
    # propagated out of deploy_monitor, leaving queued recipes wedged in
    # the queue dir indefinitely. Swallow + log; the deploy queue is the
    # source of truth, the TG ping is best-effort observability.
    #
    # urgent=True (T-0188): deploy start/finish are project-bound SYSTEM
    # notifications, not idle-DM flood — they must fire by default for any
    # project with a tg_chat set, INCLUDING during quiet hours. Without this
    # flag the quiet-hours gate (17–05 UTC ≈ the stakeholder's whole Tashkent
    # evening/night) silently dropped every deploy alert: the regression the
    # stakeholder reported as "no more alerts from @bot_squad_bot". Same
    # precedent as autoupdate_apply's apply-failure ping.
    def _tg_safe(text: str) -> None:
        try:
            tg.send(chat_id=chat_id, text=text, sid=sid, urgent=True)
        except Exception:
            log.exception("deploy_monitor: tg.send failed (non-fatal): %s", text)

    _tg_safe(f"🚚 starting deploy for {slug}/{target}")

    result = _deploy.run_next(cfg, slug)
    if result is None:
        # Race: tree went dirty between peek and run_next, or queue emptied.
        log.info("deploy_monitor: %s deferred between peek and run_next", slug)
        return

    suffix = (
        f" ({result.collapsed_count} queued requests collapsed)"
        if result.collapsed_count > 1
        else ""
    )
    if result.ok:
        _tg_safe(f"✅ deploy {slug}/{target} SUCCESS (rc={result.returncode}){suffix}")
    elif result.killed_reason:
        # A watchdog (not the recipe) killed this build — the loud, TARGETED
        # operator alert path (T-0212), not the routine project-channel ping.
        kind = (
            "no-progress watchdog (build made no log output)"
            if result.killed_reason == "no_progress"
            else "hard timeout (build ran too long)"
        )
        _alert_operators(
            cfg, slug, project,
            f"❌ deploy {slug}/{target} KILLED by {kind} (rc={result.returncode})"
            f"{suffix}. The build was terminated and the queue is now unblocked. "
            f"Log: {result.log_path}",
        )
    else:
        _tg_safe(f"❌ deploy {slug}/{target} FAILED rc={result.returncode}{suffix}")


def _alert_operators(cfg: Config, slug: str, project: object, text: str) -> None:
    """Loud, TARGETED deploy alert (T-0212) — never a broadcast.

    Two targeted channels, mirroring the telemetry alert guardrail (T-0210):
      1. urgent TG to the project's single bound chat (``tg_chat``) — one chat,
         urgent so the quiet-hours gate can't drop a wedged-build alert;
      2. a peer_send to each operator-role SID for the project.
    Both are best-effort; a TG outage or a missing operator pane never raises.
    """
    from bot_squad_worker.actions import _get_tg_client

    chat_id = getattr(project, "tg_chat", "") if project else ""
    if chat_id:
        try:
            _get_tg_client(cfg).send(
                chat_id=chat_id, text=text, sid="deploy_monitor", urgent=True
            )
        except Exception:
            log.exception("deploy_monitor: operator tg.send failed (non-fatal): %s", text)
    try:
        _peer_to_operators(cfg, slug, text)
    except Exception:
        log.exception("deploy_monitor: operator peer_send failed (non-fatal)")


def _peer_to_operators(cfg: Config, slug: str, text: str) -> None:
    """Targeted peer_send to each operator-role SID for ``slug`` (no broadcast)."""
    from bot_squad_worker import sessions as _sessions
    from bot_squad_worker import intersession as _is

    try:
        rows = _sessions.list_sessions(cfg, slug)
    except Exception:
        log.exception("deploy_monitor: list_sessions failed for %s", slug)
        return
    operator_sids = [
        r.get("sid") for r in rows
        if r.get("role") == "operator" and r.get("sid")
    ]
    for sid in dict.fromkeys(operator_sids):  # dedupe, preserve order
        try:
            _is.send(cfg, slug, "S-deploy_monitor", sid, text)
        except Exception:
            log.exception("deploy_monitor: peer_send to %s failed (non-fatal)", sid)


def tg_listener_tick(cfg: Config) -> None:
    """Poll Telegram for incoming replies and route them into sessions.

    Scheduled every 30s by APScheduler. Errors are caught and logged so
    one bad update cycle never kills the scheduler.
    """
    from bot_squad_worker import tg_listener
    try:
        tg_listener.tick(cfg)
    except Exception:
        log.exception("tg_listener_tick error")


def autoupdate_tick(cfg: Config) -> None:
    """Poll the mothership release feed and queue apply jobs when newer.

    Scheduled at ``BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS`` (default 900s)
    by APScheduler. Errors are caught and logged so one bad poll cycle
    never kills the scheduler. See ``bot_squad_worker.autoupdate`` for
    the full contract (T-0083). No-op on the mothership itself (T-0086).
    """
    from bot_squad_worker import autoupdate as _autoupdate
    try:
        _autoupdate.tick(cfg)
    except Exception:
        log.exception("autoupdate_tick error")


def autoupdate_apply_tick(cfg: Config) -> None:
    """Drain the apply queue produced by the poller (T-0084).

    Scheduled at a shorter cadence than the poll tick so a freshly-enqueued
    release starts applying within ~1 minute. The apply itself can take
    many minutes (download + build + smoke); APScheduler ``max_instances=1``
    on the registered job keeps overlapping ticks from starting a second
    apply mid-flight. No-op on the mothership itself (T-0086).
    """
    from bot_squad_worker import autoupdate_apply as _apply
    try:
        _apply.tick(cfg)
    except Exception:
        log.exception("autoupdate_apply_tick error")


def binding_gc_tick(cfg: Config) -> None:
    """T-0072/0073/0077/0142/0144: run the binding-graph reconcilers per project.

    Ordered passes per tick (gc + reconcile, then T-0151 guidance harvest):
      1. ``gc_sessions`` — flip ``status: active`` SessionMds with no live pane
         to ``status: suspended`` so the on-disk graph matches reality.
      2. ``gc_dead_bindings`` (T-0142) — refresh bindings from disk: strip
         ``task_id`` for closed/missing tasks and ``initiative`` for missing
         initiative files (fixes suspended devs showing stale task_ids).
      3. ``gc_stale_bindings`` — strip duplicate-claim primary ``task_id`` from
         losers in a dup race (preserve old value as ``last_task_id``).
      4. ``archive_dead_teammates`` (T-0142/0144) — auto-archive cleanly-exited
         post-totest devs (and verified-done live devs); makes dev zombies
         impossible without a TL lifting a finger. T-0202: also trims a live
         idle dev whose binding pass 2 just cleared, via ``last_task_id``.
      5. ``reconcile_teams`` (T-0142) — rebuild the tmux-session-keyed Team mds
         from the (now-reconciled) SessionMd registry so the team roster, TL
         slot, and archived members survive a worker reload.
      6. ``gc_tmux_sessions`` (T-0200) — reap idle, empty per-initiative/per-team
         tmux sessions (``<slug>-*`` with no live claude pane, idle past the
         grace) so they stop cluttering the host's ``tmux ls``. Runs after
         ``reconcile_teams`` so the durable Team md is already written before its
         (now-idle) tmux shell is removed.

    ``gc_sessions`` runs first so the freshly-suspended sessions inform the
    stale-binding race resolution (a live-pane claimant beats a dead one);
    ``reconcile_teams`` runs last so it sees the post-archive truth.

    Per-project / per-pass errors are caught and logged so one bad project or
    pass never kills the sweep.
    """
    from bot_squad_worker import sessions as _sessions
    from bot_squad_worker import teams as _teams
    from bot_squad_worker import close_hook as _close_hook

    passes = [
        ("gc_sessions", _sessions.gc_sessions),
        ("gc_dead_bindings", _sessions.gc_dead_bindings),
        ("gc_stale_bindings", _sessions.gc_stale_bindings),
        ("archive_dead_teammates", _sessions.archive_dead_teammates),
        ("reconcile_teams", _teams.reconcile_teams),
        # T-0200: reap idle empty per-initiative/per-team tmux sessions after the
        # Team md is rebuilt, so the durable entity survives while the empty tmux
        # shell is cleaned up.
        ("gc_tmux_sessions", _sessions.gc_tmux_sessions),
        # T-0151: harvest stakeholder guidance from freshly-suspended sessions
        # into their ticket (runs after gc_sessions has flipped them suspended).
        ("harvest_guidance", _close_hook.harvest_tick),
    ]
    for slug in cfg.projects:
        for name, fn in passes:
            try:
                fn(cfg, slug)
            except Exception:
                log.exception("binding_gc_tick: %s failed for %s", name, slug)


def telemetry_tick(cfg: Config) -> None:
    """T-0210: per-project resource-telemetry sampler.

    Sibling of binding_gc_tick (its own 60s job so a slow transcript read can
    never delay the lifecycle reconcilers). Samples each LIVE session's
    context-token window + memory footprint from the Claude transcript jsonl,
    persists a small per-session record + a project quota rollup, and fires
    crossing-only urgent alerts (context >400k/>500k, memory near cap, quota
    projected-exhaust-before-EOD, 429 throttle) to the operator + each TL.
    Per-project errors are caught and logged so one bad project never kills
    the sweep.
    """
    from bot_squad_worker import telemetry as _telemetry
    try:
        _telemetry.tick(cfg)
    except Exception:
        log.exception("telemetry_tick error")


def drift_check_tick(cfg: Config) -> None:
    """T-0149: per-project drift-enforcement pass.

    Injects a drift-check reminder into live, task-bound dev sessions that
    have gone too long without updating their ticket or whose recent activity
    is off-task (superpowers docs / premature automation). Disabled when
    ``BOT_SQUAD_DRIFT_MINUTES=0``. Per-project errors are caught and logged so
    one bad project never kills the sweep.
    """
    from bot_squad_worker import drift as _drift

    for slug in cfg.projects:
        try:
            _drift.drift_check(cfg, slug)
        except Exception:
            log.exception("drift_check_tick: unhandled error for project %s", slug)


def autonomous_tick(cfg: Config) -> None:
    """Run one orchestrator tick for every project that has autonomous mode enabled.

    Scheduled every 60 seconds by APScheduler. Per-project exceptions are
    caught and logged so one bad project doesn't kill the whole sweep.
    """
    from bot_squad_worker import autonomous as _auto

    for slug in cfg.projects:
        try:
            _auto.tick(cfg, slug)
        except Exception:
            log.exception("autonomous_tick: unhandled error for project %s", slug)


def autopilot_tick(cfg: Config) -> None:
    """T-0153: per-project autopilot watchdog pass.

    Walks every active autopilot run, auto-ends ones whose duration has elapsed
    (notifying the stakeholder), and re-pings stalled targets (no commits /
    progress notes / activity for >= the configured stall threshold). Each
    autopilot self-throttles to its own ``watchdog_minutes`` cadence, so this
    can safely fire every 60s. Per-project errors are caught and logged so one
    bad project never kills the sweep.
    """
    from bot_squad_worker import autopilot as _ap

    for slug in cfg.projects:
        try:
            _ap.tick(cfg, slug)
        except Exception:
            log.exception("autopilot_tick: unhandled error for project %s", slug)


def constant_team_tick(cfg: Config) -> None:
    """T-0154: per-project constant-team maintenance pass.

    Keeps long-lived teams (initiatives with ``constant_team: true``) staffed:
    spawns a triage session when there is pending work and the team is below
    ``team_size``. Demand-driven — an idle queue spawns nothing. Per-project
    errors are caught and logged so one bad project never kills the sweep.
    """
    from bot_squad_worker import constant_teams as _ct

    for slug in cfg.projects:
        try:
            _ct.tick(cfg, slug)
        except Exception:
            log.exception("constant_team_tick: unhandled error for project %s", slug)


def tg_stall_tick(cfg: Config) -> None:
    """T-0155: escalate agents blocked on the stakeholder to TG.

    Fires every 60s. Each marker self-throttles via its ``since`` timestamp
    (only escalates past ``cfg.tg_stall_minutes``) and a one-shot ``escalated``
    flag, so this can safely run on a tight cadence without flooding. Disabled
    when ``tg_stall_minutes`` <= 0. Errors are caught so one bad sweep never
    kills the scheduler.
    """
    from bot_squad_worker import tg_stall as _tg_stall
    try:
        _tg_stall.tick(cfg)
    except Exception:
        log.exception("tg_stall_tick error")


def oauth_refresh(cfg: Config) -> None:
    """Refresh Claude OAuth credentials. TG-ping on failure only.

    Calls refresh_oauth(cfg) from refresh_oauth.py and pings TG if it
    returns ok=False.
    """
    from bot_squad_worker.refresh_oauth import refresh_oauth as _refresh
    from bot_squad_worker.actions import _get_tg_client

    try:
        result = _refresh(cfg)
    except Exception as e:
        log.exception("oauth_refresh: unexpected error")
        result = {"ok": False, "action": "failed", "detail": str(e)}

    if not result.get("ok"):
        tg = _get_tg_client(cfg)
        detail = result.get("detail", "unknown error")
        for slug, project in cfg.projects.items():
            # urgent=True (T-0193, mirrors the T-0188 deploy fix at jobs.py:98):
            # an oauth-refresh failure is a P1 SYSTEM alert — once creds expire
            # every session breaks — so it must bypass the quiet-hours gate
            # instead of being silently dropped 22-04 UTC.
            tg.send(
                chat_id=project.tg_chat,
                text=f"❌ oauth_refresh FAILED: {detail}",
                sid="oauth_refresh",
                urgent=True,
            )
            break  # ping only the first project (single TG chat for now)
