"""Tests for bsq's read_frontmatter quote-stripping (T-0208).

`bsq` is an extensionless script, loaded via SourceFileLoader. read_frontmatter
is a pure function over a file path, so no worker needed.

Regression: the shared pyyaml dump (T-0075) and `bsq task new` now write quoted
YAML scalars (`initiative: "x.md"`); the flat reader used to keep the literal
quotes, so spawn passed `"x.md"` to the worker validator which rejected it on
its `.endswith(".md")` check.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_rf", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_rf", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "T-0001-x.md"
    p.write_text(body)
    return p


def test_strip_surrounding_quotes_unit():
    f = bsq._strip_surrounding_quotes
    assert f('"x.md"') == "x.md"
    assert f("'x.md'") == "x.md"
    assert f("x.md") == "x.md"            # already bare
    assert f('"x.md') == '"x.md'          # unmatched -> unchanged
    assert f('x.md"') == 'x.md"'          # unmatched -> unchanged
    assert f('""') == ""                  # empty quoted
    assert f('"') == '"'                  # single char -> unchanged
    assert f("'mixed\"") == "'mixed\""    # mismatched quote types -> unchanged


def test_read_frontmatter_strips_quoted_scalars(tmp_path):
    p = _write(
        tmp_path,
        '---\n'
        'id: T-0001\n'
        'initiative: "operator-ux-and-session-mgmt.md"\n'
        "priority: '110'\n"
        "plain: bare-value\n"
        "---\n\nbody\n",
    )
    fm = bsq.read_frontmatter(p)
    assert fm["initiative"] == "operator-ux-and-session-mgmt.md"
    assert fm["priority"] == "110"
    assert fm["plain"] == "bare-value"


def test_read_frontmatter_no_frontmatter(tmp_path):
    p = _write(tmp_path, "no frontmatter here\n")
    assert bsq.read_frontmatter(p) == {}
