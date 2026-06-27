"""Per-(project, user) conversation WORKING-AREA doc + resume assembly (T-0495).

M5 / F5.8+F5.9 — conversation continuity. The working-area doc is the
conversation analog of the operator's state-doc (T-0473): a FUTURE-FOCUSED
continuity artifact a conversational session writes so the next (recycled)
session picks up where it left off. Per voice-04:

    "the module provides a per-user document where a conversational session
    writes intentions/next-steps so the next (recycled) session picks up
    continuity -- the conversation analog of the operator's state doc."

Design notes
------------
* Keyed by ``(project_slug, global_user_id)`` — the SAME key as the conversation
  history store (T-0489 ``conversation_store``), and for the same reasons: the
  ``global_user_id`` is the cross-server mothership identity (T-0488), and the
  thread is project-scoped (a user has limited project access, so a cross-project
  continuity doc would leak). The two stores are siblings — history is the
  append-only event record; this is the future-focused intent layer on top.
* FUTURE-FOCUSED, NOT an event log — exactly like the operator state-doc
  (T-0473): the canonical sections are ``intentions`` / ``next_steps`` /
  ``open_threads`` (see ``WORKAREA_SECTIONS``). The history store already holds
  the event record; this doc holds only what the next session needs to *resume*.
* Stored as markdown + YAML frontmatter (the role-artifact shape), one file per
  (slug, user) under ``_mothership/conversation_workareas/<slug>/<gid>.md``. The
  markdown BODY is the human-readable, transparency-friendly surface; the
  frontmatter carries metadata (slug / global_user_id / schema / updated).
* IDEMPOTENT upsert: ``upsert`` is create-or-update with a PARTIAL merge (only
  the sections you pass change; the rest survive). Re-upserting identical content
  is a true no-op — the file bytes and the ``updated`` stamp are left untouched —
  so a per-turn writer that re-saves an unchanged doc costs nothing and the
  ``updated`` field means "last *changed*", not "last written".
* Atomic write: unique tmp + ``os.replace`` (same pattern as
  ``markdown_writer.write_task``), guarded by an in-process lock so concurrent
  API writers to the same doc don't interleave.

Deferred seams (NOT built here — kept disjoint, per the TL-D Phase-1 design):
* The conversational session WRITING this doc each turn / at autocompact, and
  spawn-boots-from-it — that is the user-conversation session role (T-0478).
* Any worker-token API route to read/write it remotely — that goes under
  ``/api/m/worker/*`` (T-0529). This module is the pure store + assembly so it
  composes under whichever route lands.

Shape on disk
(``data/_mothership/conversation_workareas/<slug>/<global_user_id>.md``)::

    ---
    slug: proj
    global_user_id: gu_abc
    schema: conversation-workarea/1
    updated: 2026-06-27T00:00:00Z
    ---

    # Conversation working area

    > Future-focused continuity doc ... NOT an event log.

    ## Intentions
    ...

    ## Next steps
    ...

    ## Open threads
    ...
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.frontmatter import dump, parse_or_none

log = logging.getLogger(__name__)

_write_lock = threading.Lock()

# Schema tag stamped into frontmatter so a future migration can recognize the
# version of the doc it is reading.
SCHEMA = "conversation-workarea/1"

# The canonical FUTURE-FOCUSED sections — the conversation analog of the
# operator state-doc's section schema (T-0473). Ordered ``field -> heading``.
# This is the closed set of sections; ``upsert`` rejects anything else so a
# typo'd key can't silently create an orphan section.
WORKAREA_SECTIONS: dict[str, str] = {
    "intentions": "Intentions",
    "next_steps": "Next steps",
    "open_threads": "Open threads",
}

# Placeholder rendered for an empty section so the doc reads cleanly and a
# human (or the next session) sees the section exists but is empty. Parsed back
# to "" on read so it round-trips.
_EMPTY_PLACEHOLDER = "_(none)_"

# A path segment (slug / global_user_id) must be a single, non-traversing name —
# identical guard to ``conversation_store._safe_segment`` (kept local so this
# module stays self-contained and does not reach into the sibling store's
# private helper).
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_segment(value: str) -> str:
    """Validate a single path segment; raise ``ValueError`` on anything that
    could traverse. Returns the value unchanged when safe."""
    v = str(value or "")
    if v in ("", ".", "..") or not _SEGMENT_RE.match(v) or v.startswith("."):
        raise ValueError(f"unsafe conversation workarea path segment: {value!r}")
    return v


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workareas_root(data_dir: Path) -> Path:
    """Root holding every per-(project, user) conversation working-area doc."""
    return Path(data_dir) / "_mothership" / "conversation_workareas"


def workarea_path(data_dir: Path, slug: str, global_user_id: str) -> Path:
    """Path to one (project, user) working-area doc (a markdown file)."""
    s = _safe_segment(slug)
    g = _safe_segment(global_user_id)
    return workareas_root(data_dir) / s / f"{g}.md"


def _empty_sections() -> dict[str, str]:
    return {k: "" for k in WORKAREA_SECTIONS}


def _render(slug: str, global_user_id: str, sections: dict[str, str],
            updated: str) -> str:
    """Serialize a working-area doc to its markdown+frontmatter string."""
    meta = {
        "slug": slug,
        "global_user_id": global_user_id,
        "schema": SCHEMA,
        "updated": updated,
    }
    lines = [
        "# Conversation working area",
        "",
        f"> Future-focused continuity doc for `{global_user_id}` @ `{slug}` — "
        "intentions / next steps / open threads the next session resumes from. "
        "NOT an event log (the history store holds the record).",
    ]
    for key, heading in WORKAREA_SECTIONS.items():
        text = (sections.get(key) or "").strip()
        lines.append("")
        lines.append(f"## {heading}")
        lines.append(text if text else _EMPTY_PLACEHOLDER)
    body = "\n".join(lines) + "\n"
    return dump(meta, body)


# Heading -> field, for parsing the body back into sections on read.
_HEADING_TO_FIELD = {h: k for k, h in WORKAREA_SECTIONS.items()}
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


def _parse_sections(body: str) -> dict[str, str]:
    """Extract the canonical sections from a rendered body.

    Splits on ``## <heading>`` lines matching a known heading; text up to the
    next ``## `` heading (or EOF) is that section's body. The empty placeholder
    round-trips to "". Unknown headings are ignored so hand-added prose between
    sections never corrupts the parse."""
    out = _empty_sections()
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            text = "\n".join(buf).strip()
            out[current] = "" if text == _EMPTY_PLACEHOLDER else text

    for line in body.splitlines():
        m = _SECTION_HEADING_RE.match(line)
        if m:
            flush()
            heading = m.group(1).strip()
            field = _HEADING_TO_FIELD.get(heading)
            current = field
            buf = []
            continue
        if current is not None:
            buf.append(line)
    flush()
    return out


def read(data_dir: Path, slug: str, global_user_id: str) -> dict:
    """Read the working-area doc for (slug, user).

    Returns a stable shape whether or not the doc exists::

        {
            "slug": ..., "global_user_id": ..., "exists": bool,
            "updated": <iso str | None>,
            "sections": {<every WORKAREA_SECTIONS key>: <str>},
        }

    A missing doc yields ``exists=False`` and an all-empty future-focused
    skeleton, so a caller can boot the same way whether or not continuity was
    ever written."""
    s = _safe_segment(slug)
    g = _safe_segment(global_user_id)
    p = workarea_path(data_dir, s, g)
    if not p.exists():
        return {
            "slug": s, "global_user_id": g, "exists": False,
            "updated": None, "sections": _empty_sections(),
        }
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        log.warning("conversation_workarea_store: unreadable doc at %s", p)
        return {
            "slug": s, "global_user_id": g, "exists": False,
            "updated": None, "sections": _empty_sections(),
        }
    parsed = parse_or_none(raw)
    meta, body = parsed if parsed else ({}, raw)
    return {
        "slug": s,
        "global_user_id": g,
        "exists": True,
        "updated": meta.get("updated"),
        "sections": _parse_sections(body),
    }


def upsert(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    *,
    sections: dict[str, str] | None = None,
    timestamp: str | None = None,
) -> dict:
    """Create-or-update the working-area doc with a PARTIAL section merge.

    ``sections`` maps a subset of ``WORKAREA_SECTIONS`` keys to new text; only
    those sections change, the rest are preserved from the existing doc. An
    unknown section key raises ``ValueError`` (closed set — a typo can't create
    an orphan section).

    Idempotent: if the merged sections equal what is already on disk, the file
    (bytes + ``updated`` stamp) is left untouched. So re-saving an unchanged doc
    is a true no-op and ``updated`` records the last *change*. Otherwise the doc
    is rewritten atomically and ``updated`` is set to ``timestamp`` (default
    now, UTC ISO-8601).

    Returns the post-upsert ``read`` shape."""
    s = _safe_segment(slug)
    g = _safe_segment(global_user_id)
    updates = sections or {}
    bad = set(updates) - set(WORKAREA_SECTIONS)
    if bad:
        raise ValueError(f"unknown workarea section(s): {sorted(bad)!r}")

    p = workarea_path(data_dir, s, g)
    with _write_lock:
        existing = read(data_dir, s, g)
        merged = dict(existing["sections"])
        for k, v in updates.items():
            merged[k] = "" if v is None else str(v)

        # Idempotent no-op: same content already on disk -> leave it untouched.
        if existing["exists"] and merged == existing["sections"]:
            return existing

        updated = timestamp or _now_iso()
        content = _render(s, g, merged, updated)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".",
                                        suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_name, p)  # atomic
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    return read(data_dir, s, g)


def assemble_resume_context(
    data_dir: Path,
    slug: str,
    global_user_id: str,
    *,
    history_limit: int = 20,
) -> dict:
    """Build the boot context a fresh conversational session resumes from.

    Pure read — combines the future-focused working-area doc (``read``) with the
    most recent ``history_limit`` records from the conversation history store
    (T-0489 ``conversation_store``), in chronological order. This is what a
    session spawned for a new message after a recycle reads to pick the dialogue
    back up.

    Returns::

        {
            "slug": ..., "global_user_id": ...,
            "workarea": <read() shape>,
            "recent_messages": [<chronological tail, up to history_limit>],
            "history_total": <full thread length>,
        }
    """
    # Imported lazily/locally so this module's only hard dependency at import
    # time is the frontmatter helper; the sibling store is pulled in on use.
    from app import conversation_store

    s = _safe_segment(slug)
    g = _safe_segment(global_user_id)
    workarea = read(data_dir, s, g)

    limit = max(0, int(history_limit))
    # Peek total first so we can window onto just the chronological tail.
    head = conversation_store.list_messages(data_dir, s, g, limit=0, offset=0)
    total = int(head.get("total", 0))
    if limit == 0 or total == 0:
        recent: list[dict] = []
    else:
        offset = max(0, total - limit)
        page = conversation_store.list_messages(
            data_dir, s, g, limit=limit, offset=offset
        )
        recent = page.get("messages", [])

    return {
        "slug": s,
        "global_user_id": g,
        "workarea": workarea,
        "recent_messages": recent,
        "history_total": total,
    }
