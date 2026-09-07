"""T-0991: every test-bearing path in this repo is covered by a named
certification arm, or is DECLARED uncovered. Nothing sits outside both.

THE DEFECT CLASS THIS PINS — a whole SUITE nobody runs
------------------------------------------------------------------------
T-0764 built a two-ended forcing function (an api response-shape pin and a
web mirror of it). `fddecfe` shipped straight through BOTH ends. Neither end
was wrong; **nobody executed either surface.** T-0982 then measured the web
suite for the first time ever — 711 tests that no certification this fleet
runs had touched — and, applying the same question to the rest of the repo,
found a FOURTH surface: `scripts/cli`, the tests for `bsq` itself, the CLI
every lane drives all day. **486 tests, collected by nothing.**

T-1036 is the same shape one level down: a single cross-tree `importorskip`
site that SKIPPED in every arm, reading as a harmless `+1 skipped` while
pinning nothing.

The recurring lesson, and the reason this file exists in THIS directory:
**a forcing function nobody runs is a comment.** So the guard against
unrun surfaces has to be bootstrapped inside the one suite that every
certification already collects — `worker/tests`. Anywhere else and it is
the very thing it is trying to detect.

WHAT IT CHECKS
------------------------------------------------------------------------
  1. Every test-bearing file resolves to a declared arm in ``ARMS``, or is
     pinned in ``DECLARED_UNCOVERED``. A new test directory covered by
     neither goes RED, naming the files.
  2. ``DECLARED_UNCOVERED`` is exact in both directions — a path that gains
     coverage must leave the list, so the list cannot rot into a permanent
     amnesty (the T-1036 ``ALLOWLIST`` discipline).
  3. **Each arm actually EXISTS where the map says it does.** The map is
     otherwise prose, and prose is not a control: it would keep claiming
     `scripts/cli` is covered long after someone deleted the job. This is
     the second end of the forcing function — the map is one end, the
     workflow file is the other.
  4. The filesystem enumerator does not MISS anything the git enumerator
     finds (see below).

WHY TWO ENUMERATORS, AND WHY THE CHECK IS ONE-DIRECTIONAL
------------------------------------------------------------------------
This test must run in **every** arm, including a `git archive` extract
(`bsq verify-isolated`), which carries no `.git`. A test that shells out to
git would ERROR there — and T-0982 measured that an ERROR is not a FAILURE,
so a runner keying on "0 failed" would call that green. That is the exact
blind spot this file exists to close, so it must not reproduce it.

So: use `git ls-files` when a repo is available (it excludes a lane's
untracked scratch, which is not a committed surface and must not trip the
gate), and fall back to a pruned filesystem walk otherwise. In an extract
the fallback is EXACT by construction — an extract contains the committed
paths and nothing else.

Check 4 asserts ``tracked ⊆ walked``, not equality, **on purpose**. The
direction that can hurt is the walk MISSING a file: that would let an
uncovered surface pass the gate silently. Extra untracked matches in a
shared clone are a working-tree artifact, and asserting equality would turn
any lane's scratch `test_foo.py` into a red for the whole fleet — a gate
that fires on a healthy tree gets disabled, and then covers nothing.

WHAT IT DELIBERATELY DOES NOT DO
------------------------------------------------------------------------
It pins **coverage, never counts.** `scripts/cli` was 486 tests when T-0982
found it and 596 when T-0991 measured it — and it grew by one more FILE
during the session that wrote this module, from a peer's commit. A pinned
count is stale within the hour; "is this path run by anything" is not.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# test_<name>.py / test_<name>.sh (pytest + the shell gates) and the vitest
# <name>.test.ts(x) convention. Measured at b40b646: 316 + 58 = 374 files,
# and `*_test.py` matches nothing in this repo.
TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+\.(?:py|sh)|[^/]+\.test\.(?:tsx?))$")

# Directories a filesystem walk must never descend into. Only the fallback
# enumerator uses this; `git ls-files` needs none of it.
PRUNE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".versions", ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov",
    ".playwright-mcp", "coverage", ".next", ".cache",
}

# path prefix -> the arm that RUNS it. Every entry must be provable by
# test_every_declared_arm_exists_where_the_map_says_it_does below.
ARMS = {
    "worker/tests/": "worker",
    "api/tests/": "api",
    "web/src/": "web",
    "scripts/cli/": "scripts-cli",   # T-0991 wired this one
}

# The `jobs:` key each arm must occupy in .github/workflows/nightly-suites.yml,
# plus a fragment its `run:` block must contain. The fragment is what stops a
# job from being renamed into existence while running something else entirely.
ARM_NIGHTLY_JOBS = {
    "worker": "worker/tests",
    "api": "pytest",
    "web": "npm test",
    "scripts-cli": "scripts/cli",
}

# Test-bearing paths that NO arm runs, each with the reason. A known and
# declared hole is a different object from one nobody has enumerated: this
# list is what makes the difference auditable.
#
# T-0991 measured these 7 and left them uncovered ON PURPOSE — wiring a shell
# surface is a different job from the `scripts/cli` one this ticket names, and
# is filed separately. What this ticket owes them is enumeration, not silence.
DECLARED_UNCOVERED = {
    ".githooks/test_commit_policy.sh": "shell gate test; run by hand, no CI job",
    ".githooks/test_duplicate_def_gate.sh": "shell gate test; run by hand, no CI job",
    ".githooks/test_push_policy.sh": "shell gate test; run by hand, no CI job",
    "scripts/hooks/test_derive_role.sh": (
        "shell mirror test; worker/tests/test_bsq_role_derivation_mirror.py READS "
        "its CASES array but never EXECUTES it, so the script itself is unrun"
    ),
    "scripts/hooks/test_worktree_guard.sh": "shell gate test; run by hand, no CI job",
    "scripts/ops/test_rebuild_api_venv.sh": "shell ops test; run by hand, no CI job",
    "scripts/ops/test_smoke_with_backoff.sh": "shell ops test; run by hand, no CI job",
}


def _git_test_files() -> list[str] | None:
    """Tracked test files, or None when there is no repo to ask."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return sorted(p for p in out.stdout.splitlines() if TEST_FILE_RE.search(p))


