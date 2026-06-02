"""Constant teams (T-0154) — long-lived teams bound to permanent initiatives.

The stakeholder's ask (2026-06-02): besides one-off feature tickets, some
initiatives are *permanent* — "prod support" (triage alerts coming off prod),
"user feedback" (process incoming reports). Each such initiative should have a
**constant team**: a worker tick that keeps the team staffed so incoming work
is picked up without an operator hand-spawning a session every time.

This is deliberately **demand-driven**, not always-on: a constant team only
spawns a triage session when there is pending work *and* the team is below its
``team_size`` cap. An idle queue spawns nothing — which makes it safe to land
the dogfood initiatives on a live host without unleashing autonomous spawning.

Config lives in the initiative's own frontmatter (``vision/initiatives/<x>.md``)::

    ---
    name: prod-support
    constant_team: true
    team_size: 1                 # max concurrent triage sessions
    consume: _alerts/*.md        # work source (see below); omit => always-on
    team_role: dev               # role contract for spawned sessions
    team_window: prod-support    # tmux window prefix (default: initiative stem)
    triage_prompt: >             # optional extra brief text
      Investigate the alert, file a ticket via task_new, then remove the file.
    ---

``consume`` modes:
  * **glob** (``_alerts/*.md``)  — each matching file is one work item. Pending
    = matching files. The spawned dev is told to triage each file and DELETE it
    when handled (so it isn't reprocessed). Live-count gating prevents a second
    spawn while a triage session is still alive.
  * **log file** (``feedback/inbox.log``) — an append-only line queue. Pending =
    lines past a persisted cursor. The tick reads the new lines, advances the
    cursor, and hands those lines to the spawned dev in its brief.
  * **omitted** — an always-on team: the tick tops the roster up to
    ``team_size`` live members regardless of any queue.

State (cursor + per-team spawn cooldown) lives under
``data/<slug>/_worker/constant_teams/``. A 60s scheduler tick
(``jobs.constant_team_tick``) drives every project. The whole subsystem can be
disabled with ``BOT_SQUAD_CONSTANT_TEAMS_DISABLED=1`` (kill switch).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Per-team minimum gap between spawns, even when more work is pending. Combined
# with live-count gating this prevents an overlapping tick or a fast-exiting
# session from stampeding the queue. Override via env for tests.
_SPAWN_COOLDOWN_SEC = int(os.environ.get("BOT_SQUAD_CONSTANT_TEAM_COOLDOWN", "90"))
_MAX_ITEMS_IN_BRIEF = 20
_LIVE_STATUSES = ("active", "paused")


# ---------------------------------------------------------------------------
# Frontmatter (minimal flat parser — no yaml dependency in the worker venv)
# ---------------------------------------------------------------------------

def _read_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    fm: dict = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if mm:
            fm[mm.group(1)] = mm.group(2).strip()
    return fm


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1", "on")


# ---------------------------------------------------------------------------
# State (cursor + cooldown)
# ---------------------------------------------------------------------------

def _state_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "constant_teams"


def _state_file(cfg: Any, slug: str, name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return _state_dir(cfg, slug) / f"{safe}.json"


def _load_state(cfg: Any, slug: str, name: str) -> dict:
    p = _state_file(cfg, slug, name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(cfg: Any, slug: str, name: str, state: dict) -> None:
    d = _state_dir(cfg, slug)
    d.mkdir(parents=True, exist_ok=True)
    p = _state_file(cfg, slug, name)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def _live_member_count(cfg: Any, slug: str, init_stem: str, window_prefix: str) -> int:
    """Count active/paused sessions belonging to this constant team.

    A session belongs to the team if its initiative binding matches the
    initiative file (primary signal) or its tmux window starts with the team's
    window prefix (fallback for sessions spawned before the binding settled).
    """
    from bot_squad_worker import sessions as S
    try:
        rows = S.list_sessions(cfg, slug)
    except Exception:  # noqa: BLE001
        log.exception("constant_teams: list_sessions failed for %s", slug)
        return 0
    n = 0
    for r in rows:
        if r.get("status") not in _LIVE_STATUSES:
            continue
        init_val = Path(str(r.get("initiative") or "")).stem
        win = str(r.get("window") or "")
        if init_val == init_stem or (window_prefix and win.startswith(window_prefix)):
            n += 1
    return n


# ---------------------------------------------------------------------------
# Work-source resolution
# ---------------------------------------------------------------------------

def _glob_items(data_dir: Path, slug: str, pattern: str) -> list[Path]:
    base = data_dir / slug
    return sorted(p for p in base.glob(pattern) if p.is_file())


def _log_new_lines(log_path: Path, cursor_lines: int) -> list[str]:
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln for ln in lines[cursor_lines:] if ln.strip()]


def _log_line_count(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    try:
        return len(log_path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def _compose_brief(
    *,
    name: str,
    mission: str,
    role: str,
    items: list[str],
    triage_prompt: str,
    consume_kind: str,
) -> str:
    work_block = ""
    if items:
        shown = items[:_MAX_ITEMS_IN_BRIEF]
        bullets = "\n".join(f"  • {it}" for it in shown)
        more = f"\n  … and {len(items) - len(shown)} more — handle them too." \
            if len(items) > len(shown) else ""
        work_block = f"\n\nPENDING WORK ({len(items)} item(s)):\n{bullets}{more}"

    if consume_kind == "glob":
        consume_rule = (
            "Each pending item is a FILE. For each: investigate, then either fix "
            "it directly or file a backlog ticket (call the `task_new` worker "
            "action — never hand-pick a T-id), then DELETE the file so it is not "
            "reprocessed. Drain the whole queue before you exit."
        )
    elif consume_kind == "log":
        consume_rule = (
            "The pending work is feedback entries (shown above). For each: decide "
            "if it's actionable; if so, file a backlog ticket via `task_new` and "
            "note the ticket id back. The cursor has already advanced, so these "
            "lines won't be re-shown — capture everything now."
        )
    else:
        consume_rule = (
            "This is an always-on team. Pull the next piece of standing work for "
            "this initiative, drive it, and log progress on its ticket."
        )

    extra = f"\n\nInitiative guidance:\n{triage_prompt.strip()}" if triage_prompt.strip() else ""

    return (
        f"You are a CONSTANT-TEAM {role} for the '{name}' initiative.\n"
        f"Mission: {mission}\n"
        f"{work_block}\n\n"
        f"How to work:\n"
        f"- {consume_rule}\n"
        f"- FIRST read AGENT_INSTRUCTIONS.md for paths/git-rules and "
        f"vision/initiatives/{name}.md for the full mission.\n"
        f"- Track everything on bot-squad tickets (`bsq ticket note`), never in "
        f"superpowers docs. Commit only with `bsq commit`.\n"
        f"- When the queue is drained, you're done — your session auto-archives. "
        f"No need to keep a session idling.{extra}\n\n"
        f"Run `bsq --help` for the verb reference."
    )


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

def _spawn_member(cfg: Any, slug: str, *, window: str, init_filename: str, brief: str) -> Optional[str]:
    from bot_squad_worker import sessions as S
    try:
        res = S.spawn(cfg, slug, window, initial_prompt=brief,
                      initiative=init_filename, owner="constant-team")
        return res.get("sid")
    except Exception:  # noqa: BLE001
        log.exception("constant_teams: spawn failed for %s/%s", slug, init_filename)
        return None


def _maintain_initiative(cfg: Any, slug: str, init_path: Path, fm: dict) -> list[dict]:
    """Maintain one constant-team initiative. Returns a list of action records."""
    name = fm.get("name") or init_path.stem
    try:
        team_size = max(1, int(fm.get("team_size", 1)))
    except (TypeError, ValueError):
        team_size = 1
    role = (fm.get("team_role") or "dev").strip()
    window_prefix = (fm.get("team_window") or name)[:40]
    consume = (fm.get("consume") or "").strip()
    triage_prompt = fm.get("triage_prompt") or ""
    mission = fm.get("mission") or name
    init_stem = init_path.stem
    actions: list[dict] = []

    live = _live_member_count(cfg, slug, init_stem, window_prefix)
    capacity = team_size - live
    if capacity <= 0:
        return actions

    # Per-team spawn cooldown (belt-and-braces against overlapping ticks).
    state = _load_state(cfg, slug, name)
    last_spawn = float(state.get("last_spawn_at", 0) or 0)
    if time.time() - last_spawn < _SPAWN_COOLDOWN_SEC:
        return actions

    data_dir = cfg.data_dir

    # ---- resolve pending work + compose brief ----
    if not consume:
        consume_kind, items, advance_cursor = "none", [], None
        to_spawn = capacity
    elif consume.endswith(".log"):
        consume_kind = "log"
        log_path = data_dir / slug / consume
        cursor = int(state.get("cursor_lines", 0) or 0)
        new_lines = _log_new_lines(log_path, cursor)
        if not new_lines:
            return actions
        items = new_lines
        advance_cursor = _log_line_count(log_path)
        to_spawn = 1  # one dev drains the new feedback batch
    else:
        consume_kind = "glob"
        paths = _glob_items(data_dir, slug, consume)
        if not paths:
            return actions
        items = [str(p.relative_to(data_dir / slug)) for p in paths]
        advance_cursor = None
        to_spawn = min(capacity, len(paths))

    to_spawn = min(to_spawn, capacity)
    if to_spawn <= 0:
        return actions

    brief = _compose_brief(
        name=name, mission=mission, role=role, items=items,
        triage_prompt=triage_prompt, consume_kind=consume_kind,
    )

    # We only ever spawn one session per tick per team (cooldown-gated); a
    # single triage dev is expected to DRAIN the queue, and live-count gating
    # tops the team up to team_size across subsequent ticks if work persists.
    sid = _spawn_member(cfg, slug, window=window_prefix,
                        init_filename=init_path.name, brief=brief)
    if sid:
        state["last_spawn_at"] = time.time()
        if advance_cursor is not None:
            state["cursor_lines"] = advance_cursor
        _save_state(cfg, slug, name, state)
        actions.append({
            "team": name, "action": "spawned", "sid": sid,
            "pending": len(items), "live_before": live,
        })
        log.info("constant_teams[%s]: spawned %s for '%s' (%d pending, %d live)",
                 slug, sid, name, len(items), live)
    return actions


def tick(cfg: Any, slug: str) -> dict:
    """One maintenance pass for a project's constant teams.

    Scans every ``vision/initiatives/*.md`` with ``constant_team: true`` and
    keeps each staffed per its config. No-op when the kill switch is set or the
    project has no constant-team initiatives. Returns ``{actions: [...]}``.
    """
    if _truthy(os.environ.get("BOT_SQUAD_CONSTANT_TEAMS_DISABLED", "")):
        return {"actions": [], "disabled": True}

    init_dir = cfg.data_dir / slug / "vision" / "initiatives"
    if not init_dir.exists():
        return {"actions": []}

    all_actions: list[dict] = []
    for init_path in sorted(init_dir.glob("*.md")):
        fm = _read_frontmatter(init_path)
        if not _truthy(fm.get("constant_team")):
            continue
        try:
            all_actions.extend(_maintain_initiative(cfg, slug, init_path, fm))
        except Exception:  # noqa: BLE001
            log.exception("constant_teams: maintain failed for %s/%s", slug, init_path.name)
    return {"actions": all_actions}
