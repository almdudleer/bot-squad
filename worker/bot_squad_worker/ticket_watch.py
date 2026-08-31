"""T-0938 — the ticket IS the message bus: fan a ticket's change out to the
sessions bound to it.

The stakeholder's ask (2026-08-31, live tmux): «вместо пути, где она обновляет
тикеты, пока оператор занимается своим делом, а система сама пингует
релевантные сессии, что в тикете апдейт, начался путь, где юзер-сессия тупо
копипастит оператору в почту мои запросы, создавая глухой телефон». He STOPPED
writing to the user-session over it.

The intended path has two halves and only one of them existed. Sessions could
already WRITE onto a ticket (``task_stakeholder_note_add`` / ``task_context_set``
/ ``task_summary_set`` / ``task_progress_add``, T-0767/T-0863) — but nothing
told anybody a ticket had changed. Peer mail was the ONLY push channel in the
system, so "make sure they see it" could only mean "paste it into an inbox",
and that is precisely the relay habit («глухой телефон») the ticket is about.
This module is the missing half: a ticket update is itself a notification.

**Detection is by POLLING the md, not by hooking the writers, and that is the
whole design.** A ticket's bytes are changed by at least four paths that do not
share a code path — the worker actions, ``bsq ticket update`` (which patches
frontmatter client-side and never reaches the worker), the api's PATCH from the
web UI, and a human or session editing the file directly. A hook on the worker
actions would notify for some writers and not others, which is a worse failure
than none: the sessions that route around the instrumented path are exactly the
ones this ticket exists to catch. Polling the file is writer-agnostic by
construction.

What is watched, per ticket: the frontmatter ``status`` plus a digest of each of
:mod:`task_body`'s four canonical sections. Those are the authored areas —
"stakeholder note, context write, status move" in T-0938's own words — and
hashing them SEPARATELY is what lets a nudge say WHICH one moved instead of
"something changed".

Self-notification is suppressed per SECTION, not per ticket. The writers record
authorship through :func:`note_author`; a session that wrote ``## Context`` is
not told about its own Context write, but IS still told if somebody else moved
the status or appended a quote in the same window. Suppressing whole tickets
instead would silently drop the cross-session half of a two-author window —
the case the fan-out exists for.

Recipients, in order:

* every non-archived session bound to the ticket (``task_id`` +
  ``extra_task_ids``, resolved by :func:`idle_timeout.bound_task_ids` rather
  than a fifth local copy of that walk);
* failing that, the live operator (``dispatch.live_operator_sids``, the
  identity SSOT) — an unheld ticket that just changed is the operator's, which
  is also the direction T-0936 takes for umbrella tickets.

Delivery is the existing bus: a durable inbox line (``intersession.send_notice``
— a tick has nowhere to report a refusal, so it splits rather than refuses) plus
the ``check mail`` pane nudge a live recipient already knows how to answer. The
notice NAMES the ticket and what moved and stops there: the content is on the
ticket, and a notice that carried the text would re-create the relay.

Kill switch: ``BOT_SQUAD_TICKET_WATCH=0`` disables both the tick and the
authorship recording (the fan-out then simply does not happen — the pre-T-0938
posture).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Section key -> the heading a human sees, so a notice names the place to look
#: in the ticket's own vocabulary. Keys are :data:`task_body.SECTION_KEYS`;
#: ``verbatim`` prints as its CURRENT spelling (T-0767 renamed the heading and
#: both spellings still parse to the one key).
SECTION_HEADINGS: dict[str, str] = {
    "verbatim": "## Stakeholder notes",
    "summary": "## Executive summary",
    "context": "## Context",
    "progress": "## Progress",
}

#: Everything the fan-out watches, in the order a notice lists it. ``status``
#: first: a status move is the change most likely to mean "act now".
WATCHED_KEYS: tuple[str, ...] = ("status", "verbatim", "summary", "context", "progress")

#: Above this many tickets changing in ONE pass, notify for NONE of them.
#: 25 tickets inside a 60s window is not N sessions working — it is a migration,
#: a bulk re-status, or a restore. Those must not turn into a fan-out storm, and
#: the snapshots are still recorded so the burst is not re-detected next pass.
#: A loud log line is left behind, because a silently swallowed burst would hide
#: a real mass event too.
_MAX_TICKETS_PER_TICK = 25

#: How long an unconsumed authorship record survives. Records are normally
#: consumed by the very next tick; this only bounds the damage of a write that
#: recorded an author and then failed, or of a change the poller never saw.
_AUTHOR_TTL_SEC = 3600

#: Bumped when the snapshot shape changes. A state file from an older version is
#: DISCARDED rather than migrated — the cost is one silent pass (everything
#: re-snapshotted, nothing notified), which is exactly the first-run behaviour
#: and strictly better than diffing against a snapshot whose keys mean
#: something else.
_STATE_VERSION = 2

#: The tick and the action-side :func:`note_author` both read-modify-write the
#: state file from the same worker process (scheduler thread vs. request
#: threads), so the whole sequence is serialised here. A THREAD lock is the
#: right scope precisely because nothing outside the worker writes this file —
#: the api never touches it, and the CLI reaches it only through
#: `ticket_author_note`, i.e. through this process.
#:
#: Known bound, stated rather than discovered later: a sweep holds this across
#: its notifications, and `input_mux.deliver_direct` waits up to ~3s on a busy
#: composer — so a concurrent `bsq ticket note` can block for roughly 3s per
#: live recipient. Measured worst case on the live board is ONE recipient per
#: change, and the alternative (releasing the lock mid-pass) trades a bounded
#: wait for interleaved state writes.
_lock = threading.RLock()

#: Sender identity on the bus, mirroring ``S-telemetry`` / ``S-deploy_monitor``.
SENDER_SID = "S-ticket_watch"


def ticket_watch_enabled() -> bool:
    """Master switch (default ON, mirroring the sibling ticks).
    ``BOT_SQUAD_TICKET_WATCH=0`` disables the fan-out entirely."""
    return os.environ.get("BOT_SQUAD_TICKET_WATCH", "1") != "0"


# ---------------------------------------------------------------------------
# snapshot + diff (pure)
# ---------------------------------------------------------------------------

def _digest(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def snapshot_text(text: str) -> dict:
    """Digest one ticket md into the shape the state file stores.

    Returns ``{"id", "status", "title", "sections": {key: digest}}``. ``title``
    and ``id`` ride along for the notice and for file→ticket resolution; only
    ``status`` and the section digests are diffed, so a typo fix in a title
    never pages every bound session.

    Frontmatter is read through :mod:`frontmatter`, the same parser every other
    ticket reader uses — a local regex would disagree with it on exactly the
    tickets that are hardest to read (a folded multi-line ``title:``, a quoted
    value containing ``---``), and a second ticket parser is how T-0286 was
    silently misread. A file with no frontmatter still snapshots (id/status/
    title empty, the whole text parsed as a body): an unparseable ticket must
    not crash the sweep.
    """
    from bot_squad_worker import frontmatter as _fm
    from bot_squad_worker import task_body

    text = text or ""
    parsed_fm = _fm.parse_or_none(text)
    if parsed_fm is None:
        meta, body = {}, text
    else:
        meta, body = parsed_fm
    sections = task_body.parse_body(body)
    return {
        "id": str(meta.get("id") or "").strip(),
        "status": str(meta.get("status") or "").strip(),
        "title": str(meta.get("title") or "").strip(),
        "sections": {k: _digest(sections.get(k, "")) for k in task_body.SECTION_KEYS},
    }


def changed_keys(prev: dict | None, cur: dict) -> list[str]:
    """Which watched fields moved between two snapshots, in :data:`WATCHED_KEYS`
    order. ``prev is None`` (a ticket seen for the first time) returns ``[]`` —
    a first sighting is not an update, and treating it as one would fan every
    ticket on the board out on first boot."""
    if prev is None:
        return []
    out: list[str] = []
    if (prev.get("status") or "") != (cur.get("status") or ""):
        out.append("status")
    pv = prev.get("sections") or {}
    cv = cur.get("sections") or {}
    for key in WATCHED_KEYS:
        if key == "status":
            continue
        if pv.get(key) != cv.get(key):
            out.append(key)
    return out


def describe_change(prev: dict | None, cur: dict, keys: list[str]) -> str:
    """One clause naming what moved — a status transition spelled out, sections
    named by their heading."""
    parts: list[str] = []
    if "status" in keys:
        old = ((prev or {}).get("status") or "?").strip() or "?"
        new = (cur.get("status") or "?").strip() or "?"
        parts.append(f"status {old} -> {new}")
    sections = [SECTION_HEADINGS[k] for k in keys if k != "status" and k in SECTION_HEADINGS]
    if sections:
        parts.append("rewritten: " + ", ".join(sections))
    return "; ".join(parts) or "changed"


def notice_text(task_id: str, title: str, what: str, rel_path: str) -> str:
    """The nudge. Names the ticket and what moved; carries NO content.

    Carrying the changed text would rebuild the relay this ticket exists to
    kill — the reader would act on a copy instead of on the ticket, and the
    board would again hold no trace of what was actually read.
    """
    head = f"[TICKET UPDATE] {task_id}"
    if title:
        head += f" — {title[:100]}"
    return (
        f"{head} · {what}. Re-read the ticket itself: {rel_path}. "
        f"The change is ON the ticket (T-0938: tickets are the message bus) — "
        f"this line only tells you it moved."
    )


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def state_path(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "_worker" / "ticket_watch.json"


def _load_state(path: Path) -> dict:
    """Read the state file, or an empty one. A corrupt/older-version file is
    discarded (see :data:`_STATE_VERSION`), never half-read."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
        return {"version": _STATE_VERSION, "tickets": {}, "files": {}, "authors": {}}
    raw.setdefault("tickets", {})
    raw.setdefault("files", {})
    raw.setdefault("authors", {})
    return raw


