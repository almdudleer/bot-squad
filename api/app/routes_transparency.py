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
import re
import time
from datetime import datetime, timezone
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

# The internal statuses (mirrors routes_backlog._VALID_STATUSES) — the keys
# the backlog count map always carries, so the UI can render a stable set.
_STATUSES = ("planned", "open", "in_progress", "to_accept", "paused", "blocked_on_user", "totest", "reopened", "closed")


def _data_dir(request: Request) -> Path:
    return request.app.state.api_config.data_dir


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


# ---------------------------------------------------------------------------
# Work-state doc (T-0473 schema; T-0942 name + staleness) — read-only exposure
# ---------------------------------------------------------------------------

# Mirrors worker work_state.WORK_STATE_ARTIFACT + assignment._ARTIFACTS_SUBDIR.
# The api reads the data dir directly (no worker round-trip), so these names are
# duplicated here on purpose — but the FALLBACK to the pre-T-0942 name matters:
# the api and the worker deploy separately, so for one release either name may
# be the live one.
_WORK_STATE_REL = "artifacts/work-state.md"
_OPERATOR_STATE_REL = "artifacts/operator-state.md"

#: Hours past which the doc is reported stale. Mirrors
#: ``work_state.stale_after_hours``' default; the api has no worker env.
_STALE_AFTER_HOURS = 24

_FM_UPDATED_RE = re.compile(r"^updated:\s*(.+)$", re.MULTILINE)
_FM_UPDATED_BY_RE = re.compile(r"^(?:updated_by|sid):\s*(.+)$", re.MULTILINE)
_FM_REV_RE = re.compile(r"^rev:\s*(\d+)\s*$", re.MULTILINE)


def _fm_block(content: str) -> str:
    """The frontmatter block only — so an `updated:` line in the BODY (a state
    doc quotes timestamps constantly) cannot be mistaken for the doc's own."""
    if not content.startswith("---\n"):
        return ""
    end = content.find("\n---", 4)
    return content[4:end] if end != -1 else ""


