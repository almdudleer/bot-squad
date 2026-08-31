"""Unified read-only system-transparency surface (T-0511 / M11-F11.4).

> "the whole system is completely transparent to the user, and he can drive the
>  system from anywhere" — clarification-03.

ONE endpoint aggregates the four state surfaces a fresh operator or stakeholder
needs to understand where the system IS — without talking to the operator:

  * the operator state-doc   — ``artifacts/operator-state.md`` (T-0473 / M2-F2.1)
  * the session tree         — the live ``list_sessions`` fan-out (reused as-is)
  * the backlog              — status counts + a light task list
  * quota / pace             — max-in-progress + global pause + per-initiative
                               pace + the standing DRIVE MODE
                               (T-0482 / T-0828 / pace.py)

This surface is STRICTLY READ-ONLY — it writes nothing and owns no state.

Why it mirrors the worker's state-file layout instead of importing the worker:
the API and worker are separate deployable packages (separate containers); the
API cannot ``import bot_squad_worker``. The established pattern is to mirror the
worker's on-disk layout from the shared data dir — see ``frontmatter.py``,
``idalloc.py``, ``routes_autoupdate.py``. So pace/quota is read directly from
``_worker/pace/pace.json`` + ``_worker/operator_redrive/operator_paused.flag``,
matching ``worker/bot_squad_worker/pace.py`` and ``operator_redrive.py``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.frontmatter import parse_or_none
from app.routes_auth import require_auth
from app.routes_sessions import list_sessions

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/transparency",
    tags=["transparency"],
    dependencies=[Depends(require_auth)],
)

# The six internal statuses (mirrors routes_backlog._VALID_STATUSES) — the keys
# the backlog count map always carries, so the UI can render a stable set.
_STATUSES = ("planned", "open", "in_progress", "paused", "blocked_on_user", "totest", "reopened", "closed")


def _data_dir(request: Request) -> Path:
    return request.app.state.api_config.data_dir


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


# ---------------------------------------------------------------------------
# Operator state-doc (T-0473) — read-only exposure
# ---------------------------------------------------------------------------

# Mirrors worker assignment.OPERATOR_STATE_ARTIFACT + _ARTIFACTS_SUBDIR.
_OPERATOR_STATE_REL = "artifacts/operator-state.md"


def _operator_state(data_dir: Path, slug: str) -> dict:
    p = data_dir / slug / "artifacts" / "operator-state.md"
    if not p.exists():
        return {"exists": False, "content": None, "updated_at": None,
                "path": _OPERATOR_STATE_REL}
    try:
        content = p.read_text()
        updated_at = p.stat().st_mtime
    except OSError:
        return {"exists": False, "content": None, "updated_at": None,
                "path": _OPERATOR_STATE_REL}
    return {"exists": True, "content": content, "updated_at": updated_at,
            "path": _OPERATOR_STATE_REL}


# ---------------------------------------------------------------------------
# Backlog — status counts + light task list
# ---------------------------------------------------------------------------

def _backlog(data_dir: Path, slug: str) -> dict:
    backlog_dir = data_dir / slug / "backlog"
    counts = {s: 0 for s in _STATUSES}
    tasks: list[dict] = []
    if backlog_dir.is_dir():
        for f in sorted(backlog_dir.glob("*.md")):
            try:
                text = f.read_text()
            except OSError:
                continue
            parsed = parse_or_none(text)
            if parsed is None:
                continue  # unparseable — skip, never 500
            fm = parsed[0]
            tid = fm.get("id")
            if not tid:
                continue
            status = str(fm.get("status") or "")
            if status in counts:
                counts[status] += 1
            tasks.append({
                "id": tid,
                "title": fm.get("title"),
                "status": status,
                "initiative": fm.get("initiative"),
                "priority": fm.get("priority"),
            })
    return {"counts": counts, "tasks": tasks}


# ---------------------------------------------------------------------------
# Quota / pace (T-0482) — mirror of pace.py + operator_redrive.py on-disk layout
# ---------------------------------------------------------------------------

def _coerce_int(v: object, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_float(v: object, default: float) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1", "on")


def _pace_raw(data_dir: Path, slug: str) -> dict:
    """The persisted pace.json as-is ({} if absent/unreadable). Mirrors
    ``worker/bot_squad_worker/pace.py:_load_raw``."""
    p = data_dir / slug / "_worker" / "pace" / "pace.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _global_paused(data_dir: Path, slug: str) -> bool:
    """True iff the operator re-drive is user-paused. Mirrors
    ``operator_redrive.is_paused`` (presence of the pause-flag file = paused)."""
    flag = data_dir / slug / "_worker" / "operator_redrive" / "operator_paused.flag"
    return flag.exists()


def _normalized_initiatives(raw: dict) -> dict[str, dict]:
    """Mirror of ``pace.py:_normalized_initiatives`` (defaults applied, .md key)."""
    out: dict[str, dict] = {}
    src = raw.get("initiatives")
    if not isinstance(src, dict):
        return out
    for name, body in src.items():
        base = Path(str(name).strip()).name
        if base.lower().endswith(".md"):
            base = base[:-3]
        key = f"{base}.md" if base else ""
        if not key:
            continue
        body = body if isinstance(body, dict) else {}
        out[key] = {
            "weight": _coerce_float(body.get("weight"), 1.0),
            "priority": _coerce_int(body.get("priority"), 0),
            "paused": _coerce_bool(body.get("paused")),
        }
    return out


# ---------------------------------------------------------------------------
# Drive modes (T-0828 / D-0069) — mirror of pace.py's `drive` block
# ---------------------------------------------------------------------------
# ⚠ THE TWIN. Everything below is a SECOND implementation of
# `worker/bot_squad_worker/pace.py`'s drive normalisation, for the same reason
# the initiative mirror above exists: the api cannot `import bot_squad_worker`
# (separate deployable packages / containers), so it mirrors the on-disk layout.
#
# If this drifts from pace.py the failure is SILENT and it is the exact defect
# the stakeholder reported: the UI renders a project with no drive mode set,
# while `bsq pace show` says one is. `api/tests/test_pace_drive_mirror.py`
# drives BOTH implementations over one fixture set and goes red the moment one
# grows a field the other lacks; lint.yml runs it on every push.

_DRIVE_SCOPES = ("open_reopened", "in_progress", "all")
_DRIVE_STOP_WHEN = ("scope_exhausted", "spend_quota")
_DRIVE_ON_STOP = ("nothing", "alert")
_DRIVE_DEFAULTS = {
    "scope": "all",
    "stop_when": "scope_exhausted",
    "on_stop": "nothing",
}
_DRIVE_CHOICES = {
    "scope": _DRIVE_SCOPES,
    "stop_when": _DRIVE_STOP_WHEN,
    "on_stop": _DRIVE_ON_STOP,
}
_DRIVE_PROVENANCE_FIELDS = ("set_by", "set_at", "source_text")


def _normalized_drive(raw: dict) -> dict:
    """Mirror of ``pace.py:_normalized_drive`` — defaults applied, closed sets
    enforced, out-of-set values REPORTED rather than silently swapped.

    Read-only surface, so there is no write path and no ``DriveModeError`` here:
    an out-of-set stored value lands in ``invalid`` with its raw value intact and
    the effective value falls back to the default. The UI must render the
    ``invalid`` entry — reporting a fallback as if it were the setting is the
    silent-None failure D-0069 names.

    ``configured`` distinguishes "no drive block on disk" from "explicitly set
    to the default". Both read ``scope: all``; only this flag answers his actual
    question, «какой режим драйва щас стоит».
    """
    out: dict = dict(_DRIVE_DEFAULTS)
    out.update({"set_by": None, "set_at": None, "source_text": None})
    out["configured"] = False
    out["invalid"] = {}

    src = raw.get("drive")
    if not isinstance(src, dict):
        return out
    out["configured"] = True

    for field, choices in _DRIVE_CHOICES.items():
        if field not in src or src[field] is None:
            continue
        sval = str(src[field]).strip()
        if sval in choices:
            out[field] = sval
        else:
            out["invalid"][field] = src[field]  # effective value stays the default

    for field in _DRIVE_PROVENANCE_FIELDS:
        v = src.get(field)
        out[field] = str(v) if v is not None else None
    return out


def _quota(data_dir: Path, slug: str, in_progress: int) -> dict:
    raw = _pace_raw(data_dir, slug)
    return {
        "max_in_progress": max(0, _coerce_int(raw.get("max_in_progress"), 0)),
        "in_progress": in_progress,
        "paused": _global_paused(data_dir, slug),
        "initiatives": _normalized_initiatives(raw),
        "drive": _normalized_drive(raw),
    }


# ---------------------------------------------------------------------------
# Aggregate endpoint
# ---------------------------------------------------------------------------

@router.get("")
async def get_transparency(
    slug: str, request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """The unified system-state view: operator state-doc + session tree + backlog
    + quota, in one read. Authenticated, read-only; session rows are owner-scoped
    by the underlying ``list_sessions`` (admins see all).

    T-0772: ``sessions_scope`` states WHICH of those two happened, because this
    payload is the one that renders the board's summary strip — ``sessions``
    (owner-scoped, per-user) and ``quota.in_progress`` (broad, counted off the
    backlog) sit in adjacent cards from this single response. Without the marker
    the strip silently mixed the two policies: a non-admin read "IN PROGRESS 6"
    beside "LIVE SESSIONS 0" and could only conclude the install was wedged.
    ``None`` when the fan-out failed — an unknown scope must never be reported
    as a scoped zero (that would blame the owner gate for a dead worker).
    """
    _check_project(request, slug)
    data_dir = _data_dir(request)

    # Session tree — reuse the existing fan-out (owner-scoped, pin-stamped). It
    # degrades to [] when no worker is reachable; never let it sink the page.
    # T-0601 (F5): list_sessions now returns {sessions, errors}; this view
    # keeps exposing the bare row list (its own contract is unchanged).
    try:
        payload = await list_sessions(slug=slug, request=request, user=user)
        sessions = payload.get("sessions", [])
        sessions_scope = payload.get("sessions_scope")
    except Exception as e:  # pragma: no cover - defensive
        log.warning("transparency: session list failed for %s: %s", slug, e)
        sessions = []
        sessions_scope = None

    backlog = _backlog(data_dir, slug)
    quota = _quota(data_dir, slug, backlog["counts"]["in_progress"])

    return {
        "slug": slug,
        "operator_state": _operator_state(data_dir, slug),
        "sessions": sessions,
        "sessions_scope": sessions_scope,
        "backlog": backlog,
        "quota": quota,
    }
