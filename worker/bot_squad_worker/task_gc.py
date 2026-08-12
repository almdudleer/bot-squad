"""F7 (extends the T-0233 stale-reaper doctrine to TASKS): auto-GC throwaway
QA tasks so they stop leaking onto the board.

A QA dogfood pass leaves behind throwaway tickets (titled ``QA-TEST-DELETEME-*``,
body "safe to delete"). Nothing purged them, so they sat on the live board
indefinitely — a dangling loop (docs/design/closed-loops-principle.md): the
process that OPENS a throwaway task never had an owner to CLOSE it.

This module owns that close: a throwaway task is archived (moved to
``backlog/_gc/`` — reversible, not hard-deleted) once it is ``closed`` OR has
aged past a short grace (so an in-flight test isn't swept mid-run).

Detection is by TITLE prefix or a ``throwaway: true`` flag — NEVER body text, so
a real ticket that merely *mentions* the convention (e.g. this feature's own
ticket) is never touched.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

# Title marker the QA dogfood passes use for disposable tickets.
_THROWAWAY_TITLE_RE = re.compile(r"^\s*QA-TEST-DELETEME", re.IGNORECASE)

# Grace before a throwaway task is swept (so an in-progress test isn't reaped
# mid-run). Env-tunable; non-positive/garbage → default.
DEFAULT_THROWAWAY_GC_SEC = 3600  # 1h


def throwaway_task_gc_sec() -> int:
    raw = os.environ.get("BOT_SQUAD_THROWAWAY_TASK_GC_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_THROWAWAY_GC_SEC


def is_throwaway_task(title: Any, meta: dict) -> bool:
    """True for a disposable QA dogfood ticket — by ``throwaway: true`` flag or a
    ``QA-TEST-DELETEME`` title prefix. Body text is intentionally NOT consulted."""
    if isinstance(meta, dict):
        flag = meta.get("throwaway")
        if flag is True or (isinstance(flag, str) and flag.strip().lower() == "true"):
            return True
    return bool(title and _THROWAWAY_TITLE_RE.match(str(title)))


def gc_throwaway_tasks(cfg: Any, slug: str, now: float | None = None) -> dict:
    """Archive throwaway tasks that are closed or aged past the grace.

    Moves each to ``backlog/_gc/`` (the board globs ``backlog/*.md`` top-level, so
    a subdir is off the board but the file is preserved — reversible). Returns
    ``{"archived": [task_id, ...]}``. Never raises on a single bad file.
    """
    from bot_squad_worker import frontmatter as fm

    if now is None:
        now = time.time()
    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return {"archived": []}
    grace = throwaway_task_gc_sec()
    archive_dir = backlog / "_gc"
    archived: list[str] = []

    for md in sorted(backlog.glob("*.md")):
        try:
            parsed = fm.parse_or_none(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        meta = parsed[0] if parsed else {}
        if not is_throwaway_task(meta.get("title", ""), meta):
            continue
        status = str(meta.get("status", "")).strip().lower()
        try:
            age = now - md.stat().st_mtime
        except OSError:
            age = grace + 1  # can't stat → treat as stale
        if status != "closed" and age < grace:
            continue  # an in-flight throwaway test — leave it
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            os.replace(md, archive_dir / md.name)
        except OSError:
            log.exception("task_gc: failed to archive %s", md)
            continue
        tid = meta.get("id") or md.stem
        archived.append(str(tid))
        log.info("task_gc: archived throwaway task %s (status=%s, age=%.0fs)", tid, status, age)

    return {"archived": archived}


# === T-0484: general task cleanup (stale + duplicate) =======================
# The throwaway GC above closes one dangling loop (disposable QA tickets). This
# section generalizes it into the REAL cleanup process the board needs: (a)
# age-based archival of stale/abandoned tasks, (b) duplicate detection + merge.
#
# Faithful to the paradigm (sessions do judgment, tooling assists): the
# detectors only SURFACE candidates; the apply paths are REVERSIBLE — every task
# moves OFF-board into ``backlog/_gc/`` (preserved, never hard-deleted; reverse a
# decision by moving the file back). gc_stale_tasks runs in the binding_gc tick
# (long grace, inactive-only); merge stays an explicit operator-driven call.

# A task is a stale-archival candidate only when it is neither being actively
# worked nor already done. open/planned/reopened are inert backlog states;
# in_progress/totest are ACTIVE and closed is terminal — age must never reap those.
_STALE_ELIGIBLE_STATUSES = frozenset({"open", "planned", "reopened"})

# Grace before an inactive task is treated as stale/abandoned. Far longer than
# the throwaway grace (1h) — a real backlog item may legitimately sit untouched
# for weeks. Env-tunable; non-positive/garbage → default.
DEFAULT_STALE_TASK_GC_SEC = 30 * 24 * 3600  # 30 days

# Collapse a title to a comparison key: lowercased, punctuation→space, runs of
# non-alphanumerics squeezed. So "Fix the login BUG!" == "fix  the  login bug".
_TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def stale_task_gc_sec() -> int:
    raw = os.environ.get("BOT_SQUAD_STALE_TASK_GC_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_STALE_TASK_GC_SEC


def is_stale_task(meta: Any, age_sec: float, grace: int | None = None) -> bool:
    """True for an inactive task aged past the stale grace.

    Throwaway tasks are excluded — they own the faster ``gc_throwaway_tasks``
    path. Active (in_progress/totest) and terminal (closed) tasks are never
    stale regardless of age.
    """
    if not isinstance(meta, dict):
        return False
    if is_throwaway_task(meta.get("title", ""), meta):
        return False
    status = str(meta.get("status", "")).strip().lower()
    if status not in _STALE_ELIGIBLE_STATUSES:
        return False
    if grace is None:
        grace = stale_task_gc_sec()
    return age_sec >= grace


def _normalize_title(title: Any) -> str:
    return _TITLE_NORMALIZE_RE.sub(" ", str(title or "").lower()).strip()


def _id_sort_key(tid: Any):
    """Sort task ids by their numeric component (T-0600 < T-0601), so the
    lowest id — the original — is the natural keeper. Non-numeric ids sort last."""
    m = re.search(r"(\d+)", str(tid))
    return (0, int(m.group(1))) if m else (1, str(tid))


def _iter_board_tasks(backlog):
    """Yield ``(md_path, meta, body)`` for each top-level task md on the board.

    Skips unreadable / frontmatter-less files. The ``_gc/`` archive subdir is a
    child dir, so a top-level ``*.md`` glob never re-scans already-archived tasks.
    """
    from bot_squad_worker import frontmatter as fm

    for md in sorted(backlog.glob("*.md")):
        try:
            parsed = fm.parse_or_none(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed:
            continue
        meta, body = parsed
        yield md, (meta or {}), body


def _archive_md(md, archive_dir, stamp: dict | None = None) -> bool:
    """Move a task md off-board into ``_gc/`` (reversible), optionally stamping
    extra frontmatter keys first. Returns True on success; never raises."""
    from bot_squad_worker import frontmatter as fm

    if stamp:
        try:
            parsed = fm.parse_or_none(md.read_text(encoding="utf-8"))
            if parsed:
                meta, body = parsed
                meta = dict(meta or {})
                meta.update(stamp)
                md.write_text(fm.dump(meta, body), encoding="utf-8")
        except OSError:
            log.exception("task_gc: failed to stamp %s before archive", md)
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        os.replace(md, archive_dir / md.name)
    except OSError:
        log.exception("task_gc: failed to archive %s", md)
        return False
    return True


def find_stale_tasks(cfg: Any, slug: str, now: float | None = None) -> dict:
    """Surface (don't touch) inactive tasks aged past the stale grace.

    Returns ``{"stale": [{"id","title","status","age_sec"}, ...]}`` — the
    tooling's ASSIST half of cleanup; ``gc_stale_tasks`` is the apply half.
    """
    if now is None:
        now = time.time()
    backlog = cfg.data_dir / slug / "backlog"
    stale: list[dict] = []
    if not backlog.exists():
        return {"stale": stale}
    grace = stale_task_gc_sec()
    for md, meta, _ in _iter_board_tasks(backlog):
        try:
            age = now - md.stat().st_mtime
        except OSError:
            continue
        if not is_stale_task(meta, age, grace):
            continue
        stale.append({
            "id": str(meta.get("id") or md.stem),
            "title": str(meta.get("title", "")),
            "status": str(meta.get("status", "")).strip().lower(),
            "age_sec": int(age),
        })
    return {"stale": stale}


def gc_stale_tasks(cfg: Any, slug: str, now: float | None = None) -> dict:
    """Age-based cleanup: archive stale/abandoned tasks off-board (reversible).

    Wired into the binding_gc tick. Only inactive tasks (open/planned/reopened)
    aged past the long grace are swept — active and closed tasks are left alone.
    Moves each to ``backlog/_gc/``. Returns ``{"archived": [task_id, ...]}``.
    Never raises on a single bad file.
    """
    if now is None:
        now = time.time()
    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return {"archived": []}
    grace = stale_task_gc_sec()
    archive_dir = backlog / "_gc"
    archived: list[str] = []
    for md, meta, _ in _iter_board_tasks(backlog):
        try:
            age = now - md.stat().st_mtime
        except OSError:
            continue
        if not is_stale_task(meta, age, grace):
            continue
        tid = str(meta.get("id") or md.stem)
        if _archive_md(md, archive_dir):
            archived.append(tid)
            log.info("task_gc: archived stale task %s (status=%s, age=%.0fs)",
                     tid, meta.get("status"), age)
    return {"archived": archived}


def find_duplicate_tasks(cfg: Any, slug: str) -> dict:
    """Suggestion-only: group on-board tasks whose titles normalize identically.

    Throwaway tasks are excluded (disposable). Never mutates — surfacing
    candidates is the ASSIST role; the merge decision is the operator's (apply
    via ``merge_tasks``). Returns ``{"duplicates": [{"normalized","ids",
    "suggested_keep","members"}, ...]}`` with the lowest-id member suggested as
    the keeper (the original).
    """
    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return {"duplicates": []}
    groups: dict[str, list[dict]] = {}
    for md, meta, _ in _iter_board_tasks(backlog):
        title = meta.get("title", "")
        if is_throwaway_task(title, meta):
            continue
        norm = _normalize_title(title)
        if not norm:
            continue
        groups.setdefault(norm, []).append({
            "id": str(meta.get("id") or md.stem),
            "title": str(title),
            "status": str(meta.get("status", "")).strip().lower(),
        })
    duplicates: list[dict] = []
    for norm, members in groups.items():
        if len(members) < 2:
            continue
        members_sorted = sorted(members, key=lambda m: _id_sort_key(m["id"]))
        duplicates.append({
            "normalized": norm,
            "ids": [m["id"] for m in members_sorted],
            "suggested_keep": members_sorted[0]["id"],
            "members": members_sorted,
        })
    duplicates.sort(key=lambda g: g["normalized"])
    return {"duplicates": duplicates}


def merge_tasks(cfg: Any, slug: str, keep: Any, dups: Any,
                now: float | None = None) -> dict:
    """Operator-driven (reversible): fold duplicate task(s) into a keeper.

    Each dup is stamped ``merged_into: <keep>`` and moved off-board to
    ``backlog/_gc/``; the keeper gets a dated back-reference note so the merge is
    discoverable and undoable (move the ``_gc`` file back to reverse). This is a
    pure file operation — intentionally NOT wired to a worker action yet (the
    operator endpoint is sequenced as a follow-up once actions.py frees up);
    exercised directly via this function. Returns
    ``{"kept","merged":[...],"missing":[...]}``.
    """
    from bot_squad_worker import frontmatter as fm

    if now is None:
        now = time.time()
    backlog = cfg.data_dir / slug / "backlog"
    archive_dir = backlog / "_gc"
    keep = str(keep)
    dup_ids = [str(d) for d in (dups or [])]

    by_id: dict[str, Any] = {}
    if backlog.exists():
        for md, meta, _ in _iter_board_tasks(backlog):
            by_id[str(meta.get("id") or md.stem)] = md

    merged: list[str] = []
    missing: list[str] = []
    for dup in dup_ids:
        md = by_id.get(dup)
        if md is None:
            missing.append(dup)
            continue
        if _archive_md(md, archive_dir, stamp={"merged_into": keep}):
            merged.append(dup)
        else:
            missing.append(dup)

    keep_md = by_id.get(keep)
    if merged and keep_md is not None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        try:
            parsed = fm.parse_or_none(keep_md.read_text(encoding="utf-8"))
            if parsed:
                meta, body = parsed
                note = (f"- {ts} · task_gc · merged duplicate(s) "
                        f"{', '.join(merged)} into this task "
                        f"(archived to backlog/_gc/, reversible).\n")
                new_body = (body or "").rstrip("\n") + "\n\n" + note
                keep_md.write_text(fm.dump(meta or {}, new_body), encoding="utf-8")
        except OSError:
            log.exception("task_gc: failed to annotate keeper %s", keep_md)

    return {"kept": keep, "merged": merged, "missing": missing}


# === T-0889: auto-pause a ticket nobody is working ==========================
# His ask, verbatim (2026-08-04, relayed forward into T-0889): "When all
# sessions working on a ticket are terminated, the ticket is automatically
# passed to another 'started' / 'paused' status".
#
# MEASURED 2026-08-12: 11 of the 13 tickets at ``in_progress`` were held by NO
# live session — 84% of the board's busiest-looking column was a label a dead
# session left behind. ``dispatch.py``'s own note says the same thing from its
# own 2026-08-11 measurement: "The status is a LABEL a dev leaves behind when it
# dies or forgets to move the ticket; it drifts up and never comes down on its
# own."
#
# This pass is the board CATCHING UP to what the routing gate already computes,
# not a new opinion: ``dispatch.decide_topology`` derives ``tasks_in_flight``
# from live sessions' bound task sets and carries ``board_in_progress`` beside it
# marked "Reported, never gated on" — precisely because of this drift. Built
# from the same primitives (``sessions._is_live_holder`` + ``_full_task_set``)
# so there is no third answer to "is anyone on this ticket".

#: The only status the auto-pause may move. "while work is incomplete" is what
#: separates ``in_progress`` from ``totest`` (work delivered, awaiting a
#: verifier — a session ending there is a HANDOFF, not an abandonment) and from
#: ``closed``; ``open``/``planned``/``reopened`` were never started, so they have
#: no session to lose; ``paused`` is the destination. One status, deliberately.
_AUTO_PAUSE_FROM_STATUSES = frozenset({"in_progress"})

#: What an unheld ``in_progress`` ticket becomes. Its canonical (4-state) home is
#: ``in-progress`` — see ``api/app/canonical_status.py`` — so a paused ticket
#: stays in the column the operator watches instead of vanishing into backlog.
_AUTO_PAUSE_TO_STATUS = "paused"

#: Quiet period on the TICKET FILE before an unheld ``in_progress`` ticket is
#: paused. This second condition is MEASURED, not defensive: on 2026-08-12
#: T-0889 itself sat at ``in_progress``, actively being built, held by NO live
#: session — its holder (``…-p207``) had recycled and the successor sessions
#: inherited none of its task binding. On the binding alone this pass would have
#: paused the ticket it was written for.
#:
#: The binding is therefore necessary but not sufficient, and ticket mtime is the
#: second instrument: a ticket somebody is working gets WRITTEN to (progress
#: notes, context, summary). Note what this does NOT claim — ``pickup``'s module
#: docstring is right that "staleness measures the last WRITE, not the last
#: work", so a fresh mtime is no evidence that work is moving. It is used only to
#: WITHHOLD a pause, never to justify one, and requiring both conditions can only
#: DELAY a correct pause — it can never mislabel live work. Env-tunable.
DEFAULT_AUTO_PAUSE_GRACE_SEC = 4 * 3600  # 4h


def auto_pause_grace_sec() -> int:
    raw = os.environ.get("BOT_SQUAD_AUTO_PAUSE_GRACE_SEC")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_AUTO_PAUSE_GRACE_SEC


def live_held_task_ids(cfg: Any, slug: str) -> set[str]:
    """Every task id held by a LIVE session of ANY role in ``slug``.

    Composed from the same two primitives the routing gate uses
    (``sessions._is_live_holder`` — status active/paused and not archived, kept
    tick-synced with real tmux panes by ``gc_sessions`` — and
    ``sessions._full_task_set`` — primary + extras).

    ROLE-AGNOSTIC on purpose, where ``dispatch.live_role_sids(cfg, slug, "dev")``
    filters to devs: his sentence is "all sessions working on a ticket", and a TL
    or user-conversation session holding a ticket is somebody working it. The
    result is a SUPERSET of the gate's dev-only set, so it can only ever spare a
    ticket the dev-only rule would have paused — never the reverse. Measured
    2026-08-12 across all three live projects: for every ticket at
    ``in_progress`` the two sets agreed, so the widening costs nothing today and
    is insurance against the day a non-dev session holds one.
    """
    from bot_squad_worker import sessions as _sessions

    out: set[str] = set()
    sess_dir = cfg.data_dir / slug / "sessions"
    if not sess_dir.exists():
        return out
    for md in sorted(sess_dir.glob("*.md")):
        meta = _sessions._read_session_metadata(md)
        if meta is None or not _sessions._is_live_holder(meta):
            continue
        out |= {t for t in _sessions._full_task_set(meta) if t and t != "~"}
    return out


def _pause_one(md, tid: str, ts: str, note: str) -> bool:
    """Re-check under the task lock, then write ``status: paused`` + a Progress
    line. Returns True if this call is what moved the ticket.

    The re-read inside the lock is not ceremony: the candidate scan runs unlocked
    (13 board mds per project per tick), so between the scan and the write a dev
    may have moved the ticket itself. Locking with ``mdlock.task_lock`` is what
    makes this mutually exclusive with the API's writer, which flocks the same
    ``<task>.md.lock``.
    """
    from bot_squad_worker import frontmatter as fm
    from bot_squad_worker import task_body as _task_body
    from bot_squad_worker.mdlock import task_lock, atomic_write

    try:
        with task_lock(md):
            parsed = fm.parse_or_none(md.read_text(encoding="utf-8"))
            if not parsed:
                return False
            meta, body = parsed
            meta = dict(meta or {})
            if str(meta.get("status", "")).strip().lower() not in _AUTO_PAUSE_FROM_STATUSES:
                return False  # somebody moved it between the scan and this write
            meta["status"] = _AUTO_PAUSE_TO_STATUS
            meta["updated"] = ts
            new_body = _task_body.append_progress(body or "", ts, "task_gc", note)
            atomic_write(md, fm.dump(meta, new_body))
    except (OSError, ValueError):
        log.exception("task_gc: failed to auto-pause %s", md)
        return False
    return True


def auto_pause_unheld_tasks(cfg: Any, slug: str, now: float | None = None) -> dict:
    """Move ``in_progress`` tickets that no live session holds to ``paused``.

    Two conditions, both required (see :data:`DEFAULT_AUTO_PAUSE_GRACE_SEC` for
    why the second exists): no live session of any role holds the ticket, AND the
    ticket file has been untouched for the grace. Throwaway and archived tickets
    are skipped — they own other paths.

    Returns ``{"paused": [task_id, ...]}``. Never raises on a single bad file:
    one unreadable ticket must not kill the tick.
    """
    if now is None:
        now = time.time()
    backlog = cfg.data_dir / slug / "backlog"
    if not backlog.exists():
        return {"paused": []}

    grace = auto_pause_grace_sec()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    held = live_held_task_ids(cfg, slug)
    paused: list[str] = []

    for md, meta, _body in _iter_board_tasks(backlog):
        if str(meta.get("status", "")).strip().lower() not in _AUTO_PAUSE_FROM_STATUSES:
            continue
        if str(meta.get("archived", "")).strip().lower() in ("true", "yes", "1", "on"):
            continue
        if is_throwaway_task(meta.get("title", ""), meta):
            continue
        tid = str(meta.get("id") or md.stem)
        if tid in held:
            continue
        try:
            age = now - md.stat().st_mtime
        except OSError:
            continue  # can't age it → can't honour the grace → leave it alone
        if age < grace:
            continue
        note = (f"auto-paused: no live session holds this ticket and it has been "
                f"untouched for {int(age // 3600)}h. Set it back to in_progress "
                f"when work resumes (`bsq ticket update {tid} in_progress`).")
        if _pause_one(md, tid, ts, note):
            paused.append(tid)
            log.info("task_gc: auto-paused %s (%s, unheld, age=%.0fs)", tid, slug, age)

    return {"paused": paused}
