"""T-0950: time-in-state deadlines + breach alerts for human/operator-gated
ticket statuses.

The audit finding this closes (fable workflow lifecycle-loop-audit,
wf_9db98295-3b9, 2026-08-31): T-0945 built a deadline+criteria mechanism for
SESSIONS only (``idle_timeout.recycle_plan``). For TICKETS, nothing sweeps age
in ``blocked_on_user`` / ``to_accept`` / ``totest`` — the three statuses whose
exit needs a human or operator to notice and act — so a ticket can sit in any
of them for weeks in total silence. ``pickup.py`` already computes a
``stale:{N}d`` sanity flag for every row, but a mechanical reject always wins
band assignment for these three statuses (they are outside
``PICKUP_STATUSES``), so the staleness signal lands in the ``excluded`` band
where nothing reads it (verified in the finding: pickup.py:412-455).

This module is deliberately narrow: it names a deadline per gated status, and
alerts ONCE per continuous stay past it — no new pickup band, no change to
orchestration-demand predicates (``task_states.PARKED_STATES`` /
``ACTIVE_STATES`` answer "does this need a session"; this answers a different
question, "does exiting this status need a human to notice, and how long has
nobody"). ``paused`` is deliberately excluded from :data:`DEFAULT_DEADLINE_SEC`
— a session choosing to set work aside is not the same as the system silently
waiting on somebody.

MEASURING THE WAIT
-------------------
Age is measured from ``status_since`` — a new frontmatter field stamped by
BOTH ticket-status writers (``api/app/markdown_writer.py::merge_task_update``
and ``scripts/cli/bsq``'s ``cmd_ticket_update``) the moment ``status`` actually
changes, never on an unrelated field write. ``updated`` was not usable for
this: every writer on this repo's tickets — a stakeholder quote, a context
rewrite, a progress note — bumps ``updated`` via the very same
``merge_task_update``/CLI write path, so using it would reset the clock on any
touch, not just the touch that matters (T-0950's whole point is measuring how
long NOBODY touched it). A ticket predating this feature carries no
``status_since`` — that is an explicit unknown, not evidence of a fresh entry,
so age falls back to ``updated`` then ``created`` (the same chain
``pickup.py::rank_row`` already uses for its own staleness measure) and the
alert names which field it used.

ALERTING
--------
One sweep (:func:`deadline_check_tick`, all projects) computes every gated
ticket's age against its status's deadline. A ticket that clears the deadline
is a BREACH; breaches batch into one message per project sweep (digest-style,
mirroring ``task_chat.lifecycle_tick_one``) and page through the
``actions._send_stakeholder_dm`` SSOT with ``msg_type="ticket_deadline"``
(URGENT class in ``msg_routes.TYPES`` — a parked ticket nobody is answering is
exactly "work is stopped unless a human acts").

Dedup is keyed on ``(ticket_id, status, status_since)`` via a sidecar
(``data/_worker/status_deadlines/<slug>.json``): the SAME stay alerts exactly
once, but a ticket that resolves and later re-enters the same gated status
(fresh ``status_since``) gets a fresh clock and a fresh alert — matching
``task_chat``'s "unchanged status never re-fires, an interesting one always
does" contract. An undelivered send (quiet hours: ``{ok: True, sent: False}``)
leaves the sidecar unstamped so the breach retries every tick until delivered,
same as the lifecycle notifier.

Kill switch: ``BOT_SQUAD_TICKET_DEADLINES=0``.

PAGE CADENCE vs SWEEP CADENCE (T-0752/operator review, 2026-09-06)
-------------------------------------------------------------------
The 300s scheduler tick is a SWEEP interval — how often the board is
re-checked — not a PAGE interval. The first live sweep found 56 of 63 gated
tickets already breaching (a pre-existing backlog, not a bug), which the
:data:`MESSAGE_CAP` batching turns into 6 pages inside 30 minutes at the raw
300s cadence — individually correct, but the T-0820/T-0834 shape: an alarm
that fires repeatedly gets muted, and the true positive it exists for stops
landing. :func:`page_min_interval_sec` decouples the two: the sweep still
runs every 300s and still rotates through the capped backlog, but an actual
PAGE for a project fires at most once per :data:`DEFAULT_PAGE_MIN_INTERVAL_SEC`
— so a steady-state breach (rare enough that the previous page is already
outside the window) still pages promptly, while a backlog catch-up drains as
readable hourly digests instead of a half-hour siren burst. The header
already carries the true total on every page, so throttling the SEND never
hides the size of the problem — it only paces how often he is asked to look.

AUTO-PAGE SCOPE (T-0950 reopen, stakeholder verdict 2026-09-06T17:21Z)
------------------------------------------------------------------------
The page-cadence fix above was already live in the running coordinator
BEFORE his reopen landed — it fixed a real bug, but it did not fix his
complaint, which rejects the mechanism, not its rate. His own words:
"Мне стали приходить какие-то дикие сообщения постоянно причём. У нас же
нет дедлайна у большинства тикетов, не понял, зачем это. Скоуп у нас
резолвится через приоритеты и статусы хорошо" — most tickets carry no
deadline in his mental model, and scope already resolves fine through
priority + status. A `totest`/`to_accept` ticket aged past its deadline is
his own backlog, managed by him on his own schedule — paging him about it is
a report that nothing is wrong, and at steady backlog depth (100+ tickets,
weeks old) it never stops firing.

`blocked_on_user` is different in kind, not degree: it means the SYSTEM
CANNOT PROCEED without him — a block, not a deadline — and silence there
costs him work rather than saving him noise. So per the operator's steer,
:data:`AUTO_PAGE_STATUSES` narrows the PUSH half of this module to
`blocked_on_user` only. `to_accept`/`totest` keep the exact same age/deadline
math (still exercised by every test written against :data:`GATED_STATUSES`)
but are pull-only now — see :func:`aging_report`, reachable on demand via the
`deadline_aging_report` action (`bsq task aging`) and never sent
unsolicited. The reading survives; the interruption doesn't.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# The three statuses the audit named — exit needs a human or operator to
# notice and act. `paused` is deliberately excluded: see module docstring.
DEFAULT_DEADLINE_SEC: dict[str, int] = {
    "blocked_on_user": 24 * 3600,   # a question the stakeholder never noticed
    "to_accept": 24 * 3600,         # the operator's queue between dev and human
    "totest": 72 * 3600,            # the human's own review backlog
}

_ENV_OVERRIDE: dict[str, str] = {
    "blocked_on_user": "BOT_SQUAD_DEADLINE_BLOCKED_ON_USER_SEC",
    "to_accept": "BOT_SQUAD_DEADLINE_TO_ACCEPT_SEC",
    "totest": "BOT_SQUAD_DEADLINE_TOTEST_SEC",
}

GATED_STATUSES = frozenset(DEFAULT_DEADLINE_SEC)

# T-0950 redesign (operator steer, 2026-09-06T23:11Z, following his 17:21Z
# reopen): the ONLY status whose breach auto-pages. `blocked_on_user` means
# the system cannot proceed without him; `to_accept`/`totest` are backlog he
# manages via priority/status, not deadline pressure — see the module
# docstring's "AUTO-PAGE SCOPE". Deadline math + tests still cover all of
# GATED_STATUSES; this only narrows the PUSH sweep.
AUTO_PAGE_STATUSES = frozenset({"blocked_on_user"})

#: Minimum gap between two PAGES for the same project (never between sweeps —
#: see the module docstring's "PAGE CADENCE vs SWEEP CADENCE"). 1h by default:
#: long enough that a multi-ticket backlog catch-up reads as an hourly digest
#: rather than a siren burst, short enough that a genuine new breach is never
#: sitting silent for more than an hour once it clears its own 24h/72h
#: deadline — a rounding error at that timescale, not a new silence.
DEFAULT_PAGE_MIN_INTERVAL_SEC = 3600
_PAGE_MIN_INTERVAL_ENV = "BOT_SQUAD_TICKET_DEADLINE_PAGE_MIN_INTERVAL_SEC"


def deadline_sec(status: str) -> Optional[int]:
    """Env-tunable deadline for ``status``, or ``None`` when it isn't gated."""
    if status not in DEFAULT_DEADLINE_SEC:
        return None
    raw = os.environ.get(_ENV_OVERRIDE[status])
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_DEADLINE_SEC[status]


