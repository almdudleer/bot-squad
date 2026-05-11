"""Autonomous orchestrator — one task at a time, vision-anchored, DOD-reviewed.

The orchestrator runs on a 60-second tick driven by APScheduler. Each tick
runs a single state-machine step:

  idle      → pick a task, spawn a worker pane, transition to working
  working   → poll pane liveness; if dead and task is now `totest`, → reviewing
                               if dead and task is still `wip`, → idle (reopen)
  reviewing → run DOD review; approve → closed/idle, reject → open + comment/idle
  sleeping  → return (sleep window — no new spawns)

State persists in data/_worker/autonomous/{slug}.json.

Orchestrator starts DISABLED by default per slug. Stakeholder enables via UI.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Maximum time (seconds) a worker pane may run before being killed.
_TASK_TIMEOUT_SECS = 1800  # 30 minutes

# Maximum DOD rejections before a task is permanently escalated.
_MAX_FAIL_COUNT = 3

# Maximum tick log entries to keep in memory / state file.
_MAX_TICK_LOG = 50

# Maximum characters of diff to pass to the DOD reviewer.
_MAX_DIFF_CHARS = 10_000

# Maximum characters of test output to pass to the DOD reviewer.
_MAX_TEST_CHARS = 5_000


@dataclass
class AutonomousState:
    slug: str
    enabled: bool = False
    status: str = "idle"               # idle | working | reviewing | sleeping
    current_task_id: Optional[str] = None
    current_pane_id: Optional[str] = None
    current_started_at: Optional[str] = None
    last_tick_at: Optional[str] = None
    sleep_start_hour: int = 22         # UTC
    sleep_end_hour: int = 8            # UTC
    fail_counts: dict = field(default_factory=dict)
    tick_log: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def state_path(cfg: Any, slug: str) -> Path:
    """Return the path to the state JSON file for slug."""
    return cfg.data_dir / "_worker" / "autonomous" / f"{slug}.json"


def load_state(cfg: Any, slug: str) -> AutonomousState:
    """Load AutonomousState from disk; returns a fresh disabled state if missing."""
    p = state_path(cfg, slug)
    if not p.exists():
        return AutonomousState(slug=slug, enabled=False)
    try:
        raw = json.loads(p.read_text())
        # Ensure slug is correct
        raw["slug"] = slug
        return AutonomousState(
            slug=raw.get("slug", slug),
            enabled=bool(raw.get("enabled", False)),
            status=raw.get("status", "idle"),
            current_task_id=raw.get("current_task_id"),
            current_pane_id=raw.get("current_pane_id"),
            current_started_at=raw.get("current_started_at"),
            last_tick_at=raw.get("last_tick_at"),
            sleep_start_hour=int(raw.get("sleep_start_hour", 22)),
            sleep_end_hour=int(raw.get("sleep_end_hour", 8)),
            fail_counts=raw.get("fail_counts", {}),
            tick_log=raw.get("tick_log", []),
        )
    except Exception as e:
        log.warning("autonomous: could not load state for %s: %s; starting fresh", slug, e)
        return AutonomousState(slug=slug, enabled=False)


def save_state(cfg: Any, state: AutonomousState) -> None:
    """Persist AutonomousState to disk as JSON."""
    p = state_path(cfg, state.slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Keep tick_log bounded
    if len(state.tick_log) > _MAX_TICK_LOG:
        state.tick_log = state.tick_log[-_MAX_TICK_LOG:]
    p.write_text(json.dumps(asdict(state), indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_hour_utc() -> int:
    return datetime.now(timezone.utc).hour


def in_sleep_window(state: AutonomousState) -> bool:
    """Return True if current UTC hour is within the configured sleep window."""
    h = _now_hour_utc()
    s, e = state.sleep_start_hour, state.sleep_end_hour
    if s < e:
        # e.g. sleep 01:00–06:00
        return s <= h < e
    else:
        # e.g. sleep 22:00–08:00  (wraps midnight)
        return h >= s or h < e


def _log_tick(state: AutonomousState, msg: str) -> None:
    """Append a timestamped entry to state.tick_log."""
    state.tick_log.append({"ts": _now_iso(), "msg": msg})
    if len(state.tick_log) > _MAX_TICK_LOG:
        state.tick_log = state.tick_log[-_MAX_TICK_LOG:]


# ---------------------------------------------------------------------------
# Task parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


def _parse_task(path: Path) -> dict:
    """Parse a backlog markdown task file into a dict with frontmatter + body.

    Uses a minimal line-by-line parser for simple ``key: value`` frontmatter
    so the worker module doesn't need PyYAML as a dependency.
    """
    try:
        text = path.read_text()
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return {}
        # Minimal YAML parser: key: value lines only (no nested structures)
        meta: dict = {}
        for line in m.group(1).splitlines():
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            colon = line.find(":")
            if colon <= 0:
                continue
            key = line[:colon].strip()
            value = line[colon + 1:].strip()
            # Strip surrounding quotes
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            meta[key] = value
        body = m.group(2).lstrip("\n")
        return {**meta, "body": body, "path": str(path)}
    except Exception as e:
        log.debug("autonomous: _parse_task error for %s: %s", path, e)
        return {}


# ---------------------------------------------------------------------------
# Task picker
# ---------------------------------------------------------------------------

_PRIORITY_ORDER = {"must": 0, "should": 1, "could": 2}


def pick_next_task(cfg: Any, slug: str) -> Optional[dict]:
    """Return the best backlog task to work next, or None if nothing is suitable.

    Ranking:
    1. Status open or reopened (skip totest / closed / wip)
    2. Priority hint in body (must > should > could)
    3. Tasks matching current tactical priorities from vision/tactical.md get bumped
    4. Skip vague tasks (title too short, title is "TODO", or no body)
    """
    backlog_dir = cfg.data_dir / slug / "backlog"
    if not backlog_dir.exists():
        return None

    # Load tactical vision for priority bumping
    tactical_keywords: list[str] = []
    tactical_path = cfg.data_dir / slug / "vision" / "tactical.md"
    if not tactical_path.exists():
        # Also try the project repo's vision dir
        project = cfg.projects.get(slug)
        if project:
            tactical_path = Path(project.repo_path) / "ops" / "bot-squad" / "vision" / "tactical.md"
    if tactical_path.exists():
        try:
            content = tactical_path.read_text()
            # Extract simple keywords: words of 4+ chars
            tactical_keywords = [w.lower() for w in re.findall(r"\b[a-zA-Z]{4,}\b", content)][:200]
        except Exception:
            pass

    candidates = []
    for md_file in sorted(backlog_dir.glob("*.md")):
        task = _parse_task(md_file)
        if not task:
            continue
        status = task.get("status", "")
        if status not in ("open", "reopened"):
            continue
        title = str(task.get("title", "")).strip()
        body = str(task.get("body", "")).strip()

        # Skip vague tasks
        if not title or len(title) < 5 or title.upper() == "TODO" or not body:
            continue

        # Determine priority
        body_lower = body.lower()
        if "**priority: must**" in body_lower or "priority: must" in body_lower:
            prio = 0
        elif "**priority: should**" in body_lower or "priority: should" in body_lower:
            prio = 1
        elif "**priority: could**" in body_lower or "priority: could" in body_lower:
            prio = 2
        else:
            prio = 1  # default to "should"

        # Vision-anchored bump: if title/body has tactical keywords, promote
        title_lower = title.lower()
        tactical_bump = 0
        if tactical_keywords:
            title_words = set(re.findall(r"\b[a-zA-Z]{4,}\b", title_lower))
            if title_words & set(tactical_keywords):
                tactical_bump = -1  # lower sort = higher priority

        # reopened gets a slight priority boost over open
        status_order = 0 if status == "reopened" else 1

        candidates.append((prio + tactical_bump, status_order, title, task))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return candidates[0][3]


# ---------------------------------------------------------------------------
# tmux / pane operations
# ---------------------------------------------------------------------------

def spawn_worker(cfg: Any, slug: str, task: dict) -> str:
    """Spawn a tmux window running claude with the task prompt.

    Returns the pane_id string (e.g. "%42").
    """
    # Build the initial prompt
    task_id = task.get("id", "unknown")
    task_title = task.get("title", "")
    task_body = task.get("body", "")

    # Try to load vision context
    tactical_ctx = ""
    strategy_ctx = ""
    project = cfg.projects.get(slug)
    if project:
        tp = Path(project.repo_path) / "ops" / "bot-squad" / "vision" / "tactical.md"
        sp = Path(project.repo_path) / "ops" / "bot-squad" / "vision" / "strategy.md"
        if tp.exists():
            tactical_ctx = tp.read_text()
        if sp.exists():
            lines = sp.read_text().split("\n")
            strategy_ctx = "\n".join(lines[:30])

    prompt = (
        f"You are working on backlog task {task_id}: {task_title}.\n\n"
        f"## Task body\n\n{task_body}\n\n"
    )
    if tactical_ctx:
        prompt += f"## Vision context (do not deviate)\n\n{tactical_ctx}\n\n"
    if strategy_ctx:
        prompt += f"## Strategy (first 30 lines)\n\n{strategy_ctx}\n\n"
    prompt += (
        "## What to do\n\n"
        "1. Read AGENTS.md if you haven't already.\n"
        "2. Implement the task within the constraints. NEVER push, NEVER merge, NEVER amend.\n"
        "3. Commit on bot_squad/dev.\n"
        "4. Run tests; fix until green.\n"
        "5. Request a deploy: ops/bot-squad-bin/deploy staging \"<reason tied to task ID>\"\n"
        f"6. Update the task status to `totest` (task ID: {task_id}).\n"
        "7. Add a comment explaining what you shipped.\n"
        "8. Stop. Do not start another task; the orchestrator handles that.\n"
    )

    window_name = f"auto-{task_id}"

    # Create a new tmux window; get its pane id
    result = subprocess.run(
        ["tmux", "new-window", "-d", "-n", window_name, "-P", "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tmux new-window failed: {result.stderr}")

    pane_id = result.stdout.strip()
    if not pane_id:
        raise RuntimeError("tmux new-window returned empty pane_id")

    # Launch claude in the new window
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, "claude", "Enter"],
        check=False,
    )
    # Wait a moment for claude to start, then send the prompt
    time.sleep(2)
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, "--", prompt, "Enter"],
        check=False,
    )

    return pane_id


def pane_alive(pane_id: str) -> bool:
    """Return True if the tmux pane is still alive."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", pane_id],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def read_task_status(cfg: Any, slug: str, task_id: str) -> str:
    """Re-read the task file's frontmatter to get the current status."""
    backlog_dir = cfg.data_dir / slug / "backlog"
    for md_file in backlog_dir.glob("*.md"):
        if md_file.name.startswith(task_id):
            task = _parse_task(md_file)
            return task.get("status", "")
    # If exact prefix not found, try substring match
    for md_file in backlog_dir.glob("*.md"):
        task = _parse_task(md_file)
        if task.get("id") == task_id:
            return task.get("status", "")
    return ""


