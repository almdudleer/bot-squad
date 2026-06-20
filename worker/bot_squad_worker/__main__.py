"""Worker entrypoint.

Run with:
    python -m bot_squad_worker --config /home/www/bot-squad/config

In production, started by systemd via bot-squad-worker.service.

Phase 2 multi-user: BOT_SQUAD_MODE selects role.
  - "coordinator" (or unset): scheduler + all actions, socket worker.sock.
  - "user-worker": tmux ops only, socket user-<USER>.sock; no scheduler.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import uvicorn

from bot_squad_worker.actions import set_config, set_mode, set_scheduler
from bot_squad_worker.config import Config
from bot_squad_worker.install_role import is_mothership, warn_if_misconfigured
from bot_squad_worker.intersession import set_shutdown_event
from bot_squad_worker.scheduler import build_scheduler
from bot_squad_worker.server import build_app


def _user_sock_path(cfg: Config, linux_user: str) -> Path:
    return cfg.data_dir / "_sock" / f"user-{linux_user}.sock"


# Backstop < systemd TimeoutStopSec (10s): even if a long-poll somehow doesn't
# drain, uvicorn force-closes in-flight connections by this deadline so the
# worker still exits before SIGKILL.
_GRACEFUL_SHUTDOWN_TIMEOUT = 8


def _install_graceful_shutdown(server, shutdown_event, sched):
    """Wrap uvicorn ``Server.handle_exit`` so SIGTERM/SIGINT ALSO flips the
    T-0119 shutdown event + stops the scheduler BEFORE uvicorn starts draining.

    Why this exists: ``uvicorn.run()`` installs uvicorn's own signal handlers,
    overriding the worker's ``_shutdown`` — so the shutdown event was never set,
    the ``peer_inbox_wait`` long-polls ran their full timeout, uvicorn's graceful
    shutdown blocked on them, and systemd SIGKILLed the worker at
    TimeoutStopSec=10s. Flipping the event here lets the long-polls return within
    ~1s so uvicorn (and the worker) exit cleanly inside the window. The event is
    set FIRST, so it survives even if uvicorn's original handler raises.
    """
    _orig = server.handle_exit

    def handle_exit(sig, frame):
        logging.getLogger("bot-squad-worker").info(
            "shutdown signal received — draining long-polls + stopping scheduler")
        shutdown_event.set()
        if sched is not None:
            try:
                sched.shutdown(wait=False)
            except Exception:
                logging.getLogger("bot-squad-worker").exception("scheduler shutdown error")
        _orig(sig, frame)

    server.handle_exit = handle_exit
    return server


def main() -> int:
    parser = argparse.ArgumentParser(prog="bot-squad-worker")
    parser.add_argument(
        "--config",
        default="/home/www/bot-squad/config",
        help="path to bot-squad config dir (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "info"),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    log = logging.getLogger("bot-squad-worker")

    mode = os.environ.get("BOT_SQUAD_MODE", "").strip() or "coordinator"
    if mode not in ("coordinator", "user-worker"):
        log.error("invalid BOT_SQUAD_MODE: %r (want coordinator|user-worker)", mode)
        return 2
    set_mode(mode)
    log.info("mode=%s", mode)

    cfg = Config.load(Path(args.config))
    set_config(cfg)
    log.info("loaded config: %d project(s)", len(cfg.projects))

    # T-0086 integration check: warn early if mothership flag and prod
    # deploy_targets disagree for bot-squad itself.
    log.info(
        "install_role: is_mothership(bot-squad)=%s",
        is_mothership(slug="bot-squad", config_dir=cfg.config_dir),
    )
    warn_if_misconfigured(cfg.config_dir, slug="bot-squad", log=log)

    if mode == "coordinator":
        sock_path = cfg.sock_path
    else:
        linux_user = getpass.getuser()
        sock_path = _user_sock_path(cfg, linux_user)
        log.info("user-worker for linux_user=%s", linux_user)

    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    sched = None
    if mode == "coordinator":
        sched = build_scheduler(cfg)
        sched.start()
        set_scheduler(sched)
        log.info("scheduler started")

    app = build_app()

    # T-0119: shutdown event flipped by SIGTERM/SIGINT so in-flight
    # peer_inbox_wait long-polls return early (reason=shutdown) instead of
    # being SIGKILL'd by systemd 90s later.
    shutdown_event = threading.Event()
    set_shutdown_event(shutdown_event)

    def _shutdown(*_: object) -> None:
        log.info("shutdown signal received")
        shutdown_event.set()
        if sched is not None:
            sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if mode == "coordinator" and sched is not None:
        def _fix_sock_perms() -> None:
            """Run shortly after uvicorn binds; tighten socket perms to 0660 + group www."""
            import grp, os
            if not sock_path.exists():
                return
            try:
                gid = grp.getgrnam("www").gr_gid
                os.chown(sock_path, -1, gid)
            except (KeyError, PermissionError, OSError):
                log.warning("could not chgrp socket to www; falling back to current group")
            sock_path.chmod(0o660)
            log.info("socket perms tightened to 0660 (group www)")

        from datetime import datetime, timedelta, timezone
        sched.add_job(
            _fix_sock_perms,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
            id="fix_sock_perms",
        )
    elif mode == "user-worker":
        # Best-effort: set group ownership on the per-user socket so the API
        # container (which runs as group www) can connect.
        import grp, time as _time
        def _delayed_chgrp() -> None:
            _time.sleep(1.0)
            try:
                if sock_path.exists():
                    gid = grp.getgrnam("www").gr_gid
                    os.chown(sock_path, -1, gid)
                    sock_path.chmod(0o660)
                    log.info("user socket perms tightened to 0660 (group www)")
            except (KeyError, PermissionError, OSError) as e:
                log.warning("could not chgrp user socket: %s", e)
        threading.Thread(target=_delayed_chgrp, daemon=True).start()

    log.info("uvicorn binding to %s", sock_path)
    # Build the Server explicitly (instead of uvicorn.run) so we can wrap its
    # signal handler — uvicorn installs its own, overriding the _shutdown above,
    # which is exactly why the worker used to be SIGKILLed on restart (T-0284).
    config = uvicorn.Config(
        app,
        uds=str(sock_path),
        log_level=args.log_level,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT,
    )
    server = uvicorn.Server(config)
    _install_graceful_shutdown(server, shutdown_event, sched)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
