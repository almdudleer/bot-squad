"""Tests for markdown_writer module — atomic writes + frontmatter merge."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import fcntl
import pytest

from app.markdown_writer import (
    allocate_next_id,
    merge_task_update,
    slugify,
    write_task,
)


# ---------------------------------------------------------------------------
# write_task
# ---------------------------------------------------------------------------

def test_write_task_produces_parseable_file(tmp_path: Path):
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0001-hello.md"
    fm = {"id": "T-0001", "title": "Hello", "status": "open"}
    write_task(p, fm, "body text\n")
    task = parse_task(p)
    assert task["id"] == "T-0001"
    assert task["title"] == "Hello"
    assert task["status"] == "open"
    assert "body text" in task["body"]


def test_write_task_atomic_no_tmp_leftover(tmp_path: Path):
    p = tmp_path / "T-0002-atomic.md"
    fm = {"id": "T-0002", "title": "Atomic", "status": "open"}
    write_task(p, fm, "body\n")
    # After successful write, the .tmp file must be gone
    tmp = tmp_path / "T-0002-atomic.md.tmp"
    assert not tmp.exists()
    assert p.exists()


def test_write_task_handles_unicode_title(tmp_path: Path):
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0003-ru.md"
    fm = {"id": "T-0003", "title": "Задача на русском", "status": "open"}
    write_task(p, fm, "тело\n")
    task = parse_task(p)
    assert task["title"] == "Задача на русском"


# ---------------------------------------------------------------------------
# merge_task_update
# ---------------------------------------------------------------------------

def test_merge_task_update_rejects_unknown_keys(tmp_path: Path):
    p = tmp_path / "T-0010-x.md"
    write_task(p, {"id": "T-0010", "title": "X", "status": "open"}, "body\n")
    with pytest.raises(ValueError, match="disallowed"):
        merge_task_update(p, {"secret": "hax"})


def test_merge_task_update_bumps_updated_timestamp(tmp_path: Path):
    p = tmp_path / "T-0011-x.md"
    write_task(p, {"id": "T-0011", "title": "X", "status": "open"}, "body\n")
    new_fm = merge_task_update(p, {"status": "closed"})
    assert new_fm["status"] == "closed"
    assert "updated" in new_fm


def test_merge_task_update_stamps_status_since_on_real_transition(tmp_path: Path):
    """T-0950: a real status change stamps WHEN, separate from `updated`
    (which every write here bumps regardless of what changed)."""
    p = tmp_path / "T-0011b-x.md"
    write_task(p, {"id": "T-0011b", "title": "X", "status": "open"}, "body\n")
    new_fm = merge_task_update(p, {"status": "in_progress"})
    assert "status_since" in new_fm
    assert new_fm["status_since"] == new_fm["updated"]


def test_merge_task_update_noop_status_never_restamps(tmp_path: Path):
    p = tmp_path / "T-0011c-x.md"
    write_task(p, {"id": "T-0011c", "title": "X", "status": "open",
                   "status_since": "2020-01-01T00:00:00Z"}, "body\n")
    new_fm = merge_task_update(p, {"status": "open"})
    assert new_fm["status_since"] == "2020-01-01T00:00:00Z"


def test_merge_task_update_reentry_within_same_second_gets_monotonic_stamp(
    tmp_path: Path, monkeypatch
):
    """T-1016: status_since is stamped at whole-second resolution (the exact
    twin of scripts/cli/bsq's writer), so two real transitions landing inside
    one wall-clock second must not collapse to an identical stamp —
    status_deadlines.py's alert dedup is keyed on
    (ticket_id, status, status_since), so an unchanged stamp on re-entry reads
    as "still the same stay" and swallows the alert. Freeze the clock rather
    than rely on wall-clock luck to land in the same second."""
    import app.markdown_writer as mw
    from datetime import datetime, timezone

    frozen = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(mw, "datetime", _FrozenDatetime)
    p = tmp_path / "T-0011e-x.md"
    write_task(p, {"id": "T-0011e", "title": "X", "status": "in_progress",
                   "status_since": "2020-01-01T00:00:00Z"}, "body\n")
    first_fm = merge_task_update(p, {"status": "blocked_on_user"})
    first = first_fm["status_since"]
    second_fm = merge_task_update(p, {"status": "in_progress"})
    third_fm = merge_task_update(p, {"status": "blocked_on_user"})
    second = third_fm["status_since"]
    assert first == "2025-01-01T00:00:00Z"
    assert second != first
    assert second == "2025-01-01T00:00:02Z"  # two same-second collisions, each bumped 1s


def test_merge_task_update_unrelated_field_never_touches_status_since(tmp_path: Path):
    """A body/title edit bumps `updated` but must NOT reset the status clock —
    that is precisely the confusion T-0950's deadline sweep exists to avoid."""
    p = tmp_path / "T-0011d-x.md"
    write_task(p, {"id": "T-0011d", "title": "X", "status": "blocked_on_user",
                   "status_since": "2020-01-01T00:00:00Z"}, "body\n")
    new_fm = merge_task_update(p, {"title": "New"})
    assert new_fm["status_since"] == "2020-01-01T00:00:00Z"
    assert new_fm["updated"] != "2020-01-01T00:00:00Z"


def test_merge_task_update_changes_title(tmp_path: Path):
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0012-x.md"
    write_task(p, {"id": "T-0012", "title": "Old", "status": "open"}, "body\n")
    merge_task_update(p, {"title": "New"})
    task = parse_task(p)
    assert task["title"] == "New"


def test_merge_task_update_changes_body(tmp_path: Path):
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0013-x.md"
    write_task(p, {"id": "T-0013", "title": "X", "status": "open"}, "old body\n")
    merge_task_update(p, {}, body="new body\n")
    task = parse_task(p)
    assert "new body" in task["body"]


def test_merge_task_update_fills_created_from_mtime(tmp_path: Path):
    """Tasks without created field get it filled from file mtime on first write."""
    p = tmp_path / "T-0014-x.md"
    # Write without created/updated (old-style task)
    p.write_text("---\nid: T-0014\ntitle: Old task\nstatus: open\n---\n\nbody\n")
    new_fm = merge_task_update(p, {"status": "totest"})
    assert "created" in new_fm
    assert "updated" in new_fm


def test_write_task_priority_round_trips_as_int(tmp_path: Path):
    """Phase 8 — `priority` round-trips cleanly as an int (no quotes)."""
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0050-prio.md"
    fm = {"id": "T-0050", "title": "P", "status": "open", "priority": 250}
    write_task(p, fm, "body\n")
    task = parse_task(p)
    assert task["priority"] == 250
    assert isinstance(task["priority"], int)
    raw = p.read_text()
    assert "priority: 250" in raw


def test_write_task_skips_none_priority(tmp_path: Path):
    """None priority must not serialize as `priority: null`."""
    p = tmp_path / "T-0051-noprio.md"
    fm = {"id": "T-0051", "title": "P", "status": "open", "priority": None}
    write_task(p, fm, "body\n")
    raw = p.read_text()
    assert "priority" not in raw


def test_merge_task_update_priority_round_trip(tmp_path: Path):
    """Phase 8 — merge_task_update accepts priority and persists it."""
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0052-mp.md"
    write_task(p, {"id": "T-0052", "title": "X", "status": "open"}, "body\n")
    merge_task_update(p, {"priority": 300})
    task = parse_task(p)
    assert task["priority"] == 300


# ---------------------------------------------------------------------------
# T-0038 — initiative / parent_task / blocked_by are merge-updatable
# ---------------------------------------------------------------------------

def test_merge_task_update_accepts_initiative(tmp_path: Path):
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0060-init.md"
    write_task(p, {"id": "T-0060", "title": "X", "status": "open"}, "body\n")
    merge_task_update(p, {"initiative": "multi-server-installation-process.md"})
    task = parse_task(p)
    assert task["initiative"] == "multi-server-installation-process.md"


def test_merge_task_update_accepts_parent_and_blocked_by(tmp_path: Path):
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0061-links.md"
    write_task(p, {"id": "T-0061", "title": "X", "status": "open"}, "body\n")
    merge_task_update(p, {"parent_task": "T-0007", "blocked_by": ["T-0001"]})
    task = parse_task(p)
    assert task["parent_task"] == "T-0007"
    assert task["blocked_by"] == ["T-0001"]


def test_merge_task_update_clears_initiative_with_none(tmp_path: Path):
    """Setting initiative=None drops the field from frontmatter."""
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0062-clr.md"
    write_task(p, {
        "id": "T-0062", "title": "X", "status": "open",
        "initiative": "foo.md",
    }, "body\n")
    merge_task_update(p, {"initiative": None})
    task = parse_task(p)
    assert task.get("initiative") is None


# ---------------------------------------------------------------------------
# T-0480 — `kind` is a first-class task field (kind: initiative)
# ---------------------------------------------------------------------------

def test_merge_task_update_accepts_kind(tmp_path: Path):
    """T-0480: an initiative is a task marked `kind: initiative`; the field is
    patchable through the shared writer like any other linkage field."""
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0070-kind.md"
    write_task(p, {"id": "T-0070", "title": "X", "status": "open"}, "body\n")
    merge_task_update(p, {"kind": "initiative"})
    task = parse_task(p)
    assert task["kind"] == "initiative"


def test_merge_task_update_accepts_initiative_kind(tmp_path: Path):
    """T-0354: persistent-vs-one-shot is patchable post-creation, distinct
    from the `kind` field (which marks initiative-vs-task)."""
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0071-initiative-kind.md"
    write_task(p, {"id": "T-0071", "title": "X", "status": "open", "kind": "initiative"}, "body\n")
    merge_task_update(p, {"initiative_kind": "persistent"})
    task = parse_task(p)
    assert task["kind"] == "initiative"
    assert task["initiative_kind"] == "persistent"


# ---------------------------------------------------------------------------
# T-0105 — session_history is allowed through merge_task_update
# ---------------------------------------------------------------------------

def test_merge_task_update_accepts_session_history(tmp_path: Path):
    """T-0105: the api PATCH path can backfill session_history.
    Worker writes inline; api may write block-yaml — both are valid YAML."""
    from app.markdown_parser import parse_task

    p = tmp_path / "T-0063-hist.md"
    write_task(p, {"id": "T-0063", "title": "X", "status": "open"}, "body\n")
    merge_task_update(p, {
        "session_history": ["S-alice-w-p2", "S-alice-w-p9"],
    })
    task = parse_task(p)
    assert task["session_history"] == ["S-alice-w-p2", "S-alice-w-p9"]


def test_write_task_emits_inline_lists(tmp_path: Path):
    """T-0075: the shared writer emits list fields INLINE (`[a, b]`), not block
    style — so worker- and api-written lists are byte-shape-identical and the
    block-vs-inline drift that hid lists from line-based readers is gone."""
    p = tmp_path / "T-0064-inline.md"
    write_task(p, {
        "id": "T-0064", "title": "X", "status": "open",
        "blocked_by": ["T-0001", "T-0002"],
    }, "body\n")
    raw = p.read_text()
    assert "blocked_by: [T-0001, T-0002]" in raw
    assert "- T-0001" not in raw


def test_merge_heals_block_style_to_inline(tmp_path: Path):
    """A pre-existing block-style list is rewritten inline on the next merge."""
    p = tmp_path / "T-0065-heal.md"
    p.write_text(
        "---\nid: T-0065\ntitle: X\nstatus: open\n"
        "blocked_by:\n- T-0001\n- T-0002\n---\n\nbody\n"
    )
    merge_task_update(p, {"status": "totest"})
    raw = p.read_text()
    assert "blocked_by: [T-0001, T-0002]" in raw
    assert "- T-0001" not in raw
    from app.markdown_parser import parse_task
    assert parse_task(p)["blocked_by"] == ["T-0001", "T-0002"]


# ---------------------------------------------------------------------------
# allocate_next_id
# ---------------------------------------------------------------------------

def test_allocate_next_id_empty_dir(tmp_path: Path):
    assert allocate_next_id(tmp_path) == "T-0001"


def test_allocate_next_id_with_existing(tmp_path: Path):
    (tmp_path / "T-0001-foo.md").touch()
    (tmp_path / "T-0042-bar.md").touch()
    assert allocate_next_id(tmp_path) == "T-0043"


def test_allocate_next_id_ignores_non_task_files(tmp_path: Path):
    (tmp_path / "README.md").touch()
    (tmp_path / ".lock").touch()
    assert allocate_next_id(tmp_path) == "T-0001"


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_strips_punctuation():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_lowercases():
    assert slugify("UPPER") == "upper"


def test_slugify_truncates_to_max_len():
    long_str = "a" * 100
    result = slugify(long_str, max_len=10)
    assert len(result) <= 10


def test_slugify_handles_consecutive_separators():
    result = slugify("hello  --  world")
    assert "--" not in result
    assert "hello" in result
    assert "world" in result


def test_slugify_handles_russian_text():
    # Non-ASCII should be stripped/transliterated; result is ASCII kebab
    result = slugify("Задача первая")
    # Should not crash; result should be ASCII only
    assert result.isascii() or result == ""


# ---------------------------------------------------------------------------
# flock correctness
# ---------------------------------------------------------------------------

def test_flock_second_acquire_blocks(tmp_path: Path):
    """Two threads: first holds lock, second cannot acquire immediately (LOCK_NB)."""
    lock_path = tmp_path / ".lock"

    acquired = threading.Event()
    released = threading.Event()
    blocked = threading.Event()

    def holder():
        with open(lock_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            acquired.set()
            released.wait(timeout=2)
            fcntl.flock(f, fcntl.LOCK_UN)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    acquired.wait(timeout=2)

    # Try non-blocking acquire — should fail while holder has the lock
    with open(lock_path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Should not reach here
            blocked.set()
        except (BlockingIOError, OSError):
            pass  # Expected: lock is held

    assert not blocked.is_set(), "Non-blocking acquire should have failed"
    released.set()
    t.join(timeout=2)