def _work_state(data_dir: Path, slug: str) -> dict:
    """The work-state doc + a STALENESS VERDICT for the board.

    T-0942: the payload used to carry only ``updated_at``, and a date is not a
    warning — the board rendered a five-week-old doc exactly like a fresh one.
    ``stale`` / ``age_seconds`` / ``updated_by`` let the UI say which it is.
    """
    art = data_dir / slug / "artifacts"
    p, rel = art / "work-state.md", _WORK_STATE_REL
    if not p.exists():
        p, rel = art / "operator-state.md", _OPERATOR_STATE_REL
    empty = {"exists": False, "content": None, "updated_at": None,
             "path": _WORK_STATE_REL, "updated_by": None, "rev": 0,
             "age_seconds": None, "stale": False,
             "stale_after_hours": _STALE_AFTER_HOURS}
    if not p.exists():
        return empty
    try:
        content = p.read_text()
        updated_at = p.stat().st_mtime
    except OSError:
        return empty
    fm = _fm_block(content)
    m = _FM_UPDATED_RE.search(fm)
    written_at = updated_at
    if m:
        try:
            written_at = datetime.strptime(
                m.group(1).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    by = _FM_UPDATED_BY_RE.search(fm)
    rev = _FM_REV_RE.search(fm)
    age = max(0.0, time.time() - written_at)
    return {"exists": True, "content": content, "updated_at": updated_at,
            "path": rel,
            "updated_by": by.group(1).strip() if by else None,
            "rev": int(rev.group(1)) if rev else 0,
            "age_seconds": int(age),
            "stale": age > _STALE_AFTER_HOURS * 3600,
            "stale_after_hours": _STALE_AFTER_HOURS}


#: Pre-T-0942 name, kept so nothing that imports it breaks mid-deploy.
_operator_state = _work_state


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

# T-0929 — the NAMED drive states. Mirror of pace.py's DRIVE_STATES /
# DRIVE_STATE_AXES / DRIVE_STATE_LABELS. `off` is deliberately NOT a storable
# state: it is the pause flag (`_global_paused`), so this normaliser never
# returns it — combine the two with `_effective_drive_state` exactly as
# pace.effective_drive_state does, or the panel prints a mode beside an
# engaged switch.
_DRIVE_STATES = ("one_task", "finish_up", "all_tasks", "off")
_DRIVE_STATE_CUSTOM = "custom"
_DRIVE_STATE_AXES = {
    "one_task": {"scope": "all", "stop_when": "scope_exhausted",
                 "max_in_progress": 1},
    "finish_up": {"scope": "in_progress", "stop_when": "scope_exhausted",
                  "max_in_progress": 0},
    "all_tasks": {"scope": "all", "stop_when": "scope_exhausted",
                  "max_in_progress": 0},
}
_DRIVE_STATE_DEFAULT = "all_tasks"
_DRIVE_STATE_LABELS = {
    "off": "nothing automatic runs",
    "one_task": "work on one task",
    "finish_up": "close what is in progress, take nothing new",
    "all_tasks": "drive the backlog end to end",
    _DRIVE_STATE_CUSTOM: "axes set by hand, no named state",
}


def _derive_drive_state(raw: dict) -> str:
    """Mirror of ``pace.py:derive_drive_state`` — the state a pre-T-0929 config
    is in, matched on the FULL axis tuple including ``max_in_progress``."""
    src = raw.get("drive")
    src = src if isinstance(src, dict) else {}
    if not src:
        return _DRIVE_STATE_DEFAULT
    cap = max(0, _coerce_int(raw.get("max_in_progress"), 0))
    for name, axes in _DRIVE_STATE_AXES.items():
        if (src.get("scope", _DRIVE_DEFAULTS["scope"]) == axes["scope"]
                and src.get("stop_when", _DRIVE_DEFAULTS["stop_when"]) == axes["stop_when"]
                and cap == axes["max_in_progress"]):
            return name
    return _DRIVE_STATE_CUSTOM


def _effective_drive_state(drive: dict, paused: bool) -> str:
    """Mirror of ``pace.py:effective_drive_state`` — ``off`` whenever the gate is
    engaged, else the configured state."""
    if paused:
        return "off"
    return str(drive.get("state") or _DRIVE_STATE_DEFAULT)


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
    out["state"] = _DRIVE_STATE_DEFAULT

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

    stored = src.get("state")
    if stored is None:
        out["state"] = _derive_drive_state(raw)
    elif str(stored).strip() == "off" or str(stored).strip() not in _DRIVE_STATE_AXES:
        out["invalid"]["state"] = stored
        out["state"] = _derive_drive_state(raw)
    else:
        out["state"] = str(stored).strip()
    return out


#: Mirrors ``operator_redrive.CAP_COUNTS`` — the unit ``max_in_progress`` counts.
#: Published so a consumer of this payload cannot read the ``in_progress`` board
#: count sitting beside the cap as the cap's own load (T-0966); this surface is
#: file-read-only by design and cannot take the live count itself, so it says
#: what the cap counts and does NOT pretend the number it has is that.
CAP_COUNTS = "live_dev_sessions"


def _quota(data_dir: Path, slug: str, in_progress: int) -> dict:
    """The pace config + the board's ``in_progress`` count.

    ⚠ T-0966: ``in_progress`` is the BOARD LABEL count and is NOT the quantity
    ``max_in_progress`` gates — the cap counts live dev sessions
    (``cap_counts``), which only the worker can measure. Measured on the live
    install 2026-09-06: 8 live dev sessions, 1 ticket labelled ``in_progress``,
    cap 7 — so rendering "1 / 7" from these two fields stated the opposite of
    what was happening. Both numbers are real; they are not the same unit, and
    the payload now says so. The LIVE reading lives on the automation snapshot
    (``/automation``, a worker proxy) and on ``bsq pace show``.
    """
    raw = _pace_raw(data_dir, slug)
    return {
        "max_in_progress": max(0, _coerce_int(raw.get("max_in_progress"), 0)),
        "cap_counts": CAP_COUNTS,
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
        # T-0942 renamed the doc; the payload key stays `operator_state` for one
        # release (the web board reads it) and `work_state` is the new name.
        "operator_state": _work_state(data_dir, slug),
        "work_state": _work_state(data_dir, slug),
        "sessions": sessions,
        "sessions_scope": sessions_scope,
        "backlog": backlog,
        "quota": quota,
    }
