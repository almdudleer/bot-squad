"""Constant-team tick state — read-only health surface (T-0295 leg b).

Until this endpoint existed, a constant team's tick state lived ONLY on disk at
``data/<slug>/_worker/constant_teams/*.json`` with nothing surfacing it: no API,
no UI. An operator asking "is the persistent initiative's team actually alive?"
had to shell into the host and cat a json. This exposes exactly what the worker
tick reads and writes, joined with the tick's config source, so the Vision page
can answer that question.

Read-only by design (D-0057 §8: web = observability, TG/CLI = the control
surface). No spawn/kill/edit affordance rides here.

WHAT THE WORKER ACTUALLY DOES (mirrored from
``worker/bot_squad_worker/constant_teams.py`` — keep in lockstep):
  * config source = ``vision/initiatives/<x>.md`` frontmatter with
    ``constant_team: <truthy>``. NOTE this is a legacy location: T-0480 moved
    initiatives into ``kind: initiative`` backlog tasks, and the tick was never
    repointed. A task-backed persistent initiative therefore has NO tick config,
    and this endpoint reports that plainly rather than implying health.
  * ``consume`` is REQUIRED (T-0457) — a no-consume team is a misconfig the tick
    warns about and skips.
  * a RETIRED name (code-level denylist) is never staffed, even if its config
    file reappears.
  * a FINISHED initiative stops being re-staffed (T-0335 item-16).

Member count is NOT computed here: the live session roster lives behind the
worker (``sessions.list_sessions``) and the Vision page already loads it. The FE
joins on ``stem``/``window_prefix`` the same way it already mirrors the worker's
TL resolver — see ``constantTeamMembers`` in web/src/constantTeams.ts.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.frontmatter import parse_or_none
from app.routes_auth import require_auth

router = APIRouter(
    prefix="/projects/{slug}/constant-teams",
    tags=["vision"],
    dependencies=[Depends(require_auth)],
)

# SSOT: worker/bot_squad_worker/constant_teams.py::_RETIRED_CONSTANT_TEAMS.
# Duplicated (the api container has no worker package on its path); if that
# frozenset changes, change this one — a stale copy here only mislabels a row's
# `retired` flag, it cannot affect staffing.
_RETIRED_CONSTANT_TEAMS = frozenset({"user-feedback", "prod-support"})


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1", "on")


def _state_filename(name: str) -> str:
    """Mirror of the worker's ``_state_file`` name sanitisation."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json"


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    parsed = parse_or_none(text)
    return parsed[0] if parsed else {}


def _read_finished(vision_dir: Path) -> set[str]:
    p = vision_dir / "finished_initiatives"
    out: set[str] = set()
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("initiatives/"):
            line = line[len("initiatives/"):]
        out.add(line)
    return out


