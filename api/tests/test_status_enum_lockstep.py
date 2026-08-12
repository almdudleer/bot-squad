"""T-0889: every COMPLETE copy of the six-status enum must equal the SSOT.

`routes_backlog._VALID_STATUSES` is the named authority, but the full six-status
enum is duplicated verbatim in several other places, each of which gates
something different. Enumerated 2026-08-12 with an ast-based sweep of the repo
(every literal set/tuple/list/dict whose string members intersect the six —
which is also why the session/routine/channel vocabularies never appear: they
share no member with it). The complete copies, and what each one gates:

  api/app/routes_backlog._VALID_STATUSES        the API write path      SSOT
  api/app/canonical_status.CANONICAL_STATE      the 6->4 rollup         guarded
  scripts/lint/backlog_frontmatter.VALID_STATUSES  the backlog linter   guarded
  web/src/canonicalStatus.ts CANONICAL_STATE    the UI model            guarded
  web/src/pages/Project.tsx COLUMNS             the board               guarded
  api/app/routes_transparency._STATUSES         the count map's keys    THIS FILE
  scripts/cli/bsq TICKET_STATUSES               `bsq ticket update`     THIS FILE

The last two carried only a "Keep in sync" / "mirrors _VALID_STATUSES" comment
and no test — verified by grep before writing this: nothing in the repo
referenced `TICKET_STATUSES`, and `test_routes_transparency.py` asserted nothing
about the status keys. Prose is not a control.

What each omission would actually do, which is why this is not tidiness:

  * `scripts/cli/bsq` — line ~5153 is `if status not in TICKET_STATUSES: die(...)`.
    Add a status to the API and forget this one and `bsq ticket update <id>
    <status>` REJECTS it. That is the CLI every session drives the backlog
    with, so the new status would be unsettable by hand and unsettable by any
    agent, while the API happily accepts it.
  * `routes_transparency._STATUSES` — "the keys the backlog count map always
    carries, so the UI can render a stable set". Omit the new status and
    tickets holding it are simply absent from that map: an undercount with no
    error anywhere.

Deliberately NOT asserted here: the subsets (`pickup.PICKUP_STATUSES`,
`recovery.ACTIVE_STATUSES`, `task_chat.NOTIFY_STATUSES`, …). Those are
predicates, not copies — whether a new status belongs in each is a judgement
call, not an invariant, and a test that forced them equal would be wrong.

Note on layout: this file locates `scripts/` INSIDE the test rather than at
module scope, on purpose. The four existing files that resolve the repo root at
import time abort the whole pytest session on an api-only mount (collection
errors, zero tests run, output that reads like a small bounded failure). A
missing `scripts/` here fails exactly one test, loudly, and the rest of the
suite still executes.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from app.routes_backlog import _VALID_STATUSES


def _find_repo_file(relative: str) -> Path:
    """Locate a repo-root-relative file — robust to layout, like the lint test."""
    env_path = os.environ.get("BOT_SQUAD_REPO_ROOT")
    if env_path:
        candidate = Path(env_path) / relative
        if candidate.exists():
            return candidate
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / relative
        if candidate.exists():
            return candidate
        # Sibling-mount layout (api at /app, scripts at /scripts).
        sibling = ancestor.parent / relative
        if sibling.exists():
            return sibling
    raise FileNotFoundError(
        f"could not locate {relative} from {here} — mount the REPO ROOT "
        f"(not api/ alone), or set BOT_SQUAD_REPO_ROOT"
    )


def _module_level_str_collection(path: Path, name: str) -> set[str]:
    """Read a module-level `NAME = (...)` string collection without importing.

    Parsed with ast, never executed: `scripts/cli/bsq` is a large CLI whose
    imports are not guaranteed to resolve inside the api test image, and
    executing it to read one constant would be a needless dependency.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and value.args:  # frozenset({...})
            value = value.args[0]
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            break
        members = {
            e.value
            for e in value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
        if not members:
            break
        return members
    raise AssertionError(
        f"could not read a non-empty string collection named {name} from {path}. "
        f"If it was renamed or restructured, UPDATE this extractor — do not "
        f"delete the check; it is the only thing holding that copy of the status "
        f"enum to routes_backlog._VALID_STATUSES (T-0889)."
    )


def test_the_extractor_reads_a_real_collection():
    """Stated separately so a green above means it read something, not nothing."""
    bsq = _find_repo_file("scripts/cli/bsq")
    assert len(_module_level_str_collection(bsq, "TICKET_STATUSES")) > 0


def test_bsq_cli_ticket_statuses_match_the_api():
    """`bsq ticket update` validates offline; a stale copy REJECTS a real status."""
    bsq = _find_repo_file("scripts/cli/bsq")
    assert _module_level_str_collection(bsq, "TICKET_STATUSES") == set(_VALID_STATUSES)


def test_transparency_status_keys_match_the_api():
    """The backlog count map's stable key set; a stale copy silently undercounts."""
    from app.routes_transparency import _STATUSES

    assert set(_STATUSES) == set(_VALID_STATUSES)


def test_missing_repo_root_fails_loudly_rather_than_silently():
    """The locator must raise, never return a default that would pass vacuously."""
    with pytest.raises(FileNotFoundError):
        _find_repo_file("scripts/cli/definitely-not-a-real-file-t0889")
