"""T-0151: session-close guidance harvest.

Stakeholder guidance arrives as chat messages during a session and is rarely
written back to the ticket — so it gets buried in the suspended session's
jsonl and the next session repeats the question. This pass closes that gap:
when a session has been suspended, it harvests the stakeholder-attributed
comments from that session's transcript and appends the ones not already on
the bound ticket to the ticket body, so a future `bsq guidance search` (and
the T-0149 spawn assembly) surface them.

Decoupled from the gc reconcilers by design: it runs as its own pass over the
SessionMd registry, keyed on a one-time ``guidance_harvested: true`` marker, so
it catches BOTH auto-suspends (gc_sessions) and explicit suspends, never
double-writes, and a failure can't wedge the lifecycle tick. Best-effort —
exceptions are logged, not raised.
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

_MARKER = "guidance_harvested"
_MAX_COMMENTS = 6          # cap appended comments per session-close
_MIN_LEN = 40              # ignore trivial one-word stakeholder messages
_MAX_LEN = 280             # truncate each harvested comment
_HARVEST_HEADING = "## Stakeholder comments (harvested at session close — T-0151)"
# Reuse the guidance-search attribution exclusions.
_NON_STAKEHOLDER_PREFIXES = ("You are a bot-squad", "[[bsq-dispatch")
_MAIL_SIGNAL = "check mail"


def _stakeholder_comments(jsonl_path: Path) -> list[str]:
    """Human-typed (userType=external) guidance messages from a transcript."""
    out: list[str] = []
    try:
        fh = open(jsonl_path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if o.get("type") != "user" or o.get("isSidechain"):
                continue
            content = (o.get("message") or {}).get("content")
            if not isinstance(content, str) or o.get("userType") != "external":
                continue
            c = " ".join(content.split()).strip()
            if (len(c) < _MIN_LEN or c.startswith("<") or c == _MAIL_SIGNAL
                    or any(c.startswith(p) for p in _NON_STAKEHOLDER_PREFIXES)):
                continue
            out.append(c[:_MAX_LEN])
    return out


def _append_comments(ticket_path: Path, comments: list[str], sid: str) -> int:
    """Append comments not already present in the ticket body. Returns count added."""
    text = ticket_path.read_text(encoding="utf-8", errors="replace")
    existing = text
    fresh = []
    seen = set()
    for c in comments:
        key = c[:120]
        # Skip if a substantial prefix already appears in the ticket (verbatim
        # request, prior harvest, or a progress note) — avoids re-appending the
        # same guidance every close.
        if key in seen or key in existing:
            continue
        seen.add(key)
        fresh.append(c)
    fresh = fresh[:_MAX_COMMENTS]
    if not fresh:
        return 0
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    block = [f"\n{_HARVEST_HEADING}\n"] if _HARVEST_HEADING not in text else ["\n"]
    for c in fresh:
        block.append(f"- {ts} · from {sid}: {c}")
    new_text = text.rstrip("\n") + "\n" + "\n".join(block) + "\n"
    tmp = ticket_path.parent / (ticket_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.rename(tmp, ticket_path)
    return len(fresh)


def harvest_tick(cfg: Any, slug: str) -> dict:
    """One harvest pass for a project. Returns a summary dict.

    Scans suspended SessionMds lacking the harvest marker, appends their
    stakeholder comments to the bound ticket, and stamps the marker so the
    work is one-time. Idempotent.
    """
    from bot_squad_worker import sessions as S
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"harvest_tick: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    backlog_dir = cfg.data_dir / slug / "backlog"
    if not sessions_dir.exists():
        return {"ok": True, "harvested": []}

    user_home = os.path.expanduser("~")
    user = S._get_current_user()
    harvested: list[dict] = []

    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(f"S-{user}-"):
            continue
        meta = S._read_session_metadata(md)
        if meta is None:
            continue
        if meta.get("status") != "suspended":
            continue
        if str(meta.get(_MARKER, "")).lower() == "true":
            continue
        task_id = meta.get("task_id") or meta.get("last_task_id")
        cwd = meta.get("cwd")
        claude_uuid = meta.get("claude_uuid")
        # Mark as harvested regardless of outcome below, so we attempt exactly
        # once per session — a missing ticket/uuid shouldn't cause re-scans.
        added = 0
        if task_id and task_id != "~" and cwd and claude_uuid and claude_uuid != "~":
            matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
            jsonl = (Path(user_home) / ".claude" / "projects"
                     / cwd.replace("/", "-") / f"{claude_uuid}.jsonl")
            if matches and jsonl.exists():
                try:
                    comments = _stakeholder_comments(jsonl)
                    if comments:
                        added = _append_comments(matches[0], comments, meta.get("sid", md.stem))
                except Exception:
                    log.exception("harvest_tick: harvest failed for %s", md.stem)
        meta[_MARKER] = "true"
        try:
            S._write_session_metadata(md, meta, atomic=True)
        except OSError:
            log.exception("harvest_tick: could not stamp marker for %s", md.stem)
            continue
        if added:
            harvested.append({"sid": meta.get("sid", md.stem), "task_id": task_id, "added": added})

    if harvested:
        log.info("harvest_tick: %s harvested %d session(s): %s", slug, len(harvested), harvested)
    return {"ok": True, "harvested": harvested}