def _save_state(path: Path, state: dict) -> None:
    from bot_squad_worker.mdlock import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, sort_keys=True))


def _prune_authors(state: dict, now: float) -> None:
    authors = state.get("authors") or {}
    for tid in list(authors):
        rec = authors[tid] or {}
        for key in list(rec):
            ts = (rec.get(key) or {}).get("ts") or 0
            if now - ts > _AUTHOR_TTL_SEC:
                rec.pop(key, None)
        if not rec:
            authors.pop(tid, None)


def note_author(cfg: Any, slug: str, task_id: str, sid: str | None,
                keys: list[str] | tuple[str, ...]) -> None:
    """Record that ``sid`` is the author of this change to ``keys`` on ``task_id``.

    Called by the ticket writers AFTER a successful write, so a failed write
    leaves no phantom author. The tick consumes these records, so a session is
    never told about its own edit — and, because the record is per SECTION, is
    still told about anyone else's edit landing in the same window.

    Best-effort by contract: an unwritable state file must never fail the write
    that already succeeded. The cost of a lost record is one redundant
    self-notification, not a lost change.
    """
    if not ticket_watch_enabled() or not sid or not task_id or not keys:
        return
    try:
        with _lock:
            path = state_path(cfg, slug)
            state = _load_state(path)
            now = time.time()
            rec = state.setdefault("authors", {}).setdefault(str(task_id), {})
            for key in keys:
                rec[key] = {"sid": str(sid), "ts": now}
            _prune_authors(state, now)
            _save_state(path, state)
    except Exception:  # noqa: BLE001 — attribution is never worth a failed write
        log.exception("ticket_watch: could not record author for %s/%s", slug, task_id)


