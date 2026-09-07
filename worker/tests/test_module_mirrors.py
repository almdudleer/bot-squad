"""Every module this repo duplicates ON PURPOSE must not diverge. One registry.

Several modules exist twice because the worker (systemd) and the api (docker)
are independent packages that never import each other, and `scripts/cli` is a
third boundary the worker package cannot import across. The failure mode of
that design is always the same: a fix lands in ONE copy and ships broken.
`task_body.py` did it TWICE — T-0714 shipped broken that way, and T-0733 landed
`is_legacy_body` in only the api copy a day later.

T-0743 replaces five copy-pasted byte comparisons with this one. Before it, the
invariant was re-implemented per pair — `test_task_body_mirror.py` plus a
hand-rolled `test_worker_and_api_copies_are_byte_identical` buried at the
bottom of `test_idalloc.py`, `test_secret_crypto.py`,
`test_initiative_resolver.py` and `test_task_dedupe_gate.py`. That is the very
shape the invariant exists to forbid: a rule against duplication, duplicated
five times, drifting the same way the modules do. It already had — the copies
disagreed on whether module docstrings were exempt, `frontmatter.py` had no
guard at all, and `secret_crypto.py`'s docstring claimed a CI gate that did not
exist. Now a pair is DECLARED once in `MIRRORS` below and the comparison lives
once in `mirror_failure()`.

To pin a new pair: add one `Mirror(...)` line. Nothing else.

T-0738 (kept): this module is the SSOT of the invariant AND the engine of the
automatic gate. A correct guard that nothing runs is not a guard — T-0733
pushed its divergence with CI green, because the lint workflow runs api lint
tests and no worker tests at all, and it stayed green until someone ran the
full worker suite by hand hours later. So `scripts/lint/module_mirrors.py`
imports and calls `mirror_failure()` from HERE, from CI and `.githooks/pre-push`
— the gate and the test can never disagree, because there is only one of them.

Keep everything above the test function importable with NOTHING but the stdlib:
the lint script runs on bare `python3` — no venv, no pytest, no api tree. That
is why `import pytest` lives inside the test function, and why a lost split
marker raises instead of asserting (asserts vanish under -O).
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

#: Split marker for pairs whose module docstrings are allowed to differ.
#: Everything from this line onward must match; the docstring above it is the
#: only place those copies may diverge.
FUTURE_MARKER = "from __future__ import annotations\n"

_REPO = Path(__file__).resolve().parents[2]


class MirrorMarkerError(Exception):
    """A copy lost the split marker its pair is compared from."""


class Mirror(NamedTuple):
    """One declared duplicated-by-design pair.

    `split_marker` None means the files must match byte-for-byte in full; set it
    to `FUTURE_MARKER` when the two copies carry their own module docstrings.
    """

    name: str
    left: str  # repo-relative
    right: str  # repo-relative
    split_marker: str | None
    why: str


#: THE registry. Adding a pair here is the whole job of pinning it — this
#: module's test and `scripts/lint/module_mirrors.py` both iterate it.
MIRRORS: tuple[Mirror, ...] = (
    Mirror(
        name="task_body",
        left="worker/bot_squad_worker/task_body.py",
        right="api/app/task_body.py",
        split_marker=FUTURE_MARKER,
        why="ticket-body parse/render; T-0714 and T-0733 each shipped a one-copy fix",
    ),
    Mirror(
        name="frontmatter",
        left="worker/bot_squad_worker/frontmatter.py",
        right="api/app/frontmatter.py",
        split_marker=FUTURE_MARKER,
        why="the single md frontmatter parser/writer (T-0075) — a split write path corrupts task md",
    ),
    Mirror(
        name="frontmatter_cli",
        left="worker/bot_squad_worker/frontmatter.py",
        right="scripts/cli/frontmatter.py",
        split_marker=FUTURE_MARKER,
        why="T-1047: bsq's own flat 'key: value lines only' reader mis-parsed folded scalars, "
            "quoted-scalar escapes and block-style lists on 458 of 950 live ticket titles; "
            "the worker package cannot be imported across the scripts/cli boundary, so bsq "
            "now shares this SAME pyyaml parser as a third mirrored copy instead of a fourth "
            "divergent implementation",
    ),
    Mirror(
        name="secret_crypto",
        left="worker/bot_squad_worker/secret_crypto.py",
        right="api/app/secret_crypto.py",
        split_marker=FUTURE_MARKER,
        why="api WRITES and worker READS the same at-rest format (T-0179) — a split silently breaks the bot token",
    ),
    Mirror(
        name="idalloc",
        left="worker/bot_squad_worker/idalloc.py",
        right="api/app/idalloc.py",
        split_marker=None,
        why="the flock'd id allocator (T-0174) — web creates and agent creates share one counter file",
    ),
    Mirror(
        name="initiative_resolver",
        left="worker/bot_squad_worker/initiative_resolver.py",
        right="api/app/initiative_resolver.py",
        split_marker=None,
        why="old-initiative-ref resolution (T-0480) — a split resolves an agent read and a web read to different tasks",
    ),
    Mirror(
        name="artifact_nesting",
        left="worker/bot_squad_worker/artifact_nesting.py",
        right="api/app/artifact_nesting.py",
        split_marker=None,
        why="cross-store parent/child resolution (T-0283) — T-0290 gave `bsq doc new` a parent, and a split lets the CLI accept a parent the web says does not exist",
    ),
    Mirror(
        name="priority",
        left="worker/bot_squad_worker/priority.py",
        right="scripts/cli/priority.py",
        split_marker=None,
        why="the ONE priority vocabulary (T-0877/T-0586) — the writer's gate and the reader's ranking; a split is exactly the drift that hid 86 tickets",
    ),
    Mirror(
        name="task_search",
        left="scripts/cli/task_search.py",
        right="worker/bot_squad_worker/task_search.py",
        split_marker=None,
        why="dedupe ranking (T-0486/T-0577) — the worker package cannot import across the scripts/ boundary",
    ),
)


def _comparable(path: Path, marker: str | None) -> str:
    """File contents to compare — the whole file, or everything past `marker`."""
    text = path.read_text()
    if marker is None:
        return text
    if marker not in text:
        raise MirrorMarkerError(f"{path} lost its `{marker.strip()}` line")
    return text.split(marker, 1)[1]


def missing_side(mirror: Mirror, root: Path | None = None) -> Path | None:
    """The absent copy, if either is missing — else None.

    A standalone worker checkout has no api tree beside it, so this is a SKIP,
    not a failure. Both sides are checked: a pair whose canonical copy vanished
    should not silently pass.
    """
    base = _REPO if root is None else root
    for rel in (mirror.left, mirror.right):
        path = base / rel
        if not path.exists():
            return path
    return None


def mirror_failure(mirror: Mirror, root: Path | None = None) -> str | None:
    """The one byte comparison. `None` when a pair agrees, else why it doesn't.

    Called by `test_duplicated_modules_are_identical` AND by
    `scripts/lint/module_mirrors.py` (the CI / pre-push gate).
    """
    base = _REPO if root is None else root
    left, right = base / mirror.left, base / mirror.right
    if _comparable(left, mirror.split_marker) == _comparable(right, mirror.split_marker):
        return None
    scope = (
        "byte-identical in full"
        if mirror.split_marker is None
        else "byte-identical below their module docstrings"
    )
    return (
        f"{mirror.name}: the two copies have diverged — a change landed in only "
        f"one. They must be {scope}. Mirror it into the other.\n"
        f"  {left}\n"
        f"  {right}\n"
        f"  why this pair is pinned: {mirror.why}"
    )


def mirror_failures(root: Path | None = None) -> list[str]:
    """Every declared pair's failure, skipping pairs with an absent copy."""
    failures = []
    for mirror in MIRRORS:
        if missing_side(mirror, root) is not None:
            continue
        failure = mirror_failure(mirror, root)
        if failure is not None:
            failures.append(failure)
    return failures


def checked_mirrors(root: Path | None = None) -> list[Mirror]:
    """Declared pairs with both copies present — the ones actually compared."""
    return [m for m in MIRRORS if missing_side(m, root) is None]


def test_duplicated_modules_are_identical():
    """One test node over every declared pair, reporting ALL divergences.

    Deliberately a loop and not `@pytest.mark.parametrize`: that decorator would
    force `import pytest` to module scope, and the lint gate imports this file on
    a bare interpreter with no venv, no pytest.
    """
    import pytest

    if not checked_mirrors():  # standalone worker checkout — nothing to compare
        pytest.skip("no declared mirror pair has both copies present")

    failures = mirror_failures()
    assert not failures, "\n\n".join(failures)
