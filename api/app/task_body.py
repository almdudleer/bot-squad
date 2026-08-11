"""Parse + compose the task body schema.

Body layout (canonical):

    ## Stakeholder notes

    <the stakeholder's own words — the original ask, then every later
     quote as a dated bullet. Human-sourced; never rewritten by sessions.>

    ## Context

    <the WORKING AREA every session edits in place — current state, not
     a chronological log>

    ## Progress

    - <iso_ts> · <sid> · <short note>
    - ...

T-0767 (stakeholder 2026-08-11) reshaped this from three co-equal authored
sections into **two authored artifacts plus a machine feed**, because the
third was eating the other two:

* `## Stakeholder notes` is his words, in one place. Previously his mid-ticket
  clarifications were recorded by whichever session heard them as ordinary
  `## Progress` notes, so the stakeholder's guidance and the sessions'
  narration shared one feed — the OVERLAP he asked to remove («вместо трех
  артефактов останутся два не пересекающихся»). `## Verbatim request` is the
  LEGACY ALIAS of this heading and parses identically, so the 809 live tickets
  need no migration; the rename is what makes the section's real job honest,
  since it stopped being only "the request" the first time he added a
  clarification.
* `## Context` is the working area. Sessions edit it IN PLACE to say what is
  true NOW («пусть редактируют конечное состояние сразу, эта история не
  важна»), rather than appending another narration of how it got that way.
* `## Progress` stays, demoted, because the watchdogs read it and not because
  it is a place to write prose: `drift.py` and `autopilot.py` key liveness off
  its last timestamp and `bsq session-search` attributes work by its SIDs.
  Deleting it would blind those; capping it is what stops it growing.

Measured on the live backlog the day this landed (809 tickets): `## Progress`
was **57.4% of all backlog bytes** — 2.74MB / 3340 notes — and the median note
was exactly 240 chars, the cap the role contracts have always stated, while
the code enforced 4000. The 44% of notes that overran that stated cap carried
**70% of all note text**. So the dominant source of bloat was a rule that
existed only as prose; see `_PROGRESS_MAX_CHARS`.

Legacy bodies without `## Verbatim request` are tolerated: the whole body
is treated as verbatim and the other sections are empty. `is_legacy_body`
reports that branch so a consumer can label such text as the ticket body
rather than as the stakeholder's ask (T-0733). Section headings are
case-insensitive; sections may appear in any order; missing sections become
empty strings.

A section ends at the NEXT level-2 heading of any name (T-0729). Non-canonical
sections an agent added (`## DoD`, `## Observed`, `## Scope`, …) are therefore
boundaries, not content: they are simply excluded from the parse rather than
absorbed into whichever canonical section precedes them. They are not surfaced
by this module at all — the md on disk stays their SSOT — but they are never
DROPPED either: every writer here (`append_progress`, `regraft_*`) splices in
place instead of recomposing from the parsed sections.
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
#
# T-0767: `stakeholder notes` and `verbatim request` are TWO SPELLINGS OF ONE
# SECTION — the stakeholder artifact — and both map to the `verbatim` key. The
# alias is what makes the rename free: no live ticket has to be rewritten, and
# a ticket carrying the old heading keeps every protection it had.
_CANONICAL_HEADING_RE = re.compile(
    r"(?i)^##\s+(stakeholder notes|verbatim request|context|progress)\b")

#: Heading emitted for the stakeholder artifact on anything written from now on.
STAKEHOLDER_HEADING = "## Stakeholder notes"

#: A level-2 heading inside text destined for INSIDE a section. Demoted to `###`
#: by `set_context`, because `_ANY_H2_RE` would otherwise treat it as the end of
#: that section — see `set_context`'s docstring for what that costs.
_H2_IN_SECTION_RE = re.compile(r"(?m)^##\s+")

# T-0767 RESTORES 240 — and the history matters, because 240 was already tried
# and deliberately abandoned. Read F-2026-07-05-bsq-30844bca41 before touching
# this number again.
#
# Originally the cap was 240 and it TRUNCATED SILENTLY, which clipped a sacred
# stakeholder verbatim on T-0566 twice. The report offered three remedies:
# "either raise the cap, hard-fail on overflow with a clear error, or auto-spill
# long notes to ## Context". The fix took the first two — 4000 plus a loud
# refusal — and could not take the third, because `## Context` had no writer to
# spill INTO. So the cap was raised to protect content that was only in the
# feed because it had nowhere else to go.
#
# T-0767 builds the missing third option, which removes the reason for the
# raise: his words now have `append_stakeholder_quote` and their own section at
# `_STAKEHOLDER_MAX_CHARS`, and durable session detail now has `set_context`.
# Nothing that justified 4000 has to live in the feed any more. THE TWO CHANGES
# ARE COUPLED: lowering this without those writers would re-break exactly what
# the feedback reported.
#
# What the restored cap buys, measured on the live backlog the day it landed:
# `## Progress` was 57.4% of all backlog bytes (2.74MB / 3340 notes), the median
# note was exactly 240 — the number the role contracts have always stated — and
# the 44% of notes that overran that stated cap carried 70% of all note text.
# The refusal stays loud (never truncation), and now names a remedy that exists.
_PROGRESS_MAX_CHARS = 240


def _heading_line(text: str, m: re.Match[str]) -> str:
    """The full heading line an `_ANY_H2_RE` match starts."""
    nl = text.find("\n", m.start())
    return text[m.start():] if nl < 0 else text[m.start():nl]


def _section_key(heading_line: str) -> str | None:
    """`verbatim` / `context` / `progress` for a canonical heading, else None.

    `## Stakeholder notes` and `## Verbatim request` both answer `verbatim`
    (T-0767) — one artifact, two spellings, so every consumer of the parse and
    every write-protection path treats them identically without knowing which
    spelling a given ticket happens to carry.
    """
    m = _CANONICAL_HEADING_RE.match(heading_line)
    if m is None:
        return None
    key = m.group(1).lower()
    return "verbatim" if key in ("verbatim request", "stakeholder notes") else key


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
        chunk = text[start:end].strip()
        # T-0767: a transitional ticket can carry BOTH spellings of the
        # stakeholder section (`## Verbatim request` from before the rename and
        # `## Stakeholder notes` added after). Plain assignment would let the
        # later heading silently REPLACE the earlier one — dropping the
        # stakeholder's original ask, the one thing this schema exists to
        # protect. Concatenate in document order instead: a duplicate section
        # is a cosmetic problem, a vanished ask is not.
        out[key] = f"{out[key]}\n\n{chunk}".strip() if out[key] else chunk

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


def _section_spans(body: str, key: str) -> list[tuple[int, int]]:
    """EVERY char span of canonical section ``key``, in document order.

    Normally one, but the stakeholder artifact has two legal spellings during
    the T-0767 transition, so a ticket may hold two spans that are both it.
    The write-protection path must cover ALL of them: protecting only the first
    would leave the second freely rewritable, which is the whole guarantee
    lost through a gap rather than through a bug.
    """
    spans: list[tuple[int, int]] = []
    for m in _ANY_H2_RE.finditer(body):
        if _section_key(_heading_line(body, m)) != key:
            continue
        nxt = _ANY_H2_RE.search(body, m.end())
        spans.append((m.start(), nxt.start() if nxt else len(body)))
    return spans


def _section_span(body: str, key: str) -> tuple[int, int] | None:
    """Char span of the FIRST canonical section ``key`` — its heading through
    just before the NEXT ``## `` heading (or EOF). ``None`` if absent."""
    spans = _section_spans(body, key)
    return spans[0] if spans else None


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
    orig_spans = _section_spans(original_body, "verbatim")
    if not orig_spans:
        return new_body
    orig_block = "\n\n".join(
        original_body[a:b].rstrip("\n") for a, b in orig_spans)

    # T-0767: excise EVERY stakeholder span the caller sent (either spelling),
    # then put the on-disk one back where the first of them stood. Splicing
    # only over the first span would silently keep a second, caller-authored
    # copy — a body PATCH could then add words under `## Stakeholder notes`
    # that the stakeholder never said, which is exactly what this function
    # exists to make impossible.
    new_spans = _section_spans(new_body, "verbatim")
    if not new_spans:
        return orig_block + "\n\n" + new_body.lstrip("\n")
    kept: list[str] = []
    cursor = 0
    for a, b in new_spans:
        kept.append(new_body[cursor:a])
        cursor = b
    kept.append(new_body[cursor:])
    head = kept[0]
    tail = "".join(kept[1:]).lstrip("\n")
    return head + orig_block + ("\n\n" + tail if tail else "\n")


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
    """Emit canonical body. Empty sections are skipped entirely.

    New bodies get `## Stakeholder notes` (T-0767). Nothing re-reads the
    spelling to decide anything — `_section_key` maps both to `verbatim` — so
    this changes what fresh tickets LOOK like without splitting the corpus into
    two behaviours.
    """
    parts: list[str] = []
    v = (verbatim or "").strip()
    c = (context or "").strip()
    p = (progress or "").strip()
    if v:
        parts.append(f"{STAKEHOLDER_HEADING}\n\n{v}\n")
    if c:
        parts.append(f"## Context\n\n{c}\n")
    if p:
        parts.append(f"## Progress\n\n{p}\n")
    return "\n".join(parts)


