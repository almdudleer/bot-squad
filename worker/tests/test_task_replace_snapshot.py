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


# --- T-1050: a STALE-BUT-WELL-FORMED body carries every heading forward, so
# the T-1045 guard above never fires on it — that is precisely the loss shape
# this ticket is about. `base_rev` closes it: an opt-in CAS check, keyed off
# a `context_rev:` counter this writer owns, that REFUSES rather than merges
# a write based on a revision the live ticket has since moved past.

def test_healthy_sequential_write_does_not_refuse(board):
    """DoD 3: an ordinary one-holder read -> write must never refuse — this
    verb runs dozens of times a night, and a guard that fires on the normal
    case is worse than none."""
    res1 = A._action_task_context_set(
        {"slug": "demo", "task_id": "T-0655", "text": "first working state"})
    assert res1["context_rev"] == 1

    # The next holder reads the rev it just got back and writes with it —
    # the exact shape the CAS check is meant to let through untouched.
    res2 = A._action_task_context_set({
        "slug": "demo", "task_id": "T-0655", "text": "second working state",
        "base_rev": res1["context_rev"],
    })
    assert res2["context_rev"] == 2
    assert "second working state" in board.read_text()


def test_blind_write_with_no_base_rev_still_does_not_refuse(board):
    """DoD 3, the other half: a caller that never passes `base_rev` at all
    (today's every existing call site) keeps working exactly as before —
    the guard is opt-in, not mandatory, on purpose (see
    `_action_task_context_set`'s docstring for why this differs from
    `work_state.write`, which DOES refuse a blind write)."""
    A._action_task_context_set(
        {"slug": "demo", "task_id": "T-0655", "text": "written blind"})
    res = A._action_task_context_set(
        {"slug": "demo", "task_id": "T-0655", "text": "written blind again"})
    assert res["context_rev"] == 2
    assert "written blind again" in board.read_text()


def test_context_rev_starts_at_zero_for_a_ticket_with_no_prior_context_write(board):
    assert "context_rev" not in board.read_text()
    res = A._action_task_context_set(
        {"slug": "demo", "task_id": "T-0655", "text": "x", "base_rev": 0})
    assert res["context_rev"] == 1


def test_stale_write_refuses_and_names_the_dropped_content(board):
    """DoD 2, the scratch-ticket proof: write A, write B from ANOTHER
    session, then attempt a write derived from A (A's rev, A's text plus its
    own addition — the exact shape of a hand-preserved stale local copy).
    Must refuse and NAME what it would have dropped, not just say the rev
    moved."""
    a = A._action_task_context_set(
        {"slug": "demo", "task_id": "T-0655", "text": "state written by session A"})
    assert a["context_rev"] == 1

    b = A._action_task_context_set({
        "slug": "demo", "task_id": "T-0655",
        "text": "state written by session A\n\nB's SHIPPED report, landed after A read",
        "base_rev": 1,
    })
    assert b["context_rev"] == 2

    # Session A now writes back its (stale) local copy, augmented with its
    # own new work — unaware B ever landed, exactly like the documented
    # "hold a local copy, re-append, write it back" recovery workaround.
    with pytest.raises(A.ActionError) as exc_info:
        A._action_task_context_set({
            "slug": "demo", "task_id": "T-0655",
            "text": "state written by session A\n\nA's own next step",
            "base_rev": 1,
        })
    msg = str(exc_info.value)
    assert "context_rev 1" in msg and "context_rev 2" in msg
    assert "B's SHIPPED report" in msg  # names what would be dropped
    assert "--base-rev 2" in msg        # DoD 4: a concrete next step

    # REFUSES rather than merges: B's write is untouched on disk.
    assert "B's SHIPPED report" in board.read_text()
    assert "A's own next step" not in board.read_text()


def test_stale_write_conflict_message_is_honest_when_nothing_is_obviously_lost(board):
    """A rev mismatch can happen with no paragraph-level loss (e.g. the other
    write only touched something outside a simple presence check) — the
    message must not falsely claim a drop it can't show."""
    A._action_task_context_set(
        {"slug": "demo", "task_id": "T-0655", "text": "shared line"})
    A._action_task_context_set({
        "slug": "demo", "task_id": "T-0655", "text": "shared line",
        "base_rev": 1,
    })
    with pytest.raises(A.ActionError, match="something else changed"):
        A._action_task_context_set({
            "slug": "demo", "task_id": "T-0655", "text": "shared line",
            "base_rev": 1,
        })
