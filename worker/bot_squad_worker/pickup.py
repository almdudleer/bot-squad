"""Which board tickets are ELIGIBLE to be picked up, and what surfaces them (T-0783a).

The incident this exists for
---------------------------
The stakeholder reopened a ticket at 11:40 and no session took it until he pushed
the operator by hand. He says he gets to do that regularly. So the felt failure is
not that drive modes are badly named — it is that **reopened and ready-but-idle
work is never picked up, and he ends up being the dispatcher.**

Nothing in the system answered "what should be taken next". The only board signal
an operator had was ``operator_redrive.count_pending_backlog`` — a COUNT of
everything non-closed (57 on bot-squad the day this was written), which tells a
fresh operator that work exists and nothing about which work is takeable. The
operator's own enumeration found 16 of 31 open tickets unmentioned in the
handoff, and part of the P1 band was not P1 work at all — a ticket whose own title
says ``P3 DEFERRED``, and an expired 20h sprint directive. (A third was cited and
turned out to be genuine; see the ownership section below.) A P1 band that is part
noise teaches an operator to stop reading it, which is very likely how T-0719 — a
genuine stakeholder-reported P1 REOPENED regression — stayed invisible while six
sessions passed through.

The two-band answer
-------------------
The eligibility question splits cleanly in two, and conflating them is what made
the old count useless:

* **Mechanical facts** — archived/terminal, a status that is not a pickup status,
  a live session already holding it, a live session holding one of its child
  LANES, an unsatisfied blocker. Nothing here needs a judgement call, so a ticket
  failing any of them is ``excluded`` with the reason NAMED.
* **Judgement signals** — the ticket is stale, its priority field contradicts its
  own title, its priority is missing or unparseable, or it is an initiative
  CONTAINER rather than a unit of work. Each of these means "a human or an
  operator should look before a dev is dispatched at this", NOT "this does not
  exist". Such a ticket lands in the ``triage`` band: still surfaced, never
  auto-dispatched.

What is left is the ``pickup`` band — takeable right now, led by T-0719.

There is deliberately NO cross-project-ownership rule
-----------------------------------------------------
An earlier cut of this module had one, and it was wrong in the most instructive
way available. T-0612's title opens ``watchrobot RV pair-trading program``, and it
had been cited twice — once in this ticket's own brief — as another project's work
sitting in our P1 band. Operator p502 read its Verbatim request: it is a
25-minute voice note about **bot-squad's own** session-lifecycle behaviour
(compacting constantly, respawning sessions, compacting over an existing compact)
observed while running on that install. Watchrobot is where he saw it, not the
owner.

So a project name in a title (or a body) is **not evidence of ownership**, and a
rule keyed on one would have suppressed a genuine P1 carrying a real stakeholder
verbatim that has sat 24 days — the exact failure this module exists to prevent,
pointed the other way. T-0612 is now a POSITIVE fixture: it must surface. A
correct ownership signal would have to read which board a ticket is on together
with what its ask is asking someone to change, which is not something a
deterministic dispatch gate can compute. Absent that, no predicate: over-
surfacing costs an operator one glance, under-surfacing is how a P1 goes
invisible for 24 days.

The same defect, found twice, on two installs
---------------------------------------------
Watchrobot's operator diagnosed this independently and its framing is the sharper
one: **once a task reads as in progress nobody picks it up, because it looks
attended while being abandoned.** It had verified free session slots and idle
sessions, so the cause was an accounting error, not a staffing shortage. Four of
its tasks were in_progress behind no live session; six of this board's were, and
that set is what the bands above are calibrated against.

Which is why the two liveness reads here are deliberate rather than incidental:

* the holder is resolved from the **session mds** (the authoritative binding),
  never from a ticket's own ``session_history`` — that field misses live bindings
  ``bsq team status`` shows, so reading it UNDER-detects live work and
  OVER-reports abandonment;
* a parent whose child lane is live counts as **attended**. An operator told to
  pick up a ticket that has a live dev under it stops trusting the queue, which
  is the same failure as a P1 band that is one third noise.

Three deliberate non-decisions
------------------------------
1. **Nothing here writes.** In particular a contradictory priority is never
   repaired in the file: the operator declined to rewrite T-0388's field to make
   a listing tidy, and the reasoning on that ticket holds. This module computes
   an ``effective_priority`` for RANKING and reports it alongside the untouched
   ``priority`` plus the ``sanity`` flags explaining why they differ.
2. **Demotion only, never promotion.** ``effective_priority`` is the max (least
   urgent) of the stored band and every demotion that fired, so a sanity flag can
   only ever move a ticket down the list. A rule that could promote would be a
   rule that can invent urgency.
3. **A triage ticket is surfaced, not hidden.** The failure being fixed is work
   going unseen; a heuristic whose false positives DROP tickets would recreate it
   in a new place. Every false positive of the judgement signals lands in a band
   the operator reads, which is the cheap direction to be wrong in.

Two limits, stated rather than papered over
-------------------------------------------
* An **expired time-boxed directive** (T-0695 — "spend 70% of the week's quota in
  20h", whose window lapsed 63 hours before it was read) is indistinguishable from
  live P1 work by any deterministic read of its frontmatter. Its ``updated`` is
  today and its title declares no other priority. Catching it needs an expiry
  FIELD, which no ticket has; guessing it from prose is not something a dispatch
  gate should do. It sits in the pickup band at P1 and that is the honest answer.
* **Staleness measures the last WRITE, not the last work.** ``updated`` is
  stamped by any note, including an operator's diagnostic one — writing the
  correction above onto T-0612 reset its clock from 24 days to zero. So the
  signal is a floor on neglect, never a measure of it, and nothing here treats a
  fresh ``updated`` as evidence that work is moving.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: Statuses whose tickets are candidates for pickup. ``totest`` is deliberately
#: absent — it is review work awaiting a verifier, not work awaiting a doer, and
#: dispatching a fresh dev at it would redo shipped work. ``in_progress`` IS here
#: because an in_progress ticket with no live holder is precisely the
#: "ready-but-idle" half of the complaint: work that was started, abandoned when
#: its session was reaped, and never taken up again.
PICKUP_STATUSES = frozenset({"reopened", "open", "planned", "in_progress"})

#: The one terminal status (task status schema: planned/open/in_progress/totest/
#: reopened/closed) — same definition ``operator_redrive`` uses.
TERMINAL_STATUSES = frozenset({"closed"})

#: ``kind: initiative`` marks an initiative CONTAINER task (INI-02, ui-polish,
#: process-paradigm …). It aggregates child work, so a lone dev dispatched at one
#: is the wrong move — but it is NOT excluded, because an initiative sitting at
#: ``in_progress`` with nothing live under it is the accounting error this module
#: is about (T-0551 and T-0556 have read as attended-while-abandoned for 5 and 11
#: days). It is a TRIAGE signal: surfaced, and the right response is to split it
#: into lanes or put a TL on it, not to auto-dispatch.
CONTAINER_KINDS = frozenset({"initiative"})

#: Band names. ``pickup`` is the only band an automatic dispatcher may act on.
BAND_PICKUP = "pickup"
BAND_TRIAGE = "triage"
BAND_EXCLUDED = "excluded"

#: Priority band used for anything whose urgency cannot be read (missing,
#: unparseable) or is contradicted by its own title. Sorts below every real
#: band without pretending to be one of them.
UNRANKED_BAND = 9

#: Days since last activity beyond which a ticket stops being auto-takeable.
#: Not a claim that stale work is unimportant — a claim that nobody has touched
#: it in two weeks, so "dispatch a dev at it" is a decision, not a default.
DEFAULT_STALE_DAYS = 14

#: How many pickup-band tickets the operator brief names. The brief is injected
#: into every re-drive prompt, so it has to stay short enough to read.
DEFAULT_BRIEF_LIMIT = 8

#: Words in a title that say the ticket is not active work, whatever its
#: priority field claims. Matched case-insensitively on word boundaries.
DEFERRAL_WORDS = ("deferred", "parked", "wontfix", "superseded", "on hold")

#: A ``P<n>`` declared inside the TITLE. T-0388 reads ``P3 DEFERRED: …`` in its
#: title while its frontmatter says ``priority: P1``; whoever wrote the title was
#: recording a decision, and the two cannot both be right.
_TITLE_PRIORITY_RE = re.compile(r"(?<![\w-])[Pp]([0-9])(?![\w-])")

#: ``P<n>`` or a bare ``<n>`` — the two shapes the board actually carries (the
#: mds are hand-written ``P1``; the API's PATCH writes an int).
_PRIORITY_RE = re.compile(r"\A[Pp]?([0-9])\Z")

#: ``T-0719-some-slug.md`` -> ``T-0719``. Only used when an md omits its own
#: ``id`` field, which none on this board does.
_FILENAME_ID_RE = re.compile(r"\A([A-Z]+-\d+)-")

_TRUEISH = ("true", "yes", "1", "on")


def stale_days() -> int:
    """The staleness window in days — env-tunable on a live worker like the
    sibling dispatch knobs (``BOT_SQUAD_PICKUP_STALE_DAYS``)."""
    raw = os.environ.get("BOT_SQUAD_PICKUP_STALE_DAYS")
    try:
        val = int(raw) if raw else DEFAULT_STALE_DAYS
    except (TypeError, ValueError):
        return DEFAULT_STALE_DAYS
    return val if val > 0 else DEFAULT_STALE_DAYS


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUEISH


def _parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds for an ISO-8601 timestamp string, or None when absent or
    unparseable. ``Z`` is accepted (``fromisoformat`` on 3.11+ handles it, and
    the explicit replace keeps older readers honest); a naive timestamp is read
    as UTC, which is what every writer in this repo produces."""
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