def append_stakeholder_quote(body: str, ts: str, source: str, text: str) -> str:
    """Append a dated stakeholder quote to the stakeholder section (T-0767).

    This is the writer that gives his words ONE home. Before it, a
    clarification he gave mid-ticket was recorded by whichever session heard it
    as an ordinary `## Progress` note — so his guidance sat in the same feed as
    session narration, got diluted by it, and got trimmed with it. (T-0767's
    own `## Progress` contains three such notes, which is how the ticket
    demonstrates its own bug.)

    Appends UNDER the original ask rather than replacing it, so the section
    reads as he asked — the original formulation first, every later quote
    beneath it — and stays one artifact rather than two.

    Written as a raw-text splice for the same reason as `append_progress`: a
    parse→compose round-trip would delete every non-canonical section on the
    ticket. Text is folded onto one line reversibly, and the quote carries its
    `source` (a date, a message id, whatever names where the words came from)
    so a later reader can check provenance without trusting the transcriber.
    """
    clean = _sanitize_stakeholder_text(text)
    if not clean:
        raise ValueError("empty stakeholder quote")
    line = f"- {ts} · {source} · {clean}"
    spans = _section_spans(body or "", "verbatim")
    if not spans:
        head = (body or "").rstrip("\n")
        return ((head + "\n\n" if head else "")
                + f"{STAKEHOLDER_HEADING}\n\n{line}\n")
    # Append under the LAST stakeholder span, so on a transitional ticket
    # carrying both spellings the newest quote lands with the newest ones.
    start, end = spans[-1]
    block = body[start:end].rstrip("\n")
    rest = body[end:].lstrip("\n")
    # The original ask is PROSE and the quotes below it are a list. Markdown
    # needs a blank line between the two or the first bullet is swallowed into
    # the preceding paragraph — which would render his clarification as part of
    # the original sentence, the one confusion this section exists to prevent.
    # Subsequent quotes append straight onto the list.
    tail = block.rstrip().rsplit("\n", 1)[-1].lstrip()
    sep = "\n" if tail.startswith("- ") else "\n\n"
    return body[:start] + block + sep + line + ("\n\n" + rest if rest else "\n")


