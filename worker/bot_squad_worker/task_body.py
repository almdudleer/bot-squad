"""Parse + compose the three-section task body schema (worker copy).

Mirror of `api/app/task_body.py`. Duplicated by design to keep worker
and api independent — see Phase 7 spec. The two files are pinned
byte-identical from `from __future__ import annotations` onward by the
mirror registry in `worker/tests/test_module_mirrors.py` (T-0729/T-0743),
gated on every push by `scripts/lint/module_mirrors.py`: a fix that lands
in only one copy is exactly how T-0714 shipped broken.
"""
from __future__ import annotations

import re

# Any level-2 heading. THE section boundary — one rule, used by both the
# read-parse path (`parse_body`) and the write-protection path (`_section_span`
# → `regraft_verbatim` / `regraft_progress`) so the two can never again
# disagree about where a section ends (T-0729: they did, and 414 of 700 tickets
# rendered agent-authored DoD/Context text inside the human-only verbatim
# block).
_ANY_H2_RE = re.compile(r"(?im)^##\s+\S")

# Which canonical section a level-2 heading opens. Prefix + word-boundary, so a
# decorated heading still counts: `## Context (WS-1 gap analysis)` and
# `## Verbatim request — source of truth (human-only, do not edit)` (60 live
# tickets) are the canonical sections, not unknown ones.
_CANONICAL_HEADING_RE = re.compile(r"(?i)^##\s+(verbatim request|context|progress)\b")

_PROGRESS_MAX_CHARS = 4000


def _heading_line(text: str, m: re.Match[str]) -> str:
    """The full heading line an `_ANY_H2_RE` match starts."""
    nl = text.find("\n", m.start())
    return text[m.start():] if nl < 0 else text[m.start():nl]


def _section_key(heading_line: str) -> str | None:
    """`verbatim` / `context` / `progress` for a canonical heading, else None."""
    m = _CANONICAL_HEADING_RE.match(heading_line)
    if m is None:
        return None
    key = m.group(1).lower()
    return "verbatim" if key == "verbatim request" else key


def parse_body(text: str) -> dict[str, str]:
    """Split a task body into {verbatim, context, progress}.

    Each section runs from its heading to the next level-2 heading of ANY name
    (or EOF), so agent-authored sections are excluded rather than absorbed.

    A body with no `## Verbatim request` heading is treated as legacy: the text
    up to the first canonical heading — the whole body when there is none —
    goes into `verbatim`, unchanged from the pre-T-0729 behaviour (13 live
    planning tickets whose ask genuinely IS the whole body).
    """
    text = text or ""
    heads = list(_ANY_H2_RE.finditer(text))
    if not heads:
        return {"verbatim": text.strip(), "context": "", "progress": ""}

    keys = [_section_key(_heading_line(text, m)) for m in heads]
    out: dict[str, str] = {"verbatim": "", "context": "", "progress": ""}

    if "verbatim" not in keys:
        # Legacy body: verbatim is whatever precedes the first CANONICAL
        # heading (non-canonical headings are part of that legacy text).
        canonical = [m for m, k in zip(heads, keys) if k is not None]
        cut = canonical[0].start() if canonical else len(text)
        out["verbatim"] = text[:cut].strip()

    for i, (m, key) in enumerate(zip(heads, keys)):
        if key is None:
            continue
        start = len(_heading_line(text, m)) + m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out[key] = text[start:end].strip()

    return out


def is_legacy_body(text: str) -> bool:
    """True when ``text`` has NO ``## Verbatim request`` heading — i.e.
    :func:`parse_body`'s ``verbatim`` came from the legacy whole-body fallback,
    not from a recorded request (T-0733).

    Read-only companion to the parser: it changes nothing about how a body is
    split, it only reports WHICH branch produced ``verbatim`` so consumers can
    LABEL it honestly. Presenting a legacy planning document (T-0553 is 11k
    chars of ``## 0. Headline framing`` / dependency-order prose) as "what you
    asked for" is the same mislabelling T-0729 existed to stop, arriving by a
    different route.

    Shares :func:`_section_span` with the parser and the write-protection path,
    so the three can never disagree about what counts as a verbatim heading.
    """
    return _section_span(text or "", "verbatim") is None


