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


def _read_sidecar(cfg: Any, slug: str) -> dict[str, dict]:
    """``{task_id: {"status":..., "since":...}}`` for the last stay already
    alerted on. Missing/corrupt file = empty (worst case a breach re-alerts
    once more — never a crash, never a blast)."""
    try:
        raw = json.loads(_sidecar_path(cfg, slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in tasks.items():
        if isinstance(v, dict) and "status" in v and "since" in v:
            out[str(k)] = {"status": str(v["status"]), "since": str(v["since"])}
    return out


def _write_sidecar(cfg: Any, slug: str, tasks: dict[str, dict]) -> None:
    p = _sidecar_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"tasks": tasks, "updated_at": _now_iso()}, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, p)


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
    state = _read_sidecar(cfg, slug)
    new_state = dict(state)
    breaches: list[dict] = []

    seen: set[str] = set()
    for r in rows:
        seen.add(r["id"])
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
    if breaches:
        text, shown = _compose_message(breaches)
        delivered = _notify(cfg, slug, text)
        if delivered:
            # Only the NAMED subset is marked alerted — a breach left out by
            # the cap stays pending so it is named in a later sweep instead of
            # disappearing into "+N more" forever.
            for b in shown:
                new_state[b["id"]] = {"status": b["status"], "since": b["since_iso"]}

    if new_state != state:
        _write_sidecar(cfg, slug, new_state)
    return {"ok": True, "slug": slug, "breaches": len(breaches), "delivered": delivered}


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