# ---------------------------------------------------------------------------
# DOD review
# ---------------------------------------------------------------------------

def run_dod_review(cfg: Any, slug: str, task: dict) -> dict:
    """Run a DOD review via a one-shot claude -p invocation.

    Returns {"approved": bool, "rationale": str}.
    Uses subprocess with captured output; the critical rule: DOD review
    MUST use `claude -p` non-interactive, one-shot prompt.
    """
    task_id = task.get("id", "unknown")
    task_body = task.get("body", "")

    # Gather diff (since task started or just HEAD~1)
    diff_stat = ""
    diff_text = ""
    project = cfg.projects.get(slug)
    if project:
        repo = str(project.repo_path)
        r1 = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD", "--stat"],
            capture_output=True, text=True, cwd=repo,
        )
        diff_stat = r1.stdout[:2000] if r1.returncode == 0 else ""
        r2 = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD"],
            capture_output=True, text=True, cwd=repo,
        )
        diff_text = r2.stdout[:_MAX_DIFF_CHARS] if r2.returncode == 0 else ""

    review_prompt = (
        f"You are reviewing backlog task {task_id} for compliance with the "
        "project's definition of done before it's marked `closed`.\n\n"
        f"## Task body\n{task_body}\n\n"
        f"## Diff (stat)\n{diff_stat}\n\n"
        f"## Diff\n{diff_text}\n\n"
        "## Decision framework\n"
        "1. Does the diff implement what the task body asked for?\n"
        "2. Are tests green?\n"
        "3. Does the work respect the project's vision/north-star?\n"
        "4. Are there obvious regressions or scope creep?\n\n"
        "Reply with EXACTLY ONE of:\n"
        "✅ APPROVE — <one-sentence rationale>\n"
        "❌ REJECT — <specific feedback for the worker>\n\n"
        "Do not run any tool calls. This is a one-shot review.\n"
    )

    try:
        result = subprocess.run(
            ["claude", "-p", review_prompt],
            capture_output=True, text=True, timeout=120,
        )
        reply = result.stdout.strip()
    except Exception as e:
        log.warning("autonomous: DOD review subprocess failed: %s", e)
        return {"approved": False, "rationale": f"review failed: {e}"}

    if reply.startswith("✅ APPROVE") or reply.startswith("✅APPROVE"):
        rationale = reply[len("✅ APPROVE"):].lstrip(" —").strip()
        return {"approved": True, "rationale": rationale}
    else:
        # Strip the REJECT prefix if present
        rationale = reply
        for prefix in ("❌ REJECT —", "❌ REJECT—", "❌REJECT —", "❌REJECT—", "❌ REJECT", "❌REJECT"):
            if reply.startswith(prefix):
                rationale = reply[len(prefix):].strip()
                break
        return {"approved": False, "rationale": rationale or reply}