def _walked_test_files() -> list[str]:
    """Test files found by walking the tree, pruning non-source directories."""
    found: list[str] = []
    stack = [REPO]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_symlink():
                continue
            if e.is_dir():
                if e.name not in PRUNE:
                    stack.append(e)
            elif TEST_FILE_RE.search(e.name):
                found.append(e.relative_to(REPO).as_posix())
    return sorted(found)


def _test_files() -> list[str]:
    return _git_test_files() or _walked_test_files()


def _arm_for(path: str) -> str | None:
    for prefix, arm in ARMS.items():
        if path.startswith(prefix):
            return arm
    return None


# Anchor for the denominator control: the repo's largest and longest-lived
# test file. If it is ever legitimately renamed this control goes red, which
# is the correct outcome — the anchor is load-bearing and must be re-chosen
# deliberately, not silently lost.
DENOMINATOR_ANCHOR = "worker/tests/test_sessions.py"


def test_the_enumerators_actually_enumerate():
    """Positive control for the DENOMINATOR (T-0833's zero-denominator class).

    Every assertion below is vacuously true over an empty file list, and a
    census that scanned nothing reports "all clear" in exactly the same words
    as a census that scanned everything. So prove both enumerators return real
    content before believing anything they say about coverage.

    The walk is anchored on THIS file, which is on disk by definition. The
    active enumerator is anchored on a tracked file instead: `_test_files()`
    prefers `git ls-files`, which does not list this module until it is
    committed, and a self-anchor there would paint every lane that edits this
    file red for a reason that has nothing to do with coverage.
    """
    walked = _walked_test_files()
    assert "worker/tests/test_certification_surface_census.py" in walked, (
        f"the filesystem walk did not find its own file — it scanned "
        f"{len(walked)} path(s), so its result is meaningless"
    )

    files = _test_files()
    assert DENOMINATOR_ANCHOR in files, (
        f"the active enumerator did not find {DENOMINATOR_ANCHOR} — it scanned "
        f"{len(files)} path(s) and every check below is therefore meaningless"
    )
    assert len(files) > 300, f"only {len(files)} test files found; expected >300"


