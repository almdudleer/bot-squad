"""Time-driven jobs the worker runs via APScheduler.

v1 ships only the heartbeat. Spec #3 adds deploy_monitor, kick_stuck,
oauth_refresh.
"""
from __future__ import annotations

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
    """Pop and run one deploy for ``slug``, sending TG pings."""
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.actions import _get_tg_client

    queued = _deploy.list_queued(cfg, slug)
    if not queued:
        return

    tg = _get_tg_client(cfg)
    chat_id = project.tg_chat  # type: ignore[attr-defined]
    sid = "deploy_monitor"

    # Ping at queue time
    tg.send(chat_id=chat_id, text=f"🚚 starting deploy for {slug}", sid=sid)

    result = _deploy.run_next(cfg, slug)
    if result is None:
        # Tree was dirty — don't ping (not an error, just deferred)
        log.info("deploy_monitor: %s deploy deferred (dirty tree)", slug)
        return

    if result.ok:
        tg.send(
            chat_id=chat_id,
            text=f"✅ deploy {slug} SUCCESS (rc={result.returncode})",
            sid=sid,
        )
    else:
        tg.send(
            chat_id=chat_id,
            text=f"❌ deploy {slug} FAILED rc={result.returncode}",
            sid=sid,
        )


def kick_stuck(cfg: Config) -> None:
    """Daily summary: send one TG message per project that has stuck state.

    Checks: dirty tree, queued/processing deploys, failed deploys in last 24h.
    Skips projects with nothing stuck (clean tree, empty queue, no recent
    failures) to avoid noise.
    """
    import time
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.actions import _get_tg_client

    tg = _get_tg_client(cfg)
    now = time.time()
    one_day = 86400.0

    for slug, project in cfg.projects.items():
        try:
            _kick_stuck_project(cfg, slug, project, tg, now, one_day)
        except Exception:
            log.exception("kick_stuck: unhandled error for project %s", slug)


def _kick_stuck_project(
    cfg: Config,
    slug: str,
    project: object,
    tg: object,
    now: float,
    one_day: float,
) -> None:
    """Build + send the stuck-state summary for one project if any issues found."""
    import subprocess
    from bot_squad_worker import deploy as _deploy

    chat_id = project.tg_chat  # type: ignore[attr-defined]
    repo_path = project.repo_path  # type: ignore[attr-defined]
    issues: list[str] = []

    # 1. Dirty tree?
    from bot_squad_worker.deploy import _is_clean
    if not _is_clean(repo_path):
        issues.append("⚠️ dirty working tree")

    # 2. Queued deploys?
    queued = _deploy.list_queued(cfg, slug)
    if queued:
        issues.append(f"📋 {len(queued)} deploy(s) queued")

    # 3. Processing (stuck) deploys?
    processing_dir = cfg.data_dir / slug / "_jobs" / "deploy" / "processing"
    if processing_dir.exists():
        processing = list(processing_dir.glob("*.json"))
        if processing:
            issues.append(f"⏳ {len(processing)} deploy(s) stuck in processing")

    # 4. Failed deploys in last 24h?
    processed_dir = cfg.data_dir / slug / "_jobs" / "deploy" / "processed"
    recent_fails = 0
    if processed_dir.exists():
        for f in processed_dir.glob("*.fail.*"):
            try:
                mtime = f.stat().st_mtime
                if now - mtime < one_day:
                    recent_fails += 1
            except OSError:
                pass
    if recent_fails:
        issues.append(f"💥 {recent_fails} failed deploy(s) in last 24h")

    if not issues:
        return  # Nothing stuck, no spam

    msg = f"[kick_stuck] {slug} needs attention:\n" + "\n".join(f"  {i}" for i in issues)
    tg.send(chat_id=chat_id, text=msg, sid="kick_stuck")  # type: ignore[attr-defined]


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