# ---------------------------------------------------------------------------
# Task status update helpers
# ---------------------------------------------------------------------------

def _reopen_task(cfg: Any, slug: str, task_id: str, comment: str) -> None:
    """Set a task status back to open and append a comment."""
    backlog_dir = cfg.data_dir / slug / "backlog"
    target: Optional[Path] = None
    for md_file in backlog_dir.glob("*.md"):
        task = _parse_task(md_file)
        if task.get("id") == task_id:
            target = md_file
            break
        if md_file.name.startswith(task_id):
            target = md_file
            break

    if target is None:
        log.warning("autonomous: _reopen_task: task %s not found in %s", task_id, backlog_dir)
        return

    try:
        from app.markdown_writer import write_task_patch  # type: ignore[import]
        write_task_patch(target, {"status": "open"}, comment)
    except ImportError:
        # Worker doesn't have api package; do it manually
        _patch_task_file(target, {"status": "open"}, comment)


def _close_task(cfg: Any, slug: str, task_id: str) -> None:
    """Set a task status to closed."""
    backlog_dir = cfg.data_dir / slug / "backlog"
    target: Optional[Path] = None
    for md_file in backlog_dir.glob("*.md"):
        task = _parse_task(md_file)
        if task.get("id") == task_id:
            target = md_file
            break
        if md_file.name.startswith(task_id):
            target = md_file
            break

    if target is None:
        log.warning("autonomous: _close_task: task %s not found in %s", task_id, backlog_dir)
        return

    _patch_task_file(target, {"status": "closed"}, None)