# ---------------------------------------------------------------------------
# recipients
# ---------------------------------------------------------------------------

def session_rows(cfg: Any, slug: str) -> list[dict]:
    """The project's session roster, or ``[]`` if it can't be read.

    Read ONCE PER SWEEP and threaded into the recipient walks, for two reasons.
    Cost: measured at 0.16s per call against the live install's 660 session mds,
    which at the storm cap (25 changed tickets) would be 4s of repeated identical
    work inside a 60s tick. Consistency: every ticket in one pass then resolves
    against the SAME roster, so a session appearing or dying mid-sweep can't put
    part of a pass on one set of recipients and part on another.
    """
    from bot_squad_worker import sessions as S

    try:
        return S.list_sessions(cfg, slug)
    except Exception:  # noqa: BLE001
        log.exception("ticket_watch: list_sessions failed for %s", slug)
        return []


def bound_sids(cfg: Any, slug: str, task_id: str,
               rows: list[dict] | None = None) -> list[str]:
    """Every non-archived session bound to ``task_id`` — primary or bundled.

    Reads the binding through :func:`idle_timeout.bound_task_ids` rather than
    re-walking ``task_id``/``extra_task_ids`` here: a bundled dev carries its
    other tickets ONLY in ``extra_task_ids``, and a local copy of that walk is
    how a fan-out would quietly miss them.

    The ARCHIVED filter is what keeps this from becoming T-0834's storm: 595 of
    the live install's 660 session mds are archived, and a target list that
    included them would write an inbox line per dead session per change. Only
    65 rows survive it, 10 of which carry a real ``task_id`` at all.
    """
    from bot_squad_worker import idle_timeout as _idle

    if rows is None:
        rows = session_rows(cfg, slug)
    out: list[str] = []
    for row in rows:
        if str(row.get("archived", "")).lower() == "true" or row.get("archived") is True:
            continue
        if row.get("status") == "archived":
            continue
        sid = row.get("sid")
        if not sid or sid in out:
            continue
        if task_id in _idle.bound_task_ids(row, None):
            out.append(sid)
    return out


