"""T-0371: entity ids must survive crossing 9999 (the 4-digit ceiling).

The atomic allocator zero-pads to a MINIMUM of 4 digits, so the 10000th task is
``T-10000`` — five digits. Every id matcher hard-coded ``\\d{4}`` (exactly four),
so a 5-digit id silently failed validation / canonicalisation. These assert the
widened ``\\d{4,}`` accepts 5-digit ids while still rejecting <4-digit / garbage.
Date (``\\d{4}-\\d{2}``) and version (``v\\d{4}\\.``) patterns are intentionally
NOT widened and are out of scope here.
"""
from __future__ import annotations

from app import routes_backlog, routes_docs, routes_usecases
from app.artifact_nesting import _id_from_stem
from app.markdown_writer import _TASK_ID_RE as MW_TASK_ID_RE


def test_task_id_re_accepts_5_digits():
    assert routes_backlog._TASK_ID_RE.match("T-10000")
    assert routes_backlog._TASK_ID_RE.match("T-0001")  # 4-digit still valid
    assert not routes_backlog._TASK_ID_RE.match("T-001")   # <4 rejected
    assert not routes_backlog._TASK_ID_RE.match("T-abcd")


def test_doc_id_re_accepts_5_digits():
    assert routes_backlog._DOC_ID_RE.match("D-10000")
    assert routes_docs._DOC_ID_RE.match("D-99999")
    assert routes_docs._TASK_ID_RE.match("T-12345")


def test_flow_id_re_accepts_5_digits():
    assert routes_usecases._FLOW_ID_RE.match("UF-10000")
    assert not routes_usecases._FLOW_ID_RE.match("UF-999")


def test_markdown_writer_task_id_captures_5_digits():
    m = MW_TASK_ID_RE.match("T-10000-some-slug")
    assert m and m.group(1) == "10000"
    # 4-digit still captures
    assert MW_TASK_ID_RE.match("T-0042-x").group(1) == "0042"


def test_id_from_stem_canonicalises_5_digit_id():
    assert _id_from_stem("D-10000-some-slug") == "D-10000"
    assert _id_from_stem("UF-12345") == "UF-12345"
    assert _id_from_stem("D-0001-x") == "D-0001"  # 4-digit unchanged
