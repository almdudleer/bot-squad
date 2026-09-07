"""T-0891 DoD 4/5: a REPLACE on a ticket must leave the previous text recoverable.

`task_context_set` and `task_summary_set` overwrite authored sections whole.
`data/` is outside git, so until now those were the only writes in the system
with no undo of any kind — not `git checkout`, not review, not history. On
2026-08-14 a mis-resolved id therefore destroyed a closed ticket's
`## Executive summary` and `## Context` permanently; the reconstruction that
stands on that ticket today is marked as one because the original could not be
found anywhere.

The guard in `bsq` (scripts/cli/test_bsq_project_resolution.py) makes the wrong
target loud. This is the other half: when the write does land — on the right
ticket or the wrong one — the bytes it replaced are still on disk.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_squad_worker import actions as A


TICKET = (
    "---\nid: T-0655\ntitle: keep-alive nudge\nstatus: closed\nupdated: 2026-07-21T00:00:00Z\n---\n\n"
    "## Verbatim request\n\nhis words, verbatim\n\n"
    "## Executive summary\n\nTHE AUTHOR'S OWN SUMMARY\n\n"
    "## Context\n\nTHE AUTHOR'S OWN WORKING STATE\n"
)


@pytest.fixture()
def board(tmp_path: Path, monkeypatch):
    backlog = tmp_path / "data" / "demo" / "backlog"
    backlog.mkdir(parents=True)
    md = backlog / "T-0655-keep-alive-nudge.md"
    md.write_text(TICKET)
    cfg = SimpleNamespace(data_dir=tmp_path / "data", projects={"demo": object()})
    monkeypatch.setattr(A, "_CONFIG", cfg)
    return md


def _versions(md: Path) -> list[Path]:
    return sorted((md.parent / ".versions" / md.stem).glob("*.md"))


def test_context_replace_leaves_the_previous_file_recoverable(board):
    """The exact loss of 2026-08-14, made undoable: the pre-write bytes are a
    whole file, so restoring is a copy — no parser in the recovery path."""
    original = board.read_bytes()

    res = A._action_task_context_set(
        {"slug": "demo", "task_id": "T-0655", "text": "text from ANOTHER project"})

    assert "text from ANOTHER project" in board.read_text()
    assert "THE AUTHOR'S OWN WORKING STATE" not in board.read_text()

    backup = Path(res["backup_path"])
    assert backup.read_bytes() == original
    # restoring is a copy, and the result is byte-identical to what was lost
    board.write_bytes(backup.read_bytes())
    assert board.read_bytes() == original


def test_summary_replace_is_snapshotted_too(board):
    original = board.read_bytes()
    res = A._action_task_summary_set(
        {"slug": "demo", "task_id": "T-0655", "text": "one paragraph of status"})
    assert Path(res["backup_path"]).read_bytes() == original
    assert "one paragraph of status" in board.read_text()


def test_successive_replaces_each_keep_their_own_predecessor(board):
    """Two sessions overwriting in turn: the first one's text must not be lost
    by the second one's snapshot."""
    A._action_task_context_set({"slug": "demo", "task_id": "T-0655", "text": "first"})
    A._action_task_context_set({"slug": "demo", "task_id": "T-0655", "text": "second"})
    A._action_task_context_set({"slug": "demo", "task_id": "T-0655", "text": "third"})

    saved = [p.read_text() for p in _versions(board)]
    assert len(saved) == 3
    assert "THE AUTHOR'S OWN WORKING STATE" in saved[0]
    assert "first" in saved[1]
    assert "second" in saved[2]
    assert "third" in board.read_text()


def test_snapshots_are_capped_so_a_chatty_ticket_cannot_grow_without_bound(board):
    for i in range(A._TASK_SNAPSHOT_KEEP + 5):
        A._action_task_context_set(
            {"slug": "demo", "task_id": "T-0655", "text": f"state {i}"})
    kept = _versions(board)
    assert len(kept) == A._TASK_SNAPSHOT_KEEP
    # the newest survive; the oldest are the ones dropped
    assert f"state {A._TASK_SNAPSHOT_KEEP + 3}" in kept[-1].read_text()


def test_append_only_writers_are_not_snapshotted(board):
    """`## Progress` and `## Stakeholder notes` only ever grow, so the previous
    content is still in the file above what was appended. Snapshotting them
    would be twenty copies of the same ticket per working day, for no undo that
    reading the file does not already give."""
    A._action_task_progress_add(
        {"slug": "demo", "sid": "S-x-p1", "task_id": "T-0655", "text": "checkpoint"})
    A._action_task_stakeholder_note_add(
        {"slug": "demo", "task_id": "T-0655", "text": "his words"})
    assert _versions(board) == []


def test_the_versions_dir_is_not_mistaken_for_a_ticket(board):
    """Every backlog reader globs `T-*.md` / `*.md` at the top level. A snapshot
    directory that shadowed a real ticket would be a second incident of this
    same class, so it is pinned rather than assumed."""
    A._action_task_context_set({"slug": "demo", "task_id": "T-0655", "text": "x"})
    assert [p.name for p in sorted(board.parent.glob("*.md"))] == [board.name]
    assert [p.name for p in sorted(board.parent.glob("T-*.md"))] == [board.name]


# --- T-1045: the guard travels all the way through the worker action, not
# just through `set_context` in isolation — `_rewrite_task_body` must turn
# the ValueError into an ActionError BEFORE the snapshot/write happens, so a
# refused write leaves neither a mutated ticket nor a spurious `.versions/`
# entry behind.

def test_context_set_action_refuses_a_dod_drop_and_leaves_no_trace(board):
    board.write_text(
        "---\nid: T-0655\ntitle: keep-alive nudge\nstatus: closed\n"
        "updated: 2026-07-21T00:00:00Z\n---\n\n"
        "## Verbatim request\n\nhis words, verbatim\n\n"
        "## DoD\n\nTBD\n\n"
        "## Context\n\n### DoD\n\n1. ship it\n"
    )
    original = board.read_bytes()

    with pytest.raises(A.ActionError, match="DoD"):
        A._action_task_context_set(
            {"slug": "demo", "task_id": "T-0655", "text": "fresh state, no DoD"})

    assert board.read_bytes() == original
    assert _versions(board) == []  # refused before the snapshot was ever taken


def test_context_set_action_refuses_a_stakeholder_quote_drop(board):
    board.write_text(
        "---\nid: T-0655\ntitle: keep-alive nudge\nstatus: closed\n"
        "updated: 2026-07-21T00:00:00Z\n---\n\n"
        "## Verbatim request\n\n(filed via task_new)\n\n"
        "## Context\n\n### Stakeholder notes\n\nhis words, verbatim\n"
    )
    original = board.read_bytes()

    with pytest.raises(A.ActionError, match="stakeholder"):
        A._action_task_context_set(
            {"slug": "demo", "task_id": "T-0655", "text": "fresh state, no quote"})

    assert board.read_bytes() == original
    assert _versions(board) == []
