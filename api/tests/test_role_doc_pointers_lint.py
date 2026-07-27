"""T-0716: tests for scripts/lint/role_doc_pointers.py (D-0043 pointer guard).

The lint fails any prose that sends a reader to ``vision/roles/<role>.md`` when
that role has a git SSOT contract under ``api/app/resources/roles/``. Same
shape as the backlog lints: locate the script, load it, drive it over synthetic
fixture trees — plus one scan of the REAL repo so a re-introduced pointer
breaks CI.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _find_lint_script() -> Path | None:
    """Locate the lint script — robust to layout (repo root, sibling mount, env)."""
    env_path = os.environ.get("ROLE_DOC_POINTERS_LINT_SCRIPT")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "scripts" / "lint" / "role_doc_pointers.py"
        if candidate.exists():
            return candidate
        candidate2 = ancestor.parent / "scripts" / "lint" / "role_doc_pointers.py"
        if candidate2.exists():
            return candidate2
    return None


LINT_PATH = _find_lint_script()

if LINT_PATH is None:
    # The api-only docker mount (`-v $PWD/api:/app`) has no scripts/ — skip
    # rather than raise, so one unmounted script can't abort collection for the
    # whole suite. CI and the repo-root mount both see it. Override with
    # ROLE_DOC_POINTERS_LINT_SCRIPT.
    pytest.skip(
        "scripts/lint/role_doc_pointers.py not reachable from this mount",
        allow_module_level=True,
    )


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("role_doc_pointers_lint", LINT_PATH)
    assert spec and spec.loader, f"could not load {LINT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lint = _load_lint_module()


def _fake_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A minimal tree with an SSOT roles dir plus the given repo-relative files."""
    ssot = tmp_path / "api" / "app" / "resources" / "roles"
    ssot.mkdir(parents=True, exist_ok=True)
    for role in ("operator", "teamlead", "dev"):
        (ssot / f"{role}.md").write_text(f"# Role: {role}\n", encoding="utf-8")
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def test_ssot_roles_read_from_the_tree(tmp_path):
    """The policed role list comes from the SSOT dir — a new contract is
    covered the day it lands, with no lint edit."""
    root = _fake_repo(tmp_path, {})
    (root / "api" / "app" / "resources" / "roles" / "qa.md").write_text("x\n")
    assert lint.ssot_roles(root) == {"operator", "teamlead", "dev", "qa"}


def test_flags_a_pointer_at_the_drifting_live_copy(tmp_path):
    root = _fake_repo(
        tmp_path,
        {"framework-skills/some-skill/SKILL.md": "Full contract: `vision/roles/operator.md`.\n"},
    )
    findings = lint.scan(root)
    assert findings == [
        ("framework-skills/some-skill/SKILL.md", 1, "vision/roles/operator.md")
    ]
    assert lint.main(["--root", str(root)]) == 1


def test_accepts_the_ssot_path(tmp_path):
    root = _fake_repo(
        tmp_path,
        {
            "framework-skills/some-skill/SKILL.md":
                "Full contract: `api/app/resources/roles/operator.md`.\n"
        },
    )
    assert lint.scan(root) == []
    assert lint.main(["--root", str(root)]) == 0


def test_bare_directory_mention_is_legal(tmp_path):
    """The fixed pointers say 'the git SSOT, NOT the drifting vision/roles/
    copy' — naming the directory to warn about it must not self-trip."""
    root = _fake_repo(
        tmp_path,
        {
            "framework-skills/some-skill/SKILL.md":
                "Read `api/app/resources/roles/dev.md` — the git SSOT, not the "
                "drifting `vision/roles/` copy.\n"
        },
    )
    assert lint.scan(root) == []


def test_doc_without_an_ssot_counterpart_is_out_of_scope(tmp_path):
    """role-hierarchy.md / session-lifecycle-contract.md live only in the live
    tree and are not spawnable contracts — pointing at them is fine."""
    root = _fake_repo(
        tmp_path,
        {
            "framework-skills/some-skill/SKILL.md":
                "Hierarchy: `vision/roles/role-hierarchy.md`; lifecycle: "
                "`vision/roles/session-lifecycle-contract.md`.\n"
        },
    )
    assert lint.scan(root) == []


def test_allowlisted_file_is_exempt(tmp_path, monkeypatch):
    root = _fake_repo(
        tmp_path,
        {"api/app/project_scaffold.py": '# writes vision/roles/operator.md\n'},
    )
    assert lint.scan(root) == []
    # …and the exemption is per-path, not global.
    monkeypatch.setitem(lint.ALLOWLIST, "api/app/project_scaffold.py", "reason")
    (root / "worker" / "x.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "worker" / "x.py").write_text("# see vision/roles/dev.md\n", encoding="utf-8")
    assert [f[0] for f in lint.scan(root)] == ["worker/x.py"]


def test_scan_skips_vendor_and_data_trees(tmp_path):
    root = _fake_repo(
        tmp_path,
        {
            "node_modules/pkg/readme.md": "vision/roles/dev.md\n",
            "data/bot-squad/vision/roles/dev.md": "the live copy itself\n",
        },
    )
    assert lint.scan(root) == []


def test_non_repo_root_exits_clean():
    """CI smoke-runs the script against a bogus root (no data tree on runners)."""
    assert lint.main(["--root", "/nonexistent"]) == 0


def _real_repo_root() -> Path | None:
    for ancestor in LINT_PATH.resolve().parents:
        if (ancestor / "api" / "app" / "resources" / "roles").is_dir():
            return ancestor
    return None


def test_the_real_repo_has_no_live_copy_pointers():
    """The regression guard proper: T-0716 swept ~9 of these; re-introducing
    one must break the build."""
    root = _real_repo_root()
    if root is None:
        pytest.skip("bot-squad checkout not available in this test environment")
    findings = lint.scan(root)
    assert findings == [], (
        "role-contract pointers must name api/app/resources/roles/<role>.md "
        f"(D-0043), found: {findings}"
    )
