"""Health endpoint — surfaces worker liveness via heartbeat freshness."""
from __future__ import annotations

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
        "uptime": time.monotonic() - _STARTED_AT,
        "worker": {"alive": alive, "last_heartbeat": last_hb},
    }
