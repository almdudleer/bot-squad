"""T-0207: tests for scripts/lint/backlog_ids.py (out-of-band id guard)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _find_lint_script() -> Path:
    """Locate the lint script — robust to layout (repo root, sibling mount, env)."""
    env_path = os.environ.get("BACKLOG_IDS_LINT_SCRIPT")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "scripts" / "lint" / "backlog_ids.py"
        if candidate.exists():
            return candidate
        candidate2 = ancestor.parent / "scripts" / "lint" / "backlog_ids.py"
        if candidate2.exists():
            return candidate2
    raise FileNotFoundError(
        "could not locate scripts/lint/backlog_ids.py — "
        "set BACKLOG_IDS_LINT_SCRIPT to point at it"
    )


LINT_PATH = _find_lint_script()


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("backlog_ids_lint", LINT_PATH)
    assert spec and spec.loader, f"could not load {LINT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def lint():
    return _load_lint_module()


def _project(tmp_path: Path, slug: str = "s", counter: int | None = None) -> Path:
    bl = tmp_path / slug / "backlog"
    bl.mkdir(parents=True)
    if counter is not None:
        cdir = tmp_path / slug / "_counters"
        cdir.mkdir(parents=True)
        (cdir / "task.txt").write_text(str(counter))
    return bl


def _ticket(backlog: Path, filename_id: str, *, fm_id: str | None = None) -> Path:
    p = backlog / f"{filename_id}-x.md"
    p.write_text(f"---\nid: {fm_id or filename_id}\ntitle: x\nstatus: open\n---\n\nbody\n")
    return p


# --- clean cases -----------------------------------------------------------

def test_clean_when_all_ids_within_counter(tmp_path, lint):
    bl = _project(tmp_path, counter=5)
    _ticket(bl, "T-0001")
    _ticket(bl, "T-0005")  # == counter is fine (last allocated)
    assert lint.lint_dir(tmp_path) == []
    assert lint.main([str(tmp_path)]) == 0


def test_non_ticket_files_ignored(tmp_path, lint):
    bl = _project(tmp_path, counter=3)
    (bl / "README.md").write_text("not a ticket\n")
    (bl / "TX-1.md").write_text("---\nid: TX-1\n---\n")  # wrong prefix
    _ticket(bl, "T-0002")
    assert lint.lint_dir(tmp_path) == []


# --- (1) out-of-band mint --------------------------------------------------

def test_flags_id_above_counter(tmp_path, lint):
    bl = _project(tmp_path, counter=5)
    bad = _ticket(bl, "T-0009")  # hand-picked above the high-water mark
    offenders = lint.lint_dir(tmp_path)
    assert len(offenders) == 1 and offenders[0][0] == bad
    assert "out-of-band" in offenders[0][1]
    assert lint.main([str(tmp_path)]) == 1


def test_oob_check_skipped_when_counter_missing(tmp_path, lint):
    """Legacy project with no counter file: cannot judge out-of-band; don't
    false-positive every ticket as > 0."""
    bl = _project(tmp_path, counter=None)
    _ticket(bl, "T-0007")
    _ticket(bl, "T-0042")
    assert lint.lint_dir(tmp_path) == []


def test_oob_check_skipped_when_counter_garbage(tmp_path, lint):
    bl = _project(tmp_path)
    cdir = tmp_path / "s" / "_counters"
    cdir.mkdir(parents=True)
    (cdir / "task.txt").write_text("not-a-number")
    _ticket(bl, "T-0009")
    assert lint.lint_dir(tmp_path) == []


# --- (2) duplicate ids -----------------------------------------------------

def test_flags_duplicate_ids(tmp_path, lint):
    bl = _project(tmp_path, counter=30)
    a = bl / "T-0030-aaa.md"
    b = bl / "T-0030-bbb.md"
    a.write_text("---\nid: T-0030\n---\n")
    b.write_text("---\nid: T-0030\n---\n")
    offenders = lint.lint_dir(tmp_path)
    flagged = {p for p, _ in offenders}
    assert flagged == {a, b}
    assert all("duplicate id T-0030" in r for _, r in offenders)


# --- (3) filename vs frontmatter id mismatch -------------------------------

def test_flags_filename_frontmatter_mismatch(tmp_path, lint):
    bl = _project(tmp_path, counter=70)
    bad = _ticket(bl, "T-0069", fm_id="T-0058")  # the real T-0069 incident
    offenders = lint.lint_dir(tmp_path)
    assert len(offenders) == 1 and offenders[0][0] == bad
    assert "frontmatter id 'T-0058'" in offenders[0][1]


def test_no_mismatch_when_ids_agree(tmp_path, lint):
    bl = _project(tmp_path, counter=70)
    _ticket(bl, "T-0069", fm_id="T-0069")
    assert lint.lint_dir(tmp_path) == []


# --- main() plumbing -------------------------------------------------------

def test_main_nonexistent_dir_exits_zero(tmp_path, lint):
    assert lint.main([str(tmp_path / "nope")]) == 0
