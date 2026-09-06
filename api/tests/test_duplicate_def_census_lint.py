"""T-1017: tests for scripts/lint/duplicate_def_census.py.

Same shape as the sibling backlog lints (test_role_doc_pointers_lint.py etc.):
locate the script, load it, drive it over synthetic fixtures — plus one scan of
the REAL repo so a re-introduced duplicate breaks the build. The two cases the
operator's T-1017 broadcast called out as the ones a naive census gets wrong
are pinned explicitly: a NESTED def must not be counted as top-level, and a
comment/docstring quoting a defect must not trip the detector.
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
    env_path = os.environ.get("DUPLICATE_DEF_CENSUS_SCRIPT")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "scripts" / "lint" / "duplicate_def_census.py"
        if candidate.exists():
            return candidate
        candidate2 = ancestor.parent / "scripts" / "lint" / "duplicate_def_census.py"
        if candidate2.exists():
            return candidate2
    return None


LINT_PATH = _find_lint_script()

if LINT_PATH is None:
    # Same posture as the sibling lints: the api-only docker mount has no
    # scripts/, so skip collection rather than aborting the whole suite.
    pytest.skip(
        "scripts/lint/duplicate_def_census.py not reachable from this mount",
        allow_module_level=True,
    )


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("duplicate_def_census_lint", LINT_PATH)
    assert spec and spec.loader, f"could not load {LINT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _load_lint_module()


# ---------------------------------------------------------------------------
# The core traversal: tree.body only, never ast.walk.
# ---------------------------------------------------------------------------

def test_module_level_def_is_counted():
    c = census.census_source("def foo():\n    pass\n")
    assert c.defs == {"foo": [1]}
    assert c.total_defs() == 1


def test_nested_def_is_not_counted_as_top_level():
    """The exact traversal bug named in T-1017 Context (p651): ast.walk would
    descend into `outer`'s body and hand back the nested `foo` as if it were
    top-level. It must not be counted at all here — not once, not twice."""
    c = census.census_source(
        "def outer():\n"
        "    def foo():\n"
        "        pass\n"
        "    return foo\n"
    )
    assert c.defs == {"outer": [1]}
    assert "foo" not in c.defs


def test_a_nested_def_sharing_a_name_with_a_top_level_one_is_not_a_duplicate():
    """Positive control, NESTED direction (operator's ticket): a `def` inside
    a function body that happens to share a name with a real top-level def
    must not be reported as a duplicate — that is the false positive the
    traversal bug produces."""
    c = census.census_source(
        "def foo():\n"
        "    pass\n"
        "\n"
        "def outer():\n"
        "    def foo():\n"
        "        pass\n"
        "    return foo\n"
    )
    assert c.duplicate_defs() == {}
    assert c.defs["foo"] == [1]


def test_class_body_def_is_not_counted_as_top_level():
    c = census.census_source(
        "class Foo:\n"
        "    def bar(self):\n"
        "        pass\n"
    )
    assert c.defs == {}
    assert c.classes == {"Foo": [1]}


# ---------------------------------------------------------------------------
# Positive control, MODULE-LEVEL direction: a real duplicate IS caught, and
# NAMED — not merely a nonzero exit code.
# ---------------------------------------------------------------------------

def test_duplicate_top_level_def_is_named():
    c = census.census_source("def foo():\n    pass\n\ndef foo():\n    pass\n")
    assert c.duplicate_defs() == {"foo": [1, 4]}
    lines = census.format_duplicates(c)
    assert any("foo" in line and "def" in line for line in lines)


def test_duplicate_top_level_class_is_named():
    c = census.census_source("class Foo:\n    pass\n\nclass Foo:\n    pass\n")
    assert c.duplicate_classes() == {"Foo": [1, 4]}
    lines = census.format_duplicates(c)
    assert any("Foo" in line and "class" in line for line in lines)


def test_check_returns_1_and_prints_the_identifier(capsys):
    rc = census.check("dupes.py", "def foo():\n    pass\n\ndef foo():\n    pass\n")
    assert rc == 1
    out = capsys.readouterr().out
    assert "foo" in out


def test_check_clean_file_returns_0_and_is_quiet_by_default(capsys):
    rc = census.check("clean.py", "def a():\n    pass\n\ndef b():\n    pass\n")
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_check_verbose_prints_summary_even_when_clean(capsys):
    rc = census.check("clean.py", "def a():\n    pass\n", verbose=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 top-level def" in out


def test_unparseable_source_returns_2_not_1():
    """A syntax error is a DIFFERENT failure than a duplicate — the census
    cannot certify uniqueness in a file it cannot read as Python at all."""
    rc = census.check("broken.py", "def foo(:\n    pass\n")
    assert rc == 2


# ---------------------------------------------------------------------------
# The trap named explicitly in T-1017's operator broadcast: a comment or
# docstring quoting a defect must not trip the detector, because `ast` reads
# syntax, not text.
# ---------------------------------------------------------------------------

def test_docstring_quoting_a_duplicate_does_not_trip_the_detector():
    source = (
        '"""\n'
        "Example of what NOT to write:\n"
        "def evil():\n"
        "    pass\n"
        "def evil():\n"
        "    pass\n"
        '"""\n'
        "def evil():\n"
        "    pass\n"
    )
    c = census.census_source(source)
    assert c.duplicate_defs() == {}
    assert c.defs == {"evil": [8]}


