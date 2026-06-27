"""Tests for the ``task_search`` ranking helper behind ``bsq task search`` /
``bsq task similar`` (Process Paradigm M4 / F4.2, T-0486).

The dedupe / clarify-vs-create tooling: given a fresh free-text input (or a seed
ticket), ``rank()`` surfaces ranked candidate backlog tickets and flags the ones
similar enough to be likely duplicates. The ``dup`` flag is the clarify-vs-create
SIGNAL — it tells the ingest session to append-to-existing (clarify the matched
task) rather than create a new one. The judgment stays in the session; this is
just the tooling that surfaces candidates (no ML classifier).

The module is pure (stdlib only, no I/O) so these tests need no worker / backlog.
The headline test (``test_duplicate_input_lands_as_clarification``) is DoD item 3:
a dup input lands as a clarification rather than a new task.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_TS_PATH = Path(__file__).resolve().parent / "task_search.py"
_loader = SourceFileLoader("bsq_task_search", str(_TS_PATH))
_spec = importlib.util.spec_from_loader("bsq_task_search", _loader)
ts = importlib.util.module_from_spec(_spec)
# Register BEFORE exec: dataclass() under `from __future__ import annotations`
# resolves string annotations by looking the module up in sys.modules.
import sys
sys.modules["bsq_task_search"] = ts
_loader.exec_module(ts)


def _t(tid, title, body="", status="open"):
    return ts.Ticket(id=tid, title=title, body=body, status=status)


# --- a small synthetic backlog the ingest tooling ranks against ------------
def _backlog():
    return [
        _t("T-0010", "staging deploy pre-gate on dirty shared dev clone blocks deploys",
           "the deploy precheck should refuse when the shared clone is dirty"),
        _t("T-0011", "render task cards with a status pill in the board column",
           "frontend polish for the kanban board"),
        _t("T-0012", "telegram egress DPI block — proxy primitive",
           "per-install tg proxy url so sends route around the DPI block"),
    ]


# --- tokenize --------------------------------------------------------------
def test_tokenize_drops_stopwords_short_and_dedupes():
    toks = ts.tokenize("The deploy should DEPLOY to the staging id ui clone")
    assert "deploy" in toks
    assert "staging" in toks
    assert "clone" in toks
    assert toks.count("deploy") == 1          # deduped (order-preserving)
    assert "the" not in toks and "should" not in toks  # stopwords dropped
    assert "id" not in toks and "ui" not in toks       # too short (<3)


def test_tokenize_empty_and_none():
    assert ts.tokenize("") == []
    assert ts.tokenize(None) == []
    assert ts.tokenize("the a an of to") == []   # all stopwords


# --- scoring ---------------------------------------------------------------
def test_title_match_weighs_more_than_body_match():
    tokens = ts.tokenize("deploy")
    title_hit = _t("T-1", "deploy recipe", body="unrelated")
    body_hit = _t("T-2", "unrelated", body="deploy recipe")
    s_title, _ = ts.score_ticket(tokens, title_hit)
    s_body, _ = ts.score_ticket(tokens, body_hit)
    assert s_title > s_body
    assert s_title == ts._TITLE_WEIGHT and s_body == 1


# --- ranking ---------------------------------------------------------------
def test_rank_orders_by_score_and_drops_zero_overlap():
    cands = ts.rank("staging deploy dirty clone", _backlog())
    assert cands, "expected at least one candidate"
    assert cands[0].id == "T-0010"            # the deploy ticket ranks first
    assert all(c.id != "T-0011" for c in cands)  # zero-overlap board ticket dropped


def test_rank_excludes_seed_ticket():
    # similar --from T-0010: the seed must not appear in its own results.
    seed = next(t for t in _backlog() if t.id == "T-0010")
    cands = ts.rank(f"{seed.title} {seed.body}", _backlog(), exclude_id="T-0010")
    assert all(c.id != "T-0010" for c in cands)


def test_rank_respects_limit():
    big = [_t(f"T-{i:04d}", "deploy clone", body="deploy") for i in range(20)]
    assert len(ts.rank("deploy clone", big, limit=5)) == 5


def test_rank_empty_query_returns_nothing():
    assert ts.rank("the a of to", _backlog()) == []


# --- the clarify-vs-create signal (DoD item 3) -----------------------------
def test_duplicate_input_lands_as_clarification():
    """A near-identical input must land as a CLARIFICATION (dup=True → append to
    the existing task), NOT as a new task. This is the headline DoD case."""
    backlog = _backlog()
    # Input that restates the existing T-0010 deploy ticket almost verbatim.
    dup_input = "staging deploy pre-gate dirty shared clone blocks deploys"
    cands = ts.rank(dup_input, backlog)
    top = cands[0]
    assert top.id == "T-0010"
    assert top.dup is True, "a restatement of an existing task must flag as duplicate"


def test_novel_input_is_not_a_duplicate():
    """A genuinely NEW input that only grazes an existing task must NOT be a dup
    (→ the ingest session creates a new task, doesn't clarify an old one)."""
    backlog = _backlog()
    # Mentions 'deploy' once but is really about a brand-new metrics dashboard.
    novel_input = "build a latency metrics dashboard for the deploy worker timings"
    cands = ts.rank(novel_input, backlog)
    assert all(not c.dup for c in cands), \
        "a novel input must not be flagged as a duplicate of an existing task"


def test_single_token_query_never_flags_duplicate():
    """A trivially short (1-token) query has 100% coverage on any hit — that's
    noise, not a duplicate. _DUP_MIN_TOKENS guards against it."""
    cands = ts.rank("deploy", _backlog())
    assert cands, "single token should still surface candidates"
    assert all(not c.dup for c in cands)
