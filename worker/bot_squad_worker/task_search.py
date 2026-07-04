#!/usr/bin/env python3
"""task_search — pure ranking helpers behind ``bsq task search`` / ``task similar``
(Process Paradigm M4 / F4.2, T-0486).

The task-ingest paradigm: a session decides whether a fresh input is about an
EXISTING task (clarify/append) or a NEW task. We do NOT classify with ML — the
judgment stays in the session. This module is the *tooling* half: given a free-text
query (or a seed ticket's title+body), it surfaces ranked candidate tickets by
title/body keyword overlap, and flags the ones similar enough to be likely
duplicates (the clarify-vs-create signal).

Kept deliberately pure (no I/O, stdlib only) so it is unit-testable in isolation
and so the ``bsq`` splice stays a thin wrapper (bsq is a shared co-edit hotspot):
the wrapper reads the backlog into ``Ticket`` records and calls ``rank()``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Tokens too generic to carry signal — they'd match nearly every ticket and
# drown the real overlap. Kept small + domain-aware (bot-squad backlog prose).
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "is", "are", "be", "was", "were", "it", "its", "this", "that", "these",
    "those", "with", "as", "from", "into", "via", "per", "should", "must", "can",
    "will", "would", "when", "where", "which", "not", "no", "if", "then", "else",
    "task", "tasks", "ticket", "tickets", "add", "new", "fix", "make", "need",
    "needs", "use", "uses", "using",
})

# A query token must clear this many characters to count (drops "id", "ui"
# noise while keeping short-but-meaningful ones rare enough to ignore).
_MIN_TOKEN_LEN = 3

# Title hits weigh more than body hits — a shared title word is a far stronger
# similarity signal than the same word buried in a progress note. Used for
# RANKING (which candidate is most relevant), not for the duplicate decision.
_TITLE_WEIGHT = 3

# Duplicate decision is COVERAGE-based, not score-based: a candidate is a likely
# duplicate when it matches at least this fraction of the query's DISTINCT
# meaningful tokens (i.e. the input is already covered by that task → clarify it,
# don't create a new one). Coverage discriminates ("matched the whole input")
# where a title-weighted score would flag every partially-related ticket.
_DUP_COVERAGE = 0.8

# ...but never flag a duplicate off a trivially-short query: a 1-token query
# always has 100% coverage on any hit, which is noise, not a duplicate.
_DUP_MIN_TOKENS = 2


@dataclass
class Ticket:
    """A backlog ticket reduced to what ranking needs."""
    id: str
    title: str
    body: str
    status: str = ""


def tokenize(text: str) -> list[str]:
    """Lowercase → alphanumeric tokens, stopwords + too-short dropped, deduped
    (order preserved). The query's distinct meaningful terms."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(raw) < _MIN_TOKEN_LEN or raw in _STOPWORDS or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def _count_present(tokens: list[str], haystack: str) -> tuple[int, list[str]]:
    """How many distinct query tokens appear (substring) in ``haystack``.
    Substring match mirrors the existing ``bsq`` guidance ``_score`` so behaviour
    is consistent across the CLI. Returns (count, the_matched_tokens)."""
    low = haystack.lower()
    matched = [t for t in tokens if t in low]
    return len(matched), matched


def score_ticket(tokens: list[str], ticket: Ticket) -> tuple[int, list[str]]:
    """Weighted overlap score for one ticket. Title matches count
    ``_TITLE_WEIGHT``×; body matches 1×. Returns (score, distinct_matched_tokens)."""
    t_n, t_match = _count_present(tokens, ticket.title)
    b_n, b_match = _count_present(tokens, ticket.body)
    score = _TITLE_WEIGHT * t_n + b_n
    matched = list(dict.fromkeys(t_match + b_match))  # union, order-stable
    return score, matched


def max_score(tokens: list[str]) -> int:
    """Best achievable score for a query: every token matched in a title."""
    return _TITLE_WEIGHT * len(tokens)


@dataclass
class Candidate:
    id: str
    title: str
    status: str
    score: int
    matched: list[str]
    dup: bool


def rank(
    query: str,
    tickets: list[Ticket],
    *,
    exclude_id: str | None = None,
    limit: int = 10,
) -> list[Candidate]:
    """Rank ``tickets`` by similarity to ``query`` (free text or a seed ticket's
    title+body). Drops zero-overlap tickets and the seed itself (``exclude_id``).
    Sort: score desc, then id asc (deterministic). The ``dup`` flag marks
    candidates over the duplicate threshold — the clarify-vs-create signal.
    Returns at most ``limit`` candidates."""
    tokens = tokenize(query)
    if not tokens:
        return []
    dup_eligible = len(tokens) >= _DUP_MIN_TOKENS
    out: list[Candidate] = []
    for t in tickets:
        if exclude_id and t.id == exclude_id:
            continue
        score, matched = score_ticket(tokens, t)
        if score <= 0:
            continue
        coverage = len(matched) / len(tokens)
        out.append(Candidate(
            id=t.id, title=t.title, status=t.status,
            score=score, matched=matched,
            dup=dup_eligible and coverage >= _DUP_COVERAGE,
        ))
    out.sort(key=lambda c: (-c.score, c.id))
    return out[: max(0, limit)]