def parse_priority(raw: Any) -> tuple[Optional[int], Optional[str]]:
    """``(band, flag)`` for a stored priority field.

    ``band`` is the integer urgency (``P1`` -> 1) or None when it cannot be read;
    ``flag`` names the problem when there is one, so a caller reports WHY a
    ticket is unranked instead of showing a silent default. The board carries
    ``''``, ``0``, ``P1`` and ``high`` today — the last of those is exactly the
    case that must not be guessed at.
    """
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None, "priority-missing"
    m = _PRIORITY_RE.match(s)
    if not m:
        return None, f"priority-unparseable:{s}"
    return int(m.group(1)), None


def title_priority(title: str) -> Optional[int]:
    """The ``P<n>`` a title declares about itself, or None. First match wins —
    a title naming two priorities is itself the contradiction, and the leading
    one is the decision it was retitled to record."""
    m = _TITLE_PRIORITY_RE.search(str(title or ""))
    return int(m.group(1)) if m else None


def deferral_words(title: str) -> list[str]:
    """Deferral words present in a title (``deferred``, ``parked``, …). A title
    that says the work is deferred outranks a priority field that says P1."""
    low = str(title or "").lower()
    return [w for w in DEFERRAL_WORDS if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", low)]


def classify_ticket(
    meta: dict,
    *,
    held_by: Optional[str] = None,
    lane_held_by: Optional[str] = None,
    open_blockers: Any = (),
    now_epoch: float,
    stale_after_days: Optional[int] = None,
) -> dict:
    """Classify ONE ticket's pickup eligibility from its frontmatter. Pure.

    The caller resolves the facts a single md cannot know — ``held_by`` (the SID
    of a live session holding it), ``lane_held_by`` (a live session holding one of
    its CHILD tickets) and ``open_blockers`` (its ``blocked_by`` ids that are not
    closed) — and this decides the band. Split that way so the whole eligibility
    rule is unit-testable against a dict, with no filesystem and no tmux.

    Returns ``{id, title, status, priority, effective_priority, band, reject,
    sanity, idle_days, rank}``. ``reject`` is the ONE mechanical reason for an
    excluded ticket (None otherwise); ``sanity`` lists every judgement signal
    that fired, which is what a triage ticket is triaged ON.
    """
    stale_cut = stale_days() if stale_after_days is None else stale_after_days
    tid = str(meta.get("id") or "").strip()
    title = str(meta.get("title") or "")
    status = str(meta.get("status") or "").strip().lower()

    prio_raw = meta.get("priority")
    band, prio_flag = parse_priority(prio_raw)

    # --- judgement signals (computed for every ticket, reported even on the
    # --- excluded ones so a reject never hides a second problem) -------------
    sanity: list[str] = []
    demotions: list[int] = [band if band is not None else UNRANKED_BAND]

    if prio_flag:
        sanity.append(prio_flag)

    t_band = title_priority(title)
    if t_band is not None and band is not None and t_band != band:
        sanity.append(f"priority-title-disagreement:P{t_band}")
        demotions.append(t_band)

    deferrals = deferral_words(title)
    if deferrals:
        sanity.append("title-says-deferred:" + ",".join(deferrals))
        demotions.append(UNRANKED_BAND)

    if str(meta.get("kind") or "").strip().lower() in CONTAINER_KINDS:
        sanity.append("initiative-container")
        demotions.append(UNRANKED_BAND)

    # Last activity: ``updated`` is the field every write path stamps; ``created``
    # is the fallback for the handful of mds predating it. Neither present is an
    # explicit UNKNOWN, never a silent "fresh" — an absent timestamp reads
    # identically to a real negative otherwise.
    # (An ``or`` chain would be wrong here: epoch 0 is falsy, so a 1970 timestamp
    # would read as absent — no ticket carries one, but a numeric ``or`` is a trap
    # left for the next reader.)
    last: Optional[float] = None
    for key in ("updated", "created", "created_at"):
        last = _parse_iso(meta.get(key))
        if last is not None:
            break
    if last is None:
        idle_days: Optional[float] = None
        sanity.append("activity-unknown")
    else:
        idle_days = max(0.0, (now_epoch - last) / 86400.0)
        if idle_days >= stale_cut:
            sanity.append(f"stale:{int(idle_days)}d>={stale_cut}d")

    effective = max(demotions)

    # --- mechanical exclusions, first match wins so the reason is unambiguous --
    reject: Optional[str] = None
    if _truthy(meta.get("archived")):
        reject = "archived"
    elif status in TERMINAL_STATUSES:
        reject = f"terminal:{status}"
    elif status not in PICKUP_STATUSES:
        # ``totest`` is the real member of this set: review work, not pickup
        # work. An unrecognised status is named as itself rather than assumed.
        reject = ("awaiting-review" if status == "totest"
                  else f"not-a-pickup-status:{status or 'missing'}")
    elif held_by:
        reject = f"held-by:{held_by}"
    elif lane_held_by:
        # A parent whose LANE is live is attended, not abandoned. Reported as its
        # own reason rather than folded into held-by, so a reader can tell "a dev
        # is on this ticket" from "a dev is on a child of this ticket".
        reject = f"lane-live:{lane_held_by}"
    else:
        blockers = [str(b) for b in (open_blockers or ()) if str(b).strip()]
        if blockers:
            reject = "blocked-by:" + ",".join(sorted(blockers))

    if reject:
        band_name = BAND_EXCLUDED
    elif sanity:
        band_name = BAND_TRIAGE
    else:
        band_name = BAND_PICKUP

    return {
        "id": tid,
        "title": title,
        "status": status,
        "priority": None if prio_raw is None else str(prio_raw),
        "effective_priority": effective,
        "band": band_name,
        "reject": reject,
        "sanity": sanity,
        "idle_days": None if idle_days is None else round(idle_days, 1),
        "rank": _rank_key(status, effective, idle_days),
    }


#: Within one effective-priority band, which status gets taken first. A
#: ``reopened`` ticket is work the stakeholder already reported once and had to
#: report again, so it leads; an idle ``in_progress`` is abandoned mid-flight
#: work, which is cheaper to resume than to start; ``planned`` trails ``open``.
STATUS_ORDER = {"reopened": 0, "in_progress": 1, "open": 2, "planned": 3}


def _rank_key(status: str, effective: int, idle_days: Optional[float]) -> list:
    """Sort key for the queue: urgency, then status, then longest-ignored first.

    Staleness is a TIEBREAK, never a promotion — it decides which of two equally
    urgent tickets has been waiting longer, and a ticket stale past the window is
    in the triage band by then anyway. An unknown idle age sorts as 0 (no claim).
    """
    return [effective, STATUS_ORDER.get(status, len(STATUS_ORDER)),
            -(idle_days or 0.0), status]


# ---------------------------------------------------------------------------
# Board sweep — resolves the two per-ticket facts classify_ticket cannot know
# ---------------------------------------------------------------------------

def _backlog_metas(cfg: Any, slug: str) -> list[dict]:
    """Frontmatter of every top-level ``backlog/*.md``. The ``_gc/`` archive is a
    child dir, so a top-level glob never re-reads archived tasks (same reasoning
    as ``operator_redrive.count_pending_backlog``)."""
    from bot_squad_worker import frontmatter as _fm

    backlog = Path(cfg.data_dir) / slug / "backlog"
    if not backlog.exists():
        return []
    out: list[dict] = []
    for md in sorted(backlog.glob("*.md")):
        try:
            parsed = _fm.parse_or_none(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed:
            continue
        meta = dict(parsed[0] or {})
        if not str(meta.get("id") or "").strip():
            # Every board md declares its id today; fall back to the filename
            # prefix rather than emitting a ticket with no id, which would make
            # the row unactionable at exactly the moment it matters.
            m = _FILENAME_ID_RE.match(md.name)
            meta["id"] = m.group(1) if m else md.stem
        out.append(meta)
    return out


def held_task_ids(cfg: Any, slug: str) -> dict[str, str]:
    """``{task_id: sid}`` for tasks a LIVE session is currently holding.

    One pass over the session mds and ONE tmux+/proc scan, rather than
    ``sessions._live_task_owner`` per ticket (which rescans tmux every call — 57
    scans on this board). Liveness is the same two-part test that function
    applies, and for the same reason: a crashed dev's md lingers ``active`` until
    the next ``gc_sessions`` tick, and treating that phantom as a holder would
    make its abandoned task invisible — which is the defect this module exists to
    fix, arriving through the back door.
    """
    from bot_squad_worker import sessions as S

    sess_dir = Path(cfg.data_dir) / slug / "sessions"
    if not sess_dir.exists():
        return {}
    try:
        live = S._live_agent_sids()
    except Exception:  # noqa: BLE001 — a tmux hiccup must not empty the board
        live = set()
    out: dict[str, str] = {}
    for md in sorted(sess_dir.glob("*.md")):
        meta = S._read_session_metadata(md)
        if meta is None or not S._is_live_holder(meta):
            continue
        sid = meta.get("sid", md.stem)
        if sid not in live:
            continue
        for tid in S._full_task_set(meta):
            out.setdefault(tid, sid)
    return out


def _blocked_by_ids(meta: dict) -> list[str]:
    """A ticket's ``blocked_by`` ids, from either the list or the legacy
    comma-string shape (both exist on the board; ``blocked_on`` is the older
    field name)."""
    out: list[str] = []
    for key in ("blocked_by", "blocked_on"):
        raw = meta.get(key)
        if isinstance(raw, (list, tuple)):
            out += [str(x).strip() for x in raw if str(x).strip()]
        elif raw:
            out += [p.strip() for p in str(raw).split(",") if p.strip()]
    return out


def pickup_queue(cfg: Any, slug: str, *, now_epoch: Optional[float] = None) -> dict:
    """The whole board, banded — the answer to "what should be taken next".

    Returns ``{ok, slug, pickup: [...], triage: [...], excluded: [...],
    counts: {...}}`` with ``pickup`` and ``triage`` ranked most-urgent-first.
    Pure read: backlog mds, session mds, one tmux scan. Safe to call on a fresh
    project with no backlog dir (every list empty).
    """
    import time as _time

    now = _time.time() if now_epoch is None else now_epoch
    metas = _backlog_metas(cfg, slug)
    held = held_task_ids(cfg, slug)

    # A blocker only blocks while it is itself unfinished — a closed blocker on a
    # ticket nobody re-read is otherwise a permanent, invisible hold.
    status_by_id = {
        str(m.get("id") or "").strip(): str(m.get("status") or "").strip().lower()
        for m in metas
    }

    # A parent whose child LANE is live is attended, not abandoned (operator
    # p502, 2026-07-30, from a scan that first over-reported 10 abandoned
    # in_progress tickets and settled at 6). ``parent_task`` is unset on this
    # board's current lane tickets — their TL holds parent AND lanes in its own
    # ``extra_task_ids``, which ``held`` already catches — so this fires zero
    # times today and exists for the shape where a lane is bound and its parent
    # is not. Getting told to pick up a ticket that has a live dev under it is
    # how an operator learns to stop trusting the queue.
    lane_held: dict[str, str] = {}
    for meta in metas:
        parent = str(meta.get("parent_task") or meta.get("parent") or "").strip()
        child = str(meta.get("id") or "").strip()
        if parent and parent != "~" and child in held:
            lane_held.setdefault(parent, held[child])

    rows: list[dict] = []
    for meta in metas:
        tid = str(meta.get("id") or "").strip()
        open_blockers = [
            b for b in _blocked_by_ids(meta)
            if status_by_id.get(b, "open") not in TERMINAL_STATUSES
        ]
        rows.append(classify_ticket(
            meta,
            held_by=held.get(tid), lane_held_by=lane_held.get(tid),
            open_blockers=open_blockers, now_epoch=now,
        ))

    banded = {BAND_PICKUP: [], BAND_TRIAGE: [], BAND_EXCLUDED: []}
    for row in rows:
        banded[row["band"]].append(row)
    for name in (BAND_PICKUP, BAND_TRIAGE):
        banded[name].sort(key=lambda r: r["rank"])

    return {
        "ok": True,
        "slug": slug,
        "pickup": banded[BAND_PICKUP],
        "triage": banded[BAND_TRIAGE],
        "excluded": banded[BAND_EXCLUDED],
        "counts": {
            "pickup": len(banded[BAND_PICKUP]),
            "triage": len(banded[BAND_TRIAGE]),
            "excluded": len(banded[BAND_EXCLUDED]),
            "board": len(rows),
        },
    }


# ---------------------------------------------------------------------------
# The surface: what an operator is actually told
# ---------------------------------------------------------------------------

#: What the brief says when the pickup band is empty. It has to be a STATEMENT,
#: not an absent section: an operator handed a brief with no queue in it reads
#: that as "the queue was not computed" and falls back to guessing, which is the
#: habit this whole module is replacing. Naming the triage count in the same
#: breath keeps "nothing is takeable" from being confused with "nothing exists".
EMPTY_PICKUP_LINE = (
    "PICKUP QUEUE: EMPTY — no ticket is takeable right now. Do NOT invent work; "
    "triage or report the empty queue instead."
)


def pickup_brief(queue: dict, *, limit: int = DEFAULT_BRIEF_LIMIT) -> str:
    """Render a :func:`pickup_queue` result as the block injected into the
    operator's re-drive prompt.

    Names the top ``limit`` takeable tickets with the facts a dispatch decision
    needs (id, status, effective urgency, days idle) and states the triage count
    so the suspect band is visible without being dispatchable. The empty case is
    said out loud — see :data:`EMPTY_PICKUP_LINE`.
    """
    pick = queue.get("pickup") or []
    triage = queue.get("triage") or []
    lines: list[str] = []
    if not pick:
        lines.append(EMPTY_PICKUP_LINE)
    else:
        lines.append(
            f"PICKUP QUEUE ({len(pick)} takeable, most urgent first) — dispatch "
            "from HERE rather than re-deriving the board:"
        )
        for row in pick[:limit]:
            idle = "idle-unknown" if row["idle_days"] is None else f"{row['idle_days']}d idle"
            lines.append(
                f"  {row['id']} [{row['status']}] P{row['effective_priority']} "
                f"· {idle} · {row['title'][:70]}"
            )
        if len(pick) > limit:
            lines.append(f"  … and {len(pick) - limit} more (`bsq pickup` for the full queue)")
    if triage:
        ids = ", ".join(r["id"] for r in triage[:limit])
        lines.append(
            f"NEEDS TRIAGE ({len(triage)}): {ids}"
            + (" …" if len(triage) > limit else "")
            + " — suspect priority, stale, or an initiative container. Look "
              "before dispatching; do NOT rewrite anyone's priority field to "
              "tidy the list."
        )
    return "\n".join(lines)
