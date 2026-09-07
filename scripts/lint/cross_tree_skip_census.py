#!/usr/bin/env python3
"""T-1036: census of cross-tree ``pytest.importorskip`` sites.

``worker/tests`` and ``api/tests`` are collected by SEPARATE gate arms — the
worker arm runs in ``worker/.venv`` (no ``app``), the api arm runs
``cd api && pytest`` (never collects ``worker/tests``). A test in one tree
that needs a module from the OTHER tree therefore guards itself with
``pytest.importorskip("<other tree's package>")`` so it can still be
COLLECTED everywhere — and that import, in both arms as they exist today,
always fails, so the test always SKIPS. T-1036 found exactly this:
``worker/tests/test_sessions.py::test_session_history_ts_preserved_on_api_patch_roundtrip``
pins ``app.markdown_writer.merge_task_update`` and skipped in every arm,
reading as a harmless ``+1 skipped`` while pinning nothing — release 3's
``8980a23`` changed that exact function with the contract covered by no arm.

T-0789 fixed THAT ONE site: ``BOT_SQUAD_REQUIRE_CROSS_TREE=1`` turns its
``importorskip`` into a hard failure, and it's wired into ``lint.yml`` and (via
AGENT_INSTRUCTIONS.md) the manual release-gate cross-tree arm. Both of those
consumers need the CURRENT list of cross-tree sites, not a hand-typed nodeid
that quietly goes stale the next time someone adds a second one — so this
module is the SSOT both read: ``--list`` prints today's nodeids (one per gate
run), and the bare/default mode fails if the census drifts from the pinned
ALLOWLIST below (a site appeared or vanished uncensused).

WHY ``ast.walk``, NOT ``tree.body`` (contrast with duplicate_def_census.py)
----------------------------------------------------------------------------
``duplicate_def_census.py`` walks only ``tree.body`` because a top-level-def
census must NOT count a ``def`` nested inside another ``def``. This census
wants the opposite: an ``importorskip`` call is just as real a skip guard
nested inside a test function (where T-1036's actual site lives — it is
inside the test body, not at module level) as one sitting bare at module
scope. Missing the nested ones would silently under-count exactly the shape
this ticket exists to catch, so this module walks the whole tree and tracks
the enclosing function (if any) to build the nodeid.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# tree -> (glob root, package prefixes that mean "this call reaches into the
# OTHER tree")
TREES = {
    "worker/tests": ["app"],
    "api/tests": ["bot_squad_worker"],
}

# Pinned SSOT — every cross-tree importorskip site known to exist. Adding a
# new one (in either tree) means: (1) add its nodeid here, (2) make sure it
# honors BOT_SQUAD_REQUIRE_CROSS_TREE the same way test_sessions.py:4397 does
# (a LOUD failure, not a skip, when the flag is set), (3) it now shows up in
# `--list` and gets run by lint.yml's cross-tree step and the release gate's
# cross-tree arm automatically — nothing else to wire by hand.
ALLOWLIST = {
    "worker/tests/test_sessions.py::test_session_history_ts_preserved_on_api_patch_roundtrip",
}


class _Visitor(ast.NodeVisitor):
    def __init__(self, prefixes: list[str]) -> None:
        self.prefixes = prefixes
        self._func_stack: list[str] = []
        self.hits: list[tuple[int, str, str | None]] = []

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_importorskip = (
            (isinstance(func, ast.Attribute) and func.attr == "importorskip")
            or (isinstance(func, ast.Name) and func.id == "importorskip")
        )
        if (
            is_importorskip
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            arg = node.args[0].value
            if any(arg == p or arg.startswith(p + ".") for p in self.prefixes):
                qualname = self._func_stack[-1] if self._func_stack else None
                self.hits.append((node.lineno, arg, qualname))
        self.generic_visit(node)


def census_file(path: Path, prefixes: list[str]) -> list[tuple[int, str, str | None]]:
    """Cross-tree ``importorskip`` call sites in one file: ``(lineno, arg, enclosing test or None)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    v = _Visitor(prefixes)
    v.visit(tree)
    return v.hits


def census_tree(tree_root: str, prefixes: list[str]) -> dict[str, list[tuple[int, str, str | None]]]:
    """Cross-tree sites under ``tree_root`` (relative to the repo root), by relative file path."""
    out: dict[str, list[tuple[int, str, str | None]]] = {}
    root = REPO_ROOT / tree_root
    for f in sorted(root.glob("*.py")):
        hits = census_file(f, prefixes)
        if hits:
            out[f"{tree_root}/{f.name}"] = hits
    return out


def all_nodeids() -> set[str]:
    """Every cross-tree site across both trees, as pytest nodeids.

    A module-level site (no enclosing test function) is named by its file
    alone — pytest can still select it, and censusing it as a bare file makes
    clear it skips the WHOLE module, not one test.
    """
    ids: set[str] = set()
    for tree_root, prefixes in TREES.items():
        for rel_path, hits in census_tree(tree_root, prefixes).items():
            for _lineno, _arg, qualname in hits:
                ids.add(f"{rel_path}::{qualname}" if qualname else rel_path)
    return ids


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cross_tree_skip_census.py",
        description=(
            "Census pytest.importorskip sites that reach across worker/tests "
            "<-> api/tests, and refuse if the census drifts from the pinned "
            "ALLOWLIST."
        ),
    )
    p.add_argument(
        "--list", action="store_true",
        help="print today's cross-tree nodeids, one per line, and exit 0 "
             "(no allowlist check) — this is what the cross-tree gate arm runs",
    )
    args = p.parse_args(argv)

    found = all_nodeids()

    if args.list:
        for nodeid in sorted(found):
            print(nodeid)
        return 0

    missing = ALLOWLIST - found  # pinned but no longer found: a stale entry
    new = found - ALLOWLIST      # found but not pinned: uncensused site

    if not missing and not new:
        print(f"cross_tree_skip_census: OK — {len(found)} site(s), matches ALLOWLIST")
        return 0

    if missing:
        print("cross_tree_skip_census: ALLOWLIST entries no longer found (stale — "
              "update ALLOWLIST if the site was legitimately removed):")
        for nodeid in sorted(missing):
            print(f"  - {nodeid}")
    if new:
        print("cross_tree_skip_census: NEW cross-tree importorskip site(s) not in "
              "ALLOWLIST — add to ALLOWLIST in scripts/lint/cross_tree_skip_census.py "
              "so the cross-tree gate arm picks it up (T-1036):")
        for nodeid in sorted(new):
            print(f"  + {nodeid}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
