"""Parse + compose the three-section task body schema.

Body layout (canonical):

    ## Verbatim request

    <stakeholder's exact words — never rewritten by sessions>

    ## Context

    <optional TL-added clarification>

    ## Progress

    - <iso_ts> · <sid> · <short note>
    - ...

Legacy bodies without `## Verbatim request` are tolerated: the whole body
is treated as verbatim and the other sections are empty. Section headings
are case-insensitive; sections may appear in any order; missing sections
become empty strings.
"""
from __future__ import annotations

import re

_HEADING_RE = re.compile(r"(?im)^##\s+(verbatim request|context|progress)\s*$")

_PROGRESS_MAX_CHARS = 240


def parse_body(text: str) -> dict[str, str]:
    """Split a task body into {verbatim, context, progress}.

    A body with no `## Verbatim request` heading is treated as legacy:
    the whole text goes into `verbatim`. Section content is returned
    with leading/trailing whitespace stripped.
    """
    text = text or ""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return {"verbatim": text.strip(), "context": "", "progress": ""}

    out: dict[str, str] = {"verbatim": "", "context": "", "progress": ""}
    # If anything precedes the first heading, treat it as legacy verbatim
    # only when no explicit `verbatim request` heading exists.
    has_verbatim_heading = any(m.group(1).lower() == "verbatim request" for m in matches)
    if not has_verbatim_heading and matches[0].start() > 0:
        out["verbatim"] = text[: matches[0].start()].strip()

    for i, m in enumerate(matches):
        key = m.group(1).lower().replace(" ", "_")
        if key == "verbatim_request":
            key = "verbatim"
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[key] = text[start:end].strip()

    # Legacy fallback: if there are headings but none are verbatim and
    # there was no preceding text, leave verbatim empty (sections are
    # explicit). This matches "well-formed but missing verbatim" cases.
    return out


_VERBATIM_HEADING_RE = re.compile(r"(?im)^##\s+verbatim request\s*$")
# Any level-2 heading (used to find where the verbatim section ends).
_ANY_H2_RE = re.compile(r"(?im)^##\s+\S")


def _verbatim_span(body: str) -> tuple[int, int] | None:
    """Char span of the ``## Verbatim request`` section — its heading through
    just before the NEXT ``## `` heading (or EOF). ``None`` if absent."""
    m = _VERBATIM_HEADING_RE.search(body)
    if m is None:
        return None
    nxt = _ANY_H2_RE.search(body, m.end())
    return m.start(), (nxt.start() if nxt else len(body))


def regraft_verbatim(original_body: str, new_body: str) -> str:
    """Return ``new_body`` with its ``## Verbatim request`` section forced back
    to ``original_body``'s (T-0289).

    Verbatim is human-only: a body replace (e.g. ``PATCH /backlog``) must never
    rewrite it. The splice is raw-text and scoped strictly to the verbatim
    heading, so every OTHER section in ``new_body`` (Context, Progress, and
    non-canonical sections like Finding/DoD on QA tickets) is preserved exactly
    as submitted. If the original had no verbatim section there is nothing to
    protect; if ``new_body`` dropped the heading, the original is re-prepended.
    """
    orig = _verbatim_span(original_body)
    if orig is None:
        return new_body
    orig_block = original_body[orig[0]:orig[1]].rstrip("\n")
    new = _verbatim_span(new_body)
    if new is None:
        return orig_block + "\n\n" + new_body.lstrip("\n")
    return new_body[:new[0]] + orig_block + "\n\n" + new_body[new[1]:].lstrip("\n")


_PROGRESS_HEADING_RE = re.compile(r"(?im)^##\s+progress\s*$")


def _progress_span(body: str) -> tuple[int, int] | None:
    """Char span of the ``## Progress`` section — its heading through just
    before the NEXT ``## `` heading (or EOF). ``None`` if absent."""
    m = _PROGRESS_HEADING_RE.search(body)
    if m is None:
        return None
    nxt = _ANY_H2_RE.search(body, m.end())
    return m.start(), (nxt.start() if nxt else len(body))


def regraft_progress(original_body: str, new_body: str) -> str:
    """Return ``new_body`` with its ``## Progress`` section forced back to
    ``original_body``'s (T-0335 item-14).

    Progress is an append-only audit feed (T-0238) whose SSOT is on disk — a
    board "Edit body" PATCH must never rewrite or drop it. Mirrors
    :func:`regraft_verbatim`: the splice is raw-text and scoped strictly to the
    Progress heading, so every OTHER section the caller submitted (Verbatim,
    Context, non-canonical sections) is preserved exactly. If the original had
    no Progress section there is nothing to protect; if ``new_body`` dropped the
    heading, the original feed is re-appended at the end.
    """
    orig = _progress_span(original_body)
    if orig is None:
        return new_body
    orig_block = original_body[orig[0]:orig[1]].rstrip("\n")
    new = _progress_span(new_body)
    if new is None:
        return new_body.rstrip("\n") + "\n\n" + orig_block + "\n"
    return new_body[:new[0]] + orig_block + "\n\n" + new_body[new[1]:].lstrip("\n")


def compose_body(verbatim: str, context: str, progress: str) -> str:
    """Emit canonical body. Empty sections are skipped entirely."""
    parts: list[str] = []
    v = (verbatim or "").strip()
    c = (context or "").strip()
    p = (progress or "").strip()
    if v:
        parts.append(f"## Verbatim request\n\n{v}\n")
    if c:
        parts.append(f"## Context\n\n{c}\n")
    if p:
        parts.append(f"## Progress\n\n{p}\n")
    return "\n".join(parts)


def _sanitize_progress_text(text: str) -> str:
    """Collapse newlines to spaces, squeeze whitespace, cap at 240 chars."""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if len(s) > _PROGRESS_MAX_CHARS:
        s = s[:_PROGRESS_MAX_CHARS].rstrip()
    return s


def append_progress(body: str, ts: str, sid: str, text: str) -> str:
    """Append `- <ts> · <sid> · <text>` to the Progress section.

    Creates the Progress section if it's missing. Preserves verbatim
    and context exactly. Text is sanitised (newlines collapsed, capped
    at 240 chars).
    """
    clean = _sanitize_progress_text(text)
    if not clean:
        raise ValueError("empty progress text")
    sections = parse_body(body)
    line = f"- {ts} · {sid} · {clean}"
    existing = sections["progress"].rstrip()
    sections["progress"] = (existing + "\n" + line) if existing else line
    return compose_body(sections["verbatim"], sections["context"], sections["progress"])