def page_min_interval_sec() -> int:
    raw = os.environ.get(_PAGE_MIN_INTERVAL_ENV)
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_PAGE_MIN_INTERVAL_SEC


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds for an ISO-8601 timestamp, or ``None`` for absent/garbage.
    Mirrors ``pickup._parse_iso`` — same tolerant Z-handling, same UTC-naive
    reading, kept as a local copy rather than a cross-module private import."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _cut_title(title: str, cap: int = 60) -> str:
    t = (title or "").strip()
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


def _fmt_age(age_sec: float) -> str:
    days = age_sec / 86400.0
    if days >= 1:
        return f"{days:.1f}d"
    return f"{age_sec / 3600.0:.1f}h"


# T-0752/operator review (2026-09-06): the first live sweep found 56 of 63
# gated tickets already breaching (a backlog that predates this feature, not
# a bug — only 3 tickets on the whole board carried `status_since` yet, so
# almost everything fell back to `updated`). Rendered one line per ticket that
# is a 5756-character, 56-line message — a dump, not the "digest-style" batch
# DoD item 3 promised. Mirrors `task_chat.DIGEST_HEADLINES`'s cap+overflow
# shape for the same reason: a message nobody can act on is not "reaching
# him" in the sense that matters, even though nothing was lost in transport
# (`_send_stakeholder_dm` chunks through `split_for_tg` either way).
#
# The fix caps what one message NAMES, never what exists: every breach still
# computed above is real and still owed an alert (see `deadline_check_tick_one`
# — only the capped, shown subset gets marked alerted in the sidecar), so the
# tickets left out of THIS message surface in a LATER sweep instead of being
# silently absorbed into a count forever. At the 300s tick cadence a 56-ticket
# backlog fully surfaces, 10 named tickets at a time, inside 30 minutes.
MESSAGE_CAP = 10