def set_context(body: str, text: str) -> str:
    """Replace the `## Context` section wholesale (T-0767).

    The WORKING-AREA writer: sessions are asked to keep Context describing what
    is true now, and until this existed there was no way to do that short of a
    whole-body PATCH — which is precisely why everything got appended to
    `## Progress` instead. Appending was one command; editing was not. That
    ergonomic gap, not a preference for narration, is what made the feed 57% of
    the backlog.

    Replaces rather than appends, on purpose — the point is the CURRENT state,
    not another layer of history.

    Raw-text splice, like the other writers, so non-canonical sections survive.
    A body with no Context section gets one inserted before `## Progress` when
    that exists (keeping the machine feed last) and appended otherwise.

    LEVEL-2 HEADINGS IN ``text`` ARE DEMOTED TO LEVEL 3, and that is load-
    bearing rather than cosmetic. Since T-0729 ANY ``## `` heading ends a
    section, so a working area written with the structure a working area
    naturally wants — `## What shipped`, `## Open questions` — would parse as
    several bogus top-level sections and `parse_body` would surface only the
    text above the first one. Nothing is lost on disk, which is what makes it
    dangerous: the loss appears at the READ layer, so the brief and the board
    would show a truncated working area while the file looked complete.
    Measured the first time this function was used on a real ticket: a 3.4KB
    handover parsed back as 182 chars.

    Demotion keeps every character and every heading, one level down, where
    `###` is not a boundary. The alternative — refusing text containing `## ` —
    is louder but makes the working area unable to hold the structure it exists
    to hold.
    """
    new = _H2_IN_SECTION_RE.sub("### ", (text or "").strip())
    span = _section_span(body or "", "context")
    if span is None:
        block = f"## Context\n\n{new}\n" if new else ""
        if not block:
            return body
        prog = _section_span(body or "", "progress")
        if prog is None:
            head = (body or "").rstrip("\n")
            return (head + "\n\n" if head else "") + block
        return body[:prog[0]] + block + "\n" + body[prog[0]:]
    rest = body[span[1]:].lstrip("\n")
    block = f"## Context\n\n{new}\n" if new else ""
    return body[:span[0]] + block + ("\n" + rest if rest else "")


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


