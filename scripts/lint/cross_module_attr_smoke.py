#!/usr/bin/env python3
"""T-0186: cross-module attribute smoke — catch wide-add absorption SPLITS.

Why this exists
---------------
The shared dev clone is edited by many concurrent bot-squad sessions. A peer's
raw ``git add -A`` / ``git add <dir>`` can sweep ANOTHER session's unstaged,
in-flight working-tree edits into the peer's commit (the T-0068 / T-0145 /
T-0093 incident class). The danger is not the absorption itself but a *split*
absorption: a multi-file change where only SOME files get swept.

The live break (2026-06-02, T-0171/T-0174): commit ``fb01921`` absorbed a
session's in-flight ``actions.py`` (which referenced the new ``Config`` field
``tg_default_chat_id``) but did NOT absorb the ``config.py`` that DEFINED that
field. Origin then shipped ``actions.py`` calling ``cfg.tg_default_chat_id`` on
a ``Config`` that lacked it → ``AttributeError`` at runtime on the tg_notify
fallback path. A live runtime break, not just a merge mess.

A plain ``import actions`` does NOT catch this: the bad attribute access lives
INSIDE a function body, so it only blows up when that path runs. The existing
guards (safe-commit refusing ``-a/--all``; the pre-commit peer hook) protect
the *committer*, never the *victim* whose edits get swept. This smoke closes
the gap on the OUTPUT side: it statically proves that every ``cfg.<attr>``
access in a consumer module resolves to a real member of the provider class.
A split that ships ``actions.py`` ahead of its ``config.py`` field is caught
here BEFORE deploy.

What it checks
--------------
For each declared ``Pair`` (consumer module, provider module, provider class):
collect every attribute access ``<var>.<attr>`` in the consumer where ``<var>``
is bound to the provider class (an explicit seed set plus auto-detected
``X = _get_config()`` / ``X = Config.load(...)`` / ``X = Config(...)``
assignments), and assert each ``<attr>`` is a member of the provider class
(dataclass field, property, method, or classmethod). Any access that does not
resolve is an absorption-split fingerprint.

Pure stdlib (``ast`` only) so it runs in CI and a pre-push hook with NO deps
and NO data tree — it reads source files in the repo, nothing else.

Usage:
    scripts/lint/cross_module_attr_smoke.py [--root REPO_ROOT]

Exits 0 when every pair resolves cleanly, 1 if any access is unresolved
(printing one ``consumer:line  var.attr  not a member of <Class>`` per offender
to stderr).
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Pair:
    name: str
    consumer: str          # repo-relative path to the module doing the access
    provider: str          # repo-relative path to the module defining the class
    provider_class: str    # the class whose members the accesses must resolve to
    # Variable names in the consumer that are KNOWN to hold a provider instance.
    # Auto-detection (see _config_bound_vars) extends this with locals assigned
    # from a recognised factory call, but the seed covers globals / params.
    seed_vars: tuple[str, ...] = field(default_factory=tuple)
    # Factory call shapes (consumer-local) that yield a provider instance, so an
    # assignment target becomes attribute-checked. Bare-name calls (``Config()``,
    # ``_get_config()``) match by func id; ``Config.load(...)`` matches by the
    # ``<provider_class>.load`` attribute form.
    factory_names: tuple[str, ...] = ("_get_config",)


# The tightly-coupled module pairs guarded on every push. Adding a pair is one
# entry here — the worker actions<->config seam is the one that broke (T-0186);
# extend as other split-prone couplings surface.
PAIRS: tuple[Pair, ...] = (
    Pair(
        name="worker actions <-> config",
        consumer="worker/bot_squad_worker/actions.py",
        provider="worker/bot_squad_worker/config.py",
        provider_class="Config",
        seed_vars=("cfg", "new_cfg"),
        factory_names=("_get_config",),
    ),
)


def provider_class_members(provider_src: str, class_name: str) -> set[str]:
    """Public-ish member names defined on ``class_name`` in ``provider_src``.

    Captures dataclass fields (``name: type`` / ``name: type = default``), plain
    class-level assignments, and methods / properties / classmethods (the
    ``def`` name is the attribute name regardless of decorator). Dunders are
    irrelevant to ``cfg.<attr>`` access and are kept harmlessly.
    """
    tree = ast.parse(provider_src)
    members: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    members.add(stmt.target.id)
                elif isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            members.add(tgt.id)
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.add(stmt.name)
    return members


def _is_factory_call(call: ast.Call, class_name: str, factory_names: tuple[str, ...]) -> bool:
    fn = call.func
    if isinstance(fn, ast.Name) and (fn.id in factory_names or fn.id == class_name):
        return True  # _get_config()  /  Config(...)
    if (
        isinstance(fn, ast.Attribute)
        and fn.attr in ("load", *factory_names)
        and isinstance(fn.value, ast.Name)
        and fn.value.id == class_name
    ):
        return True  # Config.load(...)
    return False


def _config_bound_vars(consumer_tree: ast.AST, pair: Pair) -> set[str]:
    """Seed vars plus locals assigned from a provider factory call."""
    bound = set(pair.seed_vars)
    for node in ast.walk(consumer_tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _is_factory_call(node.value, pair.provider_class, pair.factory_names):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        bound.add(tgt.id)
    return bound


def audit(consumer_src: str, provider_src: str, pair: Pair) -> list[tuple[int, str, str]]:
    """Return [(lineno, var, attr), ...] for accesses that don't resolve.

    Operates on source STRINGS (not paths) so unit tests can feed synthetic
    coherent/split modules without touching disk.
    """
    members = provider_class_members(provider_src, pair.provider_class)
    ctree = ast.parse(consumer_src)
    cvars = _config_bound_vars(ctree, pair)
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(ctree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in cvars
        ):
            attr = node.attr
            if attr.startswith("__"):  # dunder access is never an absorption split
                continue
            if attr not in members:
                offenders.append((node.lineno, node.value.id, attr))
    return offenders


def check_pair(root: Path, pair: Pair) -> list[str]:
    """Run one pair against on-disk sources; return human-readable offender lines."""
    consumer_path = root / pair.consumer
    provider_path = root / pair.provider
    if not consumer_path.is_file():
        return [f"{pair.consumer}: consumer module not found (pair {pair.name!r})"]
    if not provider_path.is_file():
        return [f"{pair.provider}: provider module not found (pair {pair.name!r})"]
    offenders = audit(
        consumer_path.read_text(encoding="utf-8"),
        provider_path.read_text(encoding="utf-8"),
        pair,
    )
    return [
        f"{pair.consumer}:{lineno}  {var}.{attr}  not a member of "
        f"{pair.provider_class} ({pair.provider}) — cross-file absorption split?"
        for lineno, var, attr in offenders
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repo root containing the module pairs (default: inferred from this script)",
    )
    args = parser.parse_args(argv)

    all_offenders: list[str] = []
    for pair in PAIRS:
        all_offenders.extend(check_pair(args.root, pair))

    if all_offenders:
        print(
            "cross-module attribute smoke FAILED — a consumer references a "
            "provider member that does not exist. This is the fingerprint of a "
            "wide-add absorption split (T-0186): a multi-file change where one "
            "file shipped without its dependency.",
            file=sys.stderr,
        )
        for line in all_offenders:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
