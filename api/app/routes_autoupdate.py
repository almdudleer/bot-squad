"""Consumer-side autoupdate status + pause + check-now endpoints (T-0089).

Surfaces the local autoupdate state (written by T-0083/T-0084 under
``data/_worker/``) to the bot-squad UI so the operator can see what's
installed, pause the next apply for ops emergencies, and force a poll
out-of-cadence after toggling pause.

Mounted on every install but gated behind ``MOTHERSHIP != "1"``: the
mothership produces releases and never consumes them (T-0086), so the
three endpoints 404 there. The UI inverts the same gate
(``VITE_MOTHERSHIP``) and doesn't render the pill on mothership builds,
but the server-side 404 is the source of truth — a misconfigured client
gets a clean failure rather than fake state.

Endpoints:

- ``GET  /api/autoupdate/status``  → composite read from autoupdate.json,
  autoupdate_alert.json, and the autoupdate_paused.flag presence test.
- ``POST /api/autoupdate/pause``  ``{paused: bool}`` → flag toggle
  (creates or removes ``data/_worker/autoupdate_paused.flag``).
- ``POST /api/autoupdate/check_now`` → fires the poller tick out-of-cadence
  via a new worker action.

All three require an authenticated session (operator UI surface). Pause
is intentionally not gated to admin-only in v0 per spec.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app.routes_auth import require_auth
from app.worker_client import WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/autoupdate",
    tags=["autoupdate"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Mothership gate — the same posture as the rest of the consumer-side surface.
# ---------------------------------------------------------------------------


def _refuse_on_mothership() -> None:
    """Raise 404 when this install is the mothership.

    Single mothership-seam check shared by all three endpoints so a future
    refactor (e.g. switching from env to install_role.py) only touches one
    spot.
    """
    if os.environ.get("MOTHERSHIP", "0") == "1":
        raise HTTPException(status_code=404, detail="autoupdate not available on mothership")


# ---------------------------------------------------------------------------
# State paths — MUST mirror what bot_squad_worker.autoupdate writes (T-0083/4).
# ---------------------------------------------------------------------------

_STATE_FILE = "autoupdate.json"
_ALERT_FILE = "autoupdate_alert.json"
_PAUSED_FLAG = "autoupdate_paused.flag"
_QUEUE_DIR = "autoupdate_queue"
_DEFAULT_INTERVAL_SECONDS = 900  # mirrors autoupdate.DEFAULT_INTERVAL_SECONDS


def _worker_dir(request: Request) -> Path:
    cfg = request.app.state.api_config
    return cfg.data_dir / "_worker"


def _read_json(path: Path) -> Optional[dict]:
    """Best-effort JSON read. Returns None on missing / unreadable / non-dict."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("routes_autoupdate: could not parse %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _interval_seconds() -> int:
    """Resolve the poll cadence the worker is using.

    The worker reads the same env var (``BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS``);
    the API uses it only to compute ``next_check_at`` for the pill — a stale
    env between API and worker just means a slightly-wrong countdown until
    the next tick replaces it.
    """
    raw = os.environ.get("BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS")
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_INTERVAL_SECONDS
    except ValueError:
        return _DEFAULT_INTERVAL_SECONDS


def _next_check_iso(last_check_at: Optional[str]) -> Optional[str]:
    """Compute ``last_check_at + interval`` as an ISO-8601 string.

    Returns None when ``last_check_at`` is missing or unparseable — the UI
    treats that as "scheduling pending" rather than guessing.
    """
    if not last_check_at:
        return None
    try:
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(last_check_at)
    except (ValueError, TypeError):
        return None
    return (dt + timedelta(seconds=_interval_seconds())).isoformat()


