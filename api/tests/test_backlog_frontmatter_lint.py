"""T-0121/T-0975: tests for scripts/lint/backlog_frontmatter.py."""
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
    statuses = ("planned", "open", "in_progress", "totest", "reopened", "closed")
    for i, st in enumerate(statuses, start=1):
        _write_task(bl, f"T-{i:04d}", st)
    result = lint.lint_dir(tmp_path)
    assert result.ok
    assert result.unreadable == []
    assert result.offenders == []
    assert result.total == 6
    assert lint.main([str(tmp_path)]) == 0


def test_lint_flags_legacy_done_status(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", "open")
    bad = _write_task(bl, "T-0002", "done")
    result = lint.lint_dir(tmp_path)
    assert result.offenders == [(bad, "done")]
    assert not result.ok
    assert lint.main([str(tmp_path)]) == 1


def test_lint_flags_other_bad_values(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", "backlog")  # T-0111 real case
    _write_task(bl, "T-0002", "wontfix")
    result = lint.lint_dir(tmp_path)
    assert sorted(s for _, s in result.offenders) == ["backlog", "wontfix"]


def test_lint_skips_md_without_frontmatter(tmp_path, lint):
    """A file with no `---` fence at all isn't a ticket — tolerated."""
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    (bl / "T-0001-x.md").write_text("just a body, no frontmatter\n")
    result = lint.lint_dir(tmp_path)
    assert result.ok
    assert result.unreadable == []


def test_lint_skips_md_with_no_status_line(tmp_path, lint):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    (bl / "T-0001-x.md").write_text("---\nid: T-0001\ntitle: x\n---\n\nbody\n")
    result = lint.lint_dir(tmp_path)
    assert result.ok


def test_lint_handles_quoted_status(tmp_path, lint):
    """YAML allows 'open' or "open" — a real YAML reader unquotes it for us."""
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    (bl / "T-0001-x.md").write_text(
        '---\nid: T-0001\ntitle: x\nstatus: "closed"\n---\n\nbody\n'
    )
    result = lint.lint_dir(tmp_path)
    assert result.ok


def test_lint_walks_multiple_slugs(tmp_path, lint):
    for slug in ("a", "b"):
        bl = tmp_path / slug / "backlog"
        bl.mkdir(parents=True)
        _write_task(bl, "T-0001", "open")
    _write_task(tmp_path / "a" / "backlog", "T-0002", "done")
    result = lint.lint_dir(tmp_path)
    assert len(result.offenders) == 1
    assert result.offenders[0][1] == "done"
    assert result.total == 3


def test_lint_dir_result_ok_when_data_dir_missing(tmp_path, lint):
    """`lint_dir` itself has nothing to flag on a missing dir — but `main`
    refuses this, see test_lint_refuses_zero_of_zero_run below: 0-of-0
    is not the same claim as "clean"."""
    missing = tmp_path / "does-not-exist"
    result = lint.lint_dir(missing)
    assert result.ok
    assert result.total == 0


def test_lint_module_constant_matches_api(lint):
    """SSOT cross-check: keep the lint set in lockstep with routes_backlog."""
    from app.routes_backlog import _VALID_STATUSES as api_set
    assert set(lint.VALID_STATUSES) == api_set


# --- T-0975: unreadable frontmatter must be REPORTED, never silently skipped ---


def test_lint_flags_unquoted_title_starting_with_quote(tmp_path, lint):
    """T-0013's actual shape: an unquoted title beginning with `"`."""
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    bad = bl / "T-0013-x.md"
    bad.write_text(
        '---\nid: T-0013\ntitle: "You\'re all set" post-install screen\nstatus: closed\n---\n\nbody\n'
    )
    result = lint.lint_dir(tmp_path)
    assert not result.ok
    assert len(result.unreadable) == 1
    assert result.unreadable[0][0] == bad
    assert result.total == 1
    assert result.read_ok == 0
    assert lint.main([str(tmp_path)]) == 1


def test_lint_flags_unquoted_title_with_bare_colon(tmp_path, lint):
    """T-0171's actual shape: an unquoted title containing a bare `: `."""
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    bad = bl / "T-0171-x.md"
    bad.write_text(
        "---\nid: T-0171\ntitle: Detached-installation: per-server field\nstatus: closed\n---\n\nbody\n"
    )
    result = lint.lint_dir(tmp_path)
    assert not result.ok
    assert len(result.unreadable) == 1
    assert result.unreadable[0][0] == bad


def test_lint_unreadable_file_is_not_silently_dropped_from_denominator(tmp_path, lint):
    """A tool that returns a clean number over a partial corpus is the defect
    (T-0975): the unreadable file must still count toward `total`, and
    `read_ok` must be strictly less than `total`."""
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", "open")
    (bl / "T-0002-x.md").write_text(
        '---\nid: T-0002\ntitle: "unterminated quote\nstatus: closed\n---\n\nbody\n'
    )
    result = lint.lint_dir(tmp_path)
    assert result.total == 2
    assert len(result.unreadable) == 1
    assert result.read_ok == 1


def test_split_frontmatter_ignores_dash_runs_in_title(lint):
    """A title containing "---" must not shift a naive `text.split("---")` —
    the real closing fence is found by a line-by-line scan (T-0975)."""
    text = "---\nid: T-0001\ntitle: before --- after\nstatus: open\n---\n\nbody\n"
    block = lint.split_frontmatter(text)
    assert block == "id: T-0001\ntitle: before --- after\nstatus: open"


def test_lint_prints_denominator_summary(tmp_path, lint, capsys):
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", "open")
    (bl / "T-0002-x.md").write_text('---\nid: T-0002\ntitle: "bad\nstatus: open\n---\n\nbody\n')
    lint.main([str(tmp_path)])
    err = capsys.readouterr().err
    assert "read 1 of 2 file(s)" in err
    assert "1 unreadable" in err


# --- T-0975 (operator review): "0 of 0" must refuse, not report a false clean ---


def test_lint_refuses_zero_of_zero_run(tmp_path, lint, capsys):
    """A missing/wrong-level/genuinely-empty path scans 0 files. Exiting 0
    here would certify NOTHING while looking like a healthy pass to a
    caller reading only the exit code — the operator hit this by hand
    (passed the backlog dir instead of the data dir) and only the stated
    denominator revealed the run was empty, not clean."""
    missing = tmp_path / "does-not-exist"
    assert lint.main([str(missing)]) == 2
    err = capsys.readouterr().err
    assert "read 0 of 0 file(s)" in err
    assert "REFUSING" in err
    assert str(missing) in err


def test_lint_refuses_zero_of_zero_on_existing_but_empty_dir(tmp_path, lint):
    """Same refusal for a data dir that exists but has no <slug>/backlog at
    all — not just a nonexistent path."""
    tmp_path.mkdir(exist_ok=True)
    assert lint.main([str(tmp_path)]) == 2


def test_lint_healthy_nonempty_run_still_exits_zero(tmp_path, lint):
    """The 0-of-0 refusal must not swallow the ordinary healthy case: files
    present, nothing wrong, still exits 0."""
    bl = tmp_path / "s" / "backlog"
    bl.mkdir(parents=True)
    _write_task(bl, "T-0001", "open")
    assert lint.main([str(tmp_path)]) == 0
