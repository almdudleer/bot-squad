#!/usr/bin/env python3
"""T-0716: role-contract pointers must name the git SSOT, not the live copy.

Why this exists
---------------
D-0043 (from T-0198) made ``api/app/resources/roles/<role>.md`` the SINGLE
source of truth for role contracts: the spawn assembler, ``bsq brief`` and the
SessionStart pointer all read it (``bsq._roles_ssot_dir``). The per-project
``data/<slug>/vision/roles/`` copy is seeded ONCE at scaffold
(``project_scaffold._seed_vision_roles``), is Vision-tab / onboarding DISPLAY
only, and then drifts.

``test_bsq_role_ssot.py`` pins the *read path*. Nothing pinned the **prose**:
~9 places still told a reader to go open ``vision/roles/<role>.md``. Measured
2026-07-26, the live ``vision/roles/operator.md`` was a month stale and still
carried a section the SSOT had dropped — a pointer that resolves to real but
silently wrong content, which is worse than a dead link and is exactly the
failure D-0043 was opened to kill.

What it checks
--------------
Any reference of the form ``vision/roles/<role>.md`` where ``<role>`` is a
contract that EXISTS in the SSOT dir (the role list is read from the tree, so a
new role contract is covered the day it lands). Bare ``vision/roles/`` mentions
are legal on purpose — the fixed pointers say "the git SSOT, **not** the
drifting ``vision/roles/`` copy", and the scaffold/seeding code has to name the
directory it writes.

Docs that live ONLY in the live tree and have no SSOT counterpart
(``role-hierarchy.md``, ``session-lifecycle-contract.md``) are not role
contracts a session is spawned with, so they are out of scope by construction.

``ALLOWLIST`` carries the few sites whose SUBJECT is the seeded copy itself
(the seeder, its tests, the install-shape marker) — each with a reason.

Pure stdlib, no data tree: it reads repo sources, so it runs in CI and
pre-commit with no deps.

Usage:
    scripts/lint/role_doc_pointers.py [--root REPO_ROOT]

Exit 0 when clean, 1 with one ``path:line  <match>`` per offender on stderr.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SSOT_DIR = "api/app/resources/roles"

# Paths (repo-relative) whose subject IS the seeded live copy, not a pointer
# telling a reader where a contract lives. Keep this list short and reasoned.
ALLOWLIST: dict[str, str] = {
    "api/app/project_scaffold.py": "the seeder itself — it writes vision/roles/",
    "api/app/routes_projects.py": "install-shape marker: gates the operator brief on the seeded file existing",
    "api/tests/test_project_scaffold.py": "tests the seeder",
    "api/tests/test_routes_projects.py": "tests the install-shape marker branch",
    "api/tests/test_project_write_authz.py": "authz matrix for the Vision-tab write path",
    "scripts/cli/test_bsq_role_ssot.py": "plants a deliberately-stale live copy as the negative fixture",
    "scripts/lint/role_doc_pointers.py": "this lint documents the pattern it forbids",
    "api/tests/test_role_doc_pointers_lint.py": "this lint's own test fixtures",
}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".playwright-mcp", "data", "_t",
}

SCAN_SUFFIXES = {
    ".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".sh", ".txt", ".sql",
}

# Extensionless scripts that carry prose worth policing.
EXTRA_FILES = ("scripts/cli/bsq", "scripts/cli/bot-squad")


def _repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / SSOT_DIR).is_dir():
            return ancestor
    return here.parents[2]


def ssot_roles(root: Path) -> set[str]:
    """Role stems that HAVE a git SSOT contract — the ones a pointer must name."""
    d = root / SSOT_DIR
    return {p.stem for p in d.glob("*.md")} if d.is_dir() else set()


def _pattern(roles: set[str]) -> re.Pattern[str] | None:
    if not roles:
        return None
    alt = "|".join(re.escape(r) for r in sorted(roles))
    return re.compile(rf"vision/roles/({alt})\.md")


def _candidates(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if path.suffix.lower() in SCAN_SUFFIXES:
            yield path
    for extra in EXTRA_FILES:
        p = root / extra
        if p.is_file():
            yield p


def scan(root: Path) -> list[tuple[str, int, str]]:
    """Return [(rel_path, lineno, matched_text)] for every offending pointer."""
    pat = _pattern(ssot_roles(root))
    if pat is None:
        return []
    findings: list[tuple[str, int, str]] = []
    for path in _candidates(root):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "vision/roles/" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in pat.finditer(line):
                findings.append((rel, lineno, m.group(0)))
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=None, help="repo root (default: auto-detect)")
    args = ap.parse_args(argv)

    root = _repo_root(args.root)
    if not (root / SSOT_DIR).is_dir():
        # Not a bot-squad checkout (CI smoke-runs the script against a bogus
        # root): nothing to police, succeed quietly like the backlog lints.
        return 0

    findings = scan(root)
    if not findings:
        return 0
    print(
        f"role-contract pointers must name the git SSOT ({SSOT_DIR}/<role>.md), "
        "not the drifting per-project vision/roles/ copy (D-0043):",
        file=sys.stderr,
    )
    for rel, lineno, match in findings:
        print(f"  {rel}:{lineno}  {match}", file=sys.stderr)
    print(
        "\nFix the pointer, or — if the line's subject really IS the seeded "
        "display copy — add the file to ALLOWLIST in "
        "scripts/lint/role_doc_pointers.py with a reason.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
