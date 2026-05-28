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
    """Iterate registered projects; run the next queued deploy for each.

    Per-project exceptions are caught and logged; one bad project doesn't
    kill the whole sweep.
    """
    from bot_squad_worker import deploy as _deploy

    for slug, project in cfg.projects.items():
        try:
            _run_project_deploy(cfg, slug, project)
        except Exception:
            log.exception("deploy_monitor: unhandled error for project %s", slug)


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
    def _tg_safe(text: str) -> None:
        try:
            tg.send(chat_id=chat_id, text=text, sid=sid)
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
    else:
        _tg_safe(f"❌ deploy {slug}/{target} FAILED rc={result.returncode}{suffix}")


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
    """T-0072/0073/0077: run the binding-graph reconcilers for every project.

    Two passes per tick:
      1. ``gc_sessions`` — flip ``status: active`` SessionMds with no live pane
         to ``status: suspended`` so the on-disk graph matches reality.
      2. ``gc_stale_bindings`` — strip duplicate-claim primary ``task_id`` from
         losers in a dup race (preserve old value as ``last_task_id``).

    ``gc_sessions`` runs first so the freshly-suspended sessions inform the
    stale-binding race resolution (a live-pane claimant beats a dead one).

    Per-project errors are caught and logged so one bad project never kills
    the sweep.
    """
    from bot_squad_worker import sessions as _sessions

    for slug in cfg.projects:
        # T-0134: gc_sessions periodic tick disabled — false-positives on
        # legacy SessionMds without pane_id (no-pane_id treated as zombie
        # instead of unverifiable). Manual invocation still works for ops
        # use. Re-enable here when T-0134 lands.
        try:
            _sessions.gc_stale_bindings(cfg, slug)
        except Exception:
            log.exception("binding_gc_tick: gc_stale_bindings failed for %s", slug)


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
            tg.send(
                chat_id=project.tg_chat,
                text=f"❌ oauth_refresh FAILED: {detail}",
                sid="oauth_refresh",
            )
            break  # ping only the first project (single TG chat for now)
