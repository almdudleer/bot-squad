"""Worker entrypoint.

Run with:
    python -m bot_squad_worker --config /home/www/bot-squad/config

In production, started by systemd via bot-squad-worker.service.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from bot_squad_worker.actions import set_config, set_scheduler
from bot_squad_worker.config import Config
from bot_squad_worker.scheduler import build_scheduler
from bot_squad_worker.server import build_app


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

    cfg = Config.load(Path(args.config))
    set_config(cfg)
    log.info("loaded config: %d project(s)", len(cfg.projects))

    cfg.sock_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg.sock_path.exists():
        cfg.sock_path.unlink()

    sched = build_scheduler(cfg)
    sched.start()
    set_scheduler(sched)
    log.info("scheduler started")

    app = build_app()

    def _shutdown(*_: object) -> None:
        log.info("shutdown signal received")
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    def _fix_sock_perms() -> None:
        """Run shortly after uvicorn binds; tighten socket perms to 0660 + group www."""
        import grp, os
        if not cfg.sock_path.exists():
            return
        try:
            gid = grp.getgrnam("www").gr_gid
            os.chown(cfg.sock_path, -1, gid)
        except (KeyError, PermissionError, OSError):
            log.warning("could not chgrp socket to www; falling back to current group")
        cfg.sock_path.chmod(0o660)
        log.info("socket perms tightened to 0660 (group www)")

    # One-shot job: fires once, ~1s after start, by which time uvicorn has bound.
    from datetime import datetime, timedelta, timezone
    sched.add_job(
        _fix_sock_perms,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
        id="fix_sock_perms",
    )

    log.info("uvicorn binding to %s", cfg.sock_path)
    uvicorn.run(app, uds=str(cfg.sock_path), log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