def recipients_for(cfg: Any, slug: str, task_id: str,
                   rows: list[dict] | None = None,
                   operator_sids: list[str] | None = None) -> tuple[list[str], str]:
    """``(sids, why)`` — who hears about this ticket's change.

    Bound sessions when there are any; otherwise the live operator, because an
    unheld ticket that just moved is the operator's business (the same holder
    logic T-0936 makes explicit for umbrella tickets). An empty list is a
    legitimate outcome — an unheld ticket with no live operator has nobody to
    tell, and inventing a recipient would put the change back on a relay.
    """
    sids = bound_sids(cfg, slug, task_id, rows)
    if sids:
        return sids, "bound"
    if operator_sids is not None:
        return list(operator_sids), "operator"
    try:
        from bot_squad_worker.dispatch import live_operator_sids

        return list(live_operator_sids(cfg, slug)), "operator"
    except Exception:  # noqa: BLE001
        log.exception("ticket_watch: live_operator_sids failed for %s", slug)
        return [], "operator"


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------

def _pane_nudge(cfg: Any, sid: str) -> bool:
    """Poke ``sid``'s live pane with the ``check mail`` signal.

    The inbox line is the durable half; without this a live session only learns
    of it on its next voluntary ``bsq inbox check``, which for a session
    mid-task can be never. Mirrors what ``bsq peer send`` does for a human-typed
    message, including T-0904's self-describing variant for providers that were
    never told what ``check mail`` means. A recipient with no live pane
    (suspended, non-tmux) is not an error — it reads the line on next start.
    """
    try:
        from bot_squad_worker import boot_orientation as _boot
        from bot_squad_worker import input_mux
        from bot_squad_worker import sessions as S

        user = S._get_current_user()
        pane = next(
            (p for p in S.list_panes() if S.compute_sid(user, p.window, p.pane_id) == sid),
            None,
        )
        if pane is None:
            return False
        from bot_squad_worker.actions import _provider_for_pane

        text = _boot.mail_nudge(_provider_for_pane(cfg, sid, pane))
        input_mux.deliver_direct(cfg.data_dir, sid, pane.pane_id, text)
        return True
    except Exception:  # noqa: BLE001 — the durable line already landed
        log.debug("ticket_watch: pane nudge failed for %s", sid, exc_info=True)
        return False


