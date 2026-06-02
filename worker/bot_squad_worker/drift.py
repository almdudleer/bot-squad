"""T-0149: drift enforcement — keep working sessions anchored to their ticket.

Stakeholder (2026-06-02): "agents always drift from the task now, forgetting
that they needed to work for 8 hours, defer some tasks for some reason ...
IDK if it's better to prompt for this or enforce directly (guess latter if
there's a robust way to do it)." This is the enforcement path: a periodic,
system-level tick (NOT an LLM prompt) that injects a drift-check reminder into
a live session's composer when it has gone too long without touching its
ticket, or when its recent activity is off-task.

Signals (per live, task-bound session):
  1. STALE     — now - last-ticket-touch > BOT_SQUAD_DRIFT_MINUTES, where a
                 ticket-touch = the latest of the ticket md ``updated:`` and
                 its last ``## Progress`` note. Only fires while the session is
                 actively working (recent jsonl activity), so a session idle at
                 a prompt is left alone.
  2. SUPERPOWERS — recent Edit/Write to ``~/.claude/superpowers/*`` (T-0152):
                 task tracking belongs on the bot-squad ticket, not doc folders.
  3. AUTOMATION — recent write of test/automation code (``*.mjs`` / playwright /
                 ``*.test.*``) with no scenario file for the bound ticket yet
                 (T-0158): manual walkthrough must come before automation.

The reminder is delivered with the T-0144 paste-buffer primitive
(``sessions._deliver_prompt``) so a multi-line block lands as one composer
entry and submits once. A per-session ``drift_checked_at`` cooldown
(BOT_SQUAD_DRIFT_COOLDOWN_MINUTES) prevents nagging.

Kill switch: ``BOT_SQUAD_DRIFT_MINUTES=0`` disables the tick entirely. The
tick targets only ``role: dev`` sessions by default to bound blast radius;
coordinators (TL/operator) self-manage.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Tool-use names whose ``file_path`` input we treat as a "write" for off-task
# signal detection.
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_JSONL_TAIL_LINES = 150
_SUPERPOWERS_RE = re.compile(r"\.claude/superpowers/|/superpowers/")
_AUTOMATION_RE = re.compile(r"\.mjs$|\.spec\.|\.test\.|playwright", re.IGNORECASE)


def _minutes_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def drift_minutes() -> int:
    return _minutes_env("BOT_SQUAD_DRIFT_MINUTES", 45)


def cooldown_minutes() -> int:
    return _minutes_env("BOT_SQUAD_DRIFT_COOLDOWN_MINUTES", 30)


def active_window_sec() -> int:
    return _minutes_env("BOT_SQUAD_DRIFT_ACTIVE_WINDOW_SEC", 1200)


def _parse_iso(ts: str | None) -> float | None:
    """Parse an ISO-8601 ``...Z`` timestamp to epoch seconds, or None."""
    if not ts or ts == "~":
        return None
    try:
        return time.mktime(time.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return None


def _ticket_last_touch(ticket_path: Path) -> float | None:
    """Latest of the ticket's frontmatter ``updated:`` and last progress-note ts."""
    try:
        text = ticket_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    candidates: list[float] = []
    fm = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if fm:
        um = re.search(r"^updated:\s*(.+)$", fm.group(1), re.MULTILINE)
        if um:
            t = _parse_iso(um.group(1))
            if t:
                candidates.append(t)
    # Progress notes look like "- 2026-06-02T01:08:21Z · <sid> · <text>".
    for m in re.finditer(r"^-\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*·", text, re.MULTILINE):
        t = _parse_iso(m.group(1))
        if t:
            candidates.append(t)
    return max(candidates) if candidates else None


def _jsonl_path(cwd: str, claude_uuid: str | None, user_home: str) -> Path | None:
    if not claude_uuid or claude_uuid == "~":
        return None
    encoded = cwd.replace("/", "-")
    return Path(user_home) / ".claude" / "projects" / encoded / f"{claude_uuid}.jsonl"


def _recent_write_targets(jsonl_path: Path, tail_lines: int = _JSONL_TAIL_LINES) -> list[str]:
    """Extract recent WRITE targets (Edit/Write/... ``file_path``) from a transcript.

    Reads the last ``tail_lines`` records and pulls ``file_path`` inputs from
    write-tool calls only. We deliberately do NOT scan Bash command strings:
    reading/grepping a superpowers path (or a sentence mentioning ``.mjs``) is
    not drift — *writing* planning artifacts to superpowers or *creating* an
    automation file before the manual walkthrough is. Scanning command text
    false-flags the very sessions working on these features (they legitimately
    type those paths) — caught in the T-0158 manual walkthrough of this tick.
    """
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-tail_lines:]
    except OSError:
        return []
    targets: list[str] = []
    for line in lines:
        try:
            o = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if o.get("type") != "assistant":
            continue
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                continue
            inp = blk.get("input") or {}
            if blk.get("name") in _WRITE_TOOLS and isinstance(inp.get("file_path"), str):
                targets.append(inp["file_path"])
    return targets


def _scenario_exists(cfg: Any, slug: str, task_id: str) -> bool:
    scen_dir = cfg.data_dir / slug / "scenarios"
    return bool(list(scen_dir.glob(f"{task_id}-*.md"))) if scen_dir.exists() else False