def _patch_task_file(path: Path, updates: dict, comment: Optional[str]) -> None:
    """Minimal in-place frontmatter patch + optional comment append.

    Only patches simple key: value lines; preserves structure of the rest.
    """
    text = path.read_text()
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return

    # Parse frontmatter lines, updating matching keys
    fm_lines = m.group(1).splitlines()
    updated_keys = set(updates.keys())
    new_fm_lines = []
    for line in fm_lines:
        stripped = line.rstrip()
        colon = stripped.find(":")
        if colon > 0:
            key = stripped[:colon].strip()
            if key in updated_keys:
                new_fm_lines.append(f"{key}: {updates[key]}")
                updated_keys.discard(key)
                continue
        new_fm_lines.append(stripped)

    # Append any keys that were not already present
    for key in list(updates.keys()):
        if key in updated_keys:
            new_fm_lines.append(f"{key}: {updates[key]}")
    # Add updated timestamp
    # Overwrite or add updated field
    ts = _now_iso()
    has_updated = any(ln.startswith("updated:") for ln in new_fm_lines)
    if has_updated:
        new_fm_lines = [f"updated: {ts}" if ln.startswith("updated:") else ln
                        for ln in new_fm_lines]
    else:
        new_fm_lines.append(f"updated: {ts}")

    body = m.group(2).lstrip("\n")
    new_fm = "\n".join(new_fm_lines)
    new_text = f"---\n{new_fm}\n---\n\n{body}"

    if comment:
        new_text = new_text.rstrip("\n") + f"\n\n---\n\n**[orchestrator {ts}]** {comment}\n"

    path.write_text(new_text)


