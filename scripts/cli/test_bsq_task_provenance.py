"""Tests for `bsq task new` provenance gate (next-wave #14, T-0452).

`bsq task new` now REQUIRES a `--provenance/--source` value and stamps it into
the freshly-created ticket's frontmatter — closing the source-attach loop at the
OPEN, instead of relying solely on the post-hoc tree scan
(scripts/lint/backlog_provenance.py). The grammar MUST stay byte-compatible with
that lint (the SSOT), so this file asserts the two validators agree.

`bsq` is an extensionless script loaded via SourceFileLoader.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_prov", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_prov", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

# Load the lint SSOT for the drift-agreement test.
_LINT_PATH = Path(__file__).resolve().parents[1] / "lint" / "backlog_provenance.py"
_lint_loader = SourceFileLoader("backlog_provenance_ssot", str(_LINT_PATH))
_lint_spec = importlib.util.spec_from_loader("backlog_provenance_ssot", _lint_loader)
lint = importlib.util.module_from_spec(_lint_spec)
_lint_loader.exec_module(lint)


VALID = ["T-0438", "F-12", "corpus:positioning", "stakeholder:2026-06-20",
         "T-0438, F-9", "corpus:occam-cut, T-0001"]
INVALID = ["", "   ", "nope", "T-99", "T-abcd", "corpus:", "F-", "stakeholder:2026",
           "random text", "T-0438 F-9"]  # space-separated (not comma) is invalid


def test_provenance_valid_accepts_grammar():
    for v in VALID:
        assert bsq._provenance_valid(v), v


def test_provenance_valid_rejects_garbage():
    for v in INVALID:
        assert not bsq._provenance_valid(v), v


def test_provenance_validator_agrees_with_lint_ssot():
    """bsq's creation-time gate must match the post-hoc lint exactly (no drift)."""
    for v in VALID + INVALID:
        assert bsq._provenance_valid(v) == lint._provenance_valid(v), v


def test_stamp_provenance_inserts_into_frontmatter():
    text = (
        "---\n"
        "id: T-0999\n"
        'title: "x"\n'
        "status: planned\n"
        "---\n\n"
        "## Verbatim request\n\nbody\n"
    )
    out = bsq._stamp_provenance(text, "T-0438")
    assert "provenance: T-0438\n" in out
    # frontmatter still closes correctly and body is preserved
    assert out.count("---\n") >= 2
    assert "## Verbatim request" in out
    # the stamped value lives INSIDE the frontmatter block (before the 2nd ---)
    head = out.split("\n---\n", 1)[0]
    assert "provenance: T-0438" in head


def test_stamp_provenance_replaces_existing():
    text = "---\nid: T-1\nprovenance: F-1\n---\n\nbody\n"
    out = bsq._stamp_provenance(text, "T-0438")
    assert "provenance: T-0438" in out
    assert "provenance: F-1" not in out
