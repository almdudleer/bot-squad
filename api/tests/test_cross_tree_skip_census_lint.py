"""T-1036: tests for scripts/lint/cross_tree_skip_census.py.

Same shape as the sibling lints (test_duplicate_def_census_lint.py etc.):
locate the script, load it, drive it over synthetic fixtures — plus one scan
of the REAL repo so a new/removed cross-tree `importorskip` site can't land
uncensused. Two things this census must get right, both measured against a
sibling lint's own history: it must find a site NESTED inside a test function
(that is exactly where T-1036's real site lives — T-1017's duplicate-def
census got bitten by the opposite mistake, undercounting because it only
walked `tree.body`), and it must not match an unrelated `importorskip` call
for a module that isn't the other tree's package.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _find_lint_script() -> Path | None:
    """Locate the lint script — robust to layout (repo root, sibling mount, env)."""
    env_path = os.environ.get("CROSS_TREE_SKIP_CENSUS_SCRIPT")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "scripts" / "lint" / "cross_tree_skip_census.py"
        if candidate.exists():
            return candidate
        candidate2 = ancestor.parent / "scripts" / "lint" / "cross_tree_skip_census.py"
        if candidate2.exists():
            return candidate2
    return None


LINT_PATH = _find_lint_script()

if LINT_PATH is None:
    # Same posture as the sibling lints: the api-only docker mount has no
    # scripts/, so skip collection rather than aborting the whole suite.
    pytest.skip(
        "scripts/lint/cross_tree_skip_census.py not reachable from this mount",
        allow_module_level=True,
    )


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("cross_tree_skip_census_lint", LINT_PATH)
    assert spec and spec.loader, f"could not load {LINT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _load_lint_module()


def _write(tmp_path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The core traversal: a cross-tree importorskip is found regardless of nesting
# depth, and the enclosing test function names the nodeid.
# ---------------------------------------------------------------------------

def test_module_level_importorskip_is_found_with_no_enclosing_test(tmp_path):
    f = _write(tmp_path, "test_a.py", 'import pytest\npytest.importorskip("app")\n')
    hits = census.census_file(f, ["app"])
    assert hits == [(2, "app", None)]


def test_importorskip_nested_in_a_test_function_is_found(tmp_path):
    """The real shape of T-1036's own site: the guard lives INSIDE the test
    body, not at module level. A census that only walked module-level
    statements (T-1017's `tree.body` traversal, correct there for the opposite
    reason) would miss this entirely."""
    f = _write(
        tmp_path, "test_b.py",
        "import pytest\n"
        "\n"
        "def test_roundtrip():\n"
        '    pytest.importorskip("app.markdown_writer")\n'
        "    assert True\n",
    )
    hits = census.census_file(f, ["app"])
    assert hits == [(4, "app.markdown_writer", "test_roundtrip")]


def test_importorskip_for_an_unrelated_module_is_not_matched(tmp_path):
    f = _write(
        tmp_path, "test_c.py",
        "import pytest\n"
        "\n"
        "def test_needs_voice():\n"
        '    pytest.importorskip("faster_whisper")\n',
    )
    assert census.census_file(f, ["app"]) == []


def test_a_module_named_app_something_else_is_not_a_false_positive(tmp_path):
    """`app` must match as a dotted prefix, not a substring — `applesauce`
    reaching for an unrelated package must not be censused as cross-tree."""
    f = _write(
        tmp_path, "test_d.py",
        "import pytest\n"
        "\n"
        "def test_x():\n"
        '    pytest.importorskip("applesauce")\n',
    )
    assert census.census_file(f, ["app"]) == []


def test_census_tree_keys_by_relative_path(tmp_path, monkeypatch):
    tree_dir = tmp_path / "worker" / "tests"
    tree_dir.mkdir(parents=True)
    (tree_dir / "test_only.py").write_text(
        "import pytest\n"
        "\n"
        "def test_needs_app():\n"
        '    pytest.importorskip("app")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(census, "REPO_ROOT", tmp_path)
    out = census.census_tree("worker/tests", ["app"])
    assert list(out.keys()) == ["worker/tests/test_only.py"]
    assert out["worker/tests/test_only.py"] == [(4, "app", "test_needs_app")]


# ---------------------------------------------------------------------------
# CLI surface: --list and the allowlist-diff check, against a fixture repo so
# the pinned ALLOWLIST doesn't have to match this fixture's contents.
# ---------------------------------------------------------------------------

def _make_fixture_repo(tmp_path, worker_hit: bool, api_hit: bool) -> Path:
    (tmp_path / "worker" / "tests").mkdir(parents=True)
    (tmp_path / "api" / "tests").mkdir(parents=True)
    worker_body = "import pytest\n\n\ndef test_needs_app():\n    pytest.importorskip('app')\n" if worker_hit else "def test_plain():\n    assert True\n"
    api_body = "import pytest\n\n\ndef test_needs_worker():\n    pytest.importorskip('bot_squad_worker')\n" if api_hit else "def test_plain():\n    assert True\n"
    (tmp_path / "worker" / "tests" / "test_x.py").write_text(worker_body, encoding="utf-8")
    (tmp_path / "api" / "tests" / "test_y.py").write_text(api_body, encoding="utf-8")
    return tmp_path


def test_all_nodeids_covers_both_trees(tmp_path, monkeypatch):
    _make_fixture_repo(tmp_path, worker_hit=True, api_hit=True)
    monkeypatch.setattr(census, "REPO_ROOT", tmp_path)
    assert census.all_nodeids() == {
        "worker/tests/test_x.py::test_needs_app",
        "api/tests/test_y.py::test_needs_worker",
    }


def _run_cli(args, cwd=None):
    return subprocess.run(
        [sys.executable, str(LINT_PATH), *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_cli_list_prints_the_pinned_real_site():
    """Positive control against the REAL repo (no fixture): --list must name
    the one site this ticket found, so a future addition is visible in the
    same command the gate arm runs — not just in this test suite."""
    proc = _run_cli(["--list"])
    assert proc.returncode == 0
    assert (
        "worker/tests/test_sessions.py::test_session_history_ts_preserved_on_api_patch_roundtrip"
        in proc.stdout.splitlines()
    )


def test_default_mode_is_clean_against_the_real_repo():
    """The regression guard proper: this must go red the moment a cross-tree
    importorskip lands (or vanishes) without updating ALLOWLIST."""
    proc = _run_cli([])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


def test_new_uncensused_site_fails_the_default_mode(tmp_path, monkeypatch):
    _make_fixture_repo(tmp_path, worker_hit=True, api_hit=False)
    monkeypatch.setattr(census, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(census, "ALLOWLIST", set())
    rc = census.main([])
    assert rc == 1


def test_stale_allowlist_entry_fails_the_default_mode(tmp_path, monkeypatch):
    _make_fixture_repo(tmp_path, worker_hit=False, api_hit=False)
    monkeypatch.setattr(census, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(census, "ALLOWLIST", {"worker/tests/test_x.py::test_needs_app"})
    rc = census.main([])
    assert rc == 1


def test_matching_allowlist_passes(tmp_path, monkeypatch):
    _make_fixture_repo(tmp_path, worker_hit=True, api_hit=False)
    monkeypatch.setattr(census, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(census, "ALLOWLIST", {"worker/tests/test_x.py::test_needs_app"})
    rc = census.main([])
    assert rc == 0