def _compose_message(breaches: list[dict]) -> tuple[str, list[dict]]:
    """``(text, shown)`` — ``shown`` is the subset actually named in ``text``,
    oldest (longest-overdue) first; that is also the only subset the caller
    should mark alerted."""
    ordered = sorted(breaches, key=lambda b: b["age_sec"], reverse=True)
    shown = ordered[:MESSAGE_CAP]
    lines = [
        f"⏳ {b['id']} {b['status']} for {_fmt_age(b['age_sec'])} "
        f"(since {b['since_source']}) — {_cut_title(b['title'])}"
        for b in shown
    ]
    overflow = len(ordered) - len(shown)
    if overflow > 0:
        lines.insert(
            0,
            f"⏳ {len(ordered)} ticket(s) past deadline, oldest "
            f"{_fmt_age(ordered[0]['age_sec'])} — showing the {len(shown)} oldest:",
        )
        lines.append(f"+{overflow} more past deadline — see the board")
    return "\n".join(lines), shown


def _scan_backlog(cfg: Any, slug: str) -> list[dict]:
    """Light frontmatter rows for every gated-status ticket on ``slug``'s
    board. Top-level ``*.md`` only (the ``_gc/`` archive subdir is naturally
    excluded); a corrupt md is skipped, never fatal to the sweep."""
    from bot_squad_worker import frontmatter as fm

    backlog = Path(cfg.data_dir) / slug / "backlog"
    rows: list[dict] = []
    if not backlog.is_dir():
        return rows
    for p in sorted(backlog.glob("*.md")):
        try:
            parsed = fm.parse_or_none(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not parsed:
            continue
        meta = parsed[0]
        status = str(meta.get("status") or "").strip()
        if status not in GATED_STATUSES:
            continue
        task_id = str(meta.get("id") or "").strip()
        if not task_id:
            continue
        rows.append({
            "id": task_id,
            "title": str(meta.get("title") or "").strip(),
            "status": status,
            "status_since": str(meta.get("status_since") or "").strip(),
            "updated": str(meta.get("updated") or "").strip(),
            "created": str(meta.get("created") or "").strip(),
        })
    return rows


def _since(row: dict) -> tuple[Optional[float], Optional[str], str]:
    """``(epoch, iso_used, source)`` for how long ``row`` has held its status.

    ``status_since`` is the real signal; a ticket predating this feature (or
    written by a path that somehow missed the stamp) falls back to
    ``updated`` then ``created`` — the same explicit chain ``pickup.rank_row``
    uses for its own staleness measure — and ``source`` says which one fired,
    so a fallback-based alert is visibly a proxy, not silently treated as
    "just entered" (which would suppress it) or "unmeasurable" (which would
    drop it)."""
    for key in ("status_since", "updated", "created"):
        iso = row.get(key) or ""
        epoch = _parse_iso(iso)
        if epoch is not None:
            return epoch, iso, key
    return None, None, "unknown"


def _sidecar_path(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / "_worker" / "status_deadlines" / f"{slug}.json"


def _read_state(cfg: Any, slug: str) -> tuple[dict[str, dict], Optional[float]]:
    """``(tasks, last_paged_at)`` — ``tasks`` is ``{task_id: {"status":...,
    "since":...}}`` for the last stay already alerted on; ``last_paged_at`` is
    the epoch of the last successful PAGE for this project (see the module
    docstring's "PAGE CADENCE vs SWEEP CADENCE"), or ``None`` if never paged.
    Missing/corrupt file = both empty/None (worst case a breach re-alerts once
    more, or a page fires promptly instead of waiting out a lost interval —
    never a crash, never a blast)."""
    try:
        raw = json.loads(_sidecar_path(cfg, slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None
    tasks_raw = raw.get("tasks") if isinstance(raw, dict) else None
    tasks: dict[str, dict] = {}
    if isinstance(tasks_raw, dict):
        for k, v in tasks_raw.items():
            if isinstance(v, dict) and "status" in v and "since" in v:
                tasks[str(k)] = {"status": str(v["status"]), "since": str(v["since"])}
    lp = raw.get("last_paged_at") if isinstance(raw, dict) else None
    last_paged_at = float(lp) if isinstance(lp, (int, float)) else None
    return tasks, last_paged_at


def _write_state(cfg: Any, slug: str, tasks: dict[str, dict],
                  last_paged_at: Optional[float]) -> None:
    p = _sidecar_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"tasks": tasks, "updated_at": _now_iso()}
    if last_paged_at is not None:
        payload["last_paged_at"] = last_paged_at
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _read_sidecar(cfg: Any, slug: str) -> dict[str, dict]:
    """The ``tasks`` half of :func:`_read_state` — kept as its own name
    because it is what the tests (and any future reader) actually want most
    of the time: which stays are already alerted."""
    tasks, _ = _read_state(cfg, slug)
    return tasks


def _write_sidecar(cfg: Any, slug: str, tasks: dict[str, dict]) -> None:
    """Write ``tasks`` while PRESERVING whatever ``last_paged_at`` is already
    on disk — a tasks-only write (e.g. dropping a resolved ticket) must never
    erase the page-cadence bookkeeping alongside it."""
    _, last_paged_at = _read_state(cfg, slug)
    _write_state(cfg, slug, tasks, last_paged_at)


def _notify(cfg: Any, slug: str, text: str) -> bool:
    """Deliver one breach batch through the page SSOT. URGENT on purpose — a
    gated ticket nobody is answering is exactly the "work is stopped unless a
    human acts" class ``msg_routes`` reserves for the siren, and burying it in
    the LOG class would reproduce the exact silence this ticket exists to end.
    Returns True only when actually delivered; a quiet-hours drop
    (``{ok: True, sent: False}``) must NOT stamp the sidecar so the breach
    retries next tick and lands on the first deliverable one."""
    try:
        from bot_squad_worker.actions import _send_stakeholder_dm

        project = cfg.projects.get(slug)
        chat_id = getattr(project, "tg_chat", "") if project else ""
        res = _send_stakeholder_dm(
            cfg, message=text, urgent=True, tg_chat_id=chat_id,
            slug=slug, msg_type="ticket_deadline",
        )
        return bool(res.get("ok")) and bool(res.get("sent"))
    except Exception:  # noqa: BLE001 — a channel outage never kills the sweep
        log.exception("status_deadlines: notify failed [%s]", slug)
        return False


def deadline_check_tick_one(cfg: Any, slug: str, now: Optional[float] = None) -> dict:
    """One sweep for one project. Returns a small audit dict."""
    if now is None:
        now = time.time()
    rows = _scan_backlog(cfg, slug)
    state, last_paged_at = _read_state(cfg, slug)
    new_state = dict(state)
    breaches: list[dict] = []

    seen: set[str] = set()
    for r in rows:
        seen.add(r["id"])
        if r["status"] not in AUTO_PAGE_STATUSES:
            # T-0950 redesign: to_accept/totest are pull-only now (see
            # aging_report) — `seen` still tracks them so a resolved ticket's
            # stale sidecar entry (from before this redesign) still gets
            # garbage-collected below, but they never generate a page.
            continue
        epoch, since_iso, source = _since(r)
        if epoch is None:
            continue  # no usable timestamp at all — nothing to measure against
        deadline = deadline_sec(r["status"])
        if deadline is None or (now - epoch) < deadline:
            continue
        prior = state.get(r["id"])
        if prior and prior["status"] == r["status"] and prior["since"] == since_iso:
            continue  # this exact stay already alerted
        breaches.append({**r, "age_sec": now - epoch, "since_iso": since_iso,
                          "since_source": source})

    # A ticket that left its gated status (resolved, or moved elsewhere)
    # drops out of the sidecar, so a future re-entry gets a fresh clock.
    for gone in set(new_state) - seen:
        del new_state[gone]
    # Likewise a ticket still on the board but no longer in a gated status.
    live_gated = {r["id"] for r in rows}
    for gone in set(new_state) - live_gated:
        new_state.pop(gone, None)

    delivered = False
    rate_limited = False
    if breaches:
        min_interval = page_min_interval_sec()
        if last_paged_at is not None and (now - last_paged_at) < min_interval:
            # Sweep and rotation still happened above (breaches is real,
            # `new_state` may still have dropped resolved tickets below) — only
            # the PAGE is held back, so a steady-state breach that arrives well
            # outside the last page's window is never delayed by this branch.
            rate_limited = True
        else:
            text, shown = _compose_message(breaches)
            delivered = _notify(cfg, slug, text)
            if delivered:
                # Only the NAMED subset is marked alerted — a breach left out
                # by the cap stays pending so it is named in a later sweep
                # instead of disappearing into "+N more" forever.
                for b in shown:
                    new_state[b["id"]] = {"status": b["status"], "since": b["since_iso"]}
                last_paged_at = now

    if new_state != state:
        # A successful page always adds at least one entry to `new_state`
        # (the shown subset), so this also covers persisting the fresh
        # `last_paged_at` — the two never change independently.
        _write_state(cfg, slug, new_state, last_paged_at)
    return {"ok": True, "slug": slug, "breaches": len(breaches),
            "delivered": delivered, "rate_limited": rate_limited}


def deadline_check_tick(cfg: Any) -> dict:
    """All-projects sweep, scheduler entrypoint. Kill switch:
    ``BOT_SQUAD_TICKET_DEADLINES=0``."""
    if os.environ.get("BOT_SQUAD_TICKET_DEADLINES", "").strip() == "0":
        return {"ok": True, "disabled": True}
    out: dict[str, Any] = {"ok": True, "projects": {}}
    for slug in cfg.projects:
        try:
            out["projects"][slug] = deadline_check_tick_one(cfg, slug)
        except Exception:  # noqa: BLE001 — one bad project never kills the sweep
            log.exception("status_deadlines: tick failed for %s", slug)
    return out


def aging_report(cfg: Any, slug: str, statuses: Optional[frozenset[str]] = None,
                  now: Optional[float] = None) -> dict:
    """On-demand, read-only view of gated-status ticket age — the PULL half
    of the T-0950 redesign (operator steer, 2026-09-06). ``blocked_on_user``
    still pages automatically via :func:`deadline_check_tick_one`;
    ``to_accept``/``totest`` no longer do (see the module docstring's
    "AUTO-PAGE SCOPE"), so this is how that backlog picture is seen — asked
    for, never sent. It shares the deadline/fallback math with the sweep but
    touches NOTHING stateful: no sidecar read/write, no dedup, no
    ``_notify`` — calling it twice in a row returns the same answer.

    ``statuses`` restricts which gated statuses are considered (default: all
    of :data:`GATED_STATUSES`). Returns ``{ok, slug, total, breaches, text}``
    — ``breaches`` is the FULL unfiltered list (oldest-first); ``text`` reuses
    :func:`_compose_message`'s cap+overflow rendering so a large backlog still
    reads as a digest rather than a wall of lines, without losing any row from
    the returned data the way a push's sidecar-marking would.
    """
    if now is None:
        now = time.time()
    wanted = statuses if statuses is not None else GATED_STATUSES
    rows = _scan_backlog(cfg, slug)
    breaches: list[dict] = []
    for r in rows:
        if r["status"] not in wanted:
            continue
        epoch, since_iso, source = _since(r)
        if epoch is None:
            continue
        deadline = deadline_sec(r["status"])
        if deadline is None or (now - epoch) < deadline:
            continue
        breaches.append({**r, "age_sec": now - epoch, "since_iso": since_iso,
                          "since_source": source})
    breaches.sort(key=lambda b: b["age_sec"], reverse=True)
    if not breaches:
        return {"ok": True, "slug": slug, "total": 0, "breaches": [],
                "text": "⏳ nothing past deadline right now."}
    text, _shown = _compose_message(breaches)
    return {"ok": True, "slug": slug, "total": len(breaches),
            "breaches": breaches, "text": text}