def _notify(cfg: Any, slug: str, sid: str, text: str) -> bool:
    from bot_squad_worker import intersession as _is

    try:
        out = _is.send_notice(cfg, slug, SENDER_SID, sid, text)
    except Exception:  # noqa: BLE001
        log.exception("ticket_watch: send_notice failed for %s", sid)
        return False
    if not out.get("delivered_to"):
        return False
    _pane_nudge(cfg, sid)
    return True


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def _scan_project(cfg: Any, slug: str) -> list[dict]:
    """Detect this project's changed tickets and fan them out. Returns one dict
    per notified (ticket, sid) pair — the tick's summary, and what the tests
    assert on."""
    backlog = Path(cfg.data_dir) / slug / "backlog"
    if not backlog.is_dir():
        return []

    with _lock:
        path = state_path(cfg, slug)
        state = _load_state(path)
        before = json.dumps(state, sort_keys=True)
        tickets: dict = state["tickets"]
        files: dict = state["files"]
        authors: dict = state["authors"]
        now = time.time()

        seen_ids: set[str] = set()
        seen_files: set[str] = set()
        pending: list[tuple[str, str, dict, dict | None, list[str]]] = []

        for md in sorted(backlog.glob("*.md")):
            try:
                st = md.stat()
            except OSError:
                continue
            seen_files.add(md.name)
            known = files.get(md.name)
            # Cheap gate FIRST: an unchanged file costs one stat, not a parse.
            # 851 live tickets / 9MB of backlog re-parsed every 60s would be
            # pure waste, and the steady state is that nothing changed.
            # NANOSECOND mtime, not st_mtime: a section rewrite that happens to
            # preserve the file's size (`status: planned` -> `status: paused`,
            # a same-length Context) would otherwise need only a same-second
            # second write to slip through the gate entirely.
            if known and known.get("mtime") == st.st_mtime_ns and known.get("size") == st.st_size:
                if known.get("id"):
                    seen_ids.add(known["id"])
                continue
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            cur = snapshot_text(text)
            # The id comes from `id:` frontmatter, never the filename (T-0231):
            # a renamed or stale file must not be diffed as a different ticket.
            tid = cur.get("id")
            if not tid:
                continue
            seen_ids.add(tid)
            files[md.name] = {"mtime": st.st_mtime_ns, "size": st.st_size, "id": tid}
            prev = tickets.get(tid)
            # No separate "first run" flag: a ticket with no prior snapshot has
            # `prev is None`, and `changed_keys` answers `[]` for that by
            # contract. One rule, in one place — a second guard here would look
            # load-bearing while doing nothing (and a control proved it did
            # nothing).
            keys = changed_keys(prev, cur)
            tickets[tid] = cur
            if keys:
                pending.append((tid, md.name, cur, prev, keys))

        for name in list(files):
            if name not in seen_files:
                files.pop(name, None)
        for tid in list(tickets):
            if tid not in seen_ids:
                tickets.pop(tid, None)
                authors.pop(tid, None)

        notified: list[dict] = []
        storm = len(pending) > _MAX_TICKETS_PER_TICK
        if storm:
            log.warning(
                "ticket_watch: %s saw %d tickets change in one pass (cap %d) — "
                "suppressing the fan-out for this pass; a burst that size is a "
                "bulk edit, not N sessions working. Changed: %s",
                slug, len(pending), _MAX_TICKETS_PER_TICK,
                ", ".join(t for t, *_ in pending[:40]),
            )
        else:
            # One roster read and one operator lookup for the whole pass — see
            # `session_rows`. Resolved lazily so a pass with nothing to say
            # (the overwhelmingly common one) pays for neither.
            rows: list[dict] | None = None
            operator_sids: list[str] | None = None
            for tid, name, cur, prev, keys in pending:
                if rows is None:
                    rows = session_rows(cfg, slug)
                    try:
                        from bot_squad_worker.dispatch import live_operator_sids
                        operator_sids = list(live_operator_sids(cfg, slug))
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "ticket_watch: live_operator_sids failed for %s", slug)
                        operator_sids = []
                rel = f"data/{slug}/backlog/{name}"
                sids, why = recipients_for(cfg, slug, tid, rows, operator_sids)
                if not sids:
                    # Measured on the live install 2026-08-31: bot-squad has NO
                    # live operator pane (the only operator window belongs to
                    # another project and is correctly scoped out), so EVERY
                    # unheld ticket's change resolves to nobody. That is the
                    # honest answer from the identity SSOT and this module will
                    # not invent a recipient — but it must not be silent either,
                    # for the same reason `intersession.send` warns when the
                    # `operator` keyword reaches nobody: a change that told no
                    # one is exactly the state the relay habit grew in.
                    log.warning(
                        "ticket_watch: %s changed (%s) and reached NOBODY — no "
                        "session is bound to it and %s has no live operator; "
                        "nothing was notified", tid, ", ".join(keys), slug,
                    )
                    continue
                ticket_authors = authors.get(tid) or {}
                for sid in sids:
                    mine = {k for k in keys
                            if (ticket_authors.get(k) or {}).get("sid") == sid}
                    rest = [k for k in keys if k not in mine]
                    if not rest:
                        continue
                    text = notice_text(tid, cur.get("title", ""),
                                       describe_change(prev, cur, rest), rel)
                    if _notify(cfg, slug, sid, text):
                        notified.append({"task_id": tid, "sid": sid,
                                         "keys": rest, "why": why})

        # Authorship is CONSUMED by the pass that saw the change — kept only
        # while it can still suppress a self-notification, dropped whether or
        # not it actually did (including under the storm cap, where nothing was
        # sent). A record that outlived its change would suppress somebody
        # else's later edit of the same section.
        for tid, *_ in pending:
            authors.pop(tid, None)
        _prune_authors(state, now)
        # Only write when something actually moved. Measured on the live board:
        # the state file is 385KB for 856 tickets, and an unconditional save
        # rewrote it on EVERY 60s tick — ~0.5GB/day of writes to say nothing
        # changed. The steady state is now a directory of stats and no write at
        # all.
        if json.dumps(state, sort_keys=True) != before:
            _save_state(path, state)

    if notified:
        log.info("ticket_watch: %s notified %d session(s): %s", slug, len(notified), notified)
    return notified


def tick(cfg: Any) -> None:
    """Per-project sweep. Per-project errors are swallowed so one bad project
    never kills the others — the same posture as its 60s siblings."""
    if not ticket_watch_enabled():
        return
    for slug in cfg.projects:
        try:
            _scan_project(cfg, slug)
        except Exception:  # noqa: BLE001
            log.exception("ticket_watch: sweep failed for %s", slug)
