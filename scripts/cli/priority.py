"""The ONE priority vocabulary: what a writer may store, and what a reader ranks.

Measured on watchrobot as T-0586; fixed here as T-0877.

The defect this closes
----------------------
``bsq task new --priority PRIORITY`` described itself as *"priority value
(frontmatter, verbatim)"* and stored ANY string it was handed. The reader —
:mod:`bot_squad_worker.pickup`'s ranking — understood only ``p1``/``p2``/``p3``.
**Nothing anywhere compared the two vocabularies**, so ``--priority high``
succeeded, produced a normal-looking ticket, and dropped it out of the queue
permanently with ``priority-unparseable:high``. No error, no warning, no second
chance: the writer said "done" on a value that was guaranteed to lose the
ticket.

That is not "somebody filled the field in wrong". ``high`` is exactly the word a
person reaches for, and it was reached for 54 times on one board. The tool
accepted it 54 times.

**Measured on the watchrobot board, 2026-08-11, before this module existed:**
the needs-triage band held 105 tickets and ALL 105 were there for the FORMAT of
this one field — 86 unparseable (high 54 · medium 16 · normal 9 · low 7) and 19
genuinely missing. Zero were there because a human had to decide something,
which is what that band is *for*. The oldest had been invisible for 20.6 days;
among the 54 ``high`` ones sat P1-grade work (telemetry flooding every session's
inbox, a live staging DB reading as a dev DB, a routine engine whose every
spawn-on-breach failed silently).

So both halves live HERE, in one module both sides import, and
``worker/tests/test_priority_vocabulary.py`` asserts they cannot drift apart:
every value the writer accepts must be a value the reader can rank. The bug was
never in either half — it was in the absence of that sentence.

"high = p1" is a decision, not an obvious identity — here is the evidence
------------------------------------------------------------------------
The verdict is **one scale, two vocabularies**, and it was checked before it was
coded rather than assumed:

* Both spellings live in the SAME field, on the same kind of ticket, in the same
  window. On watchrobot ``high``/``medium``/``low`` ran 2026-06-19 → 08-07 and
  ``pN`` ran 2026-07-06 → 08-11 — overlapping, not successive eras with
  different meanings.
* **No document defines either vocabulary.** There is no doc, no schema, no
  help text, and no second field that would make words mean *impact* while
  ``pN`` means *urgency*. Both are simply how urgent the author thought it was.
* Read side by side, the tickets grade the same. ``high`` carries "integration
  dead since <date>" and "live DB reads as dev"; ``low`` carries "confusing
  empty-state copy" and "footer artefact on scroll-up" — the same populations
  ``p1`` and ``p3`` carry.

The mapping is therefore the ordinal one, with the middle word taken as the
middle band (``normal`` and ``medium`` are one rung, not two):

    critical → p0 · high → p1 · medium/normal → p2 · low → p3

**What that costs, stated rather than buried:** on watchrobot it moves 54
tickets into band 1 against 26 that were already there, so the P1 band roughly
triples on the day it lands. That is the honest consequence of reading what
people actually wrote — the alternative (mapping ``high`` down to p2 to keep the
top band small) would be inventing a modesty the authors never expressed. The
table below is the one place to revise if the fleet later decides otherwise.

The THIRD scale, which is deliberately NOT merged
-------------------------------------------------
The web UI writes ``priority`` as an INTEGER SORT KEY for kanban ordering
(``api/app/routes_backlog.py``: a new task gets ``max(open priorities) + 100``,
and a drag-drop reorder inserts at a midpoint — which is why the boards carry
50 · 100 · 150 · 200 · 250 · 300). **That is a position in a list, not a band of
urgency**, and ``200`` does not mean "p2 with zeros". Merging the two would be
the silent guess this module exists to stop, so a multi-digit value is reported
as :data:`ORDERING_KEY_FLAG` — a triage reason that says which scale it is and
that a human has to reconcile them. Still triage, but "two scales, needs a
decision" is an actionable sentence and ``priority-unparseable`` was not.

A SINGLE digit stays a band (``3`` == ``p3``), unchanged from before this module:
the two scales are genuinely indistinguishable there, and re-reading those as
ordering keys would demote real tickets on a guess.
"""
from __future__ import annotations

import re
from typing import Any, Optional

#: The word vocabulary → its band. The mapping argued for in the docstring; this
#: table is the ONE place to revise it. ``normal`` and ``medium`` are the same
#: rung on purpose — they are two spellings of "the middle", not two levels.
WORD_BANDS: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "normal": 2,
    "low": 3,
}

