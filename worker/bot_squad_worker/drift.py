"""T-0149: drift enforcement — keep working sessions anchored to their ticket.

Stakeholder (2026-06-02): "agents always drift from the task now, forgetting
that they needed to work for 8 hours, defer some tasks for some reason ...
IDK if it's better to prompt for this or enforce directly (guess latter if
there's a robust way to do it)." This is the enforcement path: a periodic,
system-level tick (NOT an LLM prompt) that injects a drift-check reminder into
a live session's composer when it has gone too long without touching its
ticket, or when its recent activity is off-task.

Signals (per live, task-bound session):
  1. STALE     — now - last-reported-progress > BOT_SQUAD_DRIFT_MINUTES. The
                 anchor is the latest of: the bound ticket's ``updated:`` and
                 its last ``## Progress`` note, AND (T-0735) any progress THIS
                 session reported elsewhere — a note it authored on any other
                 ticket, or a feedback submission. Only fires while the session
                 is actively working (recent jsonl activity), so a session idle
                 at a prompt is left alone.
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

T-0184 (per-project opt-in + per-session off-ramp): the worker is multi-project,
so an unconditional sweep nagged dev sessions in EVERY project — a signal-tracker
dev once received a bot-squad-style nag for an unrelated ticket (T-0034). Two
guards now bound the blast radius:
  * **Per-project**: only projects with ``drift_enforcement = true`` in
    ``config/projects.toml`` are swept. Bot-squad opts in; others stay off.
  * **Per-session**: a session can silence itself with ``bsq drift off`` (sets
    ``drift_paused: true`` in its SessionMd); the tick skips paused sessions.
Two structural guards also suppress *meaningless* nags (T-0185): constant-team /
queue-consumer sessions (no single-ticket DoD to re-anchor to) are skipped, and
a session is only nagged about a ticket whose initiative matches its own.

T-0735 (the anchor counts progress reported ANYWHERE): the STALE clock keyed
solely on the session's OWN bound ticket, so a role whose output lands on OTHER
tickets — the T-0331 dogfood loop, and any QA / audit / verification session,
which file findings against the tickets they verify rather than their own — kept
tripping it however much they reported. A recurring false positive for a whole
role class is the expensive kind: it trains everyone to dismiss the signal (a
prior dogfood session turned drift off outright over exactly this), and then a
real drift goes unseen. Fixed by broadening the anchor, NOT by exempting the
role — a verify session that reports nothing anywhere still goes stale, which is
the genuinely-stalled case worth catching.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from bot_squad_worker import frontmatter as _fm
from bot_squad_worker.actions import normalize_id

log = logging.getLogger(__name__)

# Tool-use names whose ``file_path`` input we treat as a "write" for off-task
# signal detection.
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_JSONL_TAIL_LINES = 150
_SUPERPOWERS_RE = re.compile(r"\.claude/superpowers/|/superpowers/")
_AUTOMATION_RE = re.compile(r"\.mjs$|\.spec\.|\.test\.|playwright", re.IGNORECASE)

# T-0185: constant-team / queue-consumer sessions are stamped with this owner at
# spawn (constant_teams._spawn_member). They consume a line-queue and have no
# single-ticket DoD, so a "you drifted from ticket X" nag is structurally
# meaningless for them — skip them entirely.
_CONSTANT_TEAM_OWNER = "constant-team"

# T-0190: a dev whose bound ticket is in a terminal status has reported READY and
# is awaiting TL review — it is DONE, not drifting, so the "re-anchor to the
# ticket DoD" nag is structurally meaningless for it (found dogfooding the
# T-0184/T-0185 drift bundle: the drift dev was nagged about its OWN ticket
# ~89 min after setting it to ``totest``). ``reopened`` is deliberately NOT
# terminal — when a TL reopens a ticket the work is live again and the dev
# should be re-anchored.
_TERMINAL_TICKET_STATUSES = {"totest", "closed"}

# T-0184: appended to every drift reminder so the user always has an obvious
# off-ramp. ``bsq drift off`` sets ``drift_paused: true`` on the SessionMd.
_OFF_RAMP_FOOTER = (
    "\n\n(Off-ramp: silence drift checks for THIS session with `bsq drift off` — "
    "re-enable later with `bsq drift on`.)"
)


def _initiative_stem(value: str | None) -> str:
    """Normalise an initiative ref (``foo.md`` / ``foo`` / ``~``) to its bare stem.

    The ``.md``-strip is delegated to the shared ``actions.normalize_id`` (the
    T-0424 byte-identical id-normalization contract) so the strip rule lives in
    exactly one place; this wrapper only adds the ``~``/empty/whitespace
    handling that an initiative ref needs but a bare id does not.
    """
    v = (value or "").strip()
    if not v or v == "~":
        return ""
    return normalize_id(v)


def _is_constant_team(row: dict, const_stems: set[str]) -> bool:
    """True when a session is a constant-team / queue-consumer (not a single-ticket dev).

    Two independent signals (either suffices): the ``owner: constant-team`` stamp
    written at spawn (constant_teams._spawn_member), or a primary/extra initiative
    that is flagged ``constant_team: true``. Both are checked so a session whose
    owner stamp was lost still gets recognised by its initiative, and vice-versa.
    """
    if str(row.get("owner") or "") == _CONSTANT_TEAM_OWNER:
        return True
    inits = [row.get("initiative")] + list(row.get("extra_initiatives") or [])
    return any(_initiative_stem(i) in const_stems for i in inits if _initiative_stem(i))


def _truthy(value: Any) -> bool:
    """Accept YAML/JSON-ish truthy values from SessionMd frontmatter."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


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


