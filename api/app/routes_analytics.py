"""Product-analytics endpoint (T-0147).

A thin, server-computed dashboard of *bot-squad's own* operational numbers —
the "B" (internal usage) interpretation of product analytics (see the ticket
body for the A-vs-B rationale). Everything is derived on the fly from the
project's data dir; there is no separate analytics store:

  - sessions   ← data/<slug>/sessions/*.md      (started_at, status)
  - tickets    ← data/<slug>/backlog/T-*.md      (status, created, updated)
  - deploys    ← data/<slug>/_jobs/deploy/processed/*  (epoch-ms filename, .ok/.fail)

Read-only and cheap (a few hundred small md files); recomputed per request.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.frontmatter import parse_or_none
from app.markdown_parser import ParseError, parse_task
from app.routes_auth import require_auth

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{slug}/analytics",
    tags=["analytics"],
    dependencies=[Depends(require_auth)],
)

# Length of the per-day time series (sessions started, tickets closed).
_WINDOW_DAYS = 14
# Number of trailing ISO weeks reported for deploys.
_DEPLOY_WEEKS = 8


def _data_dir(request: Request, slug: str) -> Path:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    return cfg.project_data_dir(slug)


def _parse_ts(value: object) -> datetime | None:
    """Coerce a frontmatter timestamp → aware UTC datetime.

    Accepts strings (from the lightweight session parser) and the
    ``datetime``/``date`` objects that ``yaml.safe_load`` produces for backlog
    frontmatter (PyYAML auto-resolves ISO timestamps to native types).
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _empty_day_series(today: datetime, days: int) -> dict[str, int]:
    """Zero-filled date→count map for the trailing `days` (oldest first)."""
    out: dict[str, int] = {}
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        out[d] = 0
    return out


def _series_to_list(series: dict[str, int]) -> list[dict]:
    return [{"date": d, "count": c} for d, c in series.items()]


def _read_frontmatter(path: Path) -> dict:
    """Read session md frontmatter via the shared parser (T-0075).

    Sessions don't go through ``parse_task`` (no priority coercion) but use the
    SAME pyyaml-based parser, so list fields + ``~``/timestamps behave
    consistently. ``_parse_ts`` already tolerates the plain-string timestamps
    this returns.
    """
    parsed = parse_or_none(path.read_text())
    return parsed[0] if parsed is not None else {}


def _sessions_stats(data_dir: Path, today: datetime) -> dict:
    sessions_dir = data_dir / "sessions"
    by_status: dict[str, int] = {}
    archived = 0
    per_day = _empty_day_series(today, _WINDOW_DAYS)
    total = 0
    if sessions_dir.is_dir():
        for p in sessions_dir.glob("*.md"):
            meta = _read_frontmatter(p)
            if not meta:
                continue
            total += 1
            status = meta.get("status") or "unknown"
            by_status[status] = by_status.get(status, 0) + 1
            if str(meta.get("archived", "")).lower() == "true":
                archived += 1
            started = _parse_ts(meta.get("started_at"))
            if started:
                key = started.strftime("%Y-%m-%d")
                if key in per_day:
                    per_day[key] += 1
    return {
        "total": total,
        "archived": archived,
        "by_status": by_status,
        "per_day": _series_to_list(per_day),
    }


def _tickets_stats(data_dir: Path, today: datetime) -> dict:
    backlog_dir = data_dir / "backlog"
    by_status: dict[str, int] = {}
    closed_per_day = _empty_day_series(today, _WINDOW_DAYS)
    total = 0
    time_to_close_days: list[float] = []
    if backlog_dir.is_dir():
        for p in sorted(backlog_dir.glob("T-*.md")):
            try:
                task = parse_task(p)
            except (ParseError, OSError):
                continue
            total += 1
            status = task.get("status") or "unknown"
            by_status[status] = by_status.get(status, 0) + 1
            if status == "closed":
                updated = _parse_ts(task.get("updated"))
                if updated:
                    key = updated.strftime("%Y-%m-%d")
                    if key in closed_per_day:
                        closed_per_day[key] += 1
                created = _parse_ts(task.get("created"))
                if created and updated and updated >= created:
                    time_to_close_days.append(
                        (updated - created).total_seconds() / 86400.0
                    )
    mttr = {
        "closed_measured": len(time_to_close_days),
        "mean_days": round(statistics.fmean(time_to_close_days), 2)
        if time_to_close_days
        else None,
        "median_days": round(statistics.median(time_to_close_days), 2)
        if time_to_close_days
        else None,
    }
    return {
        "total": total,
        "by_status": by_status,
        "closed_per_day": _series_to_list(closed_per_day),
        "time_to_close": mttr,
    }


def _deploys_stats(data_dir: Path) -> dict:
    processed = data_dir / "_jobs" / "deploy" / "processed"
    ok = 0
    fail = 0
    last_deploy_at: datetime | None = None
    per_week: dict[str, dict[str, int]] = {}
    if processed.is_dir():
        for p in processed.iterdir():
            name = p.name
            if name.endswith(".ok"):
                outcome = "ok"
            elif ".fail" in name:
                outcome = "fail"
            else:
                continue
            if outcome == "ok":
                ok += 1
            else:
                fail += 1
            # Filename is "<epoch-ms>-<uuid>.<outcome>[.N]".
            head = name.split("-", 1)[0]
            try:
                ts = datetime.fromtimestamp(int(head) / 1000.0, tz=timezone.utc)
            except (ValueError, OverflowError):
                continue
            if last_deploy_at is None or ts > last_deploy_at:
                last_deploy_at = ts
            iso_year, iso_week, _ = ts.isocalendar()
            wk = f"{iso_year}-W{iso_week:02d}"
            bucket = per_week.setdefault(wk, {"ok": 0, "fail": 0})
            bucket[outcome] += 1
    total = ok + fail
    weeks_sorted = sorted(per_week.items())[-_DEPLOY_WEEKS:]
    return {
        "total": total,
        "ok": ok,
        "fail": fail,
        "success_rate": round(ok / total, 3) if total else None,
        "last_deploy_at": last_deploy_at.isoformat() if last_deploy_at else None,
        "per_week": [
            {"week": wk, "ok": v["ok"], "fail": v["fail"]} for wk, v in weeks_sorted
        ],
    }


@router.get("")
def get_analytics(request: Request, slug: str) -> dict:
    """Compute the internal-usage analytics snapshot for one project."""
    data_dir = _data_dir(request, slug)
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "slug": slug,
        "generated_at": now.isoformat(),
        "window_days": _WINDOW_DAYS,
        # T-0360: expose the deploys/week trailing-window length so the FE can
        # label that chart's scope explicitly (it differs from window_days).
        "deploy_weeks": _DEPLOY_WEEKS,
        "sessions": _sessions_stats(data_dir, today),
        "tickets": _tickets_stats(data_dir, today),
        "deploys": _deploys_stats(data_dir),
    }