def _section_span(body: str, key: str) -> tuple[int, int] | None:
    """Char span of canonical section ``key`` — its heading through just before
    the NEXT ``## `` heading (or EOF). ``None`` if absent."""
    for m in _ANY_H2_RE.finditer(body):
        if _section_key(_heading_line(body, m)) != key:
            continue
        nxt = _ANY_H2_RE.search(body, m.end())
        return m.start(), (nxt.start() if nxt else len(body))
    return None


def _verbatim_span(body: str) -> tuple[int, int] | None:
    """Char span of the ``## Verbatim request`` section, or ``None``."""
    return _section_span(body, "verbatim")


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


def _progress_span(body: str) -> tuple[int, int] | None:
    """Char span of the ``## Progress`` section, or ``None``."""
    return _section_span(body, "progress")


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


# T-0835: a progress note is stored as ONE physical line — `- <ts> · <sid> ·
# <text>` — and at least four independent consumers parse the feed line by line:
# `web/src/pages/TaskDetail.tsx:parseProgressList`, `bsq session-search`'s
# `_progress_lines_by_sid`, and `drift.py`'s two `^-\s*<ts>\s*·` regexes. That
# one-line-per-note shape is a DECIDED contract, written down in
# `docs/roadmap/verbatim-contract.md` ("- <iso_ts> · <sid> · <short note>   #
# append-only audit feed"), not an accident — so the fix for the silent
# flattening is NOT to widen the physical format.
#
# What the old code did was `re.sub(r"\s+", " ", text)`: every newline, tab and
# run of spaces became a single space, silently, while the same function
# REFUSED loudly to lose a single character off the end. Measured: p536's
# 2555-char `sweep.sh` paste on T-0829 became one line beginning
# `#!/usr/bin/env bash #`, so every byte survived and the script was INERT —
# everything after that first `#` is a shell comment and the comment/code
# boundaries are unrecoverable by eye. Content survived; structure did not; and
# nothing said so, which is the defect (a well-formed success guaranteeing the
# wrong invariant).
#
# So structure is preserved AT REST as an escape sequence — the note stays one
# physical line and no consumer changes — and expanded AT RENDER by
# `decode_progress_text`. `encode`→`decode` is byte-identity for every input
# (pinned by an exhaustive round-trip over the alphabet that can collide).
#
# Known bound, stated here rather than left for someone to find: a note written
# BEFORE this change containing the two literal characters `\` `n` decodes to a
# line break. Four such lines exist across the live backlog's ~9000 notes
# (measured 2026-07-30). The direction is benign — a spurious break in a
# historical note, never lost content — and every note written after this
# change round-trips exactly.
#: The characters that cannot survive on a one-line record, and what they are
#: written as. Kept tiny on purpose — the fewer characters this touches, the
#: fewer notes it changes at rest.
_ONELINE_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
#: Escape char → the character it stands for, plus ``\\`` for a backslash the
#: encoder had to protect. This is the DECODER's whole alphabet: anything else
#: after a backslash is not an escape and is passed through untouched.
_ONELINE_DECODE = {esc[-1]: raw for raw, esc in _ONELINE_ESCAPES.items()}
_ONELINE_DECODE["\\"] = "\\"


def encode_progress_text(text: str) -> str:
    """Fold ``text`` onto one physical line, losslessly and invertibly.

    MINIMAL by design: a backslash is doubled ONLY when it would otherwise be
    read as opening an escape. Doubling every backslash would also be correct,
    but it would rewrite the durable md for a large and entirely innocent
    population of notes — this repo's notes quote regexes
    (``re.sub(r"\\s+", ...)``) and paths constantly, and the ticket file is a
    HUMAN-READABLE record, not just a decoder's input. Consequence of the
    minimal rule: a note is stored byte-identically unless it really contains a
    newline, tab, CR, or a backslash that collides.

    Built RIGHT-TO-LEFT, and that is not a style choice. Whether a backslash
    needs protecting depends on what its neighbour looks like AFTER encoding,
    not before: ``\\`` + a real newline encodes to ``\\`` + ``\\n``, and a
    left-to-right pass reading the RAW neighbour sees a newline (not an escape
    letter), leaves the backslash bare, and emits ``\\\\n`` — which decodes to
    backslash-n, silently turning a line break into two characters. Caught by
    the exhaustive round-trip test, not by inspection.
    """
    out: list[str] = []
    for ch in reversed(text or ""):
        esc = _ONELINE_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch == "\\" and out and out[-1][0] in _ONELINE_DECODE:
            out.append("\\\\")
        else:
            out.append(ch)
    return "".join(reversed(out))