def test_comment_quoting_a_duplicate_does_not_trip_the_detector():
    source = (
        "# a build log once matched this exact text twice:\n"
        "# def evil():\n"
        "#     pass\n"
        "# def evil():\n"
        "#     pass\n"
        "def evil():\n"
        "    pass\n"
    )
    c = census.census_source(source)
    assert c.duplicate_defs() == {}
    assert c.defs == {"evil": [6]}


# ---------------------------------------------------------------------------
# CLI surface: file args, --stdin, exit codes.
# ---------------------------------------------------------------------------

def _run_cli(args, stdin_text=None):
    return subprocess.run(
        [sys.executable, str(LINT_PATH), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
    )


def test_cli_stdin_clean_exits_0():
    proc = _run_cli(["--stdin", "--name", "t.py"], stdin_text="def a():\n    pass\n")
    assert proc.returncode == 0


def test_cli_stdin_duplicate_exits_1_and_names_it():
    proc = _run_cli(
        ["--stdin", "--name", "t.py"],
        stdin_text="def a():\n    pass\n\ndef a():\n    pass\n",
    )
    assert proc.returncode == 1
    assert "a" in proc.stdout


def test_cli_file_arg(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("def a():\n    pass\n\ndef a():\n    pass\n", encoding="utf-8")
    proc = _run_cli([str(f)])
    assert proc.returncode == 1
    assert "a" in proc.stdout


def test_cli_no_args_errors():
    proc = _run_cli([])
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# Regression fixtures: the operator's measured baselines from T-1017, and the
# real repo's current state.
# ---------------------------------------------------------------------------

def _real_repo_root() -> Path | None:
    for ancestor in LINT_PATH.resolve().parents:
        if (ancestor / "scripts" / "cli" / "bsq").is_file():
            return ancestor
    return None


def _bsq_at(ref: str, root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:scripts/cli/bsq"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


@pytest.mark.parametrize(
    "ref,expected_defs,expected_classes",
    [
        ("1cfcbf3", 293, 2),
        ("d076df8", 295, 2),
    ],
)
def test_baseline_fixtures_from_the_operator_broadcast(ref, expected_defs, expected_classes):
    root = _real_repo_root()
    if root is None:
        pytest.skip("bot-squad checkout not available in this test environment")
    source = _bsq_at(ref, root)
    if source is None:
        pytest.skip(f"{ref} not reachable in this checkout's history")
    c = census.census_source(source)
    assert c.total_defs() == expected_defs
    assert len(c.defs) == expected_defs, "baseline commit must have zero duplicate defs"
    assert c.total_classes() == expected_classes
    assert len(c.classes) == expected_classes, "baseline commit must have zero duplicate classes"


def test_the_real_bsq_has_no_duplicate_top_level_defs_or_classes():
    """The regression guard proper: this must break the build the moment a
    duplicate top-level def/class lands in scripts/cli/bsq."""
    root = _real_repo_root()
    if root is None:
        pytest.skip("bot-squad checkout not available in this test environment")
    source = (root / "scripts" / "cli" / "bsq").read_text(encoding="utf-8")
    c = census.census_source(source)
    assert c.duplicate_defs() == {}, f"duplicate top-level defs in bsq: {c.duplicate_defs()}"
    assert c.duplicate_classes() == {}, f"duplicate top-level classes in bsq: {c.duplicate_classes()}"