def _pending_apply_version(worker_dir: Path) -> Optional[str]:
    """Peek at the oldest queued apply job to surface its version for the UI.

    Returns ``None`` when the queue is empty / missing / unreadable. The
    presence of a queued entry is what drives the UI's "applying v..." pill
    state — T-0084's drain loop doesn't write an in-progress flag, so the
    queue itself is our best proxy for "an apply is on the way".
    """
    qdir = worker_dir / _QUEUE_DIR
    if not qdir.is_dir():
        return None
    try:
        files = sorted(
            (p for p in qdir.iterdir() if p.is_file() and p.suffix == ".json"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return None
    if not files:
        return None
    try:
        entry = json.loads(files[0].read_text())
    except (OSError, json.JSONDecodeError):
        return None
    v = entry.get("version") if isinstance(entry, dict) else None
    return v if isinstance(v, str) and v else None


def _mothership_url() -> Optional[str]:
    """Return the mothership URL the worker is polling, for the UI's
    "release notes" link. Mirrors the worker's lookup order so the pill
    links to the same upstream the consumer is actually consuming from."""
    url = (
        os.environ.get("BOT_SQUAD_MOTHERSHIP_URL")
        or os.environ.get("BOTSQUAD_MOTHERSHIP_URL")
    )
    if not url:
        return None
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# GET /autoupdate/status
# ---------------------------------------------------------------------------


@router.get("/status")
def get_status(request: Request) -> dict:
    """Composite read of the consumer autoupdate state for the UI pill.

    Body shape (spec)::

        {
          "installed_version": "v..." | null,
          "last_check_at": "<iso8601>" | null,
          "next_check_at": "<iso8601>" | null,
          "last_apply_at": "<iso8601>" | null,
          "last_apply_outcome": "success" | "failed:<step>" | "never",
          "current_git_sha": "<sha40>" | null,
          "paused": bool,
          "alert": <autoupdate_alert.json shape> | null,
          "mothership_url": "<base>" | null,
          "poll_interval_seconds": int
        }

    The ``alert`` block (when present) carries the T-0085 banner payload —
    the UI uses it both to colour the pill red and to deep-link the
    operator banner. Missing files are absorbed quietly so a fresh install
    (no autoupdate.json yet) renders as "checked never" rather than 5xx.
    """
    _refuse_on_mothership()

    wdir = _worker_dir(request)
    state = _read_json(wdir / _STATE_FILE) or {}
    alert = _read_json(wdir / _ALERT_FILE)
    paused = (wdir / _PAUSED_FLAG).exists()
    last_check_at = state.get("last_check_at")

    return {
        "installed_version": state.get("installed_version"),
        "last_check_at": last_check_at,
        "next_check_at": _next_check_iso(last_check_at),
        "last_apply_at": state.get("last_apply_at"),
        "last_apply_outcome": state.get("last_apply_outcome") or "never",
        "current_git_sha": state.get("current_git_sha"),
        "paused": paused,
        "alert": alert,
        # pending_apply_version drives the "applying v..." pill state. Non-null
        # iff the apply queue has at least one entry (T-0084 drains it on its
        # own tick; we never delete from here). When paused, the queue can
        # legitimately hold entries that aren't being drained — the UI shows
        # a paused-with-pending state rather than a misleading "applying" spinner.
        "pending_apply_version": _pending_apply_version(wdir),
        "mothership_url": _mothership_url(),
        "poll_interval_seconds": _interval_seconds(),
    }


# ---------------------------------------------------------------------------
# POST /autoupdate/pause
# ---------------------------------------------------------------------------


@router.post("/pause")
def post_pause(request: Request, payload: dict) -> dict:
    """Toggle the operator pause flag.

    Body: ``{"paused": true | false}``. Creates the flag file when true,
    removes it when false. Idempotent on both sides — re-pausing a paused
    install or re-unpausing an active install both succeed with the
    current state in the response.
    """
    _refuse_on_mothership()

    if not isinstance(payload, dict) or "paused" not in payload:
        raise HTTPException(status_code=400, detail="missing 'paused' field")
    paused = payload.get("paused")
    if not isinstance(paused, bool):
        raise HTTPException(status_code=400, detail="'paused' must be boolean")

    flag = _worker_dir(request) / _PAUSED_FLAG
    if paused:
        flag.parent.mkdir(parents=True, exist_ok=True)
        # Stamp a tiny payload so a human looking at the file knows what
        # it means; the worker's check is presence-only so contents don't
        # matter — but a tagged file is friendlier in `ls -la`.
        from datetime import datetime, timezone
        flag.write_text(
            json.dumps({"paused_at": datetime.now(timezone.utc).isoformat()})
        )
    else:
        try:
            flag.unlink()
        except FileNotFoundError:
            pass  # already unpaused — idempotent

    return {"ok": True, "paused": flag.exists()}


# ---------------------------------------------------------------------------
# POST /autoupdate/check_now
# ---------------------------------------------------------------------------


@router.post("/check_now")
async def post_check_now(request: Request) -> dict:
    """Trigger the autoupdate poller tick out-of-cadence.

    Returns whatever the worker reports — typically
    ``{ok: true, scheduled: true, next_run: "<iso8601>"}``. The poll runs
    on the worker's scheduler thread; the API caller can refresh
    ``/status`` a moment later to see ``last_check_at`` advance.

    Useful after toggling pause (operator wants to see what's available
    before deciding) or after an apply failure (sanity-check the upstream).
    """
    _refuse_on_mothership()

    client = request.app.state.worker_router.coordinator()
    try:
        # Generous timeout: inline-fallback path (no scheduler) runs the
        # full tick which can take ~30s in the worst case (transient fetch
        # retry + telemetry POST). The happy path (scheduler-reschedule)
        # is sub-second.
        return await client.call_action("autoupdate_check_now", {}, timeout=45.0)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e))
