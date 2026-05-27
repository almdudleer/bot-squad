"""T-0121: tests for scripts/lint/backlog_frontmatter.py."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _find_lint_script() -> Path:
    """Locate the lint script — robust to layout (repo root, sibling mount, env)."""
    env_path = os.environ.get("BACKLOG_LINT_SCRIPT")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "scripts" / "lint" / "backlog_frontmatter.py"
        if candidate.exists():
            return candidate
        # Sibling-mount layout (api at /app, scripts at /scripts).
        candidate2 = ancestor.parent / "scripts" / "lint" / "backlog_frontmatter.py"
        if candidate2.exists():
            return candidate2
    raise FileNotFoundError(
        "could not locate scripts/lint/backlog_frontmatter.py — "
        "set BACKLOG_LINT_SCRIPT to point at it"
    )


LINT_PATH = _find_lint_script()


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("backlog_frontmatter_lint", LINT_PATH)
    assert spec and spec.loader, f"could not load {LINT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def lint():
    return _load_lint_module()


def _write_task(backlog_dir: Path, task_id: str, status: str) -> Path:
    p = backlog_dir / f"{task_id}-x.md"
    p.write_text(
        f"---\nid: {task_id}\ntitle: x\nstatus: {status}\n---\n\nbody\n"
    )
    return p


def test_lint_clean_when_all_statuses_canonical(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    for st in ("planned", "open", "in_progress", "totest", "reopened", "closed"):
        _write_task(bl, f"T-000{ord(st[0])%9+1}", st)
    assert lint.lint_dir(tmp_path) == []
    assert lint.main([str(tmp_path)]) == 0


def test_lint_flags_legacy_done_status(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", "open")
    bad = _write_task(bl, "T-0002", "done")
    offenders = lint.lint_dir(tmp_path)
    assert offenders == [(bad, "done")]
    assert lint.main([str(tmp_path)]) == 1


def test_lint_flags_other_bad_values(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", "backlog")  # T-0111 real case
    _write_task(bl, "T-0002", "wontfix")
    offenders = lint.lint_dir(tmp_path)
    assert sorted(s for _, s in offenders) == ["backlog", "wontfix"]


def test_lint_skips_md_without_frontmatter(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    (bl / "T-0001-x.md").write_text("just a body, no frontmatter\n")
    assert lint.lint_dir(tmp_path) == []


def test_lint_skips_md_with_no_status_line(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    (bl / "T-0001-x.md").write_text("---\nid: T-0001\ntitle: x\n---\n\nbody\n")
    assert lint.lint_dir(tmp_path) == []


def test_lint_handles_quoted_status(tmp_path, lint):
    """YAML allows 'open' or "open" — strip quotes before checking."""
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    (bl / "T-0001-x.md").write_text(
        '---\nid: T-0001\ntitle: x\nstatus: "closed"\n---\n\nbody\n'
    )
    assert lint.lint_dir(tmp_path) == []


def test_lint_walks_multiple_slugs(tmp_path, lint):
    for slug in ("a", "b"):
        bl = tmp_path / slug / "backlog"
        bl.mkdir(parents=True)
        _write_task(bl, "T-0001", "open")
    _write_task(tmp_path / "a" / "backlog", "T-0002", "done")
    offenders = lint.lint_dir(tmp_path)
    assert len(offenders) == 1
    assert offenders[0][1] == "done"


def test_lint_returns_zero_when_data_dir_missing(tmp_path, lint):
    missing = tmp_path / "does-not-exist"
    assert lint.lint_dir(missing) == []
    assert lint.main([str(missing)]) == 0


def test_lint_module_constant_matches_api(lint):
    """SSOT cross-check: keep the lint set in lockstep with routes_backlog."""
    from app.routes_backlog import _VALID_STATUSES as api_set
    assert set(lint.VALID_STATUSES) == api_set
