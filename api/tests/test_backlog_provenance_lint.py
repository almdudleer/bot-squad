"""T-0301: tests for scripts/lint/backlog_provenance.py."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

POST = "2026-06-25"  # on/after the default cutoff
PRE = "2026-01-01"   # before the default cutoff
CUTOFF = "2026-06-21"


def _find_lint_script() -> Path:
    env_path = os.environ.get("BACKLOG_PROVENANCE_LINT_SCRIPT")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "scripts" / "lint" / "backlog_provenance.py"
        if candidate.exists():
            return candidate
        candidate2 = ancestor.parent / "scripts" / "lint" / "backlog_provenance.py"
        if candidate2.exists():
            return candidate2
    raise FileNotFoundError(
        "could not locate scripts/lint/backlog_provenance.py — "
        "set BACKLOG_PROVENANCE_LINT_SCRIPT to point at it"
    )


LINT_PATH = _find_lint_script()


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("backlog_provenance_lint", LINT_PATH)
    assert spec and spec.loader, f"could not load {LINT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def lint():
    return _load_lint_module()


def _write_task(backlog_dir: Path, task_id: str, created: str,
                provenance: str | None) -> Path:
    p = backlog_dir / f"{task_id}-x.md"
    lines = [f"id: {task_id}", "title: x", "status: planned", f"created: {created}"]
    if provenance is not None:
        lines.append(f'provenance: "{provenance}"')
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n")
    return p


def test_clean_when_postcutoff_ticket_has_valid_provenance(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", POST, "corpus:sessions")
    assert lint.lint_dir(tmp_path, CUTOFF) == []
    assert lint.main([str(tmp_path)]) == 0  # default cutoff > POST? no — POST>=cutoff, still valid


def test_flags_postcutoff_ticket_missing_provenance(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    bad = _write_task(bl, "T-0002", POST, None)
    offenders = lint.lint_dir(tmp_path, CUTOFF)
    assert offenders == [(bad, "missing provenance")]
    assert lint.main([str(tmp_path)]) == 1


def test_grandfathers_precutoff_ticket_without_provenance(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0003", PRE, None)
    assert lint.lint_dir(tmp_path, CUTOFF) == []


def test_flags_invalid_provenance_token(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    bad = _write_task(bl, "T-0004", POST, "vibes")
    offenders = lint.lint_dir(tmp_path, CUTOFF)
    assert offenders == [(bad, "invalid provenance='vibes'")]


@pytest.mark.parametrize(
    "value",
    ["corpus:multi-server", "F-0007", "T-0221", "stakeholder:2026-06-20",
     "corpus:sessions, F-0012"],
)
def test_accepts_all_valid_provenance_forms(tmp_path, lint, value):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0005", POST, value)
    assert lint.lint_dir(tmp_path, CUTOFF) == []


def test_skips_ticket_with_unparseable_created(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    (bl / "T-0006-x.md").write_text(
        "---\nid: T-0006\ntitle: x\nstatus: planned\n---\n\nbody\n"  # no created
    )
    assert lint.lint_dir(tmp_path, CUTOFF) == []


def test_returns_zero_when_data_dir_missing(tmp_path, lint):
    missing = tmp_path / "does-not-exist"
    assert lint.lint_dir(missing, CUTOFF) == []
    assert lint.main([str(missing)]) == 0
