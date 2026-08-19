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
    consume: _alerts/*.md        # work source (REQUIRED — see below)
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

``consume`` is REQUIRED: a constant team is always demand-driven. The old
"omit => always-on" keep-alive mode is RETIRED (T-0457 / T-0423 Fork-A: "no
no-consume team can exist") — a constant_team with no consume source is a
misconfig the tick warns about and skips.

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

# Audit item 8 (Fork-4) + D6: these constant-team initiatives are RETIRED — the
# `user-feedback` firehose (auto-ticketed every inbox.log line, bypassing the
# operator's Occam prune — "the bullshit generator") and the `prod-support` dead
# loop (consumer wired, no producer). The on-host delete step removes their
# initiative files + data stores, but THIS code-level denylist is the durable
# guard the operator asked for: even if a re-seed/scaffold drops the files back,
# tick() refuses to staff them, so the firehose can't resurrect. Rides the
# deploy (not a gitignored data file), so it survives any data-dir reseed.
_RETIRED_CONSTANT_TEAMS = frozenset({"user-feedback", "prod-support"})


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
            fm[mm.group(1)] = _strip_yaml_quotes(mm.group(2).strip())
    return fm


def _strip_yaml_quotes(value: str) -> str:
    """Strip a single pair of surrounding YAML quotes.

    T-0200: this flat parser captures the raw post-colon text, so a YAML-quoted
    scalar (``name: "prod-support"``) would otherwise keep its quotes and leak a
    literal ``"`` downstream — into the team window name, the cooldown state-file
    key, and (historically) the tmux session name. Strip one matching pair so
    ``"prod-support"`` / ``'prod-support'`` → ``prod-support``.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1", "on")


# ---------------------------------------------------------------------------
# State (cursor + cooldown)
# ---------------------------------------------------------------------------

def _read_finished_initiatives(cfg: Any, slug: str) -> set[str]:
    """Basenames (``<name>.md``) of initiatives marked finished via the API.

    Mirrors ``routes_vision._read_finished_initiatives``: one name per line in
    ``vision/finished_initiatives``, stripping a legacy ``initiatives/`` prefix.
    T-0335 item-16: a finished constant-team initiative must stop being
    re-staffed by :func:`tick`.
    """
    p = cfg.data_dir / slug / "vision" / "finished_initiatives"
    if not p.exists():
        return set()
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

    # consume_kind is always a demand-driven mode here ("glob" or "log") — the
    # always-on (no-consume) mode is retired (T-0457), so _maintain_initiative
    # never composes a brief for it.
    if consume_kind == "glob":
        consume_rule = (
            "Each pending item is a FILE. For each: investigate, then either fix "
            "it directly or file a backlog ticket (call the `task_new` worker "
            "action — never hand-pick a T-id), then DELETE the file so it is not "
            "reprocessed. Drain the whole queue before you exit."
        )
    else:  # "log"
        consume_rule = (
            "The pending work is feedback entries (shown above). For each: decide "
            "if it's actionable; if so, file a backlog ticket via `task_new` and "
            "note the ticket id back. The cursor has already advanced, so these "
            "lines won't be re-shown — capture everything now."
        )

    extra = f"\n\nInitiative guidance:\n{triage_prompt.strip()}" if triage_prompt.strip() else ""

    # A demand-driven team drains its queue and exits — there is no always-on
    # keep-alive variant anymore (T-0457).
    exit_rule = (
        "When the queue is drained, you're done — your session auto-archives. "
        "No need to keep a session idling."
    )

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
        f"- {exit_rule}{extra}\n\n"
        f"Run `bsq --help` for the verb reference."
    )


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

def _spawn_member(cfg: Any, slug: str, *, window: str, init_filename: str, brief: str) -> Optional[str]:
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError
    try:
        res = S.spawn(cfg, slug, window, initial_prompt=brief,
                      initiative=init_filename, owner="constant-team",
                      dispatched_by="constant-team")  # T-0909: attributable
        return res.get("sid")
    except ActionError as e:
        # T-0345: the parallel-session cap is normal backpressure, not a fault —
        # the work stays pending and a later tick retries when a slot frees. Don't
        # ERROR-spam the journal every 60s at N/N; defer quietly. Other
        # ActionErrors are genuine faults and keep their loud traceback.
        if "capacity reached" in str(e):
            log.debug("constant_teams: spawn deferred for %s/%s (%s)", slug, init_filename, e)
        else:
            log.exception("constant_teams: spawn failed for %s/%s", slug, init_filename)
        return None
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
        # T-0457 (next-wave #7 / T-0423 Fork-A): the always-on (no-consume) mode is
        # RETIRED — "no no-consume team can exist". A constant_team with no consume
        # source is a misconfig; staffing it would run an unbounded standing loop
        # that respawns short-lived sessions. Fail closed: warn and spawn nothing.
        log.warning(
            "constant_teams: %s/%s is constant_team:true but has no `consume` "
            "source — the always-on keep-alive mode is retired (T-0423 Fork-A). "
            "Skipping (spawning nothing); add a glob or .log consume source.",
            slug, init_stem,
        )
        return actions
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


# T-0350: a drained idle member must sit idle at least this long before reaping,
# so a just-spawned member still bringing up / mid-triage is never interrupted.
_DRAINED_MEMBER_SETTLE_SEC = float(os.environ.get("BOT_SQUAD_DRAINED_MEMBER_SETTLE_SEC") or 120)


def _parse_iso_epoch(value: Any) -> float:
    """Parse an ISO ``...Z`` timestamp to epoch seconds; 0.0 on garbage/missing."""
    try:
        from datetime import datetime, timezone
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _queue_drained(cfg: Any, slug: str, init_path: Path, fm: dict) -> bool:
    """True when a constant team's consume-queue has no unprocessed work.

    ``.log`` → no new lines past the cursor; glob → no matching files. The
    always-on (no-consume) mode is retired (T-0457), so a no-consume team is never
    staffed; this guard remains only as legacy straggler-safety — if some pre-cut
    member still lingers, treat it as never-drained (conservative: don't reap a
    member we can no longer reason about a queue for).
    """
    consume = (fm.get("consume") or "").strip()
    if not consume:
        return False
    name = fm.get("name") or init_path.stem
    state = _load_state(cfg, slug, name)
    data_dir = cfg.data_dir
    if consume.endswith(".log"):
        log_path = data_dir / slug / consume
        cursor = int(state.get("cursor_lines", 0) or 0)
        return not _log_new_lines(log_path, cursor)
    return not _glob_items(data_dir, slug, consume)


def gc_drained_members(cfg: Any, slug: str, now: float | None = None) -> dict:
    """T-0350: close the demand-driven lifecycle — reap an IDLE constant-team
    member once its consume-queue is DRAINED.

    A triage dev does its work (files/updates tickets) then sits idle at the
    composer FOREVER: ``gc_tmux_sessions`` spares a live claude pane, and
    ``archive_dead_teammates`` only catches post-totest / 24h-stale, so an
    ``owner=constant-team`` session with ``task_id: ~`` has no prompt close. With
    the queue empty and the member idle past a settle window, it has nothing left
    to do → suspend it (kills the pane; the now-empty sibling tmux session is then
    reaped fast by the T-0350 ``gc_tmux_sessions`` short grace). This is the
    empty-session leak the operator kept killing by hand.

    Conservative: a ``running`` member (mid-triage) and a just-spawned one (within
    the settle window) are always spared, so live work is never interrupted.
    """
    if now is None:
        now = time.time()
    from bot_squad_worker import sessions as S
    init_dir = cfg.data_dir / slug / "vision" / "initiatives"
    if not init_dir.exists():
        return {"reaped": []}
    try:
        rows = S.list_sessions(cfg, slug)
    except Exception:  # noqa: BLE001
        log.exception("constant_teams.gc_drained_members: list_sessions failed for %s", slug)
        return {"reaped": []}

    reaped: list[str] = []
    for init_path in sorted(init_dir.glob("*.md")):
        fm = _read_frontmatter(init_path)
        if not _truthy(fm.get("constant_team")):
            continue
        if not _queue_drained(cfg, slug, init_path, fm):
            continue  # real unprocessed feedback → members are legitimately busy
        init_stem = init_path.stem
        window_prefix = (fm.get("team_window") or fm.get("name") or init_stem)[:40]
        for r in rows:
            if r.get("status") not in _LIVE_STATUSES:
                continue
            if str(r.get("owner") or "") != "constant-team":
                continue
            init_val = Path(str(r.get("initiative") or "")).stem
            win = str(r.get("window") or "")
            if not (init_val == init_stem or (window_prefix and win.startswith(window_prefix))):
                continue
            if str(r.get("activity") or "") == "running":
                continue  # actively triaging — never interrupt mid-work
            if now - _parse_iso_epoch(r.get("started_at")) < _DRAINED_MEMBER_SETTLE_SEC:
                continue  # too fresh — could be mid-bringup
            sid = r.get("sid")
            if not sid:
                continue
            try:
                # T-0444: stamp the close so the auto-reap is visible on the
                # Processes badge (this is exactly the "ships dark" cleanup).
                S.suspend(cfg, slug, sid, source="gc_drained_member",
                          reason="auto-suspended: consume-queue drained, member idle")
                reaped.append(sid)
                log.info("constant_teams: reaped idle drained member %s (team %s)", sid, init_stem)
            except Exception:  # noqa: BLE001
                log.exception("constant_teams: reap of idle drained member %s failed", sid)
    return {"reaped": reaped}


def constant_team_stems(cfg: Any, slug: str) -> set[str]:
    """Return the set of initiative stems flagged ``constant_team: true``.

    T-0177: the team reconciler folds normal-initiative + main sessions into the
    single project team, but a *constant* team (prod-support, user-feedback —
    initiatives with ``constant_team: true``) keeps its own identity. This is the
    membership oracle for that distinction, keyed by initiative file stem (the
    same key ``_tmux_session_name`` appends to form ``<slug>-<stem>``).
    """
    init_dir = cfg.data_dir / slug / "vision" / "initiatives"
    stems: set[str] = set()
    if not init_dir.exists():
        return stems
    for init_path in sorted(init_dir.glob("*.md")):
        fm = _read_frontmatter(init_path)
        if _truthy(fm.get("constant_team")):
            stems.add(init_path.stem)
    return stems


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

    # T-0335 item-16: a shipped/archived initiative stops re-staffing its team.
    finished = _read_finished_initiatives(cfg, slug)

    all_actions: list[dict] = []
    for init_path in sorted(init_dir.glob("*.md")):
        fm = _read_frontmatter(init_path)
        if not _truthy(fm.get("constant_team")):
            continue
        # D6 guard: a retired firehose/dead-loop initiative never re-staffs, even
        # if its file was re-seeded after the on-host delete (audit item 8).
        if init_path.stem in _RETIRED_CONSTANT_TEAMS or \
                (fm.get("name") or "").strip() in _RETIRED_CONSTANT_TEAMS:
            log.debug("constant_teams: skipping retired team %s (audit item 8)",
                      init_path.stem)
            continue
        if init_path.name in finished:
            continue
        try:
            all_actions.extend(_maintain_initiative(cfg, slug, init_path, fm))
        except Exception:  # noqa: BLE001
            log.exception("constant_teams: maintain failed for %s/%s", slug, init_path.name)

    # T-0350: close the lifecycle — reap idle members whose queue has drained, so
    # a finished triage dev doesn't linger idle as an empty session. Runs after
    # the maintain (spawn) pass so a member spawned THIS tick (fresh) is protected
    # by the settle window.
    try:
        reaped = gc_drained_members(cfg, slug)["reaped"]
        for sid in reaped:
            all_actions.append({"action": "reaped_drained_member", "sid": sid})
    except Exception:  # noqa: BLE001
        log.exception("constant_teams: gc_drained_members failed for %s", slug)

    return {"actions": all_actions}