def test_every_test_bearing_path_is_covered_or_declared_uncovered():
    """The gate. A new test directory run by nothing goes RED here."""
    orphans = sorted(
        p for p in _test_files()
        if _arm_for(p) is None and p not in DECLARED_UNCOVERED
    )
    assert not orphans, (
        "these test files are collected by NO certification arm and are not "
        "declared uncovered — this is the T-0991 defect class (a whole suite "
        "nobody runs). Either wire an arm and add its prefix to ARMS, or add "
        "each path to DECLARED_UNCOVERED with the reason:\n  "
        + "\n  ".join(orphans)
    )


def test_the_declared_uncovered_list_is_exact_in_both_directions():
    """A path that gained coverage must LEAVE the list.

    Without this, DECLARED_UNCOVERED decays into a permanent amnesty that
    still names files somebody wired up months ago — the failure mode
    T-1036's ALLOWLIST is written to avoid.
    """
    present = set(_test_files())
    stale = sorted(p for p in DECLARED_UNCOVERED if p not in present)
    assert not stale, (
        "DECLARED_UNCOVERED names paths that no longer exist; remove them:\n  "
        + "\n  ".join(stale)
    )
    now_covered = sorted(p for p in DECLARED_UNCOVERED if _arm_for(p) is not None)
    assert not now_covered, (
        "these paths are BOTH declared uncovered and matched by an arm "
        "prefix; drop them from DECLARED_UNCOVERED:\n  "
        + "\n  ".join(now_covered)
    )


def test_every_declared_arm_exists_where_the_map_says_it_does():
    """The second end. ARMS is otherwise prose, and prose is not a control.

    Reads .github/workflows/nightly-suites.yml — the one runner that executes
    every surface unfiltered — and proves each arm named in ARMS is a real job
    there whose `run:` block still names the tree it claims to cover.
    """
    yaml = pytest.importorskip("yaml")
    wf = REPO / ".github" / "workflows" / "nightly-suites.yml"
    assert wf.is_file(), f"{wf} is missing — every arm claim below is unbacked"
    jobs = yaml.safe_load(wf.read_text(encoding="utf-8"))["jobs"]

    for arm in sorted(set(ARMS.values())):
        assert arm in jobs, (
            f"ARMS claims {arm!r} covers "
            f"{[p for p, a in ARMS.items() if a == arm]}, but nightly-suites.yml "
            f"has no such job. Either the job was renamed/deleted (the surface "
            f"is now uncertified) or ARMS is lying."
        )
        steps = jobs[arm].get("steps") or []
        runs = "\n".join(str(s.get("run", "")) for s in steps)
        needle = ARM_NIGHTLY_JOBS[arm]
        assert needle in runs, (
            f"nightly job {arm!r} exists but no step runs {needle!r} — the job "
            f"is a shell that no longer certifies what ARMS says it does"
        )


def test_the_fallback_enumerator_misses_nothing_the_git_one_finds():
    """Pins the fallback used in a `git archive` extract, where git is absent.

    Subset, not equality, and deliberately so — see the module docstring.
    """
    tracked = _git_test_files()
    if tracked is None:
        pytest.skip("no git repo here (a `git archive` extract) — nothing to compare against")
    missed = sorted(set(tracked) - set(_walked_test_files()))
    assert not missed, (
        "the filesystem fallback MISSES these tracked test files, so in an "
        "extract the census would silently pass over them:\n  "
        + "\n  ".join(missed)
    )