def decode_progress_text(text: str) -> str:
    """Inverse of :func:`encode_progress_text` — the READ half of the round-trip.

    A single left-to-right scan, never a chain of ``replace`` calls: chained
    replaces would decode the output of an earlier step (``\\\\n`` → ``\\n`` →
    newline) and silently un-escape text the author wrote literally.

    An UNKNOWN escape is left EXACTLY as it was — ``\\s`` in a note quoting
    ``re.sub(r"\\s+", ...)`` must survive as two characters, and pre-T-0835
    notes are full of them.
    """
    out: list[str] = []
    i = 0
    n = len(text or "")
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _ONELINE_DECODE:
                out.append(_ONELINE_DECODE[nxt])
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _sanitize_progress_text(text: str) -> str:
    """Fold a note onto one line WITHOUT losing its shape; raise on over-cap text.

    Two guarantees, both stated in the terms a CALLER cares about rather than
    the terms this function happens to enforce (T-0835's reusable lesson — the
    old docstring's "Collapse newlines to spaces" was read as a formatting
    detail by four sessions in a row, including one that quoted the clause
    beside it):

    * NOTHING IS LOST. The stored form is reversible by
      :func:`decode_progress_text`; a note round-trips byte-identically apart
      from leading/trailing whitespace, which is stripped.
    * NOTHING IS TRUNCATED SILENTLY. Over-cap text raises, naming the length,
      the cap and the remedy (F-2026-07-05-bsq-30844bca41).

    The cap is measured on the STORED form, since that is what has to fit; when
    escaping made it longer than what the caller passed, the message says so
    rather than quoting a number the caller cannot reconcile with their input.
    """
    raw = (text or "").strip()
    s = encode_progress_text(raw)
    if len(s) > _PROGRESS_MAX_CHARS:
        size = (
            f"{len(s)} chars" if len(s) == len(raw)
            else f"{len(raw)} chars ({len(s)} stored, newlines escaped)"
        )
        raise ValueError(
            f"progress note is {size}, over the {_PROGRESS_MAX_CHARS}-char cap — "
            "refusing to truncate (silent loss, F-2026-07-05-bsq-30844bca41). "
            "Split the note or put long content in the ticket's ## Context section."
        )
    return s


def append_progress(body: str, ts: str, sid: str, text: str) -> str:
    """Append `- <ts> · <sid> · <text>` to the Progress section.

    Creates the Progress section (at the end) if it's missing. Text is folded
    onto one line REVERSIBLY (T-0835 — `decode_progress_text` is the read half);
    text over _PROGRESS_MAX_CHARS raises ValueError rather than truncating.

    T-0729: this is a raw-text SPLICE into the Progress section, not a
    parse→compose round-trip. Recomposing from the three parsed sections would
    silently delete every non-canonical section (`## DoD`, `## Observed`, …) —
    on 414 of 700 live tickets — the moment a session filed a progress note.
    Splicing preserves the rest of the body byte-for-byte.
    """
    clean = _sanitize_progress_text(text)
    if not clean:
        raise ValueError("empty progress text")
    line = f"- {ts} · {sid} · {clean}"
    span = _progress_span(body or "")
    if span is None:
        head = (body or "").rstrip("\n")
        return (head + "\n\n" if head else "") + f"## Progress\n\n{line}\n"
    block = body[span[0]:span[1]].rstrip("\n")
    rest = body[span[1]:].lstrip("\n")
    return body[:span[0]] + block + "\n" + line + ("\n\n" + rest if rest else "\n")