def _classify(cfg: Any, slug: str, task_id: str, title: str, stale_min: int,
              targets: list[str]) -> tuple[str | None, str]:
    """Return (signal, reminder_text). signal is None when no drift detected."""
    if any(_SUPERPOWERS_RE.search(t) for t in targets):
        return ("superpowers",
                f"⚠️ DRIFT CHECK (T-0152, enforced): you're writing under "
                f"~/.claude/superpowers/* — that is where sessions lose the thread. "
                f"ALL task tracking goes on bot-squad ticket {task_id}: use "
                f"`bsq ticket note {task_id} <text>` for progress; in-session todos "
                f"only for sub-steps. Re-anchor to the ticket DoD now. If the write "
                f"was intentional, record why with `bsq ticket note {task_id}`.")
    if any(_AUTOMATION_RE.search(t) for t in targets) and not _scenario_exists(cfg, slug, task_id):
        return ("automation",
                f"⚠️ DRIFT CHECK (T-0158, enforced): you appear to be writing "
                f"automation/test code before a manual walkthrough of {task_id}. "
                f"Order is: write the scenario (`bsq scenario new {task_id}`), walk it "
                f"through MANUALLY observing the real result, THEN automate. Do the "
                f"manual pass first.")
    if stale_min >= drift_minutes():
        return ("stale",
                f"⚠️ DRIFT CHECK (T-0149, enforced): ~{stale_min}min since you last "
                f"updated ticket {task_id} ({title}). Re-read its DoD. Are you still on "
                f"the original task, or have you drifted/deferred something? Log "
                f"progress with `bsq ticket note {task_id} <text>`, or note explicitly "
                f"why you deferred. Don't lose the thread.")
    return (None, "")


def drift_check(cfg: Any, slug: str) -> dict:
    """One drift-enforcement pass for a project. Returns a summary dict.

    Idempotent + side-effecting: injects at most one reminder per drifting
    session per cooldown window. Disabled when ``BOT_SQUAD_DRIFT_MINUTES=0``.
    """
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError

    if drift_minutes() <= 0:
        return {"ok": True, "disabled": True, "nudged": []}
    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"drift_check: unknown project slug {slug!r}")

    user_home = os.path.expanduser("~")
    backlog_dir = cfg.data_dir / slug / "backlog"
    sessions_dir = cfg.data_dir / slug / "sessions"
    now = time.time()
    cooldown = cooldown_minutes() * 60
    active_window = active_window_sec()

    try:
        rows = S.list_sessions(cfg, slug)
    except Exception:
        log.exception("drift_check: list_sessions failed for %s", slug)
        return {"ok": False, "nudged": []}

    # Resolve panes once for injection (sid -> PaneInfo).
    user = S._get_current_user()
    panes = {S.compute_sid(user, p.window, p.pane_id): p for p in S.list_panes()}

    nudged: list[dict] = []
    for row in rows:
        if row.get("status") != "active":
            continue
        if row.get("role") != "dev":
            continue  # coordinators self-manage; bound blast radius
        task_id = row.get("task_id")
        if not task_id or task_id == "~":
            continue
        # Only nag a session that is actively working (recent jsonl activity);
        # one sitting idle at a prompt is waiting, not drifting.
        activity_at = row.get("activity_at")
        if activity_at is None or (now - activity_at) > active_window:
            continue
        sid = row.get("sid")
        pane = panes.get(sid)
        if pane is None:
            continue

        matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
        if not matches:
            continue
        ticket_path = matches[0]
        title = ""
        fm = re.match(r"\A---\n(.*?)\n---\n", ticket_path.read_text(errors="replace"), re.DOTALL)
        if fm:
            tm = re.search(r"^title:\s*(.+)$", fm.group(1), re.MULTILINE)
            title = tm.group(1).strip() if tm else ""

        last_touch = _ticket_last_touch(ticket_path) or _parse_iso(row.get("started_at")) or now
        stale_min = int((now - last_touch) / 60)

        jpath = _jsonl_path(row.get("cwd", ""), row.get("claude_uuid"), user_home)
        targets = _recent_write_targets(jpath) if jpath else []

        signal, text = _classify(cfg, slug, task_id, title, stale_min, targets)
        if signal is None:
            continue

        # Cooldown: read drift_checked_at from the SessionMd.
        md_path = S._find_session_md(sessions_dir, sid, row.get("claude_uuid"))
        meta = S._read_session_metadata(md_path) if md_path else None
        last_check = _parse_iso((meta or {}).get("drift_checked_at"))
        if last_check is not None and (now - last_check) < cooldown:
            continue

        try:
            S._deliver_prompt(pane.pane_id, text)
        except Exception:
            log.exception("drift_check: inject failed for %s", sid)
            continue
        nudged.append({"sid": sid, "task_id": task_id, "signal": signal, "stale_min": stale_min})

        if md_path and meta is not None:
            meta["drift_checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            try:
                S._write_session_metadata(md_path, meta, atomic=True)
            except OSError:
                log.exception("drift_check: could not persist drift_checked_at for %s", sid)

    if nudged:
        log.info("drift_check: %s nudged %d session(s): %s", slug, len(nudged), nudged)
    return {"ok": True, "nudged": nudged}