# ---------------------------------------------------------------------------
# State machine: tick
# ---------------------------------------------------------------------------

def tick(cfg: Any, slug: str) -> None:
    """Run one state-machine step for the given project slug.

    Called by the APScheduler job every 60 seconds.
    """
    state = load_state(cfg, slug)
    state.last_tick_at = _now_iso()

    if not state.enabled:
        save_state(cfg, state)
        return

    if in_sleep_window(state):
        if state.status != "sleeping":
            state.status = "sleeping"
            _log_tick(state, "entered sleep window")
            log.debug("autonomous[%s]: sleeping (sleep window)", slug)
        save_state(cfg, state)
        return

    # If we were sleeping and now outside the window, reset to idle
    if state.status == "sleeping":
        state.status = "idle"
        _log_tick(state, "left sleep window → idle")

    if state.status == "idle":
        _tick_idle(cfg, state, slug)
    elif state.status == "working":
        _tick_working(cfg, state, slug)
    elif state.status == "reviewing":
        _tick_reviewing(cfg, state, slug)
    else:
        # Unknown status — reset
        log.warning("autonomous[%s]: unknown status %r, resetting to idle", slug, state.status)
        state.status = "idle"
        _log_tick(state, f"unknown status {state.status!r} → reset to idle")

    save_state(cfg, state)


def _tick_idle(cfg: Any, state: AutonomousState, slug: str) -> None:
    task = pick_next_task(cfg, slug)
    if task is None:
        _log_tick(state, "idle: no suitable task found")
        log.debug("autonomous[%s]: idle, no task available", slug)
        return

    task_id = task.get("id", "unknown")
    log.info("autonomous[%s]: idle → spawning worker for task %s", slug, task_id)

    try:
        pane_id = spawn_worker(cfg, slug, task)
        state.status = "working"
        state.current_task_id = task_id
        state.current_pane_id = pane_id
        state.current_started_at = _now_iso()
        _log_tick(state, f"idle: picked {task_id}, spawned pane {pane_id} → working")
    except Exception as e:
        log.error("autonomous[%s]: spawn_worker failed: %s", slug, e)
        _log_tick(state, f"idle: spawn failed for {task_id}: {e}")


