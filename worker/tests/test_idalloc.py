"""Tests for the generalized atomic ID allocator (T-0174)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bot_squad_worker import idalloc


def test_format_id_per_type():
    assert idalloc.format_id("task", 7) == "T-0007"
    assert idalloc.format_id("doc", 42) == "D-0042"
    assert idalloc.format_id("initiative", 3) == "INI-03"
    assert idalloc.format_id("routine", 7) == "R-0007"  # T-0464: routine type


def test_routine_allocates_and_scans_its_subdir(tmp_path):
    # T-0464: routines live under data/<slug>/routines/<R-NNNN>-*.md (non-recursive).
    assert idalloc.allocate_id(tmp_path, "p", "routine") == "R-0001"
    (tmp_path / "p" / "routines").mkdir(parents=True, exist_ok=True)
    (tmp_path / "p" / "routines" / "R-0050-x.md").write_text("x")
    assert idalloc.allocate_id(tmp_path, "p", "routine") == "R-0051"


def test_unknown_type_raises():
    with pytest.raises(idalloc.IdAllocError):
        idalloc.format_id("nope", 1)
    with pytest.raises(idalloc.IdAllocError):
        idalloc.allocate_id(Path("/tmp"), "slug", "nope")


def test_bad_slug_raises(tmp_path):
    for bad in ("", "a/b", ".", ".."):
        with pytest.raises(idalloc.IdAllocError):
            idalloc.allocate_id(tmp_path, bad, "task")


def test_allocate_seeds_from_empty(tmp_path):
    assert idalloc.allocate_id(tmp_path, "p", "task") == "T-0001"
    assert idalloc.allocate_id(tmp_path, "p", "task") == "T-0002"
    assert (tmp_path / "p" / "_counters" / "task.txt").read_text().strip() == "2"


def test_allocate_seeds_from_existing_files(tmp_path):
    """Migration: first allocation = max(existing entity id) + 1."""
    backlog = tmp_path / "p" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "T-0133-seed.md").write_text("x")
    (backlog / "T-0007-seed.md").write_text("x")
    assert idalloc.allocate_id(tmp_path, "p", "task") == "T-0134"


def test_scan_existing_max_honors_frontmatter_id_over_filename(tmp_path):
    """T-0231: a file's ``id:`` frontmatter can diverge from its filename's
    numeric prefix (manual edit, rename-without-refile, migration artifact).
    The filename-only scan would miss such a file entirely; allocation must
    still never mint an id some file already declares as its own."""
    backlog = tmp_path / "p" / "backlog"
    backlog.mkdir(parents=True)
    # Filename has no numeric prefix at all, but frontmatter claims T-0500.
    (backlog / "subscription-contract-redesign.md").write_text(
        "---\nid: T-0500\ntitle: x\nstatus: open\n---\n\nbody\n"
    )
    assert idalloc.allocate_id(tmp_path, "p", "task") == "T-0501"


def test_scan_existing_max_ignores_non_matching_frontmatter_ids(tmp_path):
    """A tombstone's deliberately-mismatched id (e.g. 'T-0030-DUPLICATE-DO-NOT-USE')
    must not be parsed as a numeric collision for some OTHER prefix's counter."""
    backlog = tmp_path / "p" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "T-0030-tombstone.md").write_text(
        "---\nid: T-0030-DUPLICATE-DO-NOT-USE\ntitle: x\nstatus: closed\n---\n\nbody\n"
    )
    # Filename prefix T-0030 still counts (unchanged behavior); the malformed
    # frontmatter id contributes nothing extra since it doesn't match `T-\d+`.
    assert idalloc.allocate_id(tmp_path, "p", "task") == "T-0031"


def test_allocate_self_heals_against_manual_higher_id(tmp_path):
    docs = tmp_path / "p" / "docs" / "arch"
    docs.mkdir(parents=True)
    # Counter starts at 1...
    assert idalloc.allocate_id(tmp_path, "p", "doc") == "D-0001"
    # ...then someone hand-drops a far-higher id. Next alloc must clear it.
    (docs / "D-0500-manual.md").write_text("x")
    assert idalloc.allocate_id(tmp_path, "p", "doc") == "D-0501"


def test_legacy_slug_ids_ignored_by_scan(tmp_path):
    docs = tmp_path / "p" / "docs" / "arch"
    docs.mkdir(parents=True)
    (docs / "autopilot-popover.md").write_text("x")  # non-numeric legacy
    assert idalloc.allocate_id(tmp_path, "p", "doc") == "D-0001"


def test_concurrent_allocation_no_collisions(tmp_path):
    def alloc(i: int) -> str:
        return idalloc.allocate_id(tmp_path, "p", "doc")

    with ThreadPoolExecutor(max_workers=16) as ex:
        ids = list(ex.map(alloc, range(50)))
    assert len(set(ids)) == 50
    assert set(ids) == {f"D-{i:04d}" for i in range(1, 51)}


# The worker/api byte-identical mirror check for idalloc.py used to live here as
# its own copy of the comparison. T-0743 moved it to the single registry in
# `worker/tests/test_module_mirrors.py`, which also runs on every push via
# `scripts/lint/module_mirrors.py` — this one never did.
