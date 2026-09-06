"""T-1019: warn when a run measured a state that exists in no commit.

The defect this exists for
---------------------------
A suite can report a clean, well-formed result about a state that exists in
no commit and never will. T-1002 measured this the expensive way: two runs 33
minutes apart legitimately disagreed about the same test, and NEITHER
disagreement was about the substrate — a peer had edited
``bot_squad_worker/routines.py`` from ``if "capacity reached" in str(e)`` to
``isinstance(e, SpawnBackpressure)`` and had not yet updated the test that
raises the old hand-typed string. An old stimulus met a new reader while a
container mounted the shared clone mid-edit. That produced two wrong
published conclusions in a row before a reader-at-two-shas check settled it.

T-1002's substrate guard (``substrate.py`` in this directory) asks *is my
environment complete*; it cannot reach this, and no amount of improving it
would. The question here is different: **did this run measure any COMMITTED
state at all?** The observable is PROVENANCE, not completeness.

What this does
---------------
At terminal-summary time, walk every module the run actually imported
(``sys.modules``), keep the ones whose ``__file__`` sits under the repo root,
and ask git which of those differ from HEAD (modified OR untracked — both are
"exists in no commit"). If any do, print exactly which.

Three decisions the obvious implementation gets wrong (stakeholder design,
T-1019 Context):

1. **Scoped to modules the run IMPORTED, never to ``git status``.** A dirty
   tree is this clone's NORMAL state — five lanes, always something
   modified — so a blanket dirty-tree warning fires every run and gets muted
   within a day. Scoped to imported modules it fires only when the dirt is
   UNDER THE TEST, which is rare and is the condition that matters.
2. **Differs-from-HEAD, not mtime-during-the-run.** T-1002's own case is an
   edit that landed BEFORE the run started (~5 minutes before); a
   during-the-run mtime check reads CLEAN on exactly that case. This module
   ships the differs-from-HEAD variant only.
3. **No ``.git`` reachable (a frozen ``git archive`` extract) must say CANNOT
   CHECK, never a reassuring silent clean.** A silent pass on a substrate that
   cannot even be asked the question is the same defect this ticket exists to
   remove, one instrument later.

Stated limits, not hidden: import happens at collection, so a file edited
after collection and before the assertion still reads clean here. And this
cannot name WHOSE edit it is — knowing the run measured a state that exists in
no commit is the whole finding; attribution is a separate question the peer
bus answers.

This module only REPORTS. It never touches a file, a git ref, or an import.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable, Optional

TESTS_DIR = Path(__file__).resolve().parent

#: matches ``bot_squad_worker.deploy._git_argv`` exactly (T-0880): git refuses
#: every command in a tree owned by a different linux user than the caller
#: ("detected dubious ownership") unless it is explicitly marked trusted.
#: Reusing the worker's own helper means this probe cannot drift from the one
#: the running worker itself depends on to answer the same question.
try:
    from bot_squad_worker.deploy import _git_argv
except ImportError:  # pragma: no cover - worker package unavailable
    def _git_argv(root: Path, *args: str) -> list[str]:
        return ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]


def _run_git(root: Path, *args: str) -> Optional[str]:
    """``git`` stdout for ``args`` run against ``root``; ``None`` on ANY failure
    (no git binary, no repo, timeout, non-zero exit) — every failure reads the
    same way to a caller that only wants to know "can this be answered"."""
    try:
        proc = subprocess.run(
            _git_argv(root, *args),
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """The git worktree root containing ``start`` (default: this module's own
    directory, looked up dynamically so tests can monkeypatch ``TESTS_DIR``),
    or ``None`` if none is reachable — the CANNOT-CHECK condition (a frozen
    ``git archive`` extract has no ``.git`` by construction; a ``--with-git``
    checkout does)."""
    if start is None:
        start = TESTS_DIR
    out = _run_git(start, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    try:
        return Path(out.strip()).resolve()
    except (OSError, ValueError):
        return None


def imported_repo_files(
    root: Path, modules: Optional[Iterable[ModuleType]] = None,
) -> list[Path]:
    """Repo-relative paths, sorted, of every module this run ACTUALLY IMPORTED
    from under ``root`` — never every dirty file in the tree (T-1019 decision
    1). Modules with no ``__file__`` (builtins, some namespace packages) or
    whose file sits outside ``root`` are skipped, not guessed at."""
    if modules is None:
        modules = list(sys.modules.values())
    out: set[Path] = set()
    for mod in modules:
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            p = Path(f).resolve()
            rel = p.relative_to(root)
        except (OSError, ValueError):
            continue
        out.add(rel)
    return sorted(out)


def differs_from_head(root: Path, rel_paths: list[Path]) -> list[Path]:
    """Which of ``rel_paths`` (repo-relative, already known to be imported)
    differ from HEAD right now — modified, staged, or untracked. All three
    are "exists in no commit"; a rename shows as its new path.

    Scoped to exactly ``rel_paths`` via git's own pathspec, so a peer's dirty
    file elsewhere in the tree that this run never imported cannot appear
    here (T-1019 decision 1) — this is a single git call, not a broad
    ``git status`` filtered after the fact.
    """
    if not rel_paths:
        return []
    args = ["status", "--porcelain=v1", "--untracked-files=all", "--", *[
        str(p) for p in rel_paths
    ]]
    out = _run_git(root, *args)
    if out is None:
        return []
    differing: set[Path] = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        name = line[3:]
        if " -> " in name:  # a rename entry: "old -> new"
            name = name.split(" -> ", 1)[1]
        differing.add(Path(name))
    return sorted(differing)


class Result:
    """The outcome of one :func:`check` call."""

    def __init__(self, *, cannot_check: bool, differing: list[Path]):
        self.cannot_check = cannot_check
        self.differing = differing

    @property
    def clean(self) -> bool:
        return not self.cannot_check and not self.differing


def check(modules: Optional[Iterable[ModuleType]] = None) -> Result:
    """Run the whole check once: resolve the repo root, list what THIS run
    imported from under it, and ask git which of those differ from HEAD."""
    root = repo_root()
    if root is None:
        return Result(cannot_check=True, differing=[])
    rel_paths = imported_repo_files(root, modules)
    differing = differs_from_head(root, rel_paths)
    return Result(cannot_check=False, differing=differing)


BANNER_TITLE = "THIS RUN MEASURED AN UNCOMMITTED TREE"
CANNOT_CHECK_TITLE = "PROVENANCE CANNOT BE CHECKED"

TRAILER_PREFIX = "!! UNCOMMITTED PROVENANCE"


def banner_lines(differing: list[Path]) -> list[str]:
    n = len(differing)
    lines = [
        f"{n} imported module(s) differ from HEAD — this run measured a state "
        f"that exists in no commit:",
        "",
    ]
    lines += [f"  {p}" for p in differing]
    lines += [
        "",
        "This is a REPORT, not a repair — nothing here was reverted or",
        "restaged. Import happens at collection, so an edit landing after",
        "collection and before the assertion is NOT caught by this check.",
        "This cannot say whose edit it is; that is a separate question.",
    ]
    return lines


def cannot_check_lines() -> list[str]:
    return [
        "no .git is reachable from this test tree, so its imports cannot be",
        "compared against HEAD. A frozen `git archive` extract has no .git by",
        "construction; a `--with-git` checkout does. This is NOT a clean",
        "result — it is an unanswered question.",
    ]


def trailer_line(result: Result) -> str:
    if result.cannot_check:
        return f"{TRAILER_PREFIX} — CANNOT CHECK: no .git reachable."
    names = ", ".join(str(p) for p in result.differing)
    return (
        f"{TRAILER_PREFIX} — {len(result.differing)} imported module(s) differ "
        f"from HEAD: {names}. This run's result is not a certification of HEAD."
    )
