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
