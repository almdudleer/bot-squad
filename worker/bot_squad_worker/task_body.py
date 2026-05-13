"""Parse + compose the three-section task body schema (worker copy).

Mirror of `api/app/task_body.py`. Duplicated by design to keep worker
and api independent — see Phase 7 spec.
"""
from __future__ import annotations

import re

_HEADING_RE = re.compile(r"(?im)^##\s+(verbatim request|context|progress)\s*$")

_PROGRESS_MAX_CHARS = 240


def parse_body(text: str) -> dict[str, str]:
    text = text or ""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return {"verbatim": text.strip(), "context": "", "progress": ""}

    out: dict[str, str] = {"verbatim": "", "context": "", "progress": ""}
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
    return out


def compose_body(verbatim: str, context: str, progress: str) -> str:
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
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if len(s) > _PROGRESS_MAX_CHARS:
        s = s[:_PROGRESS_MAX_CHARS].rstrip()
    return s


def append_progress(body: str, ts: str, sid: str, text: str) -> str:
    clean = _sanitize_progress_text(text)
    if not clean:
        raise ValueError("empty progress text")
    sections = parse_body(body)
    line = f"- {ts} · {sid} · {clean}"
    existing = sections["progress"].rstrip()
    sections["progress"] = (existing + "\n" + line) if existing else line
    return compose_body(sections["verbatim"], sections["context"], sections["progress"])