def _tick_working(cfg: Any, state: AutonomousState, slug: str) -> None:
    task_id = state.current_task_id or ""
    pane_id = state.current_pane_id or ""

    # Check time cap
    if state.current_started_at:
        try:
            started = datetime.fromisoformat(state.current_started_at)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed > _TASK_TIMEOUT_SECS:
                log.warning("autonomous[%s]: task %s timed out after %.0fs", slug, task_id, elapsed)
                _log_tick(state, f"working: task {task_id} timed out after {elapsed:.0f}s — killing pane")
                if pane_id:
                    subprocess.run(["tmux", "kill-pane", "-t", pane_id], check=False)
                _reopen_task(cfg, slug, task_id,
                             f"auto-killed after {int(elapsed)}s — task likely too large or stuck")
                state.status = "idle"
                state.current_task_id = None
                state.current_pane_id = None
                state.current_started_at = None
                return
        except Exception as e:
            log.debug("autonomous[%s]: could not check timeout: %s", slug, e)

    alive = pane_alive(pane_id) if pane_id else False

    if alive:
        _log_tick(state, f"working: pane {pane_id} still alive")
        return

    # Pane died — check task status
    task_status = read_task_status(cfg, slug, task_id) if task_id else ""
    log.info("autonomous[%s]: pane %s dead; task %s status=%r", slug, pane_id, task_id, task_status)

    if task_status == "totest":
        state.status = "reviewing"
        _log_tick(state, f"working: pane dead, task {task_id} is totest → reviewing")
    else:
        # Pane died but task wasn't moved to totest — reopen
        _log_tick(state, f"working: pane dead, task {task_id} status={task_status!r} → reopen + idle")
        if task_id:
            _reopen_task(cfg, slug, task_id, f"worker pane exited before setting status=totest (status={task_status!r})")
        state.status = "idle"
        state.current_task_id = None
        state.current_pane_id = None
        state.current_started_at = None


def _tick_reviewing(cfg: Any, state: AutonomousState, slug: str) -> None:
    task_id = state.current_task_id or ""
    if not task_id:
        log.warning("autonomous[%s]: reviewing state with no current_task_id; resetting", slug)
        state.status = "idle"
        return

    # Need the task dict for DOD review
    task: dict = {}
    backlog_dir = cfg.data_dir / slug / "backlog"
    for md_file in backlog_dir.glob("*.md"):
        t = _parse_task(md_file)
        if t.get("id") == task_id or md_file.name.startswith(task_id):
            task = t
            break

    if not task:
        log.warning("autonomous[%s]: reviewing but task %s not found; resetting", slug, task_id)
        _log_tick(state, f"reviewing: task {task_id} not found → idle")
        state.status = "idle"
        state.current_task_id = None
        state.current_pane_id = None
        state.current_started_at = None
        return

    _log_tick(state, f"reviewing: running DOD review for task {task_id}")
    result = run_dod_review(cfg, slug, task)

    fail_count = state.fail_counts.get(task_id, 0)

    if result["approved"]:
        log.info("autonomous[%s]: DOD approved task %s", slug, task_id)
        _close_task(cfg, slug, task_id)
        _log_tick(state, f"reviewing: approved task {task_id} → closed; idle")
        state.status = "idle"
        state.current_task_id = None
        state.current_pane_id = None
        state.current_started_at = None
        # Reset fail count on success
        state.fail_counts.pop(task_id, None)
    else:
        fail_count += 1
        state.fail_counts[task_id] = fail_count
        log.info("autonomous[%s]: DOD rejected task %s (fail %d)", slug, task_id, fail_count)

        comment = f"DOD reviewer rejected (attempt {fail_count}): {result['rationale']}"

        if fail_count >= _MAX_FAIL_COUNT:
            # Permanently escalate
            comment += f"\n\n**Escalated after {fail_count} rejections — requires human review.**"
            _patch_task_file(
                Path(task["path"]),
                {"status": "reopened"},
                comment,
            )
            _log_tick(state, f"reviewing: task {task_id} failed {fail_count}x → reopened + TG ping")
            # TG ping
            try:
                from bot_squad_worker.actions import _get_tg_client
                tg = _get_tg_client(cfg)
                project = cfg.projects.get(slug)
                if project:
                    tg.send(
                        chat_id=project.tg_chat,
                        text=f"[autonomous] task {task_id} failed DOD review {fail_count}x — needs human review",
                        sid="autonomous",
                    )
            except Exception as e:
                log.warning("autonomous: TG ping failed: %s", e)
        else:
            _reopen_task(cfg, slug, task_id, comment)
            _log_tick(state, f"reviewing: rejected task {task_id} → open + comment; idle")

        state.status = "idle"
        state.current_task_id = None
        state.current_pane_id = None
        state.current_started_at = None