def _log_pending(log_path: Path, cursor_lines: int) -> int:
    """Non-blank lines past the persisted cursor — the tick's own definition of
    pending work for a ``.log`` consume source."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    return len([ln for ln in lines[cursor_lines:] if ln.strip()])


def _glob_pending(project_dir: Path, pattern: str) -> int:
    try:
        return len([p for p in project_dir.glob(pattern) if p.is_file()])
    except (OSError, ValueError):
        return 0


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    try:
        return _dt.datetime.fromtimestamp(
            float(epoch), tz=_dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _queue(project_dir: Path, consume: str, cursor_lines: int) -> tuple[str | None, int | None]:
    """(consume_kind, queue_depth) for a consume source — None when unset."""
    if not consume:
        return None, None
    if consume.endswith(".log"):
        return "log", _log_pending(project_dir / consume, cursor_lines)
    return "glob", _glob_pending(project_dir, consume)


def _entry_from_config(project_dir: Path, init_path: Path, fm: dict,
                       finished: set[str]) -> dict:
    stem = init_path.stem
    name = str(fm.get("name") or stem).strip() or stem
    try:
        team_size = max(1, int(fm.get("team_size", 1)))
    except (TypeError, ValueError):
        team_size = 1
    consume = str(fm.get("consume") or "").strip()
    state_path = project_dir / "_worker" / "constant_teams" / _state_filename(name)
    state = _read_json(state_path)
    cursor_lines = int(state.get("cursor_lines", 0) or 0)
    consume_kind, queue_depth = _queue(project_dir, consume, cursor_lines)
    last_spawn_at = state.get("last_spawn_at") or None
    retired = stem in _RETIRED_CONSTANT_TEAMS or name in _RETIRED_CONSTANT_TEAMS
    is_finished = init_path.name in finished

    if retired:
        staffable, why = False, "retired — the tick refuses to staff this team"
    elif is_finished:
        staffable, why = False, "initiative is finished — no longer re-staffed"
    elif not consume:
        staffable, why = False, "no `consume` source — always-on mode is retired (T-0457)"
    else:
        staffable, why = True, None

    return {
        "name": name,
        "stem": stem,
        "initiative": f"initiatives/{init_path.name}",
        "configured": True,
        "config_source": f"vision/initiatives/{init_path.name}",
        "team_size": team_size,
        "team_role": str(fm.get("team_role") or "dev").strip(),
        "window_prefix": str(fm.get("team_window") or name)[:40],
        "consume": consume or None,
        "consume_kind": consume_kind,
        "queue_depth": queue_depth,
        "cursor_lines": cursor_lines if consume_kind == "log" else None,
        "last_spawn_at": float(last_spawn_at) if last_spawn_at else None,
        "last_spawn_iso": _iso(last_spawn_at),
        "state_file": f"_worker/constant_teams/{state_path.name}",
        "state_present": state_path.exists(),
        "retired": retired,
        "finished": is_finished,
        "staffable": staffable,
        "not_staffable_reason": why,
    }


def _entry_from_orphan_state(state_path: Path) -> dict:
    """Tick state on disk with no matching config — a relic of a team whose
    initiative file was deleted/archived (or retired). Surfaced rather than
    dropped: silently hiding it is how the disk and the UI drift apart."""
    name = state_path.stem
    state = _read_json(state_path)
    last_spawn_at = state.get("last_spawn_at") or None
    retired = name in _RETIRED_CONSTANT_TEAMS
    return {
        "name": name,
        "stem": name,
        "initiative": None,
        "configured": False,
        "config_source": None,
        "team_size": None,
        "team_role": None,
        "window_prefix": name,
        "consume": None,
        "consume_kind": None,
        "queue_depth": None,
        "cursor_lines": int(state.get("cursor_lines", 0) or 0) or None,
        "last_spawn_at": float(last_spawn_at) if last_spawn_at else None,
        "last_spawn_iso": _iso(last_spawn_at),
        "state_file": f"_worker/constant_teams/{state_path.name}",
        "state_present": True,
        "retired": retired,
        "finished": False,
        "staffable": False,
        "not_staffable_reason": (
            "retired — the tick refuses to staff this team" if retired
            else "no constant-team config found for this state file"
        ),
    }


@router.get("")
def list_constant_teams(slug: str, request: Request) -> dict:
    # Read gate = the router-level ``require_auth``, same as the sibling
    # ``GET /vision`` list this panel sits next to.
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")
    project_dir = cfg.project_data_dir(slug)
    vision_dir = project_dir / "vision"
    init_dir = vision_dir / "initiatives"
    state_dir = project_dir / "_worker" / "constant_teams"
    finished = _read_finished(vision_dir)

    teams: list[dict] = []
    seen_state: set[str] = set()
    if init_dir.is_dir():
        for init_path in sorted(init_dir.glob("*.md")):
            fm = _read_frontmatter(init_path)
            if not _truthy(fm.get("constant_team")):
                continue
            entry = _entry_from_config(project_dir, init_path, fm, finished)
            seen_state.add(Path(entry["state_file"]).name)
            teams.append(entry)

    if state_dir.is_dir():
        for state_path in sorted(state_dir.glob("*.json")):
            if state_path.name in seen_state:
                continue
            teams.append(_entry_from_orphan_state(state_path))

    return {
        "teams": teams,
        # The tick's config source, spelled out so a caller seeing an empty
        # list knows WHERE it looked (and that T-0480's task-backed
        # initiatives are not that place).
        "config_dir": "vision/initiatives/*.md (frontmatter `constant_team: true`)",
        "state_dir": "_worker/constant_teams",
    }
