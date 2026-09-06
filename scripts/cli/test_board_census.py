"""Tests for board_census.py (T-1026).

Two grep/hand-built board enumerations were wrong the same night, both
because they read the ticket file as flat text instead of frontmatter. This
pins the two properties that fix that, plus the exact regression that
exposed the bug live:

1. `test_title_with_inline_triple_dash_*` — a title containing the literal
   substring "---" on one line must not shift the frontmatter parse (the
   original control: planting T-9999 moved a bucket 3 -> 4, then back to 3
   on removal — "sees a one and does not leak").
2. `test_unparseable_file_is_bucketed_not_dropped` — a malformed/missing
   frontmatter block is counted under `<no status field>`, never silently
   absent.
3. `test_body_line_that_looks_like_status_does_not_win` — the actual live
   error: `grep -l '^status: open$'` matched a BODY line and reported a
   closed ticket as open. This is the highest-value regression on the
   ticket because it's the mistake a human (or a naive tool) will actually
   make.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent / "board_census.py"
_loader = SourceFileLoader("board_census_mod", str(_MOD_PATH))
_spec = importlib.util.spec_from_loader("board_census_mod", _loader)
bc = importlib.util.module_from_spec(_spec)
_loader.exec_module(bc)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_status_of_reads_frontmatter_status(tmp_path):
    p = _write(
        tmp_path, "T-0001-demo.md",
        "---\nid: T-0001\nstatus: in_progress\n---\n\nbody\n",
    )
    assert bc.status_of(p) == "in_progress"


def test_title_with_inline_triple_dash_does_not_break_parse(tmp_path):
    # This is the shape that breaks `text.split("---", 2)`: the substring
    # "---" appears mid-line, inside a quoted value, not on its own line.
    p = _write(
        tmp_path, "T-9999-demo.md",
        "---\nid: T-9999\ntitle: 'has a --- triple dash inline'\n"
        "status: to_accept\n---\n\nbody\n",
    )
    assert bc.status_of(p) == "to_accept"


def test_title_with_inline_triple_dash_census_sees_one_and_leaks_nothing(tmp_path):
    _write(tmp_path, "T-0001-a.md", "---\nid: T-0001\nstatus: to_accept\n---\n\nbody\n")
    _write(tmp_path, "T-0002-b.md", "---\nid: T-0002\nstatus: closed\n---\n\nbody\n")
    by, seen = bc.census(str(tmp_path))
    assert by["to_accept"] == ["T-0001"]
    assert seen == 2

    # Plant the triple-dash-titled ticket: to_accept should go 1 -> 2.
    planted = _write(
        tmp_path, "T-9999-c.md",
        "---\nid: T-9999\ntitle: 'foo --- bar'\nstatus: to_accept\n---\n\nbody\n",
    )
    by, seen = bc.census(str(tmp_path))
    assert sorted(by["to_accept"]) == ["T-0001", "T-9999"]
    assert seen == 3

    # And removing it must go back to exactly the original count — no leak.
    planted.unlink()
    by, seen = bc.census(str(tmp_path))
    assert by["to_accept"] == ["T-0001"]
    assert seen == 2


def test_unparseable_file_is_bucketed_not_dropped(tmp_path):
    _write(tmp_path, "T-0003-no-frontmatter.md", "no frontmatter here at all\n")
    _write(tmp_path, "T-0004-unclosed.md", "---\nid: T-0004\ntitle: no closing delimiter follows\n\nmore body text\n")
    by, seen = bc.census(str(tmp_path))
    assert seen == 2
    assert sorted(by[bc.NO_STATUS]) == ["T-0003", "T-0004"]


def test_body_line_that_looks_like_status_does_not_win(tmp_path):
    # The live T-0038 case: frontmatter status is `closed`, but a line in
    # the BODY reads exactly like a status field for a different value.
    p = _write(
        tmp_path, "T-0038-demo.md",
        "---\nid: T-0038\nstatus: closed\n---\n\n"
        "## Some section\n\nunrelated text\nstatus: open\nmore text\n",
    )
    assert bc.status_of(p) == "closed"

    by, seen = bc.census(str(tmp_path))
    assert by["closed"] == ["T-0038"]
    assert "open" not in by
    assert seen == 1


def test_missing_status_in_frontmatter_ignores_body_lookalike(tmp_path):
    # Operator-found gap (mutation testing): the T-0038 test above passes
    # on first-match-wins alone, since the real status is found before the
    # closing fence is ever reached — it never actually exercises the
    # fence. This fixture has NO status field in frontmatter at all, so the
    # only way to stay correct is to stop scanning at the closing `---`
    # rather than reading past it into the body. Without that fence, the
    # body's `status: open` gets misread as the real status — a malformed
    # ticket silently miscounted as a real `open` one, exactly the defect
    # class this tool exists to catch.
    p = _write(
        tmp_path, "T-0500-demo.md",
        "---\nid: T-0500\ntitle: malformed, no status field\n---\n\n"
        "## Progress\n\nstatus: open\n",
    )
    assert bc.status_of(p) is None

    by, seen = bc.census(str(tmp_path))
    assert by[bc.NO_STATUS] == ["T-0500"]
    assert "open" not in by
    assert seen == 1


def test_census_buckets_multiple_statuses(tmp_path):
    _write(tmp_path, "T-0010-a.md", "---\nid: T-0010\nstatus: open\n---\n\nbody\n")
    _write(tmp_path, "T-0011-b.md", "---\nid: T-0011\nstatus: open\n---\n\nbody\n")
    _write(tmp_path, "T-0012-c.md", "---\nid: T-0012\nstatus: closed\n---\n\nbody\n")
    by, seen = bc.census(str(tmp_path))
    assert sorted(by["open"]) == ["T-0010", "T-0011"]
    assert by["closed"] == ["T-0012"]
    assert seen == 3


def test_render_includes_total_and_known_order(tmp_path):
    by = {"closed": ["T-0001"], "open": ["T-0002"]}
    out = bc.render(by, 2)
    assert out.index("open") < out.index("closed")  # ORDER puts open before closed
    assert "TOTAL" in out
    assert out.strip().splitlines()[-1].split()[-1] == "2"


def test_non_md_files_are_ignored(tmp_path):
    _write(tmp_path, "T-0001-a.md", "---\nid: T-0001\nstatus: open\n---\n\nbody\n")
    _write(tmp_path, "README.txt", "not a ticket\n")
    _write(tmp_path, "T-0001-a.md.lock", "lock file\n")
    by, seen = bc.census(str(tmp_path))
    assert seen == 1
    assert by["open"] == ["T-0001"]