#: A stakeholder quote is capped far higher than a session note, and the
#: asymmetry is deliberate: a note is a session summarising its own work and can
#: always be shortened, whereas his words are the one thing on the ticket that
#: must arrive unparaphrased. 4000 is the cap notes lived under before T-0767 —
#: known to work for the one-line format — and over-cap still refuses rather
#: than truncating, naming a remedy that keeps the words intact.
_STAKEHOLDER_MAX_CHARS = 4000


def _fold_one_line(text: str, cap: int, remedy: str) -> str:
    """Fold ``text`` onto one physical line reversibly; raise past ``cap``.

    Shared by both writers so the two can never disagree about encoding — only
    about how much they allow and what they suggest instead.

    The cap is measured on the STORED form, since that is what has to fit; when
    escaping made it longer than what the caller passed, the message says so
    rather than quoting a number the caller cannot reconcile with their input.
    """
    raw = (text or "").strip()
    s = encode_progress_text(raw)
    if len(s) > cap:
        size = (
            f"{len(s)} chars" if len(s) == len(raw)
            else f"{len(raw)} chars ({len(s)} stored, newlines escaped)"
        )
        raise ValueError(
            f"text is {size}, over the {cap}-char cap — refusing to truncate "
            f"(silent loss, F-2026-07-05-bsq-30844bca41). {remedy}"
        )
    return s


def _sanitize_stakeholder_text(text: str) -> str:
    """Fold a stakeholder quote onto one line; raise past the quote cap."""
    return _fold_one_line(
        text, _STAKEHOLDER_MAX_CHARS,
        "A quote this long belongs under the `## Stakeholder notes` heading as "
        "prose rather than as a one-line bullet — edit the ticket md directly "
        "so his words stay whole.",
    )


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

    The cap is the one the role contracts have always stated (T-0767); the
    remedy names the working area, because a note long enough to trip it is
    almost always durable detail that belongs in `## Context` where the next
    session will actually read it, not in a feed that gets trimmed.
    """
    return _fold_one_line(
        text, _PROGRESS_MAX_CHARS,
        "A progress note is a one-line checkpoint. Put the durable version in "
        "the ticket's working area — `bsq ticket context <id> --file <f>` "
        "replaces `## Context` with what is true now — and leave a short "
        "pointer here.",
    )


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
