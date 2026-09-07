"""Tests for bsq's read_frontmatter (T-0208, T-1047).

`bsq` is an extensionless script, loaded via SourceFileLoader. read_frontmatter
is a pure function over a file path, so no worker needed.

T-1047: the old reader was a hand-rolled "key: value lines only" line splitter
that disagreed with the real pyyaml every writer here uses, on four
independent axes — a scalar folded past pyyaml's 80-column width read
truncated, a quoted scalar's internal escapes came back mangled instead of
unescaped, a block-style list read as completely empty, and a nested mapping
couldn't be represented at all. Measured on the live board: 458 of 950 ticket
titles wrong, 53 tickets losing session_history SIDs (which is what
`bsq spawn`'s expert auto-resume reads).

Fixtures below are composed from the actual producer wherever a live
specimen still exists on the board — copied byte-for-byte from a real ticket
frontmatter block, not hand-typed to imitate one — because the fold/escape
byte shapes are easy to get subtly wrong by hand and a hand-typed fixture
proves nothing about what pyyaml actually emits. The one exception (a still-
folded title) no longer exists live now that T-1044 pinned the dump width and
every touched ticket healed on its next write, so that fixture instead runs a
real 204-char live title (T-1003) through the real (unpinned-width) pyyaml
dumper to reproduce the exact fold shape the pre-T-1044 writer produced.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import yaml

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_rf", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_rf", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "T-0001-x.md"
    p.write_text(body)
    return p


def _write_fm_block(tmp_path: Path, fm_block: str) -> Path:
    """Write a ticket md from a raw frontmatter BLOCK (no surrounding `---`)."""
    return _write(tmp_path, f"---\n{fm_block}\n---\n\nbody\n")


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


# ---------------------------------------------------------------------------
# T-1047 arm 1 — fold truncation
# ---------------------------------------------------------------------------
def test_arm1_folded_title_reads_in_full(tmp_path):
    # The real T-1003 title (204ch), run through the real (unpinned-width)
    # pyyaml dumper to reproduce the exact fold a pre-T-1044 writer produced —
    # every ticket this old actually folded still healed on its next write,
    # so no live specimen remains to copy verbatim.
    real_title = (
        "the api/worker task_states byte-identity guard can never run in the "
        "same invocation as the pace mirror guard: they derive the worker "
        "tree from parents[3] vs parents[2], so each mount satisfies exactly "
        "one"
    )
    assert len(real_title) == 204
    meta = {"id": "T-1003", "title": real_title, "status": "planned"}
    fm_block = yaml.dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    assert "\n " in fm_block, "fixture didn't actually fold — width default must be 80"
    p = _write_fm_block(tmp_path, fm_block.rstrip("\n"))
    fm = bsq.read_frontmatter(p)
    assert fm["title"] == real_title


def test_arm1_folded_title_and_session_history_real_specimen(tmp_path):
    # T-0207, copied byte-for-byte off the live board: BOTH title and
    # session_history are folded in the same file.
    fm_block = (
        "id: T-0207\n"
        "title: Route ALL backlog-ticket creation through the bsq task_new allocator — operator\n"
        "  dispatch templating direct-writes T-NNNN files and collides\n"
        "status: closed\n"
        "session_history: [S-almdudleer-route-all-backlog-ticket-creation-throug-p87, "
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p106,\n"
        "  S-almdudleer-route-all-backlog-ticket-creation-throug-p109, "
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p120]\n"
        "created: 2026-06-09T09:48:27Z\n"
        "initiative: operator-ux-and-session-mgmt.md\n"
        "priority: '110'\n"
        "updated: 2026-06-18T19:53:38Z\n"
        "parent_task: T-0552"
    )
    p = _write_fm_block(tmp_path, fm_block)
    fm = bsq.read_frontmatter(p)
    assert fm["title"] == (
        "Route ALL backlog-ticket creation through the bsq task_new allocator "
        "— operator dispatch templating direct-writes T-NNNN files and collides"
    )
    assert fm["session_history"] == [
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p87",
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p106",
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p109",
        "S-almdudleer-route-all-backlog-ticket-creation-throug-p120",
    ]
    # created stays a plain string, never a datetime (T-1049 hazard: a bare
    # yaml.safe_load resolves an unquoted timestamp and 500s downstream json).
    assert fm["created"] == "2026-06-09T09:48:27Z"


# ---------------------------------------------------------------------------
# T-1047 arm 2 — mis-unescaping of quoted scalars
# ---------------------------------------------------------------------------
def test_arm2_escaped_title_real_specimen(tmp_path):
    # T-0013, copied byte-for-byte off the live board: a double-quoted scalar
    # whose true value itself contains literal double quotes.
    fm_block = (
        "id: T-0013\n"
        'title: "\\"You\'re all set\\" post-install screen + Next → server view"\n'
        "status: closed\n"
        "blocked_by: T-0006, T-0012\n"
        "updated: 2026-09-07T06:09:46Z\n"
        "initiative: multi-server-installation-process.md\n"
        "parent_task: T-0551"
    )
    p = _write_fm_block(tmp_path, fm_block)
    fm = bsq.read_frontmatter(p)
    assert fm["title"] == '"You\'re all set" post-install screen + Next → server view'


# ---------------------------------------------------------------------------
# T-1047 arm 3 — block-format lists read empty
# ---------------------------------------------------------------------------
def test_arm3_block_format_session_history_real_specimen(tmp_path):
    # T-0003, copied byte-for-byte off the live board.
    fm_block = (
        "id: T-0003\n"
        'title: Active operator session is displayed as "suspended" in the UI / list_sessions\n'
        "status: closed\n"
        "session_history:\n"
        "- S-almdudleer-operator-shown-active-p35\n"
        "updated: '2026-05-27T02:16:50Z'\n"
        "created: '2026-05-14T16:10:50Z'\n"
        "initiative: ui-polish.md\n"
        "parent_task: T-0556"
    )
    p = _write_fm_block(tmp_path, fm_block)
    fm = bsq.read_frontmatter(p)
    assert fm["session_history"] == ["S-almdudleer-operator-shown-active-p35"]


# ---------------------------------------------------------------------------
# T-1047 arm 4 — nested maps are unrepresentable by a flat reader
# ---------------------------------------------------------------------------
def test_arm4_nested_map_round_trips(tmp_path):
    meta = {
        "id": "T-0001",
        "session_history_ts": {
            "S-almdudleer-foo-p1": "2026-09-07T08:00:00Z",
            "S-almdudleer-bar-p2": "2026-09-07T08:05:00Z",
        },
    }
    fm_block = yaml.dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    p = _write_fm_block(tmp_path, fm_block.rstrip("\n"))
    fm = bsq.read_frontmatter(p)
    assert fm["session_history_ts"] == {
        "S-almdudleer-foo-p1": "2026-09-07T08:00:00Z",
        "S-almdudleer-bar-p2": "2026-09-07T08:05:00Z",
    }
