"""Deploy run history endpoints.

GET /api/projects/{slug}/runs
    Return a list of run summaries, newest-first, capped at 50 by default.

GET /api/projects/{slug}/runs/{run_id}/log
    Return the run log as text/plain. Capped at 2 MB by default.
    Pass ?full=1 to return the entire file.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.routes_auth import require_auth

log = logging.getLogger(__name__)

_LOG_CAP_BYTES = 2 * 1024 * 1024  # 2 MB

router = APIRouter(
    prefix="/projects/{slug}/runs",
    tags=["runs"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Directory helpers (mirrors worker/bot_squad_worker/deploy.py)
# ---------------------------------------------------------------------------

def _queue_dir(data_dir: Path, slug: str) -> Path:
    return data_dir / slug / "_jobs" / "deploy" / "queue"


def _processing_dir(data_dir: Path, slug: str) -> Path:
    return data_dir / slug / "_jobs" / "deploy" / "processing"


def _processed_dir(data_dir: Path, slug: str) -> Path:
    return data_dir / slug / "_jobs" / "deploy" / "processed"


def _runs_dir(data_dir: Path, slug: str) -> Path:
    return data_dir / slug / "_jobs" / "deploy" / "runs"


# ---------------------------------------------------------------------------
# Helpers to build run items
# ---------------------------------------------------------------------------

def _parse_payload(path: Path) -> dict[str, Any] | None:
    """Read and parse a JSON payload file. Returns None on failure."""
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning("runs: could not parse %s", path)
        return None


def _make_run_item(
    payload: dict[str, Any],
    status: str,
    rc: int | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> dict[str, Any]:
    """Build a run summary dict from a payload + derived fields."""
    queue_id = payload.get("queue_id", "")

    queued_at_raw = payload.get("queued_at")
    queued_at: str | None = None
    if queued_at_raw is not None:
        try:
            import datetime
            queued_at = datetime.datetime.fromtimestamp(
                float(queued_at_raw), tz=datetime.timezone.utc
            ).isoformat()
        except Exception:
            queued_at = str(queued_at_raw)

    started_at_str: str | None = None
    if started_at is not None:
        try:
            import datetime
            started_at_str = datetime.datetime.fromtimestamp(
                started_at, tz=datetime.timezone.utc
            ).isoformat()
        except Exception:
            pass

    ended_at_str: str | None = None
    if ended_at is not None:
        try:
            import datetime
            ended_at_str = datetime.datetime.fromtimestamp(
                ended_at, tz=datetime.timezone.utc
            ).isoformat()
        except Exception:
            pass

    return {
        "id": queue_id,
        "target": payload.get("target", ""),
        "status": status,
        "rc": rc,
        "reason": payload.get("reason", ""),
        "requested_by": payload.get("requested_by", ""),
        "queued_at": queued_at,
        "started_at": started_at_str,
        "ended_at": ended_at_str,
    }


def collect_runs(data_dir: Path, slug: str) -> list[dict[str, Any]]:
    """Collect all runs across queue, processing, and processed directories.

    Returns a list of run summary dicts, newest-first by queued_at.
    """
    runs: list[dict[str, Any]] = []

    # --- Queued ---
    q_dir = _queue_dir(data_dir, slug)
    if q_dir.exists():
        for p in q_dir.glob("*.json"):
            payload = _parse_payload(p)
            if payload:
                runs.append(_make_run_item(payload, status="queued"))

    # --- Processing ---
    pr_dir = _processing_dir(data_dir, slug)
    if pr_dir.exists():
        for p in pr_dir.glob("*.json"):
            payload = _parse_payload(p)
            if payload:
                # started_at ≈ file mtime (file was moved here when run started)
                try:
                    started_at = p.stat().st_mtime
                except OSError:
                    started_at = None
                runs.append(_make_run_item(payload, status="processing", started_at=started_at))

    # --- Processed (.ok and .fail.<rc>) ---
    prd_dir = _processed_dir(data_dir, slug)
    runs_d = _runs_dir(data_dir, slug)
    if prd_dir.exists():
        for p in prd_dir.iterdir():
            # suffix is .ok or .fail.<rc>
            if p.suffix == ".ok":
                status = "ok"
                rc = 0
                stem_base = p.stem  # e.g. "1778435487364-<uuid>"
            elif ".fail." in p.name:
                # e.g. "1778435487364-<uuid>.fail.1"
                parts = p.name.rsplit(".fail.", 1)
                if len(parts) != 2:
                    continue
                stem_base = parts[0]
                try:
                    rc = int(parts[1])
                except ValueError:
                    rc = -1
                status = "fail"
            else:
                continue

            # Extract queue_id: stem_base is "<ts>-<uuid>" — uuid is everything after first "-"
            # Actually stem_base may be "<epoch_ms>-<full-uuid>" where full-uuid has dashes.
            # Split on first '-' to get the ts prefix, rest is the uuid.
            dash_idx = stem_base.find("-")
            if dash_idx >= 0:
                queue_id_from_stem = stem_base[dash_idx + 1:]
            else:
                queue_id_from_stem = stem_base

            # Try to read the JSON payload from the processed file itself
            payload = _parse_payload(p)
            if payload is None:
                payload = {"queue_id": queue_id_from_stem}

            # Timestamps: started_at from log file mtime, ended_at from processed file mtime
            log_path = runs_d / f"{payload.get('queue_id', queue_id_from_stem)}.log"
            started_at = None
            ended_at = None
            try:
                ended_at = p.stat().st_mtime
            except OSError:
                pass
            if log_path.exists():
                try:
                    started_at = log_path.stat().st_mtime
                    # log mtime is roughly the end time; use slightly earlier as started
                    # We don't have precise start time stored — use processed file mtime as ended
                except OSError:
                    pass

            runs.append(_make_run_item(payload, status=status, rc=rc,
                                       started_at=started_at, ended_at=ended_at))

    # Sort newest-first by queued_at (falls back to string sort which is also chronological)
    runs.sort(
        key=lambda r: r.get("queued_at") or "",
        reverse=True,
    )
    return runs


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


@router.get("")
async def list_runs(
    slug: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """List deploy runs for a project, newest-first."""
    _check_project(request, slug)
    data_dir: Path = request.app.state.api_config.data_dir
    all_runs = collect_runs(data_dir, slug)
    return all_runs[offset: offset + limit]


@router.get("/{run_id}/log", response_class=PlainTextResponse)
async def get_run_log(
    slug: str,
    run_id: str,
    request: Request,
    full: int = Query(default=0),
) -> str:
    """Return run log as text/plain. Capped at 2 MB unless ?full=1."""
    _check_project(request, slug)
    data_dir: Path = request.app.state.api_config.data_dir
    log_path = _runs_dir(data_dir, slug) / f"{run_id}.log"

    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"log not found for run {run_id!r}")

    if full:
        return log_path.read_text(errors="replace")

    # Cap at 2 MB
    with log_path.open("rb") as fh:
        chunk = fh.read(_LOG_CAP_BYTES)
    return chunk.decode("utf-8", errors="replace")