# T-0735: a progress note as written by ``task_progress_add`` —
# ``- <iso-ts> · <sid> · <text>``. ``_ticket_last_touch`` above matches only the
# timestamp because it reads ONE ticket; the sweep below needs the author too.
_AUTHORED_NOTE_RE = re.compile(
    r"^-\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*·\s*([^\s·]+)\s*·", re.MULTILINE
)


def _reported_progress_index(cfg: Any, slug: str) -> dict[str, float]:
    """T-0735: sid → epoch of the LATEST progress this session reported ANYWHERE.

    The staleness clock used to key solely on the session's OWN bound ticket
    (``_ticket_last_touch``, one file). That silently under-counts an entire
    class of role: a dogfood / QA / audit session's work product is findings
    filed AGAINST OTHER tickets — a note on the ticket it just verified, a
    ``bsq feedback submit``. Both are the session visibly reporting progress,
    and neither touched its own ticket, so the clock never reset and the nag
    recurred every cooldown no matter how much it reported — the T-0735
    complaint.

    NOT counted, verified rather than assumed: filing a ticket with ``task_new``
    on its own. That path stamps ``provenance:``/``created:`` into frontmatter
    and writes no progress note, and records no author SID anywhere on the md,
    so there is nothing to attribute it by without a schema change. In practice
    a filer adds a note too (which does count); the gap is recorded on T-0735
    rather than papered over.

    So the anchor becomes "time since this session last reported ANY progress".
    Deliberately NOT a role exemption: a session that reports nothing anywhere
    still goes stale on schedule, which is the genuinely-stalled case the
    stakeholder explicitly asked to keep catching.

    Sources, both keyed by the authoring SID:
      * ``backlog/*.md`` — every ``- <ts> · <sid> · <text>`` progress note.
      * ``feedback/*.md`` — the ``submitted_by`` / ``submitted_at`` frontmatter.

    Built at most once per :func:`drift_check` pass, and only when some session
    is already about to be flagged stale, so the common (fresh-ticket) path
    never pays for the sweep.
    """
    index: dict[str, float] = {}

    def _bump(sid: str, ts: float | None) -> None:
        sid = (sid or "").strip()
        if not sid or ts is None:
            return
        if ts > index.get(sid, 0.0):
            index[sid] = ts

    backlog = cfg.data_dir / slug / "backlog"
    if backlog.is_dir():
        for md in backlog.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _AUTHORED_NOTE_RE.finditer(text):
                _bump(m.group(2), _parse_iso(m.group(1)))

    feedback = cfg.data_dir / slug / "feedback"
    if feedback.is_dir():
        for md in feedback.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
            if not fm:
                continue
            by = re.search(r"^submitted_by:\s*(.+)$", fm.group(1), re.MULTILINE)
            at = re.search(r"^submitted_at:\s*(.+)$", fm.group(1), re.MULTILINE)
            if by and at:
                _bump(by.group(1), _parse_iso(at.group(1)))

    return index


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
                f"was intentional, record why with `bsq ticket note {task_id}`."
                + _OFF_RAMP_FOOTER)
    if any(_AUTOMATION_RE.search(t) for t in targets) and not _scenario_exists(cfg, slug, task_id):
        return ("automation",
                f"⚠️ DRIFT CHECK (T-0158, enforced): you appear to be writing "
                f"automation/test code before a manual walkthrough of {task_id}. "
                f"Order is: write the scenario (`bsq scenario new {task_id}`), walk it "
                f"through MANUALLY observing the real result, THEN automate. Do the "
                f"manual pass first."
                + _OFF_RAMP_FOOTER)
    if stale_min >= drift_minutes():
        # T-0735: say what the clock ACTUALLY measures. It used to claim "since
        # you last updated ticket X" while a note this session had filed on
        # ANOTHER ticket went uncounted — a verify-only role read that as a lie
        # and learned to dismiss the signal. It is now time-since-any-report,
        # and the text names every way to reset it.
        return ("stale",
                f"⚠️ DRIFT CHECK (T-0149, enforced): ~{stale_min}min since you last "
                f"reported ANY progress — no note on {task_id} ({title}) or on any "
                f"other ticket, and no feedback submitted. Re-read {task_id}'s DoD. "
                f"Are you still on the original task, or have you drifted/deferred "
                f"something? Log progress with `bsq ticket note {task_id} <text>`, or "
                f"note explicitly why you deferred. Don't lose the thread."
                + _OFF_RAMP_FOOTER)
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
    # T-0184: per-project opt-in. The worker sweeps every project; without this
    # gate a dev session in a non-opted-in project (e.g. signal-tracker) gets
    # nagged about its ticket. Only projects that explicitly opt in are enforced.
    if not getattr(project, "drift_enforcement", False):
        return {"ok": True, "skipped": "drift_enforcement_off", "nudged": []}

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

    # T-0185: initiative stems flagged ``constant_team: true`` — a session bound
    # to one of these (or stamped ``owner: constant-team``) is a queue consumer,
    # not a single-ticket dev. Resolved once per project.
    try:
        from bot_squad_worker.constant_teams import constant_team_stems
        const_stems = constant_team_stems(cfg, slug)
    except Exception:
        log.exception("drift_check: constant_team_stems failed for %s", slug)
        const_stems = set()

    # T-0735: sid → last-reported-progress-anywhere. None until a session is
    # actually about to be flagged stale (see below), then built once per pass.
    reported_index: dict[str, float] | None = None

    nudged: list[dict] = []
    for row in rows:
        if row.get("status") != "active":
            continue
        if row.get("role") != "dev":
            continue  # coordinators self-manage; bound blast radius
        # T-0185: a constant-team / queue-consumer session has no single-ticket
        # DoD to re-anchor to, so a "you drifted from ticket X" nag is
        # structurally meaningless — and the ticket it carries is usually a
        # mis-bind (p38 carried an unrelated unassigned T-0176). Skip it.
        if _is_constant_team(row, const_stems):
            continue
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

        # T-0184: per-session off-ramp. ``bsq drift off`` stamps ``drift_paused:
        # true`` on the SessionMd; honour it so a user temporarily on something
        # else (or a session that opted out) is left alone. Read the md once and
        # reuse it below for the cooldown stamp.
        md_path = S._find_session_md(sessions_dir, sid, row.get("claude_uuid"))
        meta = S._read_session_metadata(md_path) if md_path else None
        if meta is not None and _truthy(meta.get("drift_paused")):
            continue

        # T-0231: resolve by id: frontmatter, not the alphabetically-first
        # filename match — a stale/colliding ticket file must never get
        # nagged about (or worse, silently supply a WRONG title/status) in
        # place of the session's real bound ticket.
        ticket_path = _fm.resolve_id_file(backlog_dir, task_id)
        if ticket_path is None:
            continue
        title = ""
        ticket_initiative = ""
        ticket_status = ""
        fm = re.match(r"\A---\n(.*?)\n---\n", ticket_path.read_text(errors="replace"), re.DOTALL)
        if fm:
            tm = re.search(r"^title:\s*(.+)$", fm.group(1), re.MULTILINE)
            title = tm.group(1).strip() if tm else ""
            im = re.search(r"^initiative:\s*(.+)$", fm.group(1), re.MULTILINE)
            ticket_initiative = _initiative_stem(im.group(1) if im else "")
            sm = re.search(r"^status:\s*(.+)$", fm.group(1), re.MULTILINE)
            ticket_status = sm.group(1).strip().lower() if sm else ""

        # T-0190: a dev whose bound ticket is terminal (totest/closed) has reported
        # READY and is awaiting TL review — done, not drifting. Skip it. ``reopened``
        # is NOT terminal: the work is live again, so it still gets nagged. Composes
        # with the constant-team + initiative-match guards above/below.
        if ticket_status in _TERMINAL_TICKET_STATUSES:
            continue

        # T-0185: only nag about a ticket whose initiative matches the session's.
        # The p38 incident was a feedback-initiative session nagged about a ticket
        # from operator-ux-and-session-mgmt — a cross-initiative mis-bind. Guard is
        # conservative: only skip when BOTH initiatives are known AND they differ,
        # so legacy sessions/tickets with no initiative are never falsely silenced.
        session_initiative = _initiative_stem(row.get("initiative"))
        if ticket_initiative and session_initiative and ticket_initiative != session_initiative:
            continue

        last_touch = _ticket_last_touch(ticket_path) or _parse_iso(row.get("started_at")) or now
        stale_min = int((now - last_touch) / 60)

        # T-0735: the bound ticket looks stale — but "stale" must mean "reported
        # nothing ANYWHERE", not "didn't touch this one file". Consult the
        # session's own reporting record (notes on any ticket + feedback) before
        # calling it drift. Built lazily and once per pass: only a would-be nag
        # pays for the backlog sweep.
        if stale_min >= drift_minutes():
            if reported_index is None:
                reported_index = _reported_progress_index(cfg, slug)
            reported_at = reported_index.get(sid)
            if reported_at is not None and reported_at > last_touch:
                last_touch = reported_at
                stale_min = int((now - last_touch) / 60)

        jpath = _jsonl_path(row.get("cwd", ""), row.get("claude_uuid"), user_home)
        targets = _recent_write_targets(jpath) if jpath else []

        signal, text = _classify(cfg, slug, task_id, title, stale_min, targets)
        if signal is None:
            continue

        # Cooldown: read drift_checked_at from the SessionMd (md/meta already
        # resolved above for the drift_paused check).
        last_check = _parse_iso((meta or {}).get("drift_checked_at"))
        if last_check is not None and (now - last_check) < cooldown:
            continue

        try:
            # T-0578: identity threads through so the nudge paste holds the
            # per-sid mux delivery lock (never interleaves with other writers).
            S._deliver_prompt(pane.pane_id, text,
                              data_dir=cfg.data_dir, sid=sid)
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