#: ``P<n>`` or a bare ``<n>`` — the shapes the board carried before the words
#: were readable, and the canonical form everything normalises to.
_BAND_RE = re.compile(r"\A[Pp]?([0-9])\Z")

#: Two or more digits: the web UI's kanban ORDERING KEY, never a band. See the
#: docstring — this is a different scale, reported as such rather than guessed.
_ORDERING_KEY_RE = re.compile(r"\A\d{2,}\Z")

#: Triage reasons. Named as constants because they are what an operator READS
#: in the triage band, and because the pickup band a ticket lands in is keyed on
#: their presence, not on their text.
MISSING_FLAG = "priority-missing"
ORDERING_KEY_FLAG = "priority-ordering-key"
UNPARSEABLE_FLAG = "priority-unparseable"

#: Human-facing allowed-form summary — the SAME string in the worker's gate, the
#: CLI's client-side gate and ``bsq task new --help``, so all three surfaces
#: answer a rejected value identically.
ALLOWED_HELP = "p0..p9 (or a bare digit) | critical | high | medium | normal | low"

#: Every canonical spelling a writer may hand in — finite on purpose, so a test
#: can quantify over it and assert the reader ranks every one of them. Case and
#: surrounding whitespace are additionally accepted (the test varies both); this
#: is the vocabulary, not the input grammar.
ACCEPTED_VALUES: tuple[str, ...] = tuple(
    [f"p{n}" for n in range(10)] + [str(n) for n in range(10)] + sorted(WORD_BANDS)
)


def parse_priority(raw: Any) -> tuple[Optional[int], Optional[str]]:
    """``(band, flag)`` for a STORED priority field — the reader's half.

    ``band`` is the integer urgency (``P1`` → 1, ``high`` → 1) or None when it
    cannot be read; ``flag`` names the problem when there is one, so a caller
    reports WHY a ticket is unranked instead of showing a silent default.

    Never raises and never writes: a value it cannot read is reported, not
    repaired. Rewriting somebody else's priority field to tidy a listing is
    forbidden by the operator's standing rule, and it would treat the symptom in
    the data while leaving the defect in the code — the next person to type
    ``high`` would lose their ticket again.
    """
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None, MISSING_FLAG
    m = _BAND_RE.match(s)
    if m:
        return int(m.group(1)), None
    word = WORD_BANDS.get(s.lower())
    if word is not None:
        return word, None
    if _ORDERING_KEY_RE.match(s):
        return None, f"{ORDERING_KEY_FLAG}:{s}"
    return None, f"{UNPARSEABLE_FLAG}:{s}"


def priority_valid(raw: Any) -> bool:
    """Whether a writer may store this value — the writer's half.

    True exactly when :func:`parse_priority` can rank it, which is the invariant
    the two halves are pinned on. An ordering key is NOT valid to write: the
    kanban sort key is the web UI's to mint, and a hand-typed one is far more
    likely a mistyped band.
    """
    band, _flag = parse_priority(raw)
    return band is not None


def normalize_priority(raw: Any) -> str:
    """The canonical ``pN`` for any value a writer may store.

    Raises ``ValueError`` — carrying the allowed list — for anything else. The
    message IS the fix: the old writer said "done" on a value guaranteed to lose
    the ticket, so a rejection that does not tell the caller what to type
    instead would only move the silence.

    Canonicalising at the WRITE is what stops the vocabulary sprawling again:
    the author's judgement is preserved exactly (``high`` is band 1 and is
    stored as ``p1``), while the board stops accumulating a sixth spelling of
    it. Existing tickets are never touched — the reader above handles those.
    """
    s = str(raw if raw is not None else "").strip()
    band, flag = parse_priority(s)
    if band is not None:
        return f"p{band}"
    if flag == MISSING_FLAG:
        raise ValueError(f"priority is empty — allowed: {ALLOWED_HELP}")
    if flag and flag.startswith(ORDERING_KEY_FLAG):
        raise ValueError(
            f"invalid priority {s!r}: a multi-digit value is the web UI's kanban "
            f"ORDERING KEY (max+100 / midpoint insert), not a band of urgency. "
            f"Allowed: {ALLOWED_HELP}"
        )
    raise ValueError(f"invalid priority {s!r} — allowed: {ALLOWED_HELP}")
