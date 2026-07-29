"""T-0789: tests for scripts/lint/undefined_names.py.

The gate reports an ABSENCE (no undefined names in the test trees), and a broken
static checker returns a well-formed empty result rather than an error — so a
green from it means nothing until something proves it detects the presence.
These are that positive control: the same two shapes T-0789 found in the wild,
written as fixtures, plus the two report-nothing shapes that must stay quiet
(unused imports and unused locals, 71 of which exist at 28abd2a and are NOT this
gate's business), plus one scan of the REAL trees so a re-introduced undefined
name breaks CI.

Same shape as the backlog / role-pointer lint tests: locate the script, load it,
drive it over synthetic fixture trees.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _find_lint_script() -> Path | None:
    """Locate the lint script — robust to layout (repo root, sibling mount, env)."""
    env_path = os.environ.get("UNDEFINED_NAMES_LINT_SCRIPT")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        for candidate in (ancestor / "scripts" / "lint" / "undefined_names.py",
                          ancestor.parent / "scripts" / "lint" / "undefined_names.py"):
            if candidate.exists():
                return candidate
    return None


LINT_PATH = _find_lint_script()

if LINT_PATH is None:
    # The api-only docker mount (`-v $PWD/api:/app`) has no scripts/ — skip
    # rather than raise, so one unmounted script can't abort collection for the
    # whole suite. CI and the repo-root mount both see it. Override with
    # UNDEFINED_NAMES_LINT_SCRIPT.
    pytest.skip(
        "scripts/lint/undefined_names.py not reachable from this mount",
        allow_module_level=True,
    )

pytest.importorskip(
    "pyflakes",
    reason="pyflakes is in api's [dev] extra; an older container image may predate it",
)


def _load():
    spec = importlib.util.spec_from_file_location("undefined_names_lint", LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LINT = _load()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# It FINDS the presence — the two real T-0789 shapes
# ---------------------------------------------------------------------------

def test_finds_orphaned_debris_behind_a_skip(tmp_path):
    """Shape 1 (test_sessions.py:4167): copy-paste debris naming three symbols
    that exist in another test, in a body that never executes."""
    _write(tmp_path, "test_a.py", """
import pytest


def test_roundtrip(tmp_path):
    pytest.importorskip("app.markdown_writer")
    assert True

    bind_task(cfg, "test-project", "S-alice-w-p2", "T-0096")
    assert read_history(extra_md) == ["S-alice-w-p2"]
""")
    findings = LINT.check([tmp_path])
    assert len(findings) == 4, findings
    joined = "\n".join(findings)
    for name in ("bind_task", "cfg", "read_history", "extra_md"):
        assert name in joined


def test_finds_class_referenced_out_of_its_defining_test(tmp_path):
    """Shape 2 (test_actions.py:4430): a helper class defined LOCALLY inside one
    test and referenced from another — the reference that produced a NameError
    the code under test then swallowed as the failure being simulated."""
    _write(tmp_path, "test_b.py", """
def test_failover(monkeypatch):
    class _BoomTg:
        def send(self, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr("mod._get_tg_client", lambda _c: _BoomTg())


def test_link_present_too(monkeypatch):
    monkeypatch.setattr("mod._get_tg_client", lambda _c: _BoomTg())
""")
    findings = LINT.check([tmp_path])
    assert len(findings) == 1, findings
    assert "_BoomTg" in findings[0]
    assert ":11:" in findings[0]  # the referencing test, not the defining one


def test_reports_a_file_that_does_not_parse(tmp_path):
    """A test file that cannot be parsed is not silently 'clean'."""
    _write(tmp_path, "test_broken.py", "def test_x(:\n    pass\n")
    findings = LINT.check([tmp_path])
    assert len(findings) == 1 and "does not parse" in findings[0]


# ---------------------------------------------------------------------------
# It stays QUIET on everything else — the 71 pre-existing findings this gate
# deliberately does not own
# ---------------------------------------------------------------------------

def test_ignores_unused_imports_and_locals_and_fstrings(tmp_path):
    _write(tmp_path, "test_noise.py", """
import json
import pytest
from pathlib import Path


def test_thing():
    out = compute()
    unused_local = 1
    msg = f"no placeholders here"
    return None


def compute():
    return 1
""")
    assert LINT.check([tmp_path]) == []


def test_clean_file_is_clean(tmp_path):
    _write(tmp_path, "test_ok.py", """
import os


def helper():
    return os.sep


def test_uses_helper():
    assert helper()
""")
    assert LINT.check([tmp_path]) == []


def test_names_from_star_import_are_not_flagged(tmp_path):
    """A wildcard import makes names unresolvable; pyflakes must not turn that
    into a wall of false positives (there are star-imports in scripts/)."""
    _write(tmp_path, "test_star.py", """
from os.path import *


def test_join():
    assert join("a", "b")
""")
    assert LINT.check([tmp_path]) == []


# ---------------------------------------------------------------------------
# The real trees
# ---------------------------------------------------------------------------

def test_repo_test_trees_have_no_undefined_names():
    """The gate itself, over worker/tests + api/tests + scripts. Fails CI the
    moment a reference like the two above lands again."""
    roots = [LINT.REPO_ROOT / r for r in LINT.DEFAULT_ROOTS]
    present = [r for r in roots if r.exists()]
    if not present:
        pytest.skip(f"none of {LINT.DEFAULT_ROOTS} present under {LINT.REPO_ROOT}")
    findings = LINT.check(present)
    assert findings == [], "undefined names:\n  " + "\n  ".join(findings)


def test_default_roots_are_all_present_in_a_full_checkout():
    """Guards the gate's own SCOPE: if worker/tests silently stops being a
    default root (a rename, a moved script), the gate would still print
    'clean' over a narrower tree. Skips on the api-only mount, which has one."""
    roots = {r: (LINT.REPO_ROOT / r).exists() for r in LINT.DEFAULT_ROOTS}
    if not all(roots.values()) and any(roots.values()):
        pytest.skip(f"partial checkout/mount: {roots}")
    assert all(roots.values()), roots
