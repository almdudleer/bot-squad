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
