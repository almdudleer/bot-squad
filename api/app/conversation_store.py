"""Per-(project, user) TG conversation history store (T-0489).

The durable record of the full Telegram user-conversation thread — kept "just
like we keep the jsonl files for the cloud sessions, which we can always look
up" (voice-04). This is the substrate for continuity across session recycles
(feeds the user-conversation seam, M5-T8/T9): a recycled or token-capped
session terminates, but the conversation it was attending lives on here so the
next session can pick it up.

Design notes
------------
* Keyed by ``(project_slug, global_user_id)``. The ``global_user_id`` is the
  cross-server mothership identity established by T-0488's resolve-or-link; the
  conversation is anchored on it (not the raw TG sender id) so recognition is
  consistent across servers. The conversation is ALSO project-scoped — voice-04
  is explicit that conversational sessions attach to ONE project (privacy: a
  user has limited project access, so a cross-project thread would leak).
* Mothership-level store (lives under ``_mothership/``), mirroring
  ``mothership_users_store.py`` — the bot's user-communication is centralized on
  the mothership (voice-04). One thread = one append-only JSONL file, one record
  per line (timestamp, author, text, attachments), echoing the Claude jsonl
  shape so the record is "always lookup-able".
* SINGLE WRITER = the API. The worker never writes this file directly; it POSTs
  to the API append endpoint (the same worker->API token-gated path T-0488
  established). So there is no dual-writer divergence. An in-process lock guards
  concurrent API appends from interleaving their lines.
* Atomic-enough: appends are O_APPEND line writes (each record < PIPE_BUF, so
  the kernel won't tear a single line); reads tolerate a torn/garbage trailing
  line by skipping it rather than failing the whole lookup.

Shape on disk (``data/_mothership/conversations/<slug>/<global_user_id>.jsonl``)::

    {"timestamp": "<iso8601>", "author": "user", "text": "...", "attachments": []}
    {"timestamp": "<iso8601>", "author": "session:S-...", "text": "...", "attachments": []}
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_append_lock = threading.Lock()

# A path segment (slug / global_user_id) must be a single, non-traversing name.
# Both real inputs satisfy this: project slugs are lowercase alnum+dash, and a
# global_user_id is ``gu_<hex>``. Anything with a separator or "." escape is
# rejected so a crafted value can never walk outside the conversations root.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_segment(value: str) -> str:
    """Validate a single path segment; raise ``ValueError`` on anything that
    could traverse. Returns the value unchanged when safe."""
    v = str(value or "")
    if v in ("", ".", "..") or not _SEGMENT_RE.match(v) or v.startswith("."):
        raise ValueError(f"unsafe conversation path segment: {value!r}")
    return v


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def conversations_root(data_dir: Path) -> Path:
    """Root holding every per-(project, user) conversation thread."""
    return Path(data_dir) / "_mothership" / "conversations"


def conv_path(data_dir: Path, slug: str, global_user_id: str) -> Path:
    """Path to one (project, user) conversation thread (a JSONL file)."""
    s = _safe_segment(slug)
    g = _safe_segment(global_user_id)
    return conversations_root(data_dir) / s / f"{g}.jsonl"


def append(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    *,
    author: str,
    text: str,
    attachments: list | None = None,
    timestamp: str | None = None,
) -> dict:
    """Append one message record to the thread; return the stored record.

    ``author`` is free-form ("user" for inbound TG, "session:<sid>" for an
    attending session's writeback). ``attachments`` defaults to ``[]``.
    ``timestamp`` defaults to now (UTC, ISO-8601).
    """
    record = {
        "timestamp": timestamp or _now_iso(),
        "author": str(author),
        "text": "" if text is None else str(text),
        "attachments": list(attachments) if attachments else [],
    }
    p = conv_path(data_dir, slug, global_user_id)
    line = json.dumps(record, ensure_ascii=False)
    with _append_lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return record


def _read_all(data_dir: Path, slug: str, global_user_id: str) -> list[dict]:
    """Read every record in append order, skipping any torn/garbage line."""
    p = conv_path(data_dir, slug, global_user_id)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        log.warning("conversation_store: unreadable thread at %s", p)
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # A torn trailing write or hand-edited garbage line — skip it
            # rather than failing the whole lookup (the record is best-effort).
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _paginate(records: list[dict], *, limit: int, offset: int) -> dict:
    total = len(records)
    offset = max(0, int(offset))
    limit = max(0, int(limit))
    page = records[offset:offset + limit]
    return {"total": total, "limit": limit, "offset": offset, "messages": page}


def list_messages(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Return a paginated, chronological page of the thread.

    ``total`` is the full thread length (not the page size) so a caller can
    drive pagination. A missing thread yields an empty page.
    """
    records = _read_all(data_dir, slug, global_user_id)
    return _paginate(records, limit=limit, offset=offset)


def search(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    query: str,
    *,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Return a paginated page of records whose ``text`` contains ``query``
    (case-insensitive). ``total`` is the full match count."""
    needle = str(query or "").lower()
    records = [r for r in _read_all(data_dir, slug, global_user_id)
               if needle in str(r.get("text", "")).lower()]
    return _paginate(records, limit=limit, offset=offset)
