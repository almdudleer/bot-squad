"""Unit tests for the ``bsq cluster-launch`` orchestration layer
(Process Paradigm M4 / F4.3, T-0487).

cluster-launch is a THIN orchestration verb over two existing primitives:
B's similarity ranker (``task_search.rank``, T-0486) and ``bsq spawn --bundle``
(T-0160). It adds no new ranking and no worker action — its only logic is
(1) normalize the seed id (the recurring .md-vs-stem ambiguity bites here
because ``find_ticket`` globs ``<id>-*.md``) and (2) select the default cluster
from the ranked candidates: drop CLOSED tasks (you bundle fixable problems, not
done ones), apply a min-score floor, cap at a limit, preserve similarity order.
Those two pure helpers are what this file pins; the I/O + confirm-gate + spawn
hand-off are covered by the manual walkthrough (scenario md).
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_cl", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_cl", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

Candidate = bsq._load_task_search().Candidate


def _cand(tid, score, status="open"):
    return Candidate(id=tid, title=f"{tid} title", status=status,
                     score=score, matched=["x"], dup=False)


# --- _norm_task_id: the .md-vs-stem normalization gotcha (TL-B flagged) ------

def test_norm_strips_trailing_md():
    assert bsq._norm_task_id("T-0487.md") == "T-0487"
    assert bsq._norm_task_id("T-0487.MD") == "T-0487"


def test_norm_uppercases_t_prefix():
    assert bsq._norm_task_id("t-0487") == "T-0487"
    assert bsq._norm_task_id(" t-0324.md ") == "T-0324"


def test_norm_leaves_padding_and_plain_ids_intact():
    assert bsq._norm_task_id("T-0487") == "T-0487"   # zero-padding preserved
    assert bsq._norm_task_id("T-0001") == "T-0001"


# --- _filter_cluster: drop-closed default + min-score + limit + order --------

def test_filter_drops_closed_by_default():
    cands = [_cand("T-1", 40, "open"), _cand("T-2", 39, "closed"),
             _cand("T-3", 38, "in_progress")]
    kept = [c.id for c in bsq._filter_cluster(cands, min_score=1, limit=10)]
    assert kept == ["T-1", "T-3"]            # closed T-2 dropped


def test_filter_include_closed_opt_in():
    cands = [_cand("T-1", 40, "open"), _cand("T-2", 39, "closed")]
    kept = [c.id for c in bsq._filter_cluster(
        cands, min_score=1, limit=10, include_closed=True)]
    assert kept == ["T-1", "T-2"]


def test_filter_min_score_floor():
    cands = [_cand("T-1", 5), _cand("T-2", 2), _cand("T-3", 1)]
    kept = [c.id for c in bsq._filter_cluster(cands, min_score=3, limit=10)]
    assert kept == ["T-1"]


def test_filter_caps_at_limit_preserving_similarity_order():
    # input is already similarity-sorted (score desc); filter must NOT reorder.
    cands = [_cand("T-9", 40), _cand("T-8", 35), _cand("T-7", 30), _cand("T-6", 25)]
    kept = [c.id for c in bsq._filter_cluster(cands, min_score=1, limit=2)]
    assert kept == ["T-9", "T-8"]


def test_filter_empty_when_all_closed():
    cands = [_cand("T-1", 40, "closed"), _cand("T-2", 39, "closed")]
    assert bsq._filter_cluster(cands, min_score=1, limit=10) == []
