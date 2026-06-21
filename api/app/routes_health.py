"""Health endpoint — surfaces worker liveness via heartbeat freshness."""
from __future__ import annotations

import os
import time
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

from fastapi import APIRouter, Depends, Request

router = APIRouter()


def _pkg_version() -> str:
    try:
        return version("bot-squad-api")
    except PackageNotFoundError:
        return "0.0.0"


_STARTED_AT = time.monotonic()


@router.get("/health")
def health(request: Request) -> dict:
    heartbeat: Path = request.app.state.heartbeat_path
    alive = False
    last_hb = None
    if heartbeat.exists():
        last_hb = heartbeat.stat().st_mtime
        # Worker writes every 60s; >5min stale = dead.
        alive = (time.time() - last_hb) < 300
    return {
        "ok": True,
        "version": _pkg_version(),
        # T-0379: the git sha baked into this image at build time. Lets the
        # deploy recipe / monitor (and a human curl) assert that the RUNNING
        # container is the commit that was deployed — closing the stale-image
        # gap where a deploy 'succeeded' but shipped an older HEAD.
        "git_sha": os.environ.get("BOT_SQUAD_GIT_SHA", "unknown"),
        "uptime": time.monotonic() - _STARTED_AT,
        "worker": {"alive": alive, "last_heartbeat": last_hb},
    }
