"""Tests for the generalized atomic ID allocator (T-0174)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bot_squad_worker import idalloc


def test_format_id_per_type():
    assert idalloc.format_id("task", 7) == "T-0007"
    assert idalloc.format_id("doc", 42) == "D-0042"
    assert idalloc.format_id("uc", 1) == "UC-0001"
    assert idalloc.format_id("flow", 1234) == "UF-1234"  # T-0180: user-flow prefix
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


def test_allocate_self_heals_against_manual_higher_id(tmp_path):
    docs = tmp_path / "p" / "docs" / "arch"
    docs.mkdir(parents=True)
    # Counter starts at 1...
    assert idalloc.allocate_id(tmp_path, "p", "doc") == "D-0001"
    # ...then someone hand-drops a far-higher id. Next alloc must clear it.
    (docs / "D-0500-manual.md").write_text("x")
    assert idalloc.allocate_id(tmp_path, "p", "doc") == "D-0501"


def test_scan_recursion_difference(tmp_path):
    """uc scan is shallow (top of use_cases/); flow scan recurses into it."""
    uc = tmp_path / "p" / "use_cases"
    (uc / "UC-0005" / "flows").mkdir(parents=True)
    (uc / "UC-0005.md").write_text("x")  # numeric UC at top level
    (uc / "UC-0005" / "flows" / "UF-0009-x.md").write_text("x")  # nested flow
    # uc (shallow) sees UC-0005 -> next UC-0006
    assert idalloc.allocate_id(tmp_path, "p", "uc") == "UC-0006"
    # flow (recursive) sees UF-0009 -> next UF-0010
    assert idalloc.allocate_id(tmp_path, "p", "flow") == "UF-0010"


def test_flow_prefix_disjoint_from_feedback(tmp_path):
    """T-0180: flows use UF-, curated feedback uses F- in a separate dir.

    A feedback `F-NNNN-*.md` under feedback/ must NOT influence flow allocation
    (different prefix AND different scan dir), so a bare id is never ambiguous
    between a user flow and a curated feedback item.
    """
    proj = tmp_path / "p"
    (proj / "feedback").mkdir(parents=True)
    # High-numbered feedback item that would collide if the flow scan saw it.
    (proj / "feedback" / "F-0099-some-friction.md").write_text("x")
    (proj / "use_cases").mkdir(parents=True)
    # First flow ignores the feedback F- entirely -> starts at UF-0001.
    assert idalloc.allocate_id(tmp_path, "p", "flow") == "UF-0001"
    # The flow prefix is "UF", deliberately not "F".
    assert idalloc.ENTITY_TYPES["flow"].prefix == "UF"


def test_legacy_slug_ids_ignored_by_scan(tmp_path):
    uc = tmp_path / "p" / "use_cases"
    uc.mkdir(parents=True)
    (uc / "UC-autopilot-popover.md").write_text("x")  # non-numeric legacy
    assert idalloc.allocate_id(tmp_path, "p", "uc") == "UC-0001"


def test_concurrent_allocation_no_collisions(tmp_path):
    def alloc(i: int) -> str:
        return idalloc.allocate_id(tmp_path, "p", "doc")

    with ThreadPoolExecutor(max_workers=16) as ex:
        ids = list(ex.map(alloc, range(50)))
    assert len(set(ids)) == 50
    assert set(ids) == {f"D-{i:04d}" for i in range(1, 51)}


def test_worker_and_api_copies_are_byte_identical():
    """The worker (systemd) and API (docker) run in separate environments and
    do not import each other, so the allocator is duplicated. The two copies
    MUST stay byte-identical — otherwise web creates and agent creates could
    diverge on the shared counter file."""
    repo = Path(__file__).resolve().parents[2]
    worker_copy = repo / "worker" / "bot_squad_worker" / "idalloc.py"
    api_copy = repo / "api" / "app" / "idalloc.py"
    assert api_copy.exists(), f"API mirror missing: {api_copy}"
    assert worker_copy.read_bytes() == api_copy.read_bytes(), (
        "worker/bot_squad_worker/idalloc.py and api/app/idalloc.py have drifted "
        "— re-sync them (they must be byte-identical)."
    )
